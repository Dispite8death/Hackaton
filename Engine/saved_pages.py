"""Read previously downloaded HTML from the project's SQLite database."""

import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import NamedTuple


class SavedPage(NamedTuple):
    id: int
    url: str
    date_loaded: int


def get_organization_id(conn: sqlite3.Connection, name: str) -> int | None:
    """Look up an exact institution name. Equal names require an explicit ID."""
    rows = conn.execute("SELECT id FROM organization WHERE name = ? ORDER BY id", (name,)).fetchall()
    if len(rows) > 1:
        ids = ", ".join(str(row[0]) for row in rows)
        raise ValueError(f"Несколько учреждений с таким названием. Выберите ID: {ids}")
    return rows[0][0] if rows else None


def section_pages(conn: sqlite3.Connection, organization_id: int, section: str) -> list[SavedPage]:
    """Return snapshot metadata. Each section URL appears once, with its latest saved version."""
    if section == "home":
        home = conn.execute("""
            SELECT p.id, p.url, p.date_loaded
            FROM page AS p JOIN crawl_fetch AS f ON f.page_id = p.id
            WHERE p.id_organization = ? AND f.section = 'home' AND f.depth = 0
              AND f.http_status BETWEEN 200 AND 299 AND f.error IS NULL
            ORDER BY p.date_loaded DESC, p.id DESC LIMIT 1
        """, (organization_id,)).fetchone()
        return [SavedPage(*home)] if home is not None else []

    # A news story also has fetch.section='news'. Only a link to the section
    # index in this same crawl identifies the page as an index.
    rows = conn.execute("""
        WITH targets AS (
            SELECT DISTINCT src.run_id, l.url
            FROM crawl_run AS r
            JOIN crawl_fetch AS src ON src.run_id = r.id
            JOIN crawl_link AS l ON l.fetch_id = src.id
            WHERE r.id_organization = ? AND src.page_id IS NOT NULL
              AND l.section = ? AND l.relation = 'section' AND instr(l.url, '#') = 0
        ), versions AS (
            SELECT p.id, p.url, p.date_loaded,
                   row_number() OVER (PARTITION BY p.url ORDER BY p.date_loaded DESC, p.id DESC) AS version
            FROM page AS p JOIN crawl_fetch AS f ON f.page_id = p.id
            WHERE p.id_organization = ? AND f.http_status BETWEEN 200 AND 299 AND f.error IS NULL
              AND EXISTS (
                  SELECT 1 FROM targets AS t
                  WHERE t.run_id = f.run_id AND t.url IN (f.url, f.final_url, p.url)
              )
        )
        SELECT id, url, date_loaded FROM versions WHERE version = 1 ORDER BY url
    """, (organization_id, section, organization_id))
    return [SavedPage(*row) for row in rows]


def get_snapshot_html(conn: sqlite3.Connection, page_id: int) -> str | None:
    row = conn.execute("SELECT text_page FROM page WHERE id = ?", (page_id,)).fetchone()
    return row[0] if row is not None else None


def get_homepage_html(conn: sqlite3.Connection, organization_id: int) -> str | None:
    """Get the latest successfully saved homepage of the institution."""
    pages = section_pages(conn, organization_id, "home")
    return get_snapshot_html(conn, pages[0].id) if pages else None


def main() -> int:
    if __package__:
        from .config import DB_PATH
    else:
        from config import DB_PATH

    parser = argparse.ArgumentParser(description="Показать адреса или сохранить HTML из SQLite.")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--org", type=int, help="ID учреждения")
    selector.add_argument("--name", help="Полное название учреждения из справочника")
    parser.add_argument("--section", default="home", help="Код раздела, например home, news или staff")
    parser.add_argument("--url", help="Точный адрес из списка подходящих страниц")
    parser.add_argument("--out", type=Path, help="Имя нового HTML-файла. Без --out выводятся адреса")
    args = parser.parse_args()

    try:
        database = Path(DB_PATH).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(database, uri=True)) as conn:
            organization_id = args.org
            if args.name is not None:
                organization_id = get_organization_id(conn, args.name)
                if organization_id is None:
                    parser.exit(1, "Учреждение с таким названием не найдено.\n")
            pages = section_pages(conn, organization_id, args.section)
            if args.url:
                pages = [page for page in pages if page.url == args.url]
            if not pages:
                parser.exit(1, "Подходящий HTML-снимок не найден.\n")
            if args.out is None or len(pages) > 1:
                for page in pages:
                    print(page.url)
                if args.out is not None:
                    parser.exit(2, "Найдено несколько страниц. Выберите адрес с помощью --url.\n")
                return 0
            html = get_snapshot_html(conn, pages[0].id)
            if html is None:
                parser.exit(1, "Снимок больше недоступен.\n")
        with args.out.open("x", encoding="utf-8", newline="") as file:
            file.write(html)
    except FileExistsError:
        parser.exit(1, f"Файл уже существует: {args.out}\n")
    except (sqlite3.Error, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
