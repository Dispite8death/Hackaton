"""HTTP-загрузчик: повторы, таймауты, общий лимит параллельных запросов, дисковый кэш.

Все сайты Госвеба региона обслуживаются одним сервером, поэтому число одновременных
запросов ограничено глобально (semaphore), чтобы не создавать нагрузку.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import USER_AGENT

log = logging.getLogger(__name__)


@dataclass
class Page:
    url: str
    status: int
    text: str
    final_url: str = ""
    error: str = ""
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error and bool(self.text)


class Fetcher:
    def __init__(self, cache_dir: str | Path = ".cache", ttl_hours: float = 12, timeout: float = 25,
                 max_concurrency: int = 12, use_cache: bool = True, delay: float = 0.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_hours * 3600
        self.timeout = timeout
        self.use_cache = use_cache
        self.delay = delay
        self._sem = threading.BoundedSemaphore(max_concurrency)
        self._local = threading.local()
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0}
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------ session
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            retry = Retry(total=3, connect=3, read=2, backoff_factor=1.5,
                          status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET", "HEAD"))
            s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
            s.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=8))
            s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})
            self._local.session = s
        return s

    def _bump(self, key: str) -> None:
        with self._stats_lock:
            self.stats[key] += 1

    # ------------------------------------------------------------------ cache
    def _cache_path(self, url: str, suffix: str) -> Path:
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self.cache_dir / h[:2] / f"{h}{suffix}"

    def _cache_read(self, url: str) -> Page | None:
        if not self.use_cache:
            return None
        p = self._cache_path(url, ".json.gz")
        if not p.exists() or time.time() - p.stat().st_mtime > self.ttl:
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                d = json.load(f)
            self._bump("cache_hits")
            return Page(**d, from_cache=True)
        except Exception:  # noqa: BLE001 — битый кэш просто игнорируем
            return None

    def _cache_write(self, page: Page) -> None:
        p = self._cache_path(page.url, ".json.gz")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump({"url": page.url, "status": page.status, "text": page.text,
                       "final_url": page.final_url, "error": page.error}, f, ensure_ascii=False)
        tmp.replace(p)

    # ------------------------------------------------------------------ public
    def get(self, url: str) -> Page:
        cached = self._cache_read(url)
        if cached:
            return cached
        with self._sem:
            if self.delay:
                time.sleep(self.delay)
            self._bump("requests")
            try:
                r = self._session().get(url, timeout=self.timeout, allow_redirects=True)
                ctype = r.headers.get("Content-Type", "")
                text = r.text if ("html" in ctype or "text" in ctype or not ctype) else ""
                if "html" in ctype and r.encoding and r.encoding.lower() == "iso-8859-1":
                    r.encoding = "utf-8"
                    text = r.text
                page = Page(url=url, status=r.status_code, text=text, final_url=r.url)
            except requests.RequestException as e:
                self._bump("errors")
                log.warning("GET %s failed: %s", url, e)
                return Page(url=url, status=0, text="", error=f"{type(e).__name__}: {e}"[:300])
        if page.status in (200, 404):
            self._cache_write(page)
        return page

    def image_hash(self, url: str, max_bytes: int = 3_000_000) -> str | None:
        """Перцептивный хэш изображения (dHash 64 бит).

        Одинаковые картинки-заглушки шаблона на разных сайтах пережаты по-разному (md5 различается),
        а dHash у них совпадает. Без Pillow — md5 содержимого.
        """
        p = self._cache_path(url, ".imghash")
        if self.use_cache and p.exists():
            return p.read_text() or None
        with self._sem:
            self._bump("requests")
            try:
                r = self._session().get(url, timeout=self.timeout, stream=True)
                if r.status_code != 200:
                    return None
                buf = bytearray()
                for chunk in r.iter_content(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        break
            except requests.RequestException:
                self._bump("errors")
                return None
        digest = _dhash(bytes(buf)) or "md5:" + hashlib.md5(buf).hexdigest()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(digest)
        return digest


def _dhash(data: bytes) -> str | None:
    try:
        from io import BytesIO

        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(BytesIO(data)).convert("L").resize((9, 8))
    except Exception:  # noqa: BLE001 — битая картинка
        return None
    px = list(im.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"d:{bits:016x}"
