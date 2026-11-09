"""Preflight, scoped archive jobs and progress for the local administration panel."""
from contextlib import closing
from dataclasses import asdict
from threading import Lock, Thread
import time
import json
from hashlib import sha256
import secrets

from . import audit_worker, config, crawl_store, model_batch, model_gateway, model_settings
from .loadPage import crawl_organization
from . import attached_files, backfill_materials
from .navigation import resolve_url
from .coverage_report import REQUIRED_SECTIONS, SECTION_LABELS

_lock = Lock()
_job = {"state": "idle"}
_plans = {}


def progress(**fields):
    with _lock:
        _job.update(fields)


def status():
    with _lock:
        return dict(_job)


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
    if not benchmark and profile.response_mode != "prompt":
        payload["response_format"] = {"type": "json_object"}
        if profile.response_mode == "json_schema":
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "connection", "strict": True,
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                           "required": ["ok"], "additionalProperties": False}}}
    if profile.disable_thinking:
        payload["reasoning_effort"] = "none"
        if profile.provider == "lmstudio":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
    start = time.monotonic()
    reply = model_gateway._api(profile, "/chat/completions", key, payload)
    try:
        answer = reply["choices"][0]["message"]["content"]
    except (TypeError, IndexError, KeyError) as exc:
        raise ValueError("Сервис ответил, но в ответе нет сообщения выбранной модели.") from exc
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Модель вернула пустой ответ. Проверьте идентификатор и режим генерации.")
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
    rows = conn.execute("SELECT * FROM organization WHERE id IN (" + ",".join("?" for _ in ids) + ") ORDER BY id", ids).fetchall()
    if len(rows) != len(ids):
        raise ValueError("Одно из выбранных учреждений больше не существует.")
    return rows


def latest_pages(conn, oid):
    return conn.execute("""SELECT p.id, f.run_id, f.depth, f.section FROM page p JOIN crawl_fetch f ON f.page_id=p.id
        WHERE p.id_organization=? AND NOT EXISTS (SELECT 1 FROM page n WHERE n.id_organization=p.id_organization
        AND n.url=p.url AND (n.date_loaded,n.id)>(p.date_loaded,p.id)) ORDER BY p.id""", (oid,)).fetchall()


def plan(conn, ids, mode):
    if mode not in ("download", "algorithm", "ai"):
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
    if pages and mode != "algorithm":
        try:
            model_settings.checked(profile)
        except ValueError:
            if mode != "download":
                raise
            reason = "Для оценки обработки ИИ исправьте профиль модели."
            speed = None
        else:
            counts = model_batch.batch_assess(conn, org_ids=tuple(ids), latest_only=True, include_photos=True, plan=True)
    return {"organizations": len(orgs), "pages": pages, "empty": empty, "gaps":gaps, "ready": counts["ready"],
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
                               news_months=opts["news_months"], archive_files=False, db=conn)
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
    remaining = conn.execute(pending_sql, (oid,)).fetchone()[0]
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
        return f"{name} · ошибок: {failures}, страниц отложено: {remaining}, вложений недоступно: {unavailable} ({result.reason})"
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
    if mode == "ai" and (info["missing"] or info["empty"]):
        errors.append("Часть вопросов ждёт материалов.")
    progress(state="partial" if errors else "completed", detail=" · ".join(errors) or "Готово")


def start(conn, ids, mode, work, ticket=None):
    if not work.acquire(blocking=False):
        raise ValueError("Другая проверка уже выполняется.")
    try:
        selected(conn, ids)
        verified = None
        if mode == "ai":
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
        if mode == "algorithm" and not info["pages"]:
            raise ValueError("В архиве нет страниц этих учреждений. Сначала загрузите материалы.")
        if mode == "ai":
            if not info["ready"]:
                raise ValueError("Нет готовых вопросов ИИ. Проверьте материалы и уже обработанные вопросы.")
            verify(conn)
            info["verified_profile"] = asdict(verified)
        total = {"ai": info["ready"], "algorithm": info["pages"], "download": info["organizations"]}[mode]
        estimate = {"ai": info["model_seconds"], "algorithm": None, "download": info["download_seconds"]}[mode]
        progress(state="running", mode=mode, total=total, done=0, started=time.time(), estimate=estimate, detail="Начинаем")
        def execute():
            try:
                with closing(config.connect_db()) as db, audit_worker._cycle_lock(db) as owned:
                    if not owned:
                        raise ValueError("Архив занят другим циклом. Повторите после его завершения.")
                    run(db, ids, mode, info)
            except Exception as exc:
                progress(state="error", detail=str(exc)[:500])
            finally:
                work.release()
        Thread(target=execute, daemon=True).start()
        with _lock:
            _plans.pop(ticket, None)
    except BaseException:
        work.release()
        raise
    return status()
