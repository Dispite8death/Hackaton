"""Extract event navigation and independently inspect event content in one saved HTML document."""

import re
from collections import Counter
from datetime import date
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Tag

if __package__:
    from .http_page import same_site
    from .staff_extract import _selector
else:
    from http_page import same_site
    from staff_extract import _selector


LABEL = re.compile(r"(?:все мероприятия|мероприятия(?: школы)?|(?:календарь|план) мероприятий|"
                   r"афиша(?: мероприятий)?|предстоящие мероприятия|прошедшие мероприятия|ближайшие мероприятия)", re.I)
SECTION_SEGMENTS = {"meropriyatiya", "мероприятия", "events", "afisha", "афиша", "kalendar-meropriyatiy"}
NONCONTENT = re.compile(r"(?:^|[\s_-])(?:news|novosti|footer|promo|banner|breadcrumb)(?:$|[\s_-])", re.I)
NAV = re.compile(r"(?:^|[\s_-])(?:nav|menu|navbar|navigation)(?:$|[\s_-])", re.I)
BLOCKS = ".tpl-component-gw-events, .events, #events, .events-list, .events-section, [data-section='events']"
ROW = "a.object-index, .object, .event-card, .event-item, [itemtype$='/Event']"
TITLE = "h1, h2, h3, h4, .name, .event-title, [itemprop='name']"
EMPTY = re.compile(r"(?:мероприятия (?:отсутствуют|не найдены)|нет (?:запланированных )?мероприятий|"
                   r"мероприятий (?:пока )?нет|ничего не найдено)", re.I)
BG = re.compile(r"""background(?:-image)?\s*:[^;]*?url\(\s*(?:'([^']+)'|"([^"]+)"|([^)'"]+))\s*\)""", re.I)
MONTHS = {name: i for i, name in enumerate(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(), 1)}
DATE_WORDS = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:\s*[-–—]\s*(\d{{1,2}}))?\s+({'|'.join(MONTHS)})(?:\s+(\d{{4}})(?!\d))?\b", re.I)
DATE_NUMBERS = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")
ISO_IN_TEXT = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_FIELDS = ".object-index-date-info, .object-date, .event-date, .date, time, [itemprop='startDate'], [itemprop='endDate']"


def _text(node):
    return " ".join(node.stripped_strings) if node else ""


def _url(base, href, *, internal=False):
    if not isinstance(href, str) or not href.strip():
        return None
    try:
        url = urljoin(base, href.strip())
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
            if not internal or same_site(url, base):
                return url
    except ValueError:
        pass
    return None


def _ancestors(tag):
    return (node for node in (tag, *tag.parents) if isinstance(node, Tag))


def _tokens(tag):
    return " ".join([*tag.get("class", []), tag.get("id", "")])


def _context(tag):
    ancestors = list(_ancestors(tag))
    for area in ("footer", "header", "nav"):
        if any(node.name == area for node in ancestors):
            return area
    if any(node.get("role") == "navigation" or NAV.search(_tokens(node))
           for node in ancestors if "tpl-component-gw-navigation-link" not in node.get("class", ())):
        return "nav"
    return "main"


def _content(tag):
    return _context(tag) == "main" and not any(
        node.name in {"aside", "template"} or NONCONTENT.search(_tokens(node)) for node in _ancestors(tag))


def _last_segment(url):
    return unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1].casefold()


def _listing_controls(main, page_url, ids):
    active = []
    for field in main.select(".tpl-template-filter-event [name], form [name]"):
        name = field.get("name", "")
        if not re.search(r"^filter(?:\[|$)|^(?:q|category|topic|tag|year|date|search)$", name, re.I):
            continue
        if field.has_attr("disabled"):
            continue
        if field.name == "select":
            options = field.select("option[selected]") or ([] if field.has_attr("multiple") else field.select("option")[:1])
            values = [o.get("value", _text(o)) for o in options if not o.has_attr("disabled")
                      and not re.match(r"^(?:все(?:\s|$)|любая(?:\s|$)|выберите)", _text(o), re.I)]
        elif field.name == "input" and (field.get("type", "text") not in {"checkbox","radio"} or field.has_attr("checked")):
            values = [field.get("value", "")]
        else:
            values = []
        for value in values:
            if value.strip():
                active.append({"name":name, "value":value, "selector":_selector(field, ids)})
    for name, value in parse_qsl(urlsplit(page_url).query):
        if value.strip() and re.search(r"^filter(?:\[|$)|^(?:q|category|topic|tag|year|date|search)$", name, re.I):
            active.append({"name":name, "value":value, "source":"url"})
    pages = []
    for a in main.select(".pagination a[href], a[rel~='next'], a[rel~='prev'], a.pagination-item[href]"):
        url = _url(page_url, a.get("href"), internal=True)
        if (url and url != page_url and "active" not in a.get("class", ()) and
                a.get("aria-current") != "page" and url not in pages):
            pages.append(url)
    return {"listing_filters_active":bool(active), "active_filters":active, "pagination_urls":pages}


def _printed_dates(text):
    points = []
    for m in DATE_WORDS.finditer(text):
        year = int(m[4]) if m[4] else None
        for day in (m[1], m[2]):
            if day:
                points.append((year, MONTHS[m[3].lower()], int(day)))
    if points:
        return points
    return [(int(m[3]), int(m[2]), int(m[1])) for m in DATE_NUMBERS.finditer(text)]


def _text_dates(text):
    # URLs and linked filenames can contain dates unrelated to the event.
    text = re.sub(r"(?:https?://|www\.)\S+", lambda m: " " * len(m[0]), text, flags=re.I)
    for pattern in (DATE_WORDS, DATE_NUMBERS, ISO_IN_TEXT):
        for m in pattern.finditer(text):
            if pattern is DATE_WORDS:
                year, month = int(m[4]) if m[4] else None, MONTHS[m[3].lower()]
                points = [(year,month,int(day)) for day in (m[1],m[2]) if day]
            elif pattern is DATE_NUMBERS:
                points = [(int(m[3]),int(m[2]),int(m[1]))]
            else:
                points = [(int(m[1]),int(m[2]),int(m[3]))]
            yield points, m[0], text[max(0,m.start()-45):m.end()+50].strip()


def _corroborate_year(row, calendar, ids, evidence):
    """A text date may complete an explicit single calendar day, never replace it."""
    evidence["reason"] = "no_year_in_date_field"
    evidence["calendar_parts"] = [{"day":d,"month":m,"year":y} for y,m,d in calendar]
    text_dates = set()
    invalid = False
    scopes = row.select(".description, .event-description, [itemprop='description'], .full-text")
    scope_ids = {id(el) for el in scopes}
    for el in scopes:
        ancestors = list(el.parents)
        if any(id(p) in scope_ids for p in ancestors):
            continue
        if any(p is not row and (set(p.get("class", ())) & {"object","object-full","event-card","event-item"}
                                  or p.name == "article") for p in ancestors[:ancestors.index(row)]):
            continue
        parts = []
        for string in el.find_all(string=True):
            if isinstance(string, Comment):
                continue
            parents = []
            for parent in string.parents:
                if parent is el:
                    break
                parents.append(parent)
            if any(p.name in {"a","script","style","nav","header","footer","aside","template"} or
                   set(p.get("class", ())) & {"object-date","object-index-date","date","event-date",
                                               "event-card","object","event-item"} for p in parents):
                continue
            parts.append(str(string))
        text = " ".join(" ".join(parts).split())
        source = {"selector":_selector(el, ids), "role":"card_text", "quotes":[]}
        for points, literal, quote in _text_dates(text):
            for y,m,d in points:
                text_dates.add((y,m,d))
                try:
                    date(y if y is not None else 2000,m,d)
                except ValueError:
                    invalid = True
            if len(source["quotes"]) < 24:
                source["quotes"].append({"date_text":literal, "text":quote})
        if source["quotes"]:
            evidence["sources"].append(source)
    evidence["text_dates"] = [{"year":y,"month":m,"day":d} for y,m,d in
                              sorted(text_dates, key=lambda t:(t[0] or 0,t[1],t[2]))]
    printed = {(m,d) for y,m,d in calendar}
    found = {(m,d) for y,m,d in text_dates}
    full = {t for t in text_dates if t[0] is not None}
    if invalid:
        conflict = "invalid_card_text_date"
    elif len(found) > 1 or len(full) > 1:
        conflict = "multiple_distinct_card_text_dates"
    elif found and found.isdisjoint(printed):
        conflict = "card_text_day_month_mismatch"
    else:
        conflict = None
    if conflict:
        evidence.update(reason="possible_date_error", conflict_reason=conflict)
    elif len(printed) == 1 and found == printed and len(full) == 1:
        evidence.update(reason=None, method="calendar_confirmed_by_card_text")
        return date(*full.pop())
    return None


def _event_dates(row, ids):
    candidates = []
    for node in row.select(DATE_FIELDS):
        if node.get("itemprop") in ("datePublished", "dateModified", "dateCreated"):
            continue
        if any(set(p.get("class", ())) & {"description", "event-description", "full-text"}
               for p in _ancestors(node)):
            continue
        candidates.append(node)
    fields = {id(n) for n in candidates}
    top = [n for n in candidates if not any(id(p) in fields for p in n.parents)]
    evidence = {"method":None, "reason":None, "sources":[], "shared_year":False}
    result = {"date_start":None, "date_end":None, "date_text":None, "date_datetime":None,
              "date_evidence":evidence}
    human, machine = [], []
    try:
        for n in top:
            text = re.sub(r"\s+,", ",", _text(n))
            evidence["sources"].append({"selector":_selector(n, ids), "text":text})
            human.extend(_printed_dates(text))
        result["date_text"] = " – ".join(s["text"] for s in evidence["sources"] if s["text"]) or None
        for n in candidates:
            attr = n.get("datetime") or (n.get("content") if n.get("itemprop") in ("startDate","endDate") else None)
            if not attr:
                continue
            m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?", attr)
            if not m:
                continue
            evidence["sources"].append({"selector":_selector(n, ids), "attribute":attr, "role":n.get("itemprop")})
            point = date.fromisoformat(m[1])
            machine.append((n.get("itemprop"), point))
            if result["date_datetime"] is None:
                result["date_datetime"] = attr
        if len(human) > 2 or len(machine) > 2:
            evidence["reason"] = "multiple_dates"
            return result
        if machine:
            if len(machine) == 2 and machine[0][0] == "endDate" and machine[1][0] == "startDate":
                machine.reverse()
            if all(role == "endDate" for role, _ in machine):
                evidence["reason"] = "missing_start"
                return result
            values = [p for _, p in machine]
            for i, (year, month, day) in enumerate(human):
                other = values[min(i, len(values) - 1)]
                if (month, day) != (other.month, other.day) or year is not None and other.year != year:
                    evidence["reason"] = "conflicting_date_fields"
                    return result
            start, end = values[0], values[-1]
            evidence["method"] = "machine_date_field"
        elif human:
            if not any(year for year, _, _ in human):
                confirmed = _corroborate_year(row, human, ids, evidence)
                if confirmed is not None:
                    result.update(date_start=confirmed.isoformat(), date_end=confirmed.isoformat())
                return result
            y1, m1, d1 = human[0]
            y2, m2, d2 = human[-1]
            if y1 is None or y2 is None:
                evidence["shared_year"] = True
                if y1 is None:
                    y1 = y2 - int(m1 > m2)
                else:
                    y2 = y1 + int(m2 < m1)
            start, end = date(y1, m1, d1), date(y2, m2, d2)
            evidence["method"] = "printed_date_field"
        else:
            evidence["reason"] = "unrecognized_date_field" if candidates else "no_date_field"
            return result
        if end < start:
            raise ValueError("reversed interval")
    except (ValueError, OverflowError):
        evidence["reason"] = "invalid_date_or_range"
        return result
    result.update(date_start=start.isoformat(), date_end=end.isoformat())
    return result


def _section_link(anchor, url, page_url):
    parts = urlsplit(url)
    if parts.query or parts.fragment or parts.path.rstrip("/") == urlsplit(page_url).path.rstrip("/"):
        return False
    segment = _last_segment(url)
    label = _text(anchor) or anchor.get("aria-label", "")
    if segment in SECTION_SEGMENTS:
        return True
    return bool(LABEL.fullmatch(label) and not re.search(
        r"(?:^|/)(?:news|novosti|promo|banner)(?:/|$)|\.[a-z0-9]{2,5}$", parts.path, re.I))


def _event_card(row, url, title, page_url, ids):
    description = _text(row.select_one(".description, .event-description, [itemprop='description']"))
    image = row.select_one("img")
    image_url = image_alt = image_kind = None
    if image:
        image_url = _url(page_url, image.get("data-src") or image.get("src"))
        image_alt = image.get("alt")
        image_kind = "img"
    else:
        for el in row.select("[style]"):
            match = BG.search(el["style"])
            if match:
                image_url = _url(page_url, next(v for v in match.groups() if v is not None))
                image_kind = "css_background"
                break
    flags = []
    if re.fullmatch(r"заголовок мероприятия(?:\s*(?:№|#)?\s*\d+)?[.!]?", title, re.I):
        flags.append("template_title")
    if re.fullmatch(r"(?:краткое )?описание мероприятия[.!]?", description, re.I):
        flags.append("template_description")
    if url and re.fullmatch(r"zagolovok-meropriyatiya(?:-\d+)?(?:\.html)?", _last_segment(url)):
        flags.append("template_url_slug")
    if image_url and re.fullmatch(r"(?:img_800h600(?:_\d+)?|800x600-image)\.svg", _last_segment(image_url)):
        flags.append("template_image_filename")
    return {"kind":"event", "url":url, "title":title, **_event_dates(row, ids),
            "description":description or None, "image_url":image_url, "image_alt":image_alt,
            "selector":_selector(row, ids), "evidence":{"heuristics":flags,
                "context":_text(row)[:600], "image_kind":image_kind}}


def _block_cards(block, page_url, ids, detail):
    cards = []
    if detail:
        row = block.select_one(".object-full") or block
        title = _text(row.select_one("h1"))
        return [_event_card(row, page_url, title, page_url, ids)] if title else []
    # A real listing card may have lists and links inside its description.
    # Prefer recognized card roots to those generic descendants.
    candidates = block.select(ROW) or block.select("article") or block.select("li")
    candidate_ids = {id(r) for r in candidates}
    candidates = [r for r in candidates if not any(id(p) in candidate_ids for p in r.parents)]
    if not candidates:
        for a in block.select("a[href]"):
            target = _url(page_url, a["href"], internal=True)
            if target and any(segment in SECTION_SEGMENTS for segment in
                              unquote(urlsplit(target).path).strip("/").lower().split("/")[:-1]):
                candidates.append(a)
    seen = set()
    for row in candidates:
        if not _content(row):
            continue
        heading = row.select_one(TITLE)
        anchor = row if row.name == "a" else (heading.find("a", href=True) if heading else None)
        if anchor is None:
            anchor = row.select_one("a[href]")
        url = _url(page_url, anchor.get("href"), internal=True) if anchor else None
        if anchor and (not url or anchor.get("href","").startswith("#") or
                       _section_link(anchor, url, page_url) or urlsplit(url).path == urlsplit(page_url).path):
            continue
        title = _text(heading) or _text(anchor)
        if not title or LABEL.fullmatch(title):
            continue
        if (url, title) in seen or (not anchor and not heading):
            continue
        seen.add((url, title))
        cards.append(_event_card(row, url, title, page_url, ids))
    return cards


def extract_home_events(html: str, page_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    ids = Counter(node["id"] for node in soup.select("[id]"))
    main = soup.select_one("main, [role='main']") or soup.body or soup
    links = {}
    for a in soup.select("a[href]"):
        url = _url(page_url, a["href"], internal=True)
        if not url or not _section_link(a, url, page_url):
            continue
        location = _context(a)
        if any(NONCONTENT.search(_tokens(t)) for t in _ancestors(a) if "footer" not in _tokens(t).lower()):
            continue
        kind = "section" if location == "main" else "navigation"
        key = kind, url
        selector = _selector(a, ids)
        if key not in links:
            links[key] = {"kind":kind, "url":url, "title":_text(a) or a.get("aria-label", ""),
                          "selector":selector, "evidence":{"locations":[], "selectors":[]}}
        evidence = links[key]["evidence"]
        if location not in evidence["locations"]:
            evidence["locations"].append(location)
        evidence["selectors"].append(selector)

    blocks = [b for b in main.select(BLOCKS) if _content(b) and b.name != "a"
              and "tpl-anchor" not in b.get("class", ())]
    # The template also includes an empty id anchor inside its real component.
    block_ids = {id(b) for b in blocks}
    blocks = [b for b in blocks if not any(id(p) in block_ids for p in b.parents)]
    listing = _last_segment(page_url) in SECTION_SEGMENTS
    headings = [h for h in main.select("h1,h2,h3,h4") if LABEL.fullmatch(_text(h)) and _content(h)]
    if not blocks:
        for h in headings:
            section = h.find_parent("section")
            if section is None and listing:
                section = main
            if section is not None and all(section is not existing for existing in blocks):
                blocks.append(section)

    card_list, block_rows = [], []
    page_kind = "listing" if listing else "home_or_other"
    for block in blocks:
        detail = "tpl-block-full" in block.get("class", ())
        if detail:
            page_kind = "detail"
        cards = _block_cards(block, page_url, ids, detail)
        counts = [e.get("data-object-count") for e in block.select("[data-object-count]")]
        if block.has_attr("data-object-count"):
            counts.append(block["data-object-count"])
        text = _text(block)
        is_empty = ((bool(counts) and all(v == "0" for v in counts)) or bool(EMPTY.search(text)) or
                    (not text and not block.select_one("a, script, template, iframe, [aria-busy='true']") and not counts))
        state = "cards" if cards else "empty" if is_empty else "ambiguous"
        block_rows.append({"selector":_selector(block, ids), "status":state, "declared_counts":counts,
                           "card_count":len(cards), "context":text[:320]})
        card_list.extend(cards)
        for card in cards:
            if card["url"]:
                links.setdefault(("event", card["url"]), card)
    block_status = ("cards" if card_list else "ambiguous" if any(b["status"]=="ambiguous" for b in block_rows)
                    else "empty" if blocks else "ambiguous" if headings else "not_found")
    return {"block_status":block_status, "links":list(links.values()),
            "evidence":{"source":"html", "page_kind":page_kind, "blocks":block_rows, "cards":card_list,
                        "heading_selectors":[_selector(h, ids) for h in headings],
                        **(_listing_controls(main, page_url, ids) if page_kind == "listing" else {})}}
