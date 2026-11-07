"""Поиск разделов сайта по меню главной страницы и карте сайта (/map/)."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .config import DEFAULT_PATHS, SECTION_RULES
from .extract import internal_links
from .fetcher import Fetcher


def discover(fetcher: Fetcher, base: str, home_html: str, kind: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Возвращает ({ключ раздела: url}, [все внутренние ссылки (url, текст)])."""
    links = internal_links(home_html, base)
    sitemap = fetcher.get(base + "/map/")
    if sitemap.ok:
        links += internal_links(sitemap.text, base)

    seen: dict[str, str] = {}
    for u, t in links:
        if u not in seen or (t and not seen[u]):
            seen[u] = t
    all_links = list(seen.items())

    sections: dict[str, str] = {}
    for key, (_, path_rx, text_rx) in SECTION_RULES.items():
        cands = []
        for u, t in all_links:
            path = urlparse(u).path
            if "minprosvescheniya" in path or "sektsiy" in path:
                continue
            if any(re.search(rx, path, re.I) for rx in path_rx) or \
               (t and any(re.search(rx, t.strip(), re.I) for rx in text_rx) and not path.endswith(".html")):
                cands.append(path)
        if cands:
            sections[key] = base + min(cands, key=lambda p: (p.count("/"), len(p)))

    # разделы, которые не нашлись в меню, пробуем по стандартным адресам шаблона
    defaults = {**DEFAULT_PATHS["common"], **DEFAULT_PATHS.get(kind, {})}
    for key, path in defaults.items():
        if key in sections:
            continue
        p = fetcher.get(base + path)
        if p.ok and urlparse(p.final_url or p.url).path.rstrip("/") == path.rstrip("/"):
            sections[key] = base + path
    return sections, all_links


def subpages(all_links: list[tuple[str, str]], section_url: str, limit: int = 20) -> list[tuple[str, str]]:
    """Подразделы раздела (на один уровень глубже, без карточек .html)."""
    root = urlparse(section_url).path.rstrip("/") + "/"
    out = []
    for u, t in all_links:
        p = urlparse(u).path
        if p.startswith(root) and p != root and not p.endswith(".html") and p.count("/") == root.count("/") + 1:
            out.append((u, t))
    seen, uniq = set(), []
    for u, t in out:
        if u not in seen:
            seen.add(u)
            uniq.append((u, t))
    return uniq[:limit]
