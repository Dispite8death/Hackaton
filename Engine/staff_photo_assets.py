"""Archive the image actually linked by an employee observation on a particular HTML snapshot."""

import argparse
import ipaddress
import json
import math
import re
import time
from contextlib import closing
from http.client import HTTPException
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

if __package__:
    from .home_image_files import _Download, _allowed, _raster, _read_body, archive_image_urls
    from .http_page import public_connection, same_site
    from .staff_extract import PLACEHOLDER
else:
    from home_image_files import _Download, _allowed, _raster, _read_body, archive_image_urls
    from http_page import public_connection, same_site
    from staff_extract import PLACEHOLDER


_GENERIC_PORTRAIT = re.compile(r"(?:fake|default)[-_ ](?:avatar|portrait|person|photo)|"
                               r"(?:avatar|portrait|person|photo)[-_ ](?:default|placeholder)", re.I)


def photo_exclusion_reason(url, site):
    """Check the stored source and each redirect without contacting a disallowed host."""
    if not url:
        return "absent"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if (parts.scheme not in ("http", "https") or not host or parts.port not in (None, 80, 443)
                or parts.username is not None or parts.password is not None
                or re.search(r"[\\\x00-\x20\x7f]", url) or not _allowed(url, site)):
            return "unsafe_url"
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            return "unsafe_url"
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global or address.is_multicast or address.is_reserved:
                return "unsafe_url"
        except ValueError:
            pass
        path = unquote(parts.path)
        if PLACEHOLDER.search(path) or _GENERIC_PORTRAIT.search(path):
            return "placeholder"
    except ValueError:
        return "unsafe_url"
    return None


def _download_portrait(url, site, timeout):
    result = _Download(url)
    deadline = time.monotonic() + timeout
    try:
        for _ in range(6):
            result.url = url
            reason = photo_exclusion_reason(url, site)
            if reason:
                raise ValueError(reason)
            parts = urlsplit(url)
            host = parts.hostname.encode("idna").decode("ascii")
            tls = parts.scheme == "https"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Истекло время скачивания фотографии")
            path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
            query = quote(parts.query, safe="%&=+:,;/?@-._~")
            with closing(public_connection(host, parts.port or (443 if tls else 80), tls=tls, timeout=remaining)) as conn:
                conn.request("GET", urlunsplit(("", "", path, query, "")), headers={
                    "User-Agent":"Hackaton-EduSiteAudit/1.0", "Accept":"image/*", "Accept-Encoding":"identity",
                })
                with conn.getresponse() as response:
                    result.http_status = response.status
                    if response.status in (301, 302, 303, 307, 308) and response.getheader("Location"):
                        url = urljoin(url, response.getheader("Location"))
                        continue
                    if not 200 <= response.status < 300:
                        raise ValueError(f"HTTP {response.status}")
                    data = _read_body(response, deadline)
            result.mime, result.width, result.height = _raster(data)
            result.data = data
            return result
        raise ValueError("Слишком много перенаправлений")
    except (OSError, HTTPException, EOFError, ValueError) as error:
        result.error = str(error) or type(error).__name__
        return result


def archive_staff_photos(conn, page_id, *, observation_ids=None, timeout=12):
    """Return per-observation evidence, caching original bytes under (page_id, photo_url)."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Таймаут должен быть конечным и положительным")
    page = conn.execute("""SELECT p.url, o.site_url FROM page p JOIN organization o ON o.id=p.id_organization
                           WHERE p.id=?""", (page_id,)).fetchone()
    if not page:
        raise ValueError(f"Нет снимка с ID {page_id}")
    page_url, site = page
    if "://" not in site:
        site = "https://"+site
    allowed_page = same_site(page_url, site)
    observations = conn.execute("""SELECT id, photo_url, photo_state FROM staff_observation
                                    WHERE page_id=? ORDER BY id""", (page_id,)).fetchall()
    if observation_ids is not None:
        ids = set(observation_ids)
        observations = [row for row in observations if row[0] in ids]
        if ids != {row[0] for row in observations}:
            raise ValueError(f"Указанная карточка не принадлежит снимку {page_id}")
    report = []
    for observation, url, state in observations:
        status = state if state != "linked" else photo_exclusion_reason(url, page_url)
        if status is None and not allowed_page:
            status = "unsafe_url"
        report.append({"observation_id":observation, "page_id":page_id, "image_url":url, "status":status})
    urls = {row["image_url"] for row in report if row["status"] is None}
    if urls:
        archive_image_urls(conn, page_id, sorted(urls), timeout, download=_download_portrait)
        results = {url:(status, digest, fetched, error) for url,status,digest,fetched,error in conn.execute("""
            SELECT d.image_url,CASE WHEN t.page_id IS NULL THEN d.status ELSE 'retired' END,
                   d.sha256,d.fetched_at,d.error FROM home_image_download d LEFT JOIN retention_item t
                   ON t.page_id=d.page_id AND t.kind='image' AND t.source=d.image_url WHERE d.page_id=?
        """, (page_id,))}
        for row in report:
            if row["status"] is None:
                status, digest, fetched, error = results[row["image_url"]]
                row.update(status=status, sha256=digest, fetched_at=fetched, error=error)
    return report


def main(argv=None):
    if __package__:
        from .config import connect_db
    else:
        from config import connect_db
    parser = argparse.ArgumentParser(description="Сохранить исходные фото карточек определённого архивного снимка")
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--observation", type=int, nargs="+", metavar="ID", help="Только указанные карточки этого снимка")
    parser.add_argument("--timeout", type=float, default=12)
    args = parser.parse_args(argv)
    with closing(connect_db()) as conn:
        print(json.dumps(archive_staff_photos(conn, args.page, observation_ids=args.observation, timeout=args.timeout),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
