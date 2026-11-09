import codecs
import gzip
import http.client
import ipaddress
import re
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from http.client import HTTPException
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if TYPE_CHECKING:
    from browser_capture import RenderEvidence


MAX_HTML_BYTES = 4 * 1024 * 1024


def public_connection(host, port, *, tls, timeout):
    """Return a connection pinned to a validated public address with normal TLS host verification."""
    endpoints = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(addr[0]) for _, _, _, _, addr in endpoints]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in addresses):
        raise ValueError("Адрес не является общедоступным")
    last_error = None
    for family, kind, protocol, _, address in endpoints:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(timeout)
            sock.connect(address)
            if tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
            connection.sock = sock
            return connection
        except OSError as error:
            sock.close()
            last_error = error
    raise last_error or OSError("Не удалось подключиться к общедоступному адресу")


@dataclass
class PageResponse:
    final_url: str
    status: int | None
    html: str | None
    error: str | None = None
    loaded_at: int | None = None
    render: "RenderEvidence | None" = None


def same_site(url: str, site_url: str) -> bool:
    target, site = urlsplit(url), urlsplit(site_url)
    return (
        target.scheme in {"http", "https"}
        and target.username is None and target.password is None
        and (target.hostname or "").removeprefix("www.") == (site.hostname or "").removeprefix("www.")
        and (target.port == site.port or target.port in (None, 80, 443) and site.port in (None, 80, 443))
    )


class SiteRedirectHandler(HTTPRedirectHandler):
    def __init__(self, site_url: str):
        self.site_url = site_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not same_site(newurl, self.site_url):
            raise HTTPError(req.full_url, code, "redirect outside organization", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def decode_html(body: bytes, content_type: str) -> str:
    content_hint = re.search(r"""charset\s*=\s*["']?([\w.-]+)""", content_type, re.I)
    meta_hint = re.search(rb"""<meta\b[^>]*?charset\s*=\s*["']?([\w.-]+)""", body[:8192], re.I)
    encodings = ["utf-8-sig"] if body.startswith(codecs.BOM_UTF8) else []
    if content_hint:
        encodings.append(content_hint[1])
    if meta_hint:
        encodings.append(meta_hint[1].decode("ascii"))
    for encoding in [*encodings, "utf-8", "windows-1251"]:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeError):
            continue
    return body.decode("utf-8", errors="replace")


def fetch_page(url: str, site_url: str, timeout: float) -> PageResponse:
    final_url, status = url, None
    try:
        if not same_site(url, site_url):
            return PageResponse(url, None, None, "outside organization")
        request = Request(url, headers={
            "User-Agent": "Hackaton-EduSiteAudit/1.0",
            "Accept": "text/html, application/xhtml+xml",
            "Accept-Encoding": "identity",
        })
        with build_opener(SiteRedirectHandler(site_url)).open(request, timeout=timeout) as response:
            final_url, status = response.geturl(), response.status
            content_type = response.headers.get("Content-Type", "")
            mime = content_type.partition(";")[0].strip().lower()
            if mime not in {"text/html", "application/xhtml+xml"}:
                return PageResponse(final_url, status, None, "not HTML: " + (mime or "unspecified type"))
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                with gzip.GzipFile(fileobj=response) as stream:
                    body = stream.read(MAX_HTML_BYTES + 1)
            else:
                body = response.read(MAX_HTML_BYTES + 1)
            if len(body) > MAX_HTML_BYTES:
                return PageResponse(final_url, status, None, "HTML exceeds 4 MiB")
            if not body.strip():
                return PageResponse(final_url, status, None, "empty response")
            return PageResponse(final_url, status, decode_html(body, content_type), loaded_at=int(time.time()))
    except HTTPError as error:
        with error:
            return PageResponse(error.geturl(), error.code, None, f"HTTP {error.code}: {error.reason}")
    except (OSError, HTTPException, EOFError, ValueError, zlib.error) as error:
        return PageResponse(final_url, status, None, str(error))
