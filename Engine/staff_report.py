"""Read saved staff duplicate flags and their source pages from SQLite."""

import argparse
import sqlite3
from contextlib import closing
from itertools import groupby, islice
from pathlib import Path

if __package__:
    from .config import DB_PATH
else:
    from config import DB_PATH


LATEST = """SELECT id_organization AS organization_id, max(id) AS run_id
            FROM crawl_run WHERE purpose = 'audit' AND finished_at IS NOT NULL GROUP BY id_organization"""
MATCH = {"same_profile": "совпадает профиль", "same_name": "совпадает полное ФИО",
         "possible": "возможное совпадение", "single": "отдельная запись"}


def audit_status(conn, organization_id):
    row = conn.execute(f"""
        WITH latest AS ({LATEST})
        SELECT o.name, l.run_id, s.observations FROM organization o
        LEFT JOIN latest l ON l.organization_id=o.id LEFT JOIN staff_scan_run s ON s.run_id=l.run_id
        WHERE o.id=?
    """, (organization_id,)).fetchone()
    if row is None:
        raise ValueError(f"Учреждение с ID {organization_id} не найдено.")
    name, run, observations = row
    if run is None:
        status = "нет завершённого обхода"
    elif observations is None:
        status = f"обход {run}, обработка карточек ещё не отмечена"
    else:
        status = f"обход {run}, обработано карточек: {observations}"
    print(f"ID {organization_id} · {name} · {status}")
    return run


def local_rows(conn, organization_id, run):
    return conn.execute("""
        SELECT coalesce(s.duplicate_group, printf('obs-%d', s.id)) AS group_id, s.full_name, s.role,
               s.duplicate_match, s.duplicate_card_count, s.duplicate_page_count,
               s.duplicate_roles_differ, s.duplicate_needs_review, s.duplicate_cards_on_page,
               s.id AS observation_id, p.id AS page_id, p.url AS source_url, f.section
        FROM crawl_fetch f JOIN page p ON p.id=f.page_id JOIN staff_observation s ON s.page_id=p.id
        WHERE f.run_id=? AND p.id_organization=?
          AND (s.duplicate_card_count>1 OR s.duplicate_match='possible' OR s.duplicate_needs_review=1)
        ORDER BY group_id, p.id, s.id
    """, (run, organization_id))


def cross_rows(conn, pair):
    selected = "WHERE s.organization_id IN (?, ?)" if pair else ""
    return conn.execute(f"""
        WITH latest AS ({LATEST}), candidates AS (
            SELECT s.* FROM staff_cross_org_source s JOIN latest USING (organization_id, run_id)
            {selected}
        ), shared AS (
            SELECT name_key FROM candidates GROUP BY name_key HAVING count(DISTINCT organization_id)>=2
        )
        SELECT c.* FROM candidates c JOIN shared USING (name_key)
        ORDER BY c.name_key, c.organization_id, c.page_id, c.observation_id
    """, pair or ())


def show_groups(rows, cross, limit):
    groups = groupby(rows, key=lambda row: row["name_key" if cross else "group_id"])
    printed = 0
    for _, group in islice(groups, limit):
        cards = list(group)
        first = cards[0]
        name = first["full_name"] or "ФИО не указано"
        if cross:
            org_count = len({row["organization_id"] for row in cards})
            print(f"\n{name} · совпадение полного ФИО · учреждений: {org_count} · требуется сверка")
        else:
            print(f"\n{name} · карточек: {first['duplicate_card_count']}, "
                  f"адресов страниц: {first['duplicate_page_count']}"
                  + (" · должности различаются" if first["duplicate_roles_differ"] else "")
                  + (" · требуется сверка" if any(row["duplicate_needs_review"] for row in cards) else ""))
        previous_org = None
        for row in cards:
            if cross and row["organization_id"] != previous_org:
                previous_org = row["organization_id"]
                print(f"  ID {previous_org} · {row['organization_name']}")
            flag = "" if cross else (f"{MATCH.get(row['duplicate_match'], 'требуется сверка')}, "
                                     f"на этой странице: {row['duplicate_cards_on_page']} · ")
            print(f"    {row['full_name'] or 'ФИО не указано'} · {row['role'] or 'должность не указана'} · "
                  f"{flag}карточка {row['observation_id']} · страница {row['page_id']} "
                  f"({row['section']}) · {row['source_url']}")
        printed += 1
    if not printed:
        print("В сохранённых карточках по выбранным условиям совпадений не отмечено.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Показать сохранённые совпадения карточек по последним завершённым обходам.",
        epilog="Для выбранных ID показывается, завершена ли обработка карточек. Пустой результат относится "
               "только к сохранённым карточкам. Совпадение ФИО требует сверки источников.",
    )
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--pair", nargs=2, type=int, metavar=("ID1", "ID2"), help="общие полные ФИО двух учреждений")
    choice.add_argument("--org", type=int, metavar="ID", help="повторяющиеся или похожие карточки внутри учреждения")
    choice.add_argument("--cross", action="store_true", help="полные ФИО, совпавшие в нескольких учреждениях")
    parser.add_argument("--limit", type=int, metavar="N", help="показать первые N групп со всеми их источниками")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit должен быть положительным.")
    if args.pair and args.pair[0] == args.pair[1]:
        parser.error("Для --pair укажите два разных учреждения.")
    try:
        uri = Path(DB_PATH).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            if args.org is not None:
                run = audit_status(conn, args.org)
                rows = local_rows(conn, args.org, run)
            else:
                for org in args.pair or ():
                    audit_status(conn, org)
                rows = cross_rows(conn, args.pair)
            show_groups(rows, args.org is None, args.limit)
    except sqlite3.OperationalError as exc:
        if str(exc).startswith(("no such table:", "no such column:")):
            parser.exit(1, "В этой базе ещё нет данных в формате отчёта. Сначала обработайте сохранённые "
                        "карточки командой scan_staff.py --all.\n")
        parser.exit(1, f"Не удалось открыть или прочитать базу данных: {exc}\n")
    except (sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Не удалось прочитать отчёт: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
