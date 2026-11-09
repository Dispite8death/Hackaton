"""Re-analyse saved school pages, fetch their photos or read their calendar-quarter evidence."""

import argparse
import json
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

if __package__:
    from .config import connect_db, DB_PATH
    from .crawl_store import initialize_db
    from .event_quarters import refresh_quarters
    from .home_image_files import archive_home_images
    from .home_store import analyze_saved_page
else:
    from config import connect_db, DB_PATH
    from crawl_store import initialize_db
    from event_quarters import refresh_quarters
    from home_image_files import archive_home_images
    from home_store import analyze_saved_page


def select_runs(conn, args):
    kind = args.kind or ("school" if args.action == "quarters" or args.all else None)
    if args.run is not None:
        rows = conn.execute("""
            SELECT r.id, o.id FROM crawl_run r JOIN organization o ON o.id=r.id_organization
            WHERE r.id=? AND (? IS NULL OR o.kind=?) AND r.finished_at IS NOT NULL
        """, (args.run, kind, kind)).fetchall()
    else:
        rows = conn.execute("""
            SELECT max(r.id),o.id FROM crawl_run r JOIN organization o ON o.id=r.id_organization
            WHERE (? IS NULL OR o.kind=?) AND r.finished_at IS NOT NULL AND r.purpose='audit' GROUP BY o.id ORDER BY o.id
        """, (kind, kind)).fetchall()
        if args.org is not None:
            wanted = set(args.org)
            rows = [row for row in rows if row[1] in wanted]
            missing = sorted(wanted - {row[1] for row in rows})
            if missing:
                raise ValueError(f"Нет подходящего завершённого основного обхода для ID {missing}")
    if not rows:
        raise ValueError("Не найден завершённый обход учреждения")
    return rows


def show_quarters(conn, run_id):
    labels = {"dated_candidates":"есть датированные записи", "dates_unresolved":"требуется уточнить даты",
              "listing_not_captured":"страница списка не сохранена", "listing_unresolved":"список требует проверки",
              "none_in_saved_pages":"в проверенных страницах датированных записей за квартал не найдено"}
    rows = conn.execute("""
        SELECT start_date,end_date,status,dated_events,evidence FROM school_event_quarter
        WHERE run_id=? ORDER BY start_date
    """, (run_id,)).fetchall()
    if not rows:
        print("Для этого обхода кварталы ещё не рассчитаны.")
    for start, end, status, count, evidence in rows:
        facts = json.loads(evidence)
        print(f"  {start} — {end} · {labels.get(status,status)} · событий с датой: {count}"
              f" · с неясной датой во всём обходе: {len(facts['unresolved'])}"
              f" · с шаблонным текстом: {len(facts['template_signals'])}")
        for item in facts["dated_candidates"]:
            from_text = any(ref.get("date_evidence", {}).get("method") == "calendar_confirmed_by_card_text"
                            for ref in item["sources"])
            provenance = " · год подтверждён текстом карточки" if from_text else ""
            print(f"    {item['start']} — {item['end']} · {item['title']}{provenance} · {item['event']}")
    issues = conn.execute("""
        SELECT page_id,title,calendar_text,reason,text_dates FROM school_event_date_issue
        WHERE run_id=? ORDER BY page_id,ordinal
    """, (run_id,))
    reasons = {"multiple_distinct_card_text_dates":"несколько разных дат в тексте",
               "card_text_day_month_mismatch":"день или месяц расходятся с календарём",
               "invalid_card_text_date":"в тексте указана невозможная дата"}
    for page_id,title,calendar,reason,text_dates in issues:
        found = ", ".join(f"{d['day']:02d}.{d['month']:02d}" + (f".{d['year']}" if d['year'] else "")
                          for d in json.loads(text_dates or "[]"))
        print(f"  Возможная ошибка дат · {title} · {reasons.get(reason,reason)}"
              f" · календарь «{calendar}» · в тексте «{found}» · снимок {page_id}")


def _archive_page(job):
    org, page_id = job
    with closing(connect_db()) as conn:
        count = archive_home_images(conn, page_id)
        errors = conn.execute("""
            SELECT count(*) FROM home_image_download WHERE page_id=? AND status='error'
            AND image_url IN (SELECT image_url FROM home_main_photo WHERE page_id=?
                              UNION SELECT image_url FROM home_canteen_photo WHERE page_id=?
                              UNION SELECT image_url FROM garden_building_photo WHERE page_id=?)
        """, (page_id, page_id, page_id, page_id)).fetchone()[0]
    return org, page_id, count, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (("scan","Разобрать уже сохранённые HTML с текущим словарём"),
                            ("images","Скачать или повторить получение файлов фото по снимкам"),
                            ("quarters","Показать даты и основания проверки двух кварталов")):
        action = actions.add_parser(name, help=help_text)
        action.set_defaults(kind=None)
        if name != "quarters":
            action.add_argument("--kind", choices=("school","kindergarten"), help="тип учреждения, для --all по умолчанию school")
        scope = action.add_mutually_exclusive_group(required=True)
        scope.add_argument("--org", type=int, nargs="+", help="ID учреждений в справочнике")
        scope.add_argument("--all", action="store_true", help="последние основные обходы по типу учреждения")
        scope.add_argument("--run", type=int, help="конкретный законченный обход, включая пробный")
    args = parser.parse_args(argv)
    jobs = []
    parse_errors = 0
    try:
        database = (sqlite3.connect(Path(DB_PATH).resolve().as_uri()+"?mode=ro",uri=True)
                    if args.action=="quarters" else connect_db())
        with closing(database) as conn:
            if args.action!="quarters":
                initialize_db(conn)
            for run_id, org in select_runs(conn, args):
                if args.action=="quarters":
                    print(f"Школа ID {org}, обход {run_id}")
                    show_quarters(conn, run_id)
                    continue
                pages = conn.execute("""
                    SELECT f.page_id,f.depth FROM crawl_fetch f WHERE f.run_id=? AND f.page_id IS NOT NULL
                    AND (f.depth=0 OR (f.section='events' AND EXISTS (
                        SELECT 1 FROM crawl_run r JOIN organization o ON o.id=r.id_organization
                        WHERE r.id=f.run_id AND o.kind='school'
                    ))) ORDER BY f.id
                """, (run_id,)).fetchall()
                if args.action=="images":
                    jobs.extend((org, page_id) for page_id,depth in pages if depth==0)
                    continue
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    errors = sum(not analyze_saved_page(conn, page_id) for page_id, _depth in pages)
                    refresh_quarters(conn,run_id)
                parse_errors += errors
                print(f"Учреждение ID {org}, обход {run_id} · разобрано страниц: {len(pages)-errors}, ошибок: {errors}",flush=True)
        if jobs:
            counts = Counter()
            with ThreadPoolExecutor(max_workers=min(3,len(jobs))) as pool:
                for org,page_id,archived,errors in pool.map(_archive_page,jobs):
                    counts.update(saved=archived, errors=errors)
                    print(f"Учреждение ID {org}, снимок {page_id} · сохранено фото: {archived}, ошибок: {errors}",flush=True)
            print(f"Снимков: {len(jobs)}, файлов по снимкам: {counts['saved']}, ошибок: {counts['errors']}")
            return int(bool(counts['errors']))
    except (ValueError,sqlite3.Error,OSError) as error:
        parser.exit(1,f"{error}\n")
    return int(bool(parse_errors))


if __name__ == "__main__":
    raise SystemExit(main())

