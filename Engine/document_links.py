"""Read downloadable files, their cards and paging controls from an archived document list."""

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

if __package__:
    from .http_page import same_site
    from .navigation import DOCUMENT_EXTENSIONS, resolve_url
else:
    from http_page import same_site
    from navigation import DOCUMENT_EXTENSIONS, resolve_url


FILE_TYPES = (DOCUMENT_EXTENSIONS - {".sig"}) | {".csv", ".7z", ".rar", ".odg", ".odp"}
SCAN_TYPES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
SIGNATURE_TYPES = {".sig", ".p7s", ".p7b", ".cer", ".sgn"}
CARD = {"gw-document-item", "gw-current-document", "document-object-item", "document-item", "document-card"}
TITLE = ".gw-document-item__title,.document-caption,.document-title,.document-object-text,.card-title,h2,h3,h4"
SERVICE = re.compile(r"breadcrumb|sidebar|side-menu|(?:^|[-_ ])menu(?:$|[-_ ])|tooltip|download-eds|banner|social|share-button", re.I)
SIGNATURE = re.compile(r"(?:скачать\s+)?(?:файл\s+)?(?:электронн\w*\s+)?подпись(?:\s+документа)?\s*$", re.I)
PAGER = re.compile(r"paginat|paginator|(?:^|[\s_-])pager(?:$|[\s_-])|page-link|page-item", re.I)
GENERIC = re.compile(r"(?:(?:скачать|загрузить|открыть|посмотреть|просмотреть|download|open|view)"
                     r"(?:\s+(?:в браузере|файл|документ))?(?:\s*[(\[]?\s*[\d.,]+\s*[ккмгkmg]?[бb][)\]]?)?"
                     r"|файл|документы?|подробнее|pdf|docx?|xlsx?)\s*[»→›]?", re.I)


def _text(node):
    return " ".join(node.stripped_strings).strip() if node else ""


def _title(node):
    if node:
        for value in (_text(node), node.get("title"), node.get("aria-label")):
            value = " ".join((value or "").split())
            if value and not GENERIC.fullmatch(value) and not SIGNATURE.fullmatch(value):
                return value
    return ""


def _excluded(node, *, allow_nav=False):
    for p in (node, *node.parents):
        markers = " ".join((p.get("id", ""), *p.get("class", ())))
        if (p.name in {"aside","header","footer","template","noscript"} or SERVICE.search(markers)
                or (not allow_nav and (p.name=="nav" or p.get("role")=="navigation"))):
            return True
    return False


def _file_type(node, url, known_card=False):
    path = PurePosixPath(unquote(urlsplit(url).path))
    names = [path.name, node.get("download") or ""]
    query = parse_qs(urlsplit(url).query)
    for key in ("filename", "file", "name"):
        names.extend(query.get(key, []))
    suffixes = [PurePosixPath(name).suffix.lower() for name in names]
    if SIGNATURE_TYPES.intersection(suffixes) or SIGNATURE.fullmatch(_text(node)):
        return None
    for suffix in suffixes:
        if suffix in FILE_TYPES or suffix in SCAN_TYPES and (known_card or node.has_attr("download")):
            return suffix[1:]
    mime = node.get("type", "").split(";",1)[0].lower()
    if mime=="application/pdf":
        return "pdf"
    if node.has_attr("download") or (path.stem.casefold() in {"download","скачать"} and
                                     GENERIC.fullmatch(_text(node))):
        return "unknown"
    return None


def _target(node, base):
    raw = (node.get("href") or "").strip()
    return resolve_url(base, raw) if raw and not raw.startswith("#") else None


def _pager(node, current, base):
    if _excluded(node, allow_nav=True):
        return None
    ancestors = (node, *node.parents)
    if any(p.has_attr("disabled") or p.get("aria-disabled")=="true" or p.get("aria-current")=="page"
           or any(c in {"active","disabled"} or c.endswith(("_active","_disabled"))
                  for c in p.get("class", ())) for p in ancestors):
        return None
    href = node.get("href") or node.get("data-href") or node.get("data-url") or ""
    more = "gw-js-show-more-button" in node.get("class", ())
    if more and (node.get("data-params") or "").startswith("?"):
        href = (node.get("data-action-url") or "") + node["data-params"]
    if not href.strip() or href.lstrip().startswith("#"):
        return None
    url = resolve_url(base, href)
    if not url or url == current or not same_site(current, url):
        return None
    parts = urlsplit(url)
    keys = {key.lower() for key in parse_qs(parts.query)}
    if keys & {"sort","order"} or any(key.startswith("filter[") for key in keys):
        return None
    if (more or set(node.get("rel", ())) & {"next","prev"}
            or any(PAGER.search(" ".join(p.get("class", ()))) for p in ancestors)
            or (parts.path == urlsplit(current).path and keys & {"curpos","page","paged","offset"})):
        return url
    return None


def extract_documents(soup, base_url):
    current = resolve_url(base_url, "")
    base_tag = soup.find("base", href=True)
    base = (_target(base_tag, base_url) if base_tag else None) or base_url
    main = soup.select_one("main,[role=main]") or soup.body or soup
    files, pages, details = {}, {}, {}
    heading = main.select_one(".gw-title-with-icon-description__title") or main.find("h1")
    page_title = _title(heading)
    listing = not page_title or bool(re.search(r"^(?:нормативные |основные )?документы(?:\s|$)",page_title,re.I))
    links = main.select("a[href],button[data-action-url],[data-href],[data-url]")
    for node in links:
        pager = _pager(node, current, base)
        if pager:
            pages[pager] = None
            continue
        url = _target(node, base)
        if not url or _excluded(node) or url==current:
            continue
        card = node.find_parent(lambda p: bool(CARD.intersection(p.get("class", ()))))
        container = card or node.find_parent(["article","li","tr","p"])
        title_node = container.select_one(TITLE) if container else None
        detail_url = None
        if title_node:
            anchor = title_node if title_node.name=="a" else title_node.find_parent("a") or title_node.find("a", href=True)
            if anchor:
                target = _target(anchor, base)
                if target and target != current and same_site(current,target) and not _file_type(anchor,target,bool(card)):
                    detail_url = target
                    details[target] = None
        fmt = _file_type(node,url,bool(card))
        if not fmt:
            # Plain lists on older sites occasionally expose just the HTML card.
            parts = urlsplit(url)
            if (listing and not detail_url and container and container.name in {"article","li","tr"}
                    and parts.path.endswith((".html", ".htm")) and same_site(current,url)
                    and parts.path.startswith(urlsplit(current).path.rstrip("/")+"/")
                    and _title(node) and not SIGNATURE.search(_title(node))):
                details[url] = None
            continue
        link_title = _title(node)
        card_title = _title(title_node)
        if link_title:
            title, source = link_title, "link"
        elif card_title:
            title, source = card_title, "card"
        elif container and container.name=="tr" and (cell:=container.find(["th","td"])) and not cell.find("a") and _title(cell):
            title, source = _title(cell), "card"
        elif page_title and not listing and (card or urlsplit(current).path.endswith((".htm",".html"))):
            title, source = page_title, "page_title"
        else:
            name = node.get("download") or PurePosixPath(unquote(urlsplit(url).path)).name
            title, source = name, "filename"
        if not detail_url and not listing and (source=="page_title" or
                (card and (card_title==page_title or "gw-current-document" in card.get("class",())))):
            detail_url = current
        record = {"url":url, "title":title, "title_source":source, "format":fmt, "detail_url":detail_url}
        old = files.get(url)
        if old is None:
            files[url] = record
        else:
            if not old["detail_url"]:
                old["detail_url"] = detail_url
            if old["title_source"]=="filename" and source!="filename":
                old["title"], old["title_source"] = title, source
            if old["format"]=="unknown":
                old["format"] = fmt
    return {"documents":list(files.values()), "pagination_urls":list(pages), "detail_urls":list(details)}
