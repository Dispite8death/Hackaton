"""Store and export browser artifacts captured during the same visit as a page snapshot."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

if __package__:
    from .retention import retired_for_page
else:
    from retention import retired_for_page

if TYPE_CHECKING:
    from browser_capture import RenderEvidence


ARTIFACT_COLUMNS = {
    "dom": "rendered_html",
    "screenshot": "screenshot",
    "full_screenshot": "full_screenshot",
    "pdf": "pdf",
}


def incomplete_document(details: dict) -> bool:
    """Older captures can have only readyState; compare the document itself, not print geometry."""
    return details.get("document_complete_for_extraction") is False or details.get("document_ready_state") == "loading"


def save_render(conn: sqlite3.Connection, page_id: int, render: RenderEvidence) -> None:
    """Write once in the caller's transaction. Old snapshots keep their original capture."""
    artifacts = {kind: getattr(render, column) for kind, column in ARTIFACT_COLUMNS.items()}
    available = {kind for kind, data in artifacts.items() if data}
    requested = set(render.details.get("requested_artifacts", ("dom", "screenshot", "full_screenshot")))
    if render.pdf or "pdf" in render.details.get("artifact_errors", {}):
        requested.add("pdf")
    if not available:
        status = "failed"
    elif requested - available or incomplete_document(render.details):
        status = "partial"
    else:
        status = "complete"
    image_mime = None
    if render.screenshot or render.full_screenshot:
        image_mime = render.details.get("screenshot_mime", "image/jpeg")
    conn.execute("""
        INSERT INTO page_render (
            page_id, captured_at, url, status, viewport_width, viewport_height, css_loaded, details,
            rendered_html, screenshot_mime, screenshot, full_screenshot, pdf
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (page_id, render.captured_at, render.url, status, render.viewport_width, render.viewport_height,
          render.css_loaded, json.dumps(render.details, ensure_ascii=False, allow_nan=False),
          render.rendered_html or None, image_mime, render.screenshot or None,
          render.full_screenshot or None, render.pdf or None))


def render_info(conn: sqlite3.Connection, page_id: int) -> dict | None:
    """Read metadata without loading image, PDF or DOM contents."""
    row = conn.execute("""
        SELECT captured_at, url, status, viewport_width, viewport_height, css_loaded, details, screenshot_mime,
               rendered_html IS NOT NULL, screenshot IS NOT NULL, full_screenshot IS NOT NULL, pdf IS NOT NULL
        FROM page_render WHERE page_id = ?
    """, (page_id,)).fetchone()
    if row is None:
        return None
    timestamp, url, status, width, height, css, details, mime, *present = row
    return {
        "page_id": page_id, "captured_at": timestamp, "url": url, "status": status,
        "viewport_width": width, "viewport_height": height,
        "css_loaded": None if css is None else bool(css), "screenshot_mime": mime,
        "available_artifacts": [kind for kind, exists in zip(ARTIFACT_COLUMNS, present) if exists],
        "retired_artifacts":[r for r in retired_for_page(conn,page_id) if r["kind"] in ARTIFACT_COLUMNS],
        "details": json.loads(details),
    }


def staff_source_html(conn: sqlite3.Connection, page_id: int) -> str | None:
    """Use the same HTML source the staff extractor used during this page's capture."""
    row = conn.execute("""
        SELECT CASE WHEN json_extract(r.details, '$.staff_source') = 'rendered_dom'
                         AND json_extract(r.details, '$.document_complete_for_extraction') IS NOT 0
                         AND json_extract(r.details, '$.document_ready_state') IS NOT 'loading'
                         AND r.rendered_html IS NOT NULL THEN r.rendered_html ELSE p.text_page END
        FROM page AS p LEFT JOIN page_render AS r ON r.page_id = p.id
        WHERE p.id = ?
    """, (page_id,)).fetchone()
    return row[0] if row is not None else None


def render_artifact(conn: sqlite3.Connection, page_id: int, kind: str) -> bytes | None:
    """Get original image/PDF bytes or UTF-8 DOM/JSON; return None for unavailable artifacts."""
    kind = kind.replace("-", "_")
    if kind == "details":
        info = render_info(conn, page_id)
        return json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8") if info is not None else None
    column = ARTIFACT_COLUMNS[kind]
    row = conn.execute(f"SELECT {column} FROM page_render WHERE page_id = ?", (page_id,)).fetchone()
    if row is None or row[0] is None:
        return None
    return row[0].encode("utf-8") if kind == "dom" else row[0]


def main() -> int:
    if __package__:
        from .config import DB_PATH
    else:
        from config import DB_PATH

    parser = argparse.ArgumentParser(description="Экспорт материалов браузерного обхода из SQLite по page.id")
    parser.add_argument("--page", required=True, type=int, metavar="ID", help="ID сохранённой страницы")
    parser.add_argument("--format", choices=("details", "dom", "screenshot", "full-screenshot", "pdf"),
                        default="details", help="JSON со сведениями, DOM, JPEG окна, длинный JPEG или PDF")
    parser.add_argument("--out", type=Path, help="Новый файл для экспорта; JSON можно вывести в консоль")
    args = parser.parse_args()
    if args.out is None and args.format != "details":
        parser.error("Укажите --out для сохранения материала")
    try:
        with closing(sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            data = render_artifact(conn, args.page, args.format)
        if data is None:
            parser.exit(1, f"Материал {args.format!r} не сохранён для page.id={args.page}.\n")
        if args.out is None:
            print(data.decode("utf-8"))
        else:
            with args.out.open("xb") as output:
                output.write(data)
            print(args.out)
    except (OSError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
