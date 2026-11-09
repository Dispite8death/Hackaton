"""Export a reproducible review of one archived school homepage and its crawl."""

import argparse
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag

if __package__:
    from .retention import retired_for_page
else:
    from retention import retired_for_page


def _records(conn, sql, args):
    cursor = conn.execute(sql, args)
    columns = [item[0] for item in cursor.description]
    result = []
    for row in cursor:
        item = dict(zip(columns, row))
        if "evidence" in item:
            item["evidence"] = json.loads(item["evidence"])
        result.append(item)
    return result


def _first(conn, sql, args):
    return next(iter(_records(conn, sql, args)), None)


def _time(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None


def _write(folder, relative, data):
    if folder is None:
        return None
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return relative


def _render(conn, page_id, folder):
    render = _first(conn, """
        SELECT captured_at,url,status,viewport_width,viewport_height,css_loaded,screenshot_mime,
               screenshot IS NOT NULL AS has_viewport, full_screenshot IS NOT NULL AS has_full,
               rendered_html IS NOT NULL AS has_dom,pdf IS NOT NULL AS has_pdf,
               json_extract(details,'$.print_comparison') AS print_comparison,
               json_extract(details,'$.entry_links') AS entry_links,
               json_extract(details,'$.artifact_errors') AS artifact_errors,
               json_extract(details,'$.peer_styles') AS peer_styles,
               json_extract(details,'$.full_screenshot_coverage') AS full_screenshot_coverage,
               json_extract(details,'$.full_screenshot_bounds') AS full_screenshot_bounds
        FROM page_render WHERE page_id=?
    """, (page_id,))
    if render is None:
        return None
    render["retired_artifacts"]=[r for r in retired_for_page(conn,page_id) if r["kind"] in ("screenshot","full_screenshot","pdf")]
    for key in ("peer_styles", "full_screenshot_bounds", "print_comparison", "entry_links", "artifact_errors"):
        if render[key] is not None:
            render[key] = json.loads(render[key])
    render["captured_at_utc"] = _time(render["captured_at"])
    extension = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(render["screenshot_mime"], "bin")
    for column, exists, filename in (
        ("screenshot", "has_viewport", f"viewport.{extension}"),
        ("full_screenshot", "has_full", f"full.{extension}"),
        ("rendered_html", "has_dom", "rendered.html"),
        ("pdf", "has_pdf", "page.pdf"),
    ):
        present = bool(render.pop(exists))
        path = None
        if present and folder is not None:
            data = conn.execute(f"SELECT {column} FROM page_render WHERE page_id=?", (page_id,)).fetchone()[0]
            path = _write(folder, f"pages/{page_id}/{filename}", data.encode("utf-8") if isinstance(data, str) else data)
        render[column] = {"available": present, "file": path}
    return render


def _downloads(conn, pages, folder):
    assets = []
    exported = {}
    for page in pages:
        rows = _records(conn, """
            SELECT d.page_id,d.image_url,d.final_url,d.fetched_at,d.status,d.http_status,d.error,d.sha256,
                   a.mime,a.width,a.height,length(a.data) AS size_bytes
            FROM available_home_image_download d LEFT JOIN home_image_asset a USING(sha256)
            WHERE d.page_id=? ORDER BY d.image_url
        """, (page["page_id"],))
        for asset in rows:
            asset["fetched_at_utc"] = _time(asset["fetched_at"])
            digest = asset["sha256"]
            if folder is not None and digest and digest not in exported:
                # Never use filenames printed on the source website to write into the bundle.
                if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise ValueError("Некорректный SHA-256 изображения в архиве")
                suffix = {"image/jpeg":"jpg", "image/png":"png", "image/webp":"webp", "image/gif":"gif",
                          "image/avif":"avif", "image/bmp":"bmp"}.get(asset["mime"], "bin")
                data = conn.execute("SELECT data FROM home_image_asset WHERE sha256=?", (digest,)).fetchone()[0]
                exported[digest] = _write(folder, f"images/{digest}.{suffix}", data)
            asset["file"] = exported.get(digest)
        assets.extend(rows)
    return assets


def _report(conn, page_id, folder):
    source = _first(conn, """
        SELECT p.id AS page_id,p.url,p.date_loaded,r.id AS run_id,r.purpose,r.started_at,r.finished_at,
               r.state,r.stop_reason,o.id AS organization_id,o.name AS organization_name,
               (SELECT max(id) FROM crawl_run WHERE id_organization=o.id AND purpose='audit'
                 AND finished_at IS NOT NULL) AS latest_finished_audit_id
        FROM page p JOIN crawl_fetch f ON f.page_id=p.id JOIN crawl_run r ON r.id=f.run_id
        JOIN organization o ON o.id=p.id_organization AND o.id=r.id_organization
        WHERE p.id=? AND f.depth=0 AND o.kind='school'
    """, (page_id,))
    if source is None:
        raise ValueError(f"Сохранённая главная страница школы с page_id={page_id} не найдена")
    run_id = source["run_id"]
    for key in ("date_loaded", "started_at", "finished_at"):
        source[key + "_utc"] = _time(source[key])
    criteria = {key: _first(conn, f"SELECT * FROM home_{key} WHERE page_id=?", (page_id,))
                for key in ("main_photo", "canteen_photo", "school_description", "event_block")}
    pages = _records(conn, """
        SELECT p.id AS page_id,f.section,f.url AS requested_url,p.url,p.date_loaded,f.http_status
        FROM crawl_fetch f JOIN page p ON p.id=f.page_id
        WHERE f.run_id=? AND p.id_organization=? AND
              (p.id=? OR f.section='events' OR EXISTS(SELECT 1 FROM home_event_block b WHERE b.page_id=p.id))
        ORDER BY p.id!=?, f.id
    """, (run_id, source["organization_id"], page_id, page_id))
    aliases = {}
    has_problems_table = conn.execute("""
        SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='home_analysis_problem')
    """).fetchone()[0]
    for page in pages:
        for url in (page["url"], page["requested_url"]):
            aliases.setdefault(urldefrag(url).url, set()).add(page["page_id"])
        page["removed_materials"]=retired_for_page(conn,page["page_id"])
    for page in pages:
        pid = page["page_id"]
        page["analysis_problem"] = (_first(conn, """
            SELECT page_id,attempted_at,error FROM home_analysis_problem WHERE page_id=?
        """, (pid,)) if has_problems_table else None)
        if page["analysis_problem"] is not None:
            problem = page["analysis_problem"]
            problem["attempted_at_utc"] = _time(problem["attempted_at"])
        page["date_loaded_utc"] = _time(page["date_loaded"])
        page["response_html_file"] = None
        if folder is not None:
            text = conn.execute("SELECT text_page FROM page WHERE id=?", (pid,)).fetchone()[0]
            page["response_html_file"] = _write(folder, f"pages/{pid}/response.html", text.encode("utf-8"))
        page["render"] = _render(conn, pid, folder)
        page["event_block"] = _first(conn, "SELECT * FROM home_event_block WHERE page_id=?", (pid,))
        links = _records(conn, "SELECT * FROM home_event_link WHERE page_id=? ORDER BY ordinal", (pid,))
        for link in links:
            target = urldefrag(link["url"]).url if link["url"] else None
            link["target_page_ids_in_bundle"] = sorted(aliases.get(target, ()))
            signals = link["evidence"].get("evidence", {}).get("heuristics", [])
            link["template_assessment"] = {
                "signals": signals,
                "status": ("not_applicable" if link["kind"] != "event" else
                           "signals_present" if signals else "no_stored_signals"),
            }
            link["event_truth"] = "independent_evidence_required" if link["kind"] == "event" else "not_an_event_record"
        page["event_links"] = links
    quarters = _records(conn, """
        SELECT * FROM school_event_quarter WHERE run_id=? ORDER BY CASE period WHEN 'current' THEN 0 ELSE 1 END
    """, (run_id,))
    assets = _downloads(conn, pages, folder)
    by_ref = {(a["page_id"], a["image_url"]): a for a in assets}
    reviewed_urls = set()
    for category in ("main_photo", "canteen_photo"):
        criterion = criteria[category]
        if criterion and criterion["image_url"]:
            reviewed_urls.add(criterion["image_url"])
            archived = by_ref.get((page_id, criterion["image_url"]))
            criterion["archive"] = ({"status": archived["status"], "sha256": archived["sha256"], "file": archived["file"]}
                                    if archived else {"status": "not_archived"})
    description = criteria["school_description"] or {"page_id": page_id, "status": "not_analyzed"}
    count = description.get("char_count")
    length_result = "needs_review"
    if description["status"] == "found" and count is not None and description.get("text"):
        length_result = "within_range" if 300 <= count <= 400 else "outside_range"
    description["length_check"] = {"min_chars": 300, "max_chars": 400, "char_count": count, "result": length_result}
    criteria["school_description"] = description
    photo = criteria["main_photo"] or {"page_id": page_id, "status": "not_analyzed"}
    original = by_ref.get((page_id, photo.get("image_url"))) or {}
    width, height = original.get("width"), original.get("height")
    dimension_result = "unknown"
    if photo["status"] == "found" and original.get("status") == "saved" and width and height:
        dimension_result = "exact_match" if (width, height) == (1200, 900) else "different_dimensions"
    photo["original_dimension_check"] = {"required_width": 1200, "required_height": 900, "width": width,
                                         "height": height, "unit": "source_file_pixels", "result": dimension_result}
    criteria["main_photo"] = photo
    reviewed_urls.update(link["image_url"] for p in pages for link in p["event_links"] if link["image_url"])
    repeats = _records(conn, """
        SELECT h.* FROM home_reused_image h JOIN json_each(?) selected ON selected.value=h.image_url
        ORDER BY h.image_url
    """, (json.dumps(sorted(reviewed_urls)),))
    hashes = {a["sha256"] for a in assets if a["sha256"]}
    has_asset_view = conn.execute("""
        SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='view' AND name='home_reused_asset')
    """).fetchone()[0]
    identical = _records(conn, """
        SELECT h.* FROM home_reused_asset h JOIN json_each(?) selected ON selected.value=h.sha256
        ORDER BY h.sha256
    """, (json.dumps(sorted(hashes)),)) if has_asset_view and hashes else []
    scope = {"scope": "latest_finished_audit_per_organization",
             "selected_run_in_scope": run_id == source["latest_finished_audit_id"]}
    return {
        "source": source, "criteria": criteria, "pages": pages, "analysis_problem_table_available": bool(has_problems_table),
        "quarters": {p: next((r for r in quarters if r["period"] == p), None) for p in ("current", "next")},
        "image_downloads": assets,
        "reused_image_urls": {**scope, "matching_urls_from_this_bundle": repeats},
        "reused_image_bytes": {**scope, "view_available": bool(has_asset_view),
                               "matching_hashes_from_this_bundle": identical,
                               "interpretation": "Одинаковые байты для нескольких позиций. Требуется проверка контекста."},
        "interpretation": {
            "archive_scope": "Главная и сохранённые страницы мероприятий выбранного обхода. HTML выгружается в UTF-8.",
            "catalog_links": "Ссылка на каталог показывает маршрут к разделу и сама по себе не свидетельствует о событии.",
            "template_assessment": "Признаки шаблона взяты из сохранённого текста и разметки.",
            "event_truth": "independent_evidence_required",
            "image_download_time": "fetched_at относится к скачиванию файла, date_loaded — к снимку HTML.",
            "repeat_scope": "Повторы URL рассчитаны по последним завершённым audit всех учреждений на момент экспорта.",
        },
    }


def _publish(source, destination):
    # POSIX rename() can replace an existing empty directory; use the OS no-replace operation.
    if os.name == "nt":
        os.rename(source, destination)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    a, b = os.fsencode(source), os.fsencode(destination)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        result = libc.renameat2(-100, a, -100, b, 1)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        result = libc.renamex_np(a, b, 4)
    else:
        raise OSError("На этой платформе недоступна атомарная публикация новой папки")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), destination)


def review_home(conn: sqlite3.Connection, page_id: int, out: Path | None = None) -> dict:
    """Read a consistent SQLite snapshot and optionally publish the review as a new directory."""
    return publish_review(conn, out, lambda staging: _report(conn, page_id, staging))


def publish_review(conn, out, build):
    """Publish all material files and the manifest together, from one SQLite read snapshot."""
    destination = Path(out).absolute() if out is not None else None
    if destination is not None and os.path.lexists(destination):
        raise FileExistsError(f"Папка или файл уже существуют: {destination}")
    staging = None
    try:
        if destination is not None:
            parent = destination.parent.resolve(strict=True)
            candidate = parent / f".home-review-{uuid.uuid4().hex}"
            # On Windows inherit the parent ACL so an app-container caller can
            # fill and rename its directory. mkdir rejects an existing candidate.
            candidate.mkdir(mode=0o777 if os.name == "nt" else 0o700)
            staging = candidate
        conn.execute("SAVEPOINT home_review_read")
        try:
            report = build(staging)
        finally:
            conn.execute("RELEASE home_review_read")
        if staging is not None:
            contents = json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
            _write(staging, "review.json", contents.encode("utf-8"))
            _publish(staging, destination)
            staging = None
        return report
    finally:
        if staging is not None:
            shutil.rmtree(staging)


def main():
    if __package__:
        from .config import DB_PATH
    else:
        from config import DB_PATH
    parser = argparse.ArgumentParser(description="Экспорт материалов главной страницы школы для проверки")
    parser.add_argument("--page", type=int, required=True, help="ID снимка главной страницы школы")
    parser.add_argument("--out", type=Path, help="новая папка для JSON и оригиналов, без этого параметра — JSON в консоль")
    args = parser.parse_args()
    try:
        with closing(sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            report = review_home(conn, args.page, args.out)
        if args.out is None:
            sys.stdout.buffer.write((json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8"))
        else:
            print(args.out / "review.json")
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
