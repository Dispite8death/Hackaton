"""SQLite storage for crawl attempts, HTML snapshots and outgoing links."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING

if __package__:
    from .config import SCHEMA_SQL, STAFF_DUPLICATE_COLUMNS
    from .render_store import save_render
    from .staff_cross_org import refresh_cross_org_matches
    from .staff_duplicates import refresh_staff_duplicates
    from .staff_store import save_render_staff
    from .home_store import analyze_saved_page
    from .home_terms import initialize_terms
    from .event_quarters import refresh_quarters
    from .timeline_audit import refresh_timeline
    from .institution_audit import audit_run
    from .page_passport import index_page
    from .news_store import index_news_page
    from .site_navigator import index_navigator
else:
    from config import SCHEMA_SQL, STAFF_DUPLICATE_COLUMNS
    from render_store import save_render
    from staff_cross_org import refresh_cross_org_matches
    from staff_duplicates import refresh_staff_duplicates
    from staff_store import save_render_staff
    from home_store import analyze_saved_page
    from home_terms import initialize_terms
    from event_quarters import refresh_quarters
    from timeline_audit import refresh_timeline
    from institution_audit import audit_run
    from page_passport import index_page
    from news_store import index_news_page
    from site_navigator import index_navigator

if TYPE_CHECKING:
    from navigation import Link
    from browser_capture import RenderEvidence


def initialize_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)
        initialize_terms(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(crawl_run)")}
        for name, sql_type in (
            ("max_depth", "INTEGER"), ("max_pages", "INTEGER"), ("stop_reason", "TEXT"),
            ("purpose", "TEXT NOT NULL DEFAULT 'audit' CHECK (purpose IN ('audit', 'sample'))"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE crawl_run ADD COLUMN {name} {sql_type}")
        staff_columns = {row[1] for row in conn.execute("PRAGMA table_info(staff_observation)")}
        for name, definition in STAFF_DUPLICATE_COLUMNS.items():
            if name not in staff_columns:
                conn.execute(f"ALTER TABLE staff_observation ADD COLUMN {name} {definition}")
        visual_columns = {row[1] for row in conn.execute("PRAGMA table_info(staff_element)")}
        missing_context = False
        for name, sql_type in (
            ("render_source", "TEXT CHECK (render_source IN ('html_only','live','archived_with_assets'))"),
            ("render_url", "TEXT"), ("css_loaded", "INTEGER CHECK (css_loaded IN (0,1))"),
            ("viewport_width", "INTEGER CHECK (viewport_width > 0)"),
            ("viewport_height", "INTEGER CHECK (viewport_height > 0)"),
        ):
            if name not in visual_columns:
                conn.execute(f"ALTER TABLE staff_element ADD COLUMN {name} {sql_type}")
                missing_context = True
        if missing_context:
            # Old checks did not establish which resources and viewport the browser used.
            conn.execute("""
                UPDATE staff_element SET visual_status = 'unknown'
                WHERE visual_status NOT IN ('pending','unknown')
                  AND (render_source IS NULL OR render_source = 'html_only' OR css_loaded IS NOT 1
                       OR viewport_width IS NULL OR viewport_height IS NULL
                       OR (render_source = 'live' AND render_url IS NULL))
            """)


def select_organizations(conn: sqlite3.Connection, selectors=None):
    """Resolve IDs or exact names from the project directory in the requested order."""
    all_orgs = conn.execute("SELECT id, name, site_url, kind FROM organization ORDER BY id").fetchall()
    if not all_orgs:
        raise ValueError("Список учреждений пуст. Сначала запустите Engine/importer.py")
    if selectors is None:
        return all_orgs
    selected = {}
    for selector in selectors:
        matches = [org for org in all_orgs if str(org["id"]) == selector or org["name"] == selector]
        if len(matches) != 1:
            raise ValueError(f"Уточните ID учреждения: {selector!r} (совпадений: {len(matches)})")
        selected[matches[0]["id"]] = matches[0]
    return list(selected.values())


def start_run(
    conn: sqlite3.Connection,
    organization_id: int,
    *,
    max_depth: int | None = None,
    max_pages: int | None = None,
    purpose: str = "audit",
) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO crawl_run (id_organization, max_depth, max_pages, purpose) VALUES (?, ?, ?, ?)",
            (organization_id, max_depth, max_pages, purpose),
        )
        return cursor.lastrowid


def save_headings(
    conn: sqlite3.Connection, fetch_id: int, headings: Iterable[tuple[str, str]],
) -> None:
    """Save classified headings in the caller's transaction."""
    conn.executemany(
        "INSERT OR IGNORE INTO crawl_heading (fetch_id, section, label) VALUES (?, ?, ?)",
        ((fetch_id, section, label) for section, label in headings),
    )


def _index_snapshot(conn, page_id):
    # A broken markup fragment can stop one parser. Keep the HTTP and browser evidence for reanalysis.
    for analyze in (index_page, index_news_page, index_navigator):
        conn.execute("SAVEPOINT derived_page_data")
        try:
            analyze(conn, page_id)
            conn.execute("DELETE FROM page_index_issue WHERE page_id=? AND analyzer=?", (page_id, analyze.__name__))
        except (ValueError, TypeError, KeyError, AttributeError, AssertionError, IndexError, RecursionError) as error:
            conn.execute("ROLLBACK TO derived_page_data")
            conn.execute("INSERT OR REPLACE INTO page_index_issue VALUES(?,?,?)",
                         (page_id, analyze.__name__, f"{type(error).__name__}: {str(error)[:500]}"))
        finally:
            conn.execute("RELEASE derived_page_data")


def record_fetch(
    conn: sqlite3.Connection,
    run_id: int,
    url: str,
    section: str,
    depth: int,
    *,
    final_url: str | None = None,
    status: int | None = None,
    error: str | None = None,
    html: str | None = None,
    links: Iterable[Link] = (),
    headings: Iterable[tuple[str, str]] = (),
    loaded_at: int | None = None,
    render: RenderEvidence | None = None,
    original_page_id: int | None = None,
    revisit_relation: str = "revisit",
) -> int:
    """Save one attempt and its discoveries together. Only successful HTML gets a page."""
    if html is not None and (error is not None or status is None or not 200 <= status < 300):
        raise ValueError("Для снимка HTML нужен успешный HTTP-ответ")
    if loaded_at is None:
        loaded_at = int(time.time())

    with conn:
        page_id = None
        if html is not None:
            page = conn.execute(
                """
                INSERT INTO page (url, id_organization, date_loaded, text_page)
                VALUES (?, (SELECT id_organization FROM crawl_run WHERE id = ?), ?, ?)
                """,
                (final_url or url, run_id, loaded_at, html),
            )
            page_id = page.lastrowid
            if render is not None:
                save_render(conn, page_id, render)
                save_render_staff(conn, page_id, render)
        fetch = conn.execute(
            """
            INSERT INTO crawl_fetch
                (run_id, url, final_url, section, depth, fetched_at, http_status, error, page_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, url, final_url, section, depth, loaded_at, status, error, page_id),
        )
        if original_page_id is not None:
            owner = conn.execute("""
                SELECT 1 FROM page p JOIN crawl_run r ON r.id_organization=p.id_organization
                WHERE p.id=? AND r.id=? AND r.purpose='sample'
            """, (original_page_id, run_id)).fetchone()
            if not owner:
                raise ValueError("Для повторного посещения нужен пробный обход того же учреждения")
            conn.execute("INSERT INTO page_revisit(original_page_id,fetch_id,relation) VALUES(?,?,?)",
                         (original_page_id, fetch.lastrowid, revisit_relation))
        conn.executemany(
            """
            INSERT INTO crawl_link (fetch_id, url, label, section, relation, follow)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                (fetch.lastrowid, link.url, link.label, link.section, link.relation, link.follow)
                for link in links
            ),
        )
        save_headings(conn, fetch.lastrowid, headings)
        if page_id is not None and (depth == 0 or section == "events"):
            analyze_saved_page(conn, page_id)
        if page_id is not None:
            _index_snapshot(conn, page_id)
        return fetch.lastrowid


def finish_run(
    conn: sqlite3.Connection, run_id: int, state: str, *, reason: str | None = None,
) -> None:
    with conn:
        run = conn.execute(
            "UPDATE crawl_run SET state = ?, stop_reason = ?, finished_at = unixepoch() WHERE id = ? RETURNING purpose",
            (state, reason, run_id),
        ).fetchone()
        if run is not None:
            refresh_staff_duplicates(conn, run_id)
            refresh_quarters(conn, run_id)
            refresh_timeline(conn, run_id)
            audit_run(conn, run_id)
            if run[0] == "audit":
                refresh_cross_org_matches(conn)
