"""Server-owned quick checks; each saved institution is a restart checkpoint."""
import asyncio
from contextlib import closing
from datetime import datetime
from dataclasses import asdict

from Engine import operation_log
from . import core, db, llm
from .integration import ai_status, audit_settings

audit_lock = asyncio.Lock()
_tasks = set()
LABELS = {"online": "Загрузка и проверка сайтов", "cache": "Проверка сохранённых страниц",
          "summary": "Сводка ИИ", "explain": "План исправления с ИИ"}


def _interrupt_run(conn, job):
    if rid := job["context"].get("run_id"):
        conn.execute("UPDATE runs SET status='interrupted' WHERE id=? AND status='running'", (rid,))


def status():
    with closing(db.conn()) as conn:
        return operation_log.status(conn, _interrupt_run)


async def start(mode=None, sites=(), *, resume_id=None, result_id=None):
    if audit_lock.locked():
        raise ValueError("Алгоритмический аудит занят. Дождитесь завершения текущего задания.")
    await audit_lock.acquire()
    ownership = None
    try:
        with closing(db.conn()) as conn:
            ownership = operation_log.lease(conn)
            old = operation_log.recover(conn, _interrupt_run)
            if resume_id is not None:
                if old.get("id") != resume_id or old["state"] not in ("error", "interrupted"):
                    raise ValueError("Задание уже изменилось. Обновите страницу.")
                mode = old["mode"]
            elif not sites:
                raise ValueError("Выберите хотя бы одно учреждение.")
            if mode not in LABELS:
                raise ValueError("Неизвестный режим проверки.")
            profile = None
            if mode in ("summary", "explain"):
                model = audit_settings()
                if model["llm_enabled"] != "1":
                    raise ValueError(ai_status())
                profile = asdict(model["llm_profile"])
                if resume_id is not None and old["context"].get("profile") != profile:
                    raise ValueError("Профиль модели изменился. Для работы с новой моделью начните новый разбор.")
            job = operation_log.begin(conn, mode, [[s["id"], s["name"]] for s in sites],
                                      resume_id=resume_id, context={"result_id": result_id, "profile": profile})
            if resume_id and job["context"].get("run_id"):
                with conn:
                    conn.execute("UPDATE runs SET status='running',finished_at=NULL WHERE id=?", (job["context"]["run_id"],))
        task = asyncio.create_task(_execute(job, ownership))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return job["id"]
    except BaseException:
        if ownership is not None:
            ownership.close()
        audit_lock.release()
        raise


async def _execute(job, ownership):
    try:
        with closing(db.conn()) as conn:
            try:
                mode = job["mode"]
                settings = db.get_settings()
                settings["checkpoint"] = operation_log.checkpoint
                run_id = job["context"].get("run_id")
                if mode in ("online", "cache") and run_id is None:
                    run_id = conn.execute("INSERT INTO runs(started_at,status,sites_total) VALUES(?,'running',?)",
                                          (datetime.now().isoformat(), job["total"])).lastrowid
                    job["context"]["run_id"] = run_id
                    operation_log.save(conn, job)
                for site_id, name in job["steps"][job["done"]:]:
                    operation_log.checkpoint()
                    job["detail"] = name
                    operation_log.save(conn, job)
                    site = db.get_site(site_id)
                    note = None
                    if not site or not site["active"]:
                        note = name + " · учреждение убрано из реестра"
                    elif mode == "cache" and not core.load_manifest(core.CACHE_DIR / f"site_{site_id}"):
                        note = name + " · нет сохранённых страниц. Выберите «Проверить сайты»."
                    elif mode in ("summary", "explain"):
                        result_id = job["context"]["result_id"]
                        result = conn.execute("SELECT run_id FROM run_sites WHERE id=? AND site_id=?", (result_id,site_id)).fetchone()
                        if not result:
                            raise ValueError("Исходный отчёт больше недоступен.")
                        model = audit_settings()
                        if model["llm_enabled"] != "1":
                            raise ValueError(ai_status())
                        if asdict(model["llm_profile"]) != job["context"]["profile"]:
                            raise ValueError("Профиль ИИ изменился после запуска.")
                        findings = db.get_site_findings(result[0], site_id)
                        answer = await (llm.summarize if mode == "summary" else llm.explain)(site, findings, model)
                        column = "llm_summary" if mode == "summary" else "ai_explanation"
                        conn.execute(f"UPDATE run_sites SET {column}=? WHERE id=?", (answer, result_id))
                    elif not conn.execute("SELECT 1 FROM run_sites WHERE run_id=? AND site_id=?", (run_id, site_id)).fetchone():
                        result = await core.audit_site(site, settings, offline=mode == "cache")
                        if not result.get("metrics", {}).get("pages"):
                            note = name + " · нет доступных страниц для проверки. Предыдущий отчёт сохранён."
                        else:
                            db.save_site_result(run_id, site, result)
                    if run_id:
                        conn.execute("UPDATE run_sites SET checked_at=coalesce(checked_at,?) WHERE run_id=? AND site_id=?",
                                     (datetime.now().isoformat(), run_id, site_id))
                    if run_id and job["done"] + 1 == job["total"]:
                        conn.execute("""UPDATE runs SET status='done', finished_at=?,
                            avg_score=(SELECT avg(score) FROM run_sites WHERE run_id=?) WHERE id=?""",
                            (datetime.now().isoformat(), run_id, run_id))
                    operation_log.complete_step(conn, job, note)
            except BaseException as error:
                operation_log.stop(conn, error)
                with conn:
                    _interrupt_run(conn, job)
                if not isinstance(error, (Exception, operation_log.ServerStopping)):
                    raise
    finally:
        ownership.close()
        audit_lock.release()
