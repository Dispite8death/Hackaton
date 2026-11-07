"""Archive the raster files referenced by the two home-page photo criteria."""

import argparse
import hashlib
import io
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__:
    from .archive_access import uses_archive
    from .retention_records import RETENTION_ITEM_SQL
    from .http_page import same_site
else:
    from archive_access import uses_archive
    from retention_records import RETENTION_ITEM_SQL
    from http_page import same_site


HOME_IMAGE_SCHEMA_SQL = RETENTION_ITEM_SQL+"""
CREATE TABLE IF NOT EXISTS home_image_asset (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    mime TEXT NOT NULL,
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS home_image_download (
    page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    image_url TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    final_url TEXT NOT NULL,
    http_status INTEGER,
    status TEXT NOT NULL CHECK (status IN ('saved','error')),
    error TEXT,
    sha256 TEXT REFERENCES home_image_asset(sha256),
    CHECK ((status = 'saved' AND sha256 IS NOT NULL AND error IS NULL)
        OR (status = 'error' AND sha256 IS NULL AND error IS NOT NULL)),
    PRIMARY KEY (page_id, image_url)
);
CREATE VIEW IF NOT EXISTS available_home_image_download AS SELECT d.* FROM home_image_download d
    WHERE NOT EXISTS(SELECT 1 FROM retention_item t WHERE t.page_id=d.page_id AND t.kind='image' AND t.source=d.image_url);
"""
_TABLES = {"main_photo": "home_main_photo", "canteen_photo": "home_canteen_photo",
           "garden_building": "garden_building_photo"}
_CDN = {"media.gosweb.gosuslugi.ru", "static.gosweb.gosuslugi.ru"}
_MIME = {"JPEG": "image/jpeg", "MPO": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif",
         "BMP": "image/bmp", "AVIF": "image/avif"}
# JPEG's decoder identifies MPO after opening the first frame; MPO has no open handler of its own.
_DECODERS = tuple(fmt for fmt in _MIME if fmt != "MPO")
_MAX_BYTES = 16 * 1024 * 1024
_MAX_PIXELS = 24_000_000
_MAX_JPEG_SOURCE_PIXELS = 80_000_000
_MAX_FRAMES = 64


def _allowed(url, site):
    target = urlsplit(url)
    return same_site(url, site) or (
        target.scheme == "https" and target.hostname in _CDN and target.port in (None, 443)
        and target.username is None and target.password is None
    )


class _ImageRedirect(HTTPRedirectHandler):
    def __init__(self, site):
        self.site = site

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed(urljoin(req.full_url, newurl), self.site):
            raise HTTPError(req.full_url, code, "переход за пределы сайта и его CDN", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_body(response, deadline):
    length = response.headers.get("Content-Length")
    if length and int(length) > _MAX_BYTES:
        raise ValueError("Изображение больше 16 МиБ")
    data = bytearray()
    while len(data) <= _MAX_BYTES:
        if time.monotonic() >= deadline:
            raise TimeoutError("Истекло время скачивания изображения")
        block = response.read1(min(65536, _MAX_BYTES + 1 - len(data)))
        if not block:
            return bytes(data)
        data.extend(block)
    raise ValueError("Изображение больше 16 МиБ")


def _jpeg_frames(data, frames):
    """Verify each JPEG scan with a fresh decoder so MPO seeks cannot reuse draft state."""
    from PIL import Image

    if not 1 <= frames <= _MAX_FRAMES:
        raise ValueError("Превышен лимит кадров JPEG")
    decoded_pixels = 0
    for index in range(frames):
        with Image.open(io.BytesIO(data), formats=("JPEG",)) as frame:
            frame.seek(index)
            area = frame.width * frame.height
            if area > _MAX_JPEG_SOURCE_PIXELS:
                raise ValueError("Кадр JPEG превышает лимит исходных пикселей")
            if area > _MAX_PIXELS and frame.info.get("progressive"):
                # The progressive decoder also buffers source-resolution coefficients.
                raise ValueError("Прогрессивный JPEG превышает лимит памяти декодера")
            if frames > 1 or area > _MAX_PIXELS:
                frame.draft(None, (1, 1))  # libjpeg decodes at up to 1/8 along each axis.
            decoded_pixels += frame.width * frame.height
            if decoded_pixels > _MAX_PIXELS:
                raise ValueError("Превышен лимит декодированных пикселей JPEG")
            frame.load()


def _raster(data):
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data), formats=_DECODERS) as image:
            mime = _MIME.get(image.format)
            if mime is None:
                raise ValueError(f"Неизвестный формат изображения: {image.format}")
            width, height = image.size
            if image.format in {"JPEG", "MPO"}:
                frames = getattr(image, "n_frames", 1)
                _jpeg_frames(data, frames)
                return mime, width, height
            if width * height > _MAX_PIXELS:
                raise ValueError("Изображение превышает лимит пикселей")
            image.verify()
        with Image.open(io.BytesIO(data), formats=_DECODERS) as image:
            total_pixels = 0
            for frame in range(_MAX_FRAMES + 1):
                try:
                    image.seek(frame)
                except EOFError:
                    break
                total_pixels += image.width * image.height
                if frame == _MAX_FRAMES or total_pixels > _MAX_PIXELS:
                    raise ValueError("Превышен лимит кадров или пикселей")
                image.load()
    except (Image.DecompressionBombError, SyntaxError, OSError, EOFError) as error:
        raise ValueError(f"Не удалось проверить растровое изображение: {error}") from error
    return mime, width, height


@dataclass
class _Download:
    url: str
    http_status: int | None = None
    error: str | None = None
    data: bytes | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None


def _download(image_url, site, timeout):
    result = _Download(image_url)
    try:
        result.url = urljoin(site, image_url)
        if not _allowed(result.url, site):
            raise ValueError("Адрес за пределами сайта и его CDN")
        request = Request(result.url, headers={"User-Agent": "Hackaton-EduSiteAudit/1.0",
                                               "Accept": "image/*", "Accept-Encoding": "identity"})
        deadline = time.monotonic() + timeout
        with build_opener(_ImageRedirect(site)).open(request, timeout=timeout) as response:
            result.url, result.http_status = response.geturl(), response.status
            body = _read_body(response, deadline)
        result.mime, result.width, result.height = _raster(body)
        result.data = body
    except HTTPError as error:
        with error:
            result.url, result.http_status = error.geturl(), error.code
            result.error = f"HTTP {error.code}: {error.reason}"
    except (OSError, HTTPException, EOFError, ValueError) as error:
        result.error = str(error) or type(error).__name__
    return result


def archive_home_images(conn: sqlite3.Connection, page_id: int, timeout: float = 12) -> int:
    """Download each unique candidate URL for the committed snapshot. Retry previously failed URLs."""
    urls = [r[0] for r in conn.execute("""
        SELECT image_url FROM home_main_photo WHERE page_id=? AND trim(image_url)<>''
        UNION SELECT image_url FROM home_canteen_photo WHERE page_id=? AND trim(image_url)<>''
        UNION SELECT image_url FROM garden_building_photo WHERE page_id=? AND trim(image_url)<>''
    """, (page_id, page_id, page_id))]
    return archive_image_urls(conn, page_id, urls, timeout)


@uses_archive
def archive_image_urls(conn, page_id, urls, timeout, *, download=None):
    """Keep original image bytes and per-snapshot fetch results; each committed URL is idempotent."""
    if conn.in_transaction:
        raise ValueError("Вызывайте скачивание изображений после фиксации снимка и его критериев")
    if timeout <= 0:
        raise ValueError("Таймаут должен быть положительным")
    row = conn.execute("SELECT url FROM page WHERE id=?", (page_id,)).fetchone()
    if row is None:
        raise ValueError(f"Нет снимка с ID {page_id}")
    existing = {r[0] for r in conn.execute(
        "SELECT image_url FROM home_image_download WHERE page_id=? AND status='saved'", (page_id,))}
    retired={r[0] for r in conn.execute("SELECT source FROM retention_item WHERE page_id=? AND kind='image'",(page_id,))}
    download = download or _download
    saved = 0
    for url in dict.fromkeys(urls):
        if url in retired:
            continue
        if url in existing:
            saved += 1
            continue
        result = download(url, row[0], timeout)
        fetched_at = int(time.time())
        digest = hashlib.sha256(result.data).hexdigest() if result.data is not None else None
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            # A concurrent worker may have archived this URL while the request was in flight.
            if conn.execute("""
                SELECT 1 FROM home_image_download WHERE page_id=? AND image_url=? AND status='saved'
            """, (page_id, url)).fetchone():
                saved += 1
                continue
            if digest:
                conn.execute("""
                    INSERT INTO home_image_asset(sha256,mime,width,height,data) VALUES(?,?,?,?,?)
                    ON CONFLICT(sha256) DO UPDATE SET data=excluded.data WHERE length(home_image_asset.data)=0
                """, (digest, result.mime, result.width, result.height, result.data))
            conn.execute("""
                INSERT INTO home_image_download(page_id,image_url,fetched_at,final_url,http_status,status,error,sha256)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(page_id,image_url) DO UPDATE SET
                fetched_at=excluded.fetched_at,final_url=excluded.final_url,http_status=excluded.http_status,
                status=excluded.status,error=excluded.error,sha256=excluded.sha256
            """, (page_id, url, fetched_at, result.url, result.http_status,
                  "saved" if digest else "error", result.error, digest))
        saved += int(digest is not None)
    return saved


def export_home_image(conn: sqlite3.Connection, page_id: int, category: str, path: Path | str) -> Path:
    if category not in _TABLES:
        raise ValueError("Неизвестная категория изображения")
    row = conn.execute(f"""
        SELECT a.mime, a.data FROM {_TABLES[category]} c
        JOIN available_home_image_download d ON d.page_id=c.page_id AND d.image_url=c.image_url
        JOIN home_image_asset a ON a.sha256=d.sha256 WHERE c.page_id=?
    """, (page_id,)).fetchone()
    if row is None:
        raise ValueError("Для указанного снимка и категории файл не сохранён")
    output = Path(path)
    if output.is_dir():
        suffix = "jpg" if row[0] == "image/jpeg" else row[0].split("/")[1]
        output = output / f"page-{page_id}-{category}.{suffix}"
    with output.open("xb") as stream:
        stream.write(row[1])
    return output


def main():
    if __package__:
        from .config import DB_PATH
    else:
        from config import DB_PATH
    parser = argparse.ArgumentParser(description="Экспорт сохранённого фото главной страницы")
    actions = parser.add_subparsers(dest="action", required=True)
    export = actions.add_parser("export", help="записать исходные байты изображения по ID снимка и категории")
    export.add_argument("page", type=int, help="ID снимка")
    export.add_argument("category", choices=_TABLES, help="категория изображения")
    export.add_argument("path", type=Path, help="новый файл или существующая папка")
    args = parser.parse_args()
    try:
        with closing(sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            name = export_home_image(conn, args.page, args.category, args.path)
            status = conn.execute(f"SELECT status FROM {_TABLES[args.category]} WHERE page_id=?", (args.page,)).fetchone()[0]
            print(f"{name} (статус кандидата: {status})")
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
