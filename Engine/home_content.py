"""Identify school homepage content in a saved document. All evidence is from HTML."""

import re
from collections import Counter
from html import unescape
from itertools import groupby
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from soupsieve import escape

if __package__:
    from .home_terms import WEAK_CANTEEN_TERMS, normalize_term
else:
    from home_terms import WEAK_CANTEEN_TERMS, normalize_term
OTHER_SECTION = re.compile(r"(?:^|[\s_-])(?:news|novosti|event[s]?|meropriyatiya|staff|person|employee|"
                           r"director|navigation|footer|menu|bibliotek\w*|library)(?:$|[\s_-])", re.I)
UTILITY_IMAGE = re.compile(r"госуслуг|gosuslug|госпаблик|решаем вместе|баннер|банер|banner|виджет|"
                           r"меню (?:на|ежедневн|питания)|жалоб\w* на|zhalob|qr[-_ ]?код", re.I)
OTHER_FACILITY = re.compile(r"\b(?:спортивн\w*\s+зал|спортзал\w*|тренажерн\w*\s+зал|библиотек\w*)", re.I)
PLACEHOLDER = re.compile(r"placeholder|no[-_ ]?(?:photo|image)|заглушк", re.I)
SCHOOL = re.compile(r"школ|гимнази|лице[йея]|образовательн\w* учрежден|образовательн\w* организац|м[ба]оу", re.I)
TEACHING = re.compile(r"обуча[юе]|обучение|ученик|учащи|образовательн|педагог", re.I)
NOT_HERO = re.compile(r"новост|мероприят|столов|пищеблок|обеден|буфет|библиотек|спорт(?:ивн\w*\s+)?зал", re.I)
WELCOME = re.compile(r"(?:добро пожаловать|рад\w* приветствовать)", re.I)
STATS = re.compile(r"\b(?:выпускник\w*|средний балл|результаты (?:егэ|огэ|школы))", re.I)
HERO = re.compile(r"(?:^|[\s_-])(?:hero|intro|school-cover|school-description|school-about)(?:$|[\s_-])", re.I)
BOUNDARY = re.compile(r"новост|мероприят|результаты школы|педагогический состав|организация питания|"
                      r"библиотек|профильные направления|независимая оценка", re.I)


def _clean(value):
    return " ".join(unescape(value or "").split())


def _text(node):
    return _clean(node.get_text(" ", strip=True)) if node is not None else ""


def _key(value):
    return normalize_term(_clean(value))


def _markers(node):
    return " ".join([node.get("id", ""), *node.get("class", [])])


def _allowed(node, main):
    for part in (node, *node.parents):
        if part is main:
            break
        if (part.name in {"nav", "header", "footer", "aside", "template", "form"}
                or part.get("role") in {"navigation", "banner", "contentinfo"} or part.has_attr("hidden")
                or part.get("aria-hidden") == "true" or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden",
                                                                  part.get("style", ""), re.I)
                or OTHER_SECTION.search(_markers(part))):
            return False
    return True


def _selector(tag, ids):
    chain = []
    while isinstance(tag, Tag) and tag.name != "[document]":
        value = tag.get("id")
        if value and ids[value] == 1:
            chain.append("#" + escape(value))
            break
        chain.append(f"{tag.name}:nth-of-type({1 + len(tag.find_previous_siblings(tag.name))})")
        tag = tag.parent
    return " > ".join(reversed(chain))


def _component(anchor):
    if anchor is not None and "tpl-anchor" in anchor.get("class", []):
        return anchor.parent
    return anchor


def _url(base, value):
    try:
        parts = urlsplit(urljoin(base, value.strip()))
        if value.strip() and parts.scheme in {"http", "https"} and parts.hostname and parts.username is None:
            return parts.geturl()
    except ValueError:
        pass
    return None


def _terms(terms, category, text):
    normalized = _key(text)
    return [word for word in terms.get(category, ()) if word and normalize_term(word) in normalized]


def _near_text(img, main):
    # Stop before neighboring gallery items and unrelated sections. A CMS may insert
    # multiple empty wrappers between a photo and its actual companion text column.
    for box in img.parents:
        if box is main or box.name in {"body", "html"} or len(box.find_all("img", limit=2)) > 1:
            break
        text = _text(box)
        if text and len(text) <= 5000:
            return text
    return ""


def _introduction(img, main):
    """An unnamed introductory section must place one picture alongside the institution's own introduction."""
    for box in img.parents:
        if box.name in {"html", "body", "[document]"} or len(box.find_all("img", limit=2)) > 1:
            break
        heads = box.find_all(["h1", "h2", "h3"])
        if (NOT_HERO.search(img.get("alt", "")) or any(NOT_HERO.search(_text(c)) for c in box.select("figcaption"))
                or any(BOUNDARY.search(_text(h)) or NOT_HERO.search(_text(h)) for h in heads)):
            break
        heading = next((h for h in heads if SCHOOL.search(_text(h)) or WELCOME.search(_text(h))), None)
        if heading is not None and _allowed(heading, main):
            for p in box.select("p"):
                text = _text(p)
                if (len(text) >= 35 and _allowed(p, main) and
                        (SCHOOL.search(text) or WELCOME.search(text) or SCHOOL.search(_text(heading)) and TEACHING.search(text))
                        and not NOT_HERO.search(text[:90]) and not BOUNDARY.search(text[:90])
                        and not STATS.search(text[:90])):
                    return f"{_text(heading)} · {text}"
        if box is main:
            break
    return ""


def _image_sources(img, page_url):
    """Record HTML options only. Responsive selection depends on the browser's viewport and DPR."""
    sources = []
    for attr in ("data-src", "src", "data-srcset", "srcset"):
        value = img.get(attr, "").strip()
        if not value:
            continue
        if attr.endswith("srcset"):
            # A data URI may contain comma-separated payload; never turn its tail into a relative URL.
            if re.search(r"(?:^|[\s,])(?:data|blob|javascript):", value, re.I):
                continue
            options = []
            for candidate in value.split(","):
                parts = candidate.split()
                if len(parts) == 1 or (len(parts) == 2 and re.fullmatch(r"(?:\d+w|\d*\.?\d+x)", parts[1])):
                    options.append((parts[0], parts[1] if len(parts) == 2 else None))
        else:
            options = [(value.replace(" ", "%20"), None)]
        for raw, descriptor in options:
            if raw.startswith("#") or re.search(r"[\x00-\x20\\]", raw):
                continue
            url = _url(page_url, raw)
            if url:
                sources.append({"url": url, "attribute": attr, "descriptor": descriptor})
    # Some lazy-loaders put a placeholder into src before the browser resolves srcset.
    preferred = next((s for s in sources if not PLACEHOLDER.search(urlsplit(s["url"]).path)), None)
    return preferred or (sources[0] if sources else None), sources


def _in_hero(node, main):
    return any(HERO.search(_markers(part)) for part in (node, *node.parents) if part is not main)


def _photo_result(candidates):
    candidates.sort(key=lambda entry: -entry[0])
    result = {"status": "not_found", "image_url": None, "alt": None,
              "evidence": {"selector": None, "signals": [], "context": "", "alternatives": []}}
    if not candidates:
        return result
    rank, first = candidates[0]
    tied = any(score == rank and item["image_url"] != first["image_url"] for score, item in candidates[1:])
    result["status"] = "ambiguous" if rank == 1 or tied else "found"
    if not tied:
        result.update(image_url=first["image_url"], alt=first["alt"])
    result["evidence"] = {**first, "candidate_count": len(candidates), "alternatives": [entry for _, entry in candidates[1:]]}
    if tied:
        result["evidence"]["signals"] = [*first["signals"], "several_matching_images"]
    return result


def _photos(main, page_url, terms, ids, order, boundary):
    main_anchor = _component(main.select_one("#izobrazheniya"))
    canteen_anchor = _component(main.select_one("#korotko-o-stolovoy-izobrazhenie"))
    candidates = {"main_photo": [], "canteen_photo": []}
    for img in main.select("img"):
        if not _allowed(img, main):
            continue
        preferred, sources = _image_sources(img, page_url)
        if preferred is None:
            continue
        url = preferred["url"]
        alt = _clean(img.get("alt"))
        image_label = f"{alt} {_clean(img.get('title'))} {unquote(urlsplit(url).path.rsplit('/', 1)[-1])}"
        link = img.find_parent("a", href=True)
        if UTILITY_IMAGE.search(image_label) or (link and re.search(
                r"/(?:menu|zhaloby)[./_-]", link["href"], re.I)):
            continue

        def record(category, rank, signals, context=""):
            if PLACEHOLDER.search(image_label):
                rank, signals = 1, [*signals, "placeholder"]
            candidates[category].append((rank, {"image_url": url, "alt": alt, "selector": _selector(img, ids),
                                                "signals": signals, "context": context[:400],
                                                "source_attribute": preferred["attribute"], "html_sources": sources}))

        parents = {id(p) for p in img.parents}
        early = order[id(img)] < boundary
        hero = _in_hero(img, main)
        main_terms = _terms(terms, "main_photo", image_label)
        introduction = ""
        if early and id(main_anchor) in parents:
            record("main_photo", 4, ["upper_image_component", *main_terms], image_label)
        elif early and (hero or main_terms):
            record("main_photo", 3 if hero and main_terms else 2, ["upper_content", *(["hero"] if hero else []),
                                                                  *main_terms], image_label)
        elif early and (introduction := _introduction(img, main)):
            record("main_photo", 3, ["school_heading_and_intro_with_single_image"], introduction)

        local = _near_text(img, main)
        in_canteen = id(canteen_anchor) in parents
        photo_terms = _terms(terms, "canteen_photo", image_label)
        context_terms = _terms(terms, "canteen_photo", local)
        if (not in_canteen and not any(_key(term) not in WEAK_CANTEEN_TERMS for term in [*photo_terms, *context_terms])
                or (id(main_anchor) in parents or hero or introduction) and not photo_terms):
            continue
        # Menu banners and furniture in unrelated stories can also mention meals.
        if not in_canteen and (UTILITY_IMAGE.search(local[:140]) or re.search(r"меню|menu", image_label, re.I)):
            continue
        signals = (["canteen_component"] if in_canteen else []) + list(dict.fromkeys([*photo_terms, *context_terms]))
        conflict = OTHER_FACILITY.search(_key(alt)) or OTHER_FACILITY.search(_key(local[:250]))
        if conflict:
            if in_canteen or photo_terms:
                record("canteen_photo", 1, [*signals, "conflicting_facility", conflict[0]], local)
        else:
            record("canteen_photo", 4 if in_canteen else 3 if photo_terms else 2, signals, local)
    return {category: _photo_result(matches) for category, matches in candidates.items()}


def _paragraphs(node):
    # Older editors leave text loose in an article between its paragraphs.
    # Keep it in source order while joining inline emphasis inside the same paragraph.
    pieces = ((id(s.find_parent(["p", "li", "div", "article", "section", "td", "h2", "h3", "h4"])), str(s))
              for s in node.find_all(string=True)
              if type(s) is NavigableString and s.parent.name not in {"script", "style"} and s.strip())
    return [_clean(" ".join(value for _, value in group)) for _, group in groupby(pieces, key=lambda part: part[0])]


def _description(main, ids, order, boundary):
    anchor = _component(main.select_one("#tekst-o-shkole"))
    items = []
    if anchor is not None and _allowed(anchor, main) and _text(anchor):
        items.append((4, anchor, "school_description_component", _paragraphs(anchor)))
    if not items:
        used_paragraphs = set()
        for node in main.select('article, .object-item-text, [itemprop="description"], .school-description, p'):
            if order[id(node)] >= boundary or not _allowed(node, main):
                continue
            if id(node) in used_paragraphs or any(parent is item[1] for parent in node.parents for item in items):
                continue
            if any(order[id(h)] >= boundary for h in node.select("h2,h3")):
                continue
            lines = _paragraphs(node)
            siblings = []
            if node.name == "p":
                for sibling in node.find_next_siblings():
                    if sibling.name != "p" or order[id(sibling)] >= boundary or not _allowed(sibling, main):
                        break
                    siblings.append(sibling)
                    lines.extend(_paragraphs(sibling))
            text = " ".join(lines)
            if len(text) < 32 or not SCHOOL.search(text) or (STATS.search(text) and len(text) < 100):
                continue
            welcome_only = WELCOME.search(text) and len(lines) == 1 and len(re.split(r"[.!?]\s+\S", text)) == 1
            if not (_in_hero(node, main) or node.get("itemprop") == "description" or node.name in {"article", "p"}
                    or WELCOME.search(text)):
                continue
            items.append((1 if welcome_only else 3, node, "welcome_only" if welcome_only else "upper_description", lines))
            used_paragraphs.update(id(p) for p in siblings)
    result = {"status": "not_found", "text": None,
              "evidence": {"selector": None, "signals": [], "context": "", "paragraphs": [], "length": 0,
                           "alternatives": []}}
    if items:
        items.sort(key=lambda item: -item[0])
        rank, node, reason, lines = items[0]
        text = " ".join(lines)
        result.update(status="ambiguous" if rank == 1 or (len(items) > 1 and items[1][0] == rank) else "found",
                      text=text, evidence={"selector": _selector(node, ids), "signals": [reason], "context": text[:300],
                                           "paragraphs": lines, "length": len(text),
                                           "alternatives": [{"selector": _selector(n, ids), "context": " ".join(p)[:300]}
                                                            for _, n, _, p in items[1:6]]})
    return result


def extract_home_content(html, page_url, terms):
    """Return main_photo, canteen_photo and school_description observations from one DOM parse."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("main, [role=main]") or soup.body or soup
    ids = Counter(node["id"] for node in soup.find_all(id=True))
    order = {id(node): index for index, node in enumerate(main.find_all(True))}
    boundary = next((order[id(h)] for h in main.select("h2,h3")
                     if BOUNDARY.search(_text(h)) and _allowed(h, main)), len(order))
    if soup.base:
        page_url = _url(page_url, soup.base.get("href", "")) or page_url
    return {**_photos(main, page_url, terms, ids, order, boundary),
            "school_description": _description(main, ids, order, boundary)}
