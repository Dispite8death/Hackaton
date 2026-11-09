"""Preflight, scoped archive jobs and progress for the local administration panel."""
from contextlib import closing
from dataclasses import asdict
from threading import Lock, Thread, local
import time
import json
from hashlib import sha256
import secrets

from . import audit_worker, config, crawl_store, model_batch, model_gateway, model_settings
from .loadPage import crawl_organization
from . import attached_files, backfill_materials, operation_log
from .navigation import resolve_url
from .coverage_report import REQUIRED_SECTIONS, SECTION_LABELS

_lock = Lock()
_current = local()
_plans = {}
STAGES = {"algorithm": ("algorithm",), "download": ("download",), "ai": ("ai",),
          "download_algorithm": ("download", "algorithm"), "download_ai": ("download", "algorithm", "ai")}
LABELS = {"download": "Загрузка", "algorithm": "Алгоритмы", "ai": "Проверка ИИ"}


def progress(**fields):
    operation_log.checkpoint()
    callback = getattr(_current, "report", None)
    if callback:
        callback(**fields)


def close_interrupted_visit(conn, job):
    rid = job.get("crawl_run")
    if rid and conn.execute("SELECT 1 FROM crawl_run WHERE id=? AND state='running'", (rid,)).fetchone():
        crawl_store.finish_run(conn, rid, "interrupted", reason="portal_interrupted")


def status(conn):
    return operation_log.status(conn, close_interrupted_visit)


def profile_for_job(conn):
    reason, profile, key = model_settings.portal_connection(conn)
    if profile is None:
        raise ValueError(reason + ". Сохраните модель и подключение в настройках.")
    return profile, key


def verify(conn, benchmark=False):
    profile, key = profile_for_job(conn)
    prompt = ("Проверь запись школьного сайта. Перечисли в 3 предложениях, чего не хватает для подтверждения "
              "новости: указаны участники и тема, отсутствуют дата, фотография и ссылка на источник."
              if benchmark else 'Верни JSON с единственным полем ok и значением true: {"ok":true}.')
    payload = {"model": profile.model, "messages": [{"role": "user", "content": prompt}]}
    schema = {"type":"object", "properties":{"ok":{"type":"boolean"}},
              "required":["ok"], "additionalProperties":False}
    model_gateway.apply_response_options(profile, payload, "connection", None if benchmark else schema)
    start = time.monotonic()
    reply = model_gateway._api(profile, "/chat/completions", key, payload)
    try:
        answer = reply["choices"][0]["message"]["content"]
    except (TypeError, IndexError, KeyError) as exc:
        raise ValueError("Сервис ответил, но в ответе нет сообщения выбранной модели.") from exc
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Модель вернула пустой итоговый ответ. Проверьте модель и формат ответа. Для модели с рассуждениями попробуйте включить «Отключить режим рассуждений модели».")
    if not benchmark:
        try:
            valid = json.loads(answer).get("ok") is True
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Модель ответила, но не выполнила тест JSON. Проверьте модель и формат ответа.")
    duration = max(.01, time.monotonic() - start)
    if benchmark:
        record_benchmark(conn, profile, duration)
    return {"message": "Модель ответила", "seconds": round(duration, 2), "model": profile.model}


def benchmark_key(profile):
    signature = json.dumps(asdict(profile), sort_keys=True).encode()
    return "portal_benchmark_" + sha256(signature).hexdigest()


def record_benchmark(conn, profile, seconds):
    with conn:
        model_settings.set_setting(conn, benchmark_key(profile), {"seconds": seconds})


def selected(conn, ids):
    if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or any(type(i) != int or not 0 < i < 2**63 for i in ids):
        raise ValueError("Выберите учреждения из списка.")
    ids = sorted(set(ids))
    rows = conn.execute("SELECT * FROM active_organization WHERE id IN (" + ",".join("?" for _ in ids) + ") ORDER BY id", ids).fetchall()
    if len(rows) != len(ids):
        raise ValueError("Одно из выбранных учреждений больше не существует.")
    return rows


def latest_pages(conn, oid):
    return conn.execute("""SELECT p.id, f.run_id, f.depth, f.section FROM page p JOIN crawl_fetch f ON f.page_id=p.id
        WHERE p.id_organization=? AND NOT EXISTS (SELECT 1 FROM page n WHERE n.id_organization=p.id_organization
        AND n.url=p.url AND (n.date_loaded,n.id)>(p.date_loaded,p.id)) ORDER BY p.id""", (oid,)).fetchall()


def plan(conn, ids, mode):
    if mode not in STAGES:
        raise ValueError("Выберите вид операции.")
    orgs = selected(conn, ids)
    pages = 0
    empty = []
    gaps = []
    durations = []
    settings = audit_worker.get_settings(conn)
    for org in orgs:
        snapshots = latest_pages(conn, org["id"])
        saved = len(snapshots)
        pages += saved
        if not saved:
            empty.append(org["name"])
        else:
            sections = {r["section"] for r in snapshots}
            missing = sorted(set(REQUIRED_SECTIONS[org["kind"]]) - sections)
            if missing:
                gaps.append({"name":org["name"], "sections":[SECTION_LABELS.get(s,s) for s in missing]})
        previous = model_settings.setting(conn, "portal_download_" + str(org["id"]), {})
        if previous.get("limits") == download_limits(settings):
            durations.append(previous["seconds"])
    profile = model_settings.get_profile(conn)
    sample = model_settings.setting(conn, benchmark_key(profile), {})
    same = "seconds" in sample
    speed = sample.get("seconds") if same else None
    reason = ("Пользователь отказался от теста производительности." if same and speed is None
              else "Тест производительности этого профиля ещё не выполнен.")
    counts = {"ready": 0, "missing": 0, "completed": 0}
    counts_known = True
    if pages and mode not in ("algorithm", "download_algorithm"):
        try:
            model_settings.checked(profile)
        except ValueError:
            counts_known = False
            reason = "Для оценки обработки ИИ исправьте профиль модели."
            speed = None
        else:
            counts = model_batch.batch_assess(conn, org_ids=tuple(ids), latest_only=True, include_photos=True, plan=True)
    return {"organizations": len(orgs), "pages": pages, "empty": empty, "gaps":gaps, "ready": counts["ready"], "counts_known":counts_known,
            "missing": counts["missing"], "completed": counts["completed"], "page_limit": settings["pages_per_org"],
            "download_seconds": sum(durations) if len(durations) == len(orgs) else None,
            "model_seconds": round(counts["ready"] * speed, 2) if speed and pages else None,
            "benchmark_reason": "" if speed else reason, "seconds_per_question": speed}


def download_limits(settings):
    return [settings["pages_per_org"], settings["max_depth"], settings["news_months"]]


def plan_signature(conn, ids, mode):
    return (tuple(sorted(set(ids))), mode, benchmark_key(model_settings.get_profile(conn)),
            tuple(download_limits(audit_worker.get_settings(conn))))


def preview(conn, ids, mode):
    selected(conn, ids)
    signature = plan_signature(conn, ids, mode)
    info = plan(conn, ids, mode)
    token = secrets.token_urlsafe(24)
    if signature != plan_signature(conn, ids, mode):
        raise ValueError("Настройки изменились во время расчёта. Повторите оценку материалов.")
    with _lock:
        while len(_plans) >= 16:
            del _plans[next(iter(_plans))]
        _plans[token] = (time.monotonic(), signature, info)
    return {**info, "ticket": token}


def download(conn, org, opts):
    oid, name = org["id"], org["name"]
    started = time.monotonic()
    progress(detail=name + " · основной обход")
    result = crawl_organization(org, max_pages=opts["pages_per_org"], max_depth=opts["max_depth"],
                               news_months=opts["news_months"], archive_files=False, db=conn,
                               before_page=lambda rid: progress(crawl_run=rid))
    failures = result.errors
    target = (oid, resolve_url(org["site_url"], ""), org["kind"])
    progress(detail=name + " · поиск дополнительных ссылок")
    backfill_materials.seed(conn, [target], news_months=opts["news_months"])
    pending_sql = "SELECT count(*) FROM material_backfill WHERE organization_id=? AND state='pending'"
    while remaining := conn.execute(pending_sql, (oid,)).fetchone()[0]:
        progress(detail=f"{name} · дополнительные страницы, известных в очереди: {remaining}")
        runs = []
        n = backfill_materials.process_org(target, 12, 25, 15, db=conn, runs=runs, news_months=opts["news_months"])
        failures += sum(r["errors"] for r in runs)
        if not n and conn.execute(pending_sql, (oid,)).fetchone()[0] >= remaining:
            break
    remaining = conn.execute("SELECT count(*) FROM material_backfill WHERE organization_id=? AND state IN ('pending','working')", (oid,)).fetchone()[0]
    file_pages = conn.execute("""SELECT DISTINCT a.page_id FROM page_attachment a JOIN page p ON p.id=a.page_id
        WHERE p.id_organization=? AND a.state IN ('pending','error')""", (oid,)).fetchall()
    for index, (pid,) in enumerate(file_pages, 1):
        progress(detail=f"{name} · вложения, страница {index} из {len(file_pages)}")
        files = attached_files.download_pending(conn, page_id=pid, retry_transient=True)
        failures += files["error"] + files["leased_elsewhere"]
    unavailable = conn.execute("""SELECT count(DISTINCT a.url) FROM page_attachment a JOIN page p ON p.id=a.page_id
        WHERE p.id_organization=? AND a.state!='saved' AND NOT EXISTS
        (SELECT 1 FROM page n WHERE n.id_organization=p.id_organization AND n.url=p.url
         AND (n.date_loaded,n.id)>(p.date_loaded,p.id))""", (oid,)).fetchone()[0]
    if failures or remaining or unavailable or result.state in ("failed","interrupted"):
        return (f"Сохранено основных страниц: {result.loaded}. Ошибок загрузки: {failures}. "
                f"Страниц в очереди: {remaining}. Недоступных вложений: {unavailable}. "
                "Для проверки используются последние доступные копии.")
    with conn:
        model_settings.set_setting(conn, "portal_download_" + str(oid),
                                   {"limits": download_limits(opts), "seconds": time.monotonic()-started})
    return None


def run(conn, ids, mode, info):
    orgs = selected(conn, ids)
    opts = audit_worker.get_settings(conn)
    errors = []
    done = 0
    if mode == "ai":
        def ensure_profile():
            operation_log.checkpoint()
            current, _ = profile_for_job(conn)
            if asdict(current) != info["verified_profile"]:
                raise ValueError("Профиль ИИ изменился. Повторите проверку подключения.")
        ensure_profile()
        def report(result):
            nonlocal done
            done += 1
            progress(done=done, detail=result["check_id"])
            if result.get("error"):
                raise ValueError(result["error"])
        result = model_batch.batch_assess(conn, org_ids=ids, latest_only=True, include_photos=True, limit=info["ready"],
                                          on_result=report, before_check=ensure_profile)
        if result["stop_reason"]:
            raise ValueError(result["stop_reason"])
        if not result["processed"]:
            errors.append("Готовых вопросов уже нет. Обновите план материалов.")
    else:
        for org in orgs:
            progress(detail=org["name"])
            if mode == "download":
                issue = download(conn, org, opts)
                if issue:
                    errors.append(issue)
                done += 1
                progress(done=done)
            else:
                runs = set()
                for pid, rid, depth, section in latest_pages(conn, org["id"]):
                    with conn:
                        if depth == 0 or section == "events":
                            crawl_store.analyze_saved_page(conn, pid)
                        crawl_store._index_snapshot(conn, pid)
                    runs.add(rid)
                    done += 1
                    progress(done=done)
                for rid in runs:
                    with conn:
                        crawl_store.refresh_quarters(conn, rid)
                        crawl_store.refresh_timeline(conn, rid)
                        crawl_store.audit_run(conn, rid)
            if mode == "algorithm" and org["name"] in info["empty"]:
                errors.append(org["name"] + " · нужно загрузить страницы")
    if mode == "ai" and info["missing"]:
        errors.append(f'Ожидают материалов: {info["missing"]} вопросов.')
    return " · ".join(errors)


def _stage(db, job, oid, stage, name):
    operation_log.checkpoint()
    prefix = name + " · " + LABELS[stage]
    job.update(detail=prefix, unit_done=0, unit_total=None, unit_estimate=None, unit_started=time.time())
    operation_log.save(db, job)

    def report(done=None, detail=None, crawl_run=None):
        new_visit = crawl_run is not None and crawl_run != job.get("crawl_run")
        if new_visit:
            job["crawl_run"] = crawl_run
        if done is not None:
            job["unit_done"] = done
        if detail:
            job["detail"] = detail if detail.startswith(name) else prefix + " · " + detail
        if not db.in_transaction and (new_visit or time.time() - job["updated"] >= 1):
            operation_log.save(db, job)

    _current.report = report
    # Count model questions only for the AI step, after the newer pages have been indexed.
    item = plan(db, [oid], "algorithm" if stage == "download" else stage)
    item["verified_profile"] = job["context"]["profile"]
    job["unit_total"] = {"algorithm": item["pages"], "ai": item["ready"], "download": 1}[stage]
    if stage == "download":
        job["unit_estimate"] = item["download_seconds"]
    elif stage == "ai":
        job["unit_estimate"] = item["model_seconds"]
    operation_log.save(db, job)
    if stage == "ai" and not item["ready"]:
        note = (f'Вопросов с материалами для новой проверки нет. Уже проверено: {item["completed"]}. '
                f'Ожидают материалов: {item["missing"]}.' if item["pages"] else "В архиве нет страниц учреждения.")
    else:
        note = run(db, [oid], stage, item)
    if stage == "algorithm" and item["gaps"]:
        note = "Нет сохранённой страницы раздела: " + ", ".join(item["gaps"][0]["sections"])
    totals = job.setdefault("processed", {})
    totals[stage] = totals.get(stage, 0) + job["unit_done"]
    operation_log.complete_step(db, job, prefix + " · " + note if note else None)


def _execute(job, ownership, work):
    try:
        with closing(config.connect_db()) as db:
            try:
                for oid, stage, name in job["steps"][job["done"]:]:
                    _stage(db, job, oid, stage, name)
            except BaseException as exc:
                operation_log.stop(db, exc)
    finally:
        ownership.close()
        work.release()


def start(conn, ids, mode, work, ticket=None, *, resume_id=None):
    if not work.acquire(blocking=False):
        raise ValueError("Другая проверка уже выполняется.")
    ownership = None
    try:
        ownership = operation_log.lease(conn)
        operation_log.initialize(conn)
        old = operation_log.recover(conn, close_interrupted_visit)
        if resume_id is not None:
            if old.get("id") != resume_id or old["state"] not in ("interrupted", "error"):
                raise ValueError("Нет прерванного задания для продолжения.")
            mode = old["mode"]
            ids = list(dict.fromkeys(step[0] for step in old["steps"][old["done"]:]))
        orgs = selected(conn, ids)
        if mode not in STAGES:
            raise ValueError("Выберите вид операции.")
        needs_ai = "ai" in STAGES[mode]
        verified = None
        if needs_ai:
            verified, _ = profile_for_job(conn)
        if ticket is None:
            info = plan(conn, ids, mode)
        else:
            with _lock:
                cached = _plans.get(ticket)
            if (not cached or cached[1] != plan_signature(conn, ids, mode)
                    or time.monotonic() - cached[0] > 1800):
                raise ValueError("Параметры изменились или план устарел. Повторите оценку материалов.")
            # The worker rechecks available evidence and completed questions when processing.
            info = dict(cached[2])
        if resume_id is None and mode == "algorithm" and not info["pages"]:
            raise ValueError("В архиве нет страниц этих учреждений. Сначала загрузите материалы.")
        if needs_ai:
            if resume_id is None and mode == "ai" and not info["ready"]:
                raise ValueError("Нет готовых вопросов ИИ. Проверьте материалы и уже обработанные вопросы.")
            if resume_id is not None and old["context"].get("profile") != asdict(verified):
                raise ValueError("Профиль модели изменился. Верните прежний профиль или начните новую проверку.")
            verify(conn)
        steps = [[org["id"], stage, org["name"]] for org in orgs for stage in STAGES[mode]]
        job = operation_log.begin(conn, mode, steps, resume_id=resume_id,
                                  context={"profile": asdict(verified) if verified else None})
        Thread(target=_execute, args=(job, ownership, work), daemon=True).start()
        with _lock:
            _plans.pop(ticket, None)
    except BaseException:
        if ownership is not None:
            ownership.close()
        work.release()
        raise
    return {"state": "running", "id": job["id"]}
