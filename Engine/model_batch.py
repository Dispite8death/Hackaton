"""Run the deferred audit questions against the saved archive, with resumable batches."""

import argparse
import hashlib
import json
import os
from collections import Counter
from contextlib import closing, contextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path

if __package__:
    from .archive_access import uses_archive
    from .deferred_checks import GROUPS, review_group
    from .model_gateway import assess
    from .model_settings import checked, get_profile
    from .page_passport import questions_for
    from .visual_materials import visual_bundle
else:
    from archive_access import uses_archive
    from deferred_checks import GROUPS, review_group
    from model_gateway import assess
    from model_settings import checked, get_profile
    from page_passport import questions_for
    from visual_materials import visual_bundle


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


@contextmanager
def _one_worker(conn):
    # OS releases the lock even if the program is killed between two questions.
    location = conn.execute("PRAGMA database_list").fetchone()[2]
    if not location:
        yield
        return
    target = Path(location)
    with target.with_name("." + target.name + ".model-batch.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError as exc:
                raise ValueError("С этой базой уже работает другая партия ИИ.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("С этой базой уже работает другая партия ИИ.") from exc
        yield


def _candidate_pages(conn, page_ids, org_ids, run_ids, latest_only, attachment_role):
    filters, params = ["p.id_organization IN (SELECT id FROM active_organization)"], []
    for column, values in (("b.page_id",page_ids),("p.id_organization",org_ids),("f.run_id",run_ids)):
        if values:
            filters.append(f"{column} IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
    if latest_only:
        filters.append("""NOT EXISTS(SELECT 1 FROM page newer WHERE newer.id_organization=p.id_organization
            AND newer.url=p.url AND (newer.date_loaded,newer.id)>(p.date_loaded,p.id))""")
    if attachment_role:
        filters.append("EXISTS(SELECT 1 FROM page_attachment a WHERE a.page_id=b.page_id AND a.role=?)")
        params.append(attachment_role)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    # Finish reading the candidate list before a network call, releasing SQLite's read lock.
    return conn.execute("""SELECT b.page_id,r.captured_at,r.pdf IS NOT NULL,r.full_screenshot IS NOT NULL,
        r.screenshot IS NOT NULL FROM page_passport b JOIN page p ON p.id=b.page_id
        JOIN crawl_fetch f ON f.page_id=b.page_id LEFT JOIN page_render r ON r.page_id=b.page_id"""
        + where + " ORDER BY b.page_id",params).fetchall()


def _can_run(check, has_pdf, has_full, has_viewport):
    if check.get("publication"):
        return True
    policy = check.get("evidence_policy",{})
    if "print_comparison" in policy.get("sources",()):
        return bool(has_pdf and has_full)
    if check.get("attachment"):
        return check["attachment"].get("state","saved") == "saved"
    kinds = set(check["material_kinds"])
    return bool({"html","dom"} & kinds or "pdf" in kinds and has_pdf or
                "full" in kinds and has_full or "viewport" in kinds and has_viewport)


@uses_archive
def batch_assess(conn, *, page_ids=(), org_ids=(), run_ids=(), group=None, limit=10, plan=False,
                 force=False, on_result=None, include_photos=False, rotate=False, latest_only=False,
                 attachment_role=None, check_ids=(), before_check=None):
    if group is not None and group not in GROUPS:
        raise ValueError("Неизвестная группа проверок")
    if limit is not None and limit < 1:
        raise ValueError("Размер партии должен быть положительным")
    if attachment_role and group not in (None,"attachments"):
        raise ValueError("Роль файла применима к группе оригиналов вложений")
    if attachment_role:
        group = "attachments"
    profile = checked(get_profile(conn))
    signature = _digest(asdict(profile))
    include_photos = group=="photos" or include_photos and group is None
    pages = _candidate_pages(conn,page_ids,org_ids,run_ids,latest_only,attachment_role)
    totals = Counter(pages=len(pages),ready=0,completed=0,missing=0,processed=0,errors=0)
    by_group = Counter()
    outcomes = Counter()
    last = None
    stopped = None
    with (nullcontext() if plan else _one_worker(conn)):
        cursor_key = "model_batch_cursor_" + _digest((signature,page_ids,org_ids,run_ids,group,attachment_role,check_ids))[:24]
        if rotate:
            old = conn.execute("SELECT value FROM app_setting WHERE name=?", (cursor_key,)).fetchone()
            after = int(old[0]) if old else 0
            pages.sort(key=lambda row:(row[0]<=after,row[0]))
        for pid, captured, pdf, full, viewport in pages:
            saved = set(tuple(r) for r in conn.execute("""SELECT check_id,input_key FROM model_batch_result
                WHERE page_id=? AND profile_key=?""",(pid,signature)))
            photo_cache = {}
            for check in questions_for(conn,pid,include_photos=include_photos):
                if check_ids and check["id"] not in check_ids:
                    continue
                category = review_group(check)
                if not category or group and group != category or category=="photos" and not include_photos:
                    continue
                if attachment_role and check.get("attachment",{}).get("role")!=attachment_role:
                    continue
                if check.get("visual",{}).get("manual_only"):
                    continue
                extra_input = None
                if check.get("visual"):
                    bundle = visual_bundle(conn,pid,check,include_guide=False,cache=photo_cache)
                    extra_input = [m["sha256"] for m,_,_ in bundle if m["kind"]=="image"]
                    available = bool(extra_input)
                elif category=="photos" and "image" in check["material_kinds"]:
                    table = {"main_photo_original_size":"home_main_photo","garden_building_photo":"garden_building_photo"}.get(check["id"])
                    source = conn.execute(f"""SELECT d.sha256 FROM {table} h JOIN available_home_image_download d
                        ON d.page_id=h.page_id AND d.image_url=h.image_url WHERE h.page_id=? AND d.status='saved'""",
                        (pid,)).fetchone() if table else None
                    extra_input = source[0] if source else None
                    available = bool(extra_input) or _can_run(check,pdf,full,viewport)
                else:
                    available = _can_run(check,pdf,full,viewport)
                if not available:
                    totals["missing"] += 1
                    continue
                key = _digest((check,captured,pdf,full,viewport,extra_input))
                if (check["id"],key) in saved and not force:
                    totals["completed"] += 1
                    continue
                by_group[category] += 1
                totals["ready"] += 1
                if plan:
                    continue
                if before_check:
                    before_check()
                result = assess(conn,pid,check["id"],profile=profile)
                last = {"page_id":pid,"check_id":check["id"],"assessment_id":result["assessment_id"],
                        "outcome":result["outcome"],"error":result["error"]}
                totals["processed"] += 1
                outcomes[result["outcome"]] += 1
                if result["outcome"] == "error":
                    totals["errors"] += 1
                    # Leave the job available after the endpoint or input is corrected.
                    if (result["error"] or "").startswith(("Нет ответа от","API вернул HTTP 401","API вернул HTTP 403")):
                        stopped = "Сервер модели недоступен для этого профиля."
                else:
                    with conn:
                        conn.execute("INSERT OR REPLACE INTO model_batch_result VALUES(?,?,?,?,?)",
                                     (pid,check["id"],signature,key,result["assessment_id"]))
                if on_result:
                    on_result(last)
                if rotate:
                    with conn:
                        conn.execute("INSERT OR REPLACE INTO app_setting(name,value) VALUES(?,?)",(cursor_key,str(pid)))
                if stopped or limit is not None and totals["processed"] >= limit:
                    break
            if stopped or not plan and limit is not None and totals["processed"] >= limit:
                break
    return {**totals,"by_group":dict(by_group),"outcomes":dict(outcomes),"last":last,"stop_reason":stopped}


def main():
    if __package__:
        from .config import connect_db
        from .crawl_store import initialize_db
    else:
        from config import connect_db
        from crawl_store import initialize_db
    parser = argparse.ArgumentParser(description="Передать архивные PDF, тексты, снимки и оригиналы активной модели")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--page",nargs="+",type=int)
    scope.add_argument("--org",nargs="+",type=int)
    scope.add_argument("--run",nargs="+",type=int)
    scope.add_argument("--all",action="store_true")
    parser.add_argument("--group",choices=GROUPS)
    parser.add_argument("--check",nargs="+",help="проверить только перечисленные ID вопросов")
    parser.add_argument("--attachment-role",help="роль оригинала, например schedule, programs или catalog")
    parser.add_argument("--plan",action="store_true",help="посчитать готовые вопросы без обращений к модели")
    parser.add_argument("--force",action="store_true",help="повторить также завершённые вопросы")
    parser.add_argument("--include-photos",action="store_true",help="включить проверки фото и карточек")
    parser.add_argument("--latest-only",action="store_true",help="только последний снимок каждого URL учреждения")
    count = parser.add_mutually_exclusive_group()
    count.add_argument("--limit",type=int,default=10)
    count.add_argument("--drain",action="store_true",help="обработать всю выборку")
    args = parser.parse_args()
    with closing(connect_db()) as conn:
        initialize_db(conn)
        result = batch_assess(conn,page_ids=args.page,org_ids=args.org,run_ids=args.run,group=args.group,
                              limit=None if args.drain else args.limit,force=args.force,plan=args.plan,
                              include_photos=args.include_photos,latest_only=args.latest_only,
                              attachment_role=args.attachment_role,check_ids=args.check or (),
                              on_result=lambda r:print(json.dumps(r,ensure_ascii=False),flush=True))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
