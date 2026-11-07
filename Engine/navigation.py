"""Find audit sections and their evidence links in a single HTML snapshot."""

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePosixPath
import re
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit

if __package__:
    from .http_page import same_site
else:
    from http_page import same_site


# Visible labels and URL segments are matched as whole phrases. A headline
# mentioning the director or a document must not become a section by accident.
SECTION_NAMES = {
    "news": ("Новости", "Все новости", "Новости школы", "Новости детского сада",
             "Новости и события", "Все новости и события", "Новости и объявления", "novosti", "novosti-i-sobytiya", "news"),
    "events": ("Мероприятия", "Все мероприятия", "Календарь мероприятий",
               "План мероприятий", "Афиша", "meropriyatiya", "events"),
    "documents": ("Документы", "Наши документы", "Все документы", "Документы школы",
                  "Нормативные документы", "dokumenty", "documents", "document"),
    "management": ("Структура и органы управления", "Структура и органы управления образовательной организацией",
                   "Администрация", "Руководство", "Руководство образовательной организации", "rukovodstvo",
                   "struktura-i-organy-upravleniya", "struktura-i-organy-upravleniya-obrazovatelnoy-organizatsiey",
                   "struktura-i-organy-upravleniya-obrazovatelnoy-organizatsii",
                   "administratsiya", "struct", "managers"),
    "staff": ("Педагогический состав", "Руководство и педагогический состав", "Руководство. Педагогический состав",
              "Наши педагоги", "Учителя", "Педагоги", "pedagogicheskiy-sostav",
              "rukovodstvo-pedagogicheskiy-sostav", "employees"),
    "team": ("Наш коллектив", "Все сотрудники", "Все педагоги", "Все учителя", "Весь коллектив",
             "Сотрудники учреждения", "Педагогический коллектив", "nash-kollektiv"),
    "contacts": ("Контакты", "Наши контакты", "Контактная информация", "Контакты и режим работы",
                 "Режим работы", "График работы", "Часы приема", "kontakty", "contacts", "rezhim-raboty"),
    "schedule": ("Расписание", "Расписание уроков", "Расписание занятий", "raspisanie", "raspisanie-urokov"),
    "history": ("История", "История школы", "История учреждения", "Наша история", "istoriya-shkoly", "history"),
    "about": ("О школе", "Наша школа", "О детском саде", "Наш детский сад", "Об учреждении",
              "Об организации", "Основные сведения", "o-shkole", "nasha-shkola", "nash-detskiy-sad", "osnovnye-svedeniya", "common"),
    "information": ("Сведения об образовательной организации", "Сведения об ОО",
                    "svedeniya-ob-obrazovatelnoy-organizatsii", "sveden"),
    "programs": ("Образование", "Образовательные программы", "Реализуемые образовательные программы",
                 "obrazovanie", "realizuemye-obrazovatelnye-programmy", "education"),
    "staff_resources": ("Педагогам и сотрудникам", "pedagogam-i-sotrudnikam"),
    "hub": ("Родителям", "Родителям и ученикам", "Родителям и обучающимся", "Школьная жизнь",
            "Официально", "Карта сайта", "Навигатор по сайту", "roditelyam-i-uchenikam", "ofitsialno", "sitemap"),
}
# These are audit targets. Navigation may also pass through supporting sections:
# for example the official information hub and its programs can link documents.
REQUIRED_SECTIONS = {
    "school": ("news", "events", "documents", "schedule", "about", "history", "management", "staff", "contacts"),
    "kindergarten": ("home", "news", "events", "team", "information", "documents", "programs", "staff", "staff_resources", "contacts"),
}
EXTRA_SECTIONS = {"school": {"history", "schedule"}, "kindergarten": {"staff_resources"}}
FEEDS = {"news", "events"}
DETAIL_SECTIONS = {"news", "events", "team", "staff", "management", "documents", "information", "staff_resources", "programs"}
STAFF_DIRECTORIES = {"team", "staff", "management"}
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".zip", ".sig"}
ASSET_EXTENSIONS = {".jpg", ".jpeg", ".webp", ".png", ".gif", ".svg", ".ico", ".mp3", ".mp4", ".css", ".js", ".xml"}


def words(value: str) -> str:
    return " ".join(re.findall(r"[а-яa-z0-9]+", value.casefold().replace("ё", "е")))


SECTION_BY_NAME = {words(alias): section for section, aliases in SECTION_NAMES.items() for alias in aliases}
ANCHOR_SECTIONS = {**SECTION_BY_NAME, "program": "programs", "programs": "programs", "contact": "contacts"}
GARDEN_COMBINED = {words(alias) for alias in (
    "Новости и события", "Все новости и события", "Новости и мероприятия", "Все новости и мероприятия",
    "novosti-i-sobytiya", "novosti-i-meropriyatiya", "news-and-events", "news-events")}
GARDEN_SECTIONS = {**SECTION_BY_NAME, **dict.fromkeys(GARDEN_COMBINED, "news"),
                   **dict.fromkeys(("события", "все события", "события детского сада", "календарь событий",
                                    "sobytiya", "sobytija", "sobytia"), "events")}
GALLERY_SEGMENTS = {"gallery", "photo gallery", "photogallery", "fotogalereya", "fotogalereja", "galereya",
                    "галерея", "фотогалерея", "альбомы", "фотоальбом", "фотоальбомы", "fotoalbom",
                    "fotoalbomy", "albums", "album", "photos", "images", "netcat files"}
IMAGE_EXTENSIONS = {".avif", ".bmp", ".heic", ".heif", ".jpe", ".jfif", ".tif", ".tiff", ".svgz"}


@dataclass(frozen=True)
class Link:
    url: str
    label: str
    section: str
    relation: str
    follow: bool


@dataclass(frozen=True)
class _Context:
    navigation: bool = False
    chrome: bool = False
    content: bool = False
    pager: bool = False
    inactive: bool = False
    person: bool = False
    gallery: bool = False


class AnchorParser(HTMLParser):
    """Collect anchor labels, including image alt text and semantic menu context."""

    VOID = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self):
        super().__init__()
        self.base_href = ""
        self.anchors = []
        self.stack = []
        self.anchor = None
        self.label_parts = []
        self.image_alt = ""

    def finish_anchor(self):
        if self.anchor:
            tag, attrs, context = self.anchor
            label = " ".join("".join(self.label_parts).split())
            label = label or attrs.get("aria-label") or attrs.get("title") or self.image_alt
            self.anchors.append((tag, attrs, label, context))
        self.anchor = None
        self.label_parts = []
        self.image_alt = ""

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        parent = self.stack[-1][1] if self.stack else _Context()
        class_id = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
        navigation = parent.navigation or tag == "nav" or attrs.get("role") == "navigation" or bool(
            re.search(r"(?:^|[\s_-])(menu|nav|navbar|navigation)(?:$|[\s_-])", class_id))
        chrome = parent.chrome or navigation or tag in {"header", "footer", "aside"}
        content = parent.content or (tag == "article" or
                    (attrs.get("itemtype") or "").rsplit("/", 1)[-1] in {"Person", "NewsArticle", "Article", "Event"} or
                    bool(re.search(r"(?:^|[\s_-])(card|news-item|event-item|document-item|person)(?:$|[\s_-])", class_id)))
        pager = parent.pager or bool(re.search(r"paginat|paginator|\bpager\b|\bpage-link\b|\bpage-item\b", class_id))
        inactive = parent.inactive or "disabled" in attrs or attrs.get("aria-disabled") == "true" or (
            attrs.get("aria-current") == "page" or bool(re.search(r"(?:^|[\s_-])(active|disabled)(?:$|[\s_-])", class_id)))
        person = parent.person or (attrs.get("itemtype") or "").endswith("/Person") or bool(
            re.search(r"\b(?:gw-person(?:al)?(?:-info)?-card|gw-employee-info|tpl-component-gw-staff|person-card|staff-card|employee-card)\b", class_id))
        gallery = (parent.gallery or "data-fancybox" in attrs or "data-lightbox" in attrs or
                   bool(re.search(r"(?:^|[\s_-])(?:gallery|lightbox|fancybox)(?:$|[\s_-])",
                                  class_id + " " + (attrs.get("rel") or "").lower())))
        context = _Context(navigation, chrome, content, pager, inactive, person, gallery)
        # The school's public "Показать еще" button advertises an HTML listing
        # fragment and its next offset in the markup. No script execution needed.
        more = (tag == "button" and "gw-js-show-more-button" in class_id and
                attrs.get("data-action-url") and (attrs.get("data-params") or "").startswith("?"))
        if more:
            attrs["href"] = attrs["data-action-url"] + attrs["data-params"]
        if (tag in {"a", "button"} or attrs.get("role") in {"button", "link"} or
                pager and (attrs.get("data-href") or attrs.get("data-url"))):
            self.finish_anchor()
            self.anchor = (tag, attrs, context)
        elif tag == "img" and self.anchor and attrs.get("alt"):
            self.image_alt = attrs["alt"]
        elif tag == "base" and not self.base_href:
            self.base_href = attrs.get("href") or ""
        if tag not in self.VOID:
            self.stack.append((tag, context))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.anchor and tag == self.anchor[0]:
            self.finish_anchor()
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.anchor:
            self.label_parts.append(data)


def resolve_url(base: str, href: str) -> str | None:
    """Keep path/query semantics, discard fragments and produce a requestable HTTP URL."""
    try:
        parts = urlsplit(urljoin(base, href))
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return None
        port = parts.port
        host = parts.hostname.encode("idna").decode()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            host += f":{port}"
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/?%:@!$&'()*+,;=-._~")
        return urlunsplit((parts.scheme, host, path, query, ""))
    except (ValueError, UnicodeError):
        return None


def _href(attrs):
    for key in ("href", "data-href", "data-url"):
        value = (attrs.get(key) or "").strip()
        if value and not value.startswith("#") and not value.lower().startswith("javascript:"):
            return value
    return (attrs.get("href") or "").strip()


def _staff_control(tag, attrs, label, ctx, section):
    if section not in STAFF_DIRECTORIES or ctx.inactive:
        return False
    text = words(label)
    return bool(ctx.pager and (tag == "a" or text.isdigit() or not text) or
                "next" in (attrs.get("rel") or "").lower().split() or
                text in {"далее", "следующая", "следующая страница", "показать еще", "загрузить еще", "next"} or
                "gw-js-show-more-button" in (attrs.get("class") or "").split())


def _pagination_url(url, site, page):
    if not url or url == page.geturl() or not same_site(url, site):
        return False
    target = urlsplit(url)
    keys = {key.casefold() for key, _ in parse_qsl(target.query)}
    segments = set(unquote(target.path).lower().split("/"))
    return not (keys & {"order", "sort"} or any(k.startswith("filter[") for k in keys) or
                (keys == {"cur_cc"} and target.path == page.path) or
                PurePosixPath(target.path.lower()).suffix in ASSET_EXTENSIONS | DOCUMENT_EXTENSIONS or
                segments & {"logout", "login", "search", "redirect", "goto", "click"})


def classify(url, label, attrs, navigation, chrome, content, site, page, kind, parent, anchor_section, *, gallery=False):
    target = urlsplit(url)
    path = unquote(target.path)
    segments = [words(re.sub(r"\.(?:html?|php|aspx?)$", "", part, flags=re.I))
                for part in path.split("/") if part]
    segment = segments[-1] if segments else ""
    names = GARDEN_SECTIONS if kind == "kindergarten" else SECTION_BY_NAME
    ancestor = next((names[s] for s in reversed(segments[:-1]) if s in names), None)
    section = names.get(segment) if anchor_section else (names.get(words(label)) or names.get(segment))
    if section == "history" and "profilnye napravleniya" in segments:
        section = None  # «История» также бывает названием школьного предмета.
    suffix = PurePosixPath(path.lower()).suffix
    if suffix in DOCUMENT_EXTENSIONS or "download" in attrs or segment in {"download", "скачать"}:
        return Link(url, label, parent if parent in DETAIL_SECTIONS else "documents", "document", False)
    if not same_site(url, site):
        return Link(url, label, section or parent, "external", False)
    if suffix in ASSET_EXTENSIONS:
        return None
    if (kind == "kindergarten" and (parent in FEEDS or section in FEEDS or ancestor in FEEDS)
            and (gallery or suffix in IMAGE_EXTENSIONS or GALLERY_SEGMENTS.intersection(segments)
                 or words(label) in GALLERY_SEGMENTS | {"открыть фото", "открыть изображение", "смотреть фото"})):
        return None
    if any(s in {"click", "counter", "goto", "redirect", "logout", "login", "search"} for s in segments):
        return None
    query_keys = {key.lower() for key, _ in parse_qsl(target.query)}
    if any(key.startswith("filter[") or key in {"sort", "order"} for key in query_keys):
        return Link(url, label, section or parent, "navigation", False)
    if parent in DETAIL_SECTIONS and (
        "data-action-url" in attrs or (target.path == page.path and any(
            key in {"page", "paged", "curpos", "offset"} or key.startswith("pagen_") for key in query_keys
        ))
    ):
        return Link(url, label, parent, "pagination", True)
    if query_keys == {"cur_cc"} and target.path == page.path:
        return None  # This is the first page of the same listing again.
    item_query = bool(query_keys & {"id", "element_id", "itemid", "newsid", "objectid"})
    if query_keys and not item_query:
        return Link(url, label, section or parent, "navigation", False)
    if section and not (item_query and section in DETAIL_SECTIONS):
        other_extras = EXTRA_SECTIONS["school" if kind == "kindergarten" else "kindergarten"]
        return Link(url, label, section, "navigation" if section == "hub" else "section",
                    section not in other_extras)
    if anchor_section and not query_keys:
        return Link(url, label, "hub", "navigation", True)
    if navigation:
        return Link(url, label, "unknown", "navigation", False)
    if chrome or not (label or attrs.get("title")):
        return None
    # CMSes often keep article ids in the query rather than the URL path.
    same_branch = target.path.startswith(page.path.rstrip("/") + "/") or target.path == page.path
    if item_query and section in DETAIL_SECTIONS:
        return Link(url, label, section, "detail", True)
    if ancestor in DETAIL_SECTIONS or (parent in DETAIL_SECTIONS and (same_branch or content or item_query)):
        return Link(url, label, ancestor if ancestor in DETAIL_SECTIONS else parent, "detail", True)
    return None


def discover_links(html: str, page_url: str, site_url: str, kind: str, section: str = "home") -> list[Link]:
    parser = AnchorParser()
    parser.feed(html)
    parser.close()
    parser.finish_anchor()
    site = resolve_url(site_url, "") or site_url
    page = urlsplit(resolve_url(page_url, "") or page_url)
    base = resolve_url(page_url, parser.base_href) or page_url
    links = {}
    names = GARDEN_SECTIONS if kind == "kindergarten" else SECTION_BY_NAME

    def remember(link):
        # A linked image and the adjacent text often lead to the same page.
        key = (link.url, link.section, link.relation,
               link.label if link.relation == "pagination_unresolved" else "")
        if key not in links or len(link.label) > len(links[key].label):
            links[key] = link

    for tag, attrs, label, ctx in parser.anchors:
        href = _href(attrs)
        feed_pager = (kind == "kindergarten" and section in FEEDS and not ctx.inactive and
                      (ctx.pager or "next" in (attrs.get("rel") or "").lower().split() or
                       words(label) in {"показать еще", "загрузить еще", "следующая страница"}))
        if _staff_control(tag, attrs, label, ctx, section) or feed_pager:
            url = resolve_url(base, href) if href and not href.startswith("#") else None
            if _pagination_url(url, site, page):
                remember(Link(url, label or attrs.get("rel") or "Пагинация", section, "pagination", True))
            elif (not url or not same_site(url, site) or
                  words(label) in {"далее", "следующая", "следующая страница", "показать еще", "загрузить еще", "next"}):
                remember(Link(page.geturl(), label or "Пагинация", section, "pagination_unresolved", False))
            continue
        if ctx.pager and ctx.inactive:
            continue
        if tag != "a" and not (attrs.get("data-action-url") or SECTION_BY_NAME.get(words(label)) in STAFF_DIRECTORIES):
            continue
        if not href or href == "#":
            continue
        url = resolve_url(base, href)
        if not url:
            continue
        fragment = urlsplit(href).fragment
        anchor_section = (names.get(words(unquote(fragment))) or ANCHOR_SECTIONS.get(words(unquote(fragment)))
                          or names.get(words(label))) if fragment else None
        link = None if url == page.geturl() else classify(
            url, label, attrs, ctx.navigation, ctx.chrome, ctx.content, site, page, kind, section, anchor_section,
            gallery=ctx.gallery
        )
        profile_label = ctx.person and (
            re.fullmatch(r"[А-ЯЁ][а-яё-]+(?:\s+(?:[А-ЯЁ][а-яё-]+|[А-ЯЁ]\.)){1,3}", label) or
            words(label) in {"подробнее", "профиль", "личная страница"} or
            re.search(r"gw-person(?:al)?(?:-info)?-card|profile", attrs.get("class") or "") or
            "/persony/" in urlsplit(url).path)
        if (profile_label and _pagination_url(url, site, page) and not urlsplit(href).fragment
                and (link is None or link.relation == "detail")):
            link = Link(url, label, section if section in STAFF_DIRECTORIES else "staff", "detail", True)
        if link:
            remember(link)
            if kind == "kindergarten" and link.section == "news" and link.relation == "section":
                slug = words(re.sub(r"\.(?:html?|php|aspx?)$", "", unquote(urlsplit(url).path.rstrip("/").split("/")[-1]), flags=re.I))
                if words(label) in GARDEN_COMBINED or slug in GARDEN_COMBINED:
                    remember(Link(link.url, link.label, "events", "section", link.follow))
        if anchor_section and same_site(url, site):
            if url == page.geturl() or (link and link.relation in {"section", "navigation", "detail"}):
                remember(Link(url + "#" + fragment, label, anchor_section, "section", False))
                if kind == "kindergarten" and (words(label) in GARDEN_COMBINED or words(unquote(fragment)) in GARDEN_COMBINED):
                    remember(Link(url + "#" + fragment, label, "events", "section", False))
    return list(links.values())


def with_navigator_links(links: list[Link], navigator: dict) -> list[Link]:
    """Use actual map controls and panel links recovered from the same snapshot."""
    map_urls = {entry["url"] for entry in navigator["entries"] if entry["kind"] == "map_link" and entry["url"]}
    result = {(link.url, link.section, link.relation, link.follow): link for link in links
              if not (link.url in map_urls and link.section == "hub")}
    for entry in navigator["entries"]:
        if entry["kind"] == "map_link" and entry["url"]:
            link = Link(entry["url"], entry["label"], "site_navigator", "navigation", True)
            result.setdefault((link.url, link.section, link.relation, link.follow), link)
    for item in navigator["links"]:
        if item["url"] and item["url"] not in map_urls:
            link = Link(item["url"], item["label"], item["section"], item["relation"], item["follow"])
            result.setdefault((link.url, link.section, link.relation, link.follow), link)
    return list(result.values())


def staff_pagination_controls(html: str, page_url: str, site_url: str, section: str) -> dict[str, list[str]]:
    """Observed URLs and unresolved control labels. No controls alone cannot establish completeness."""
    links = discover_links(html, page_url, site_url, "school", section) if section in STAFF_DIRECTORIES else []
    return {"urls": [link.url for link in links if link.relation == "pagination"],
            "unresolved": [link.label for link in links if link.relation == "pagination_unresolved"]}


class HeadingParser(HTMLParser):
    """Extract headings from page content, skipping menus, link cards and hidden text."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.current = None
        self.parts = []
        self.headings = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        class_id = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
        excluded = (self.stack and self.stack[-1][1]) or (
            tag in {"nav", "header", "footer", "aside", "script", "style", "template"} or
            (tag == "a" and attrs.get("href")) or
            attrs.get("role") in {"navigation", "menu"} or
            "hidden" in attrs or "inert" in attrs or (attrs.get("aria-hidden") or "").lower() == "true" or
            bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs.get("style") or "", re.I)) or
            bool(re.search(r"(?:^|[\s_-])(?:menu|navbar|nav|navigation|card|news-item|event-item|"
                           r"document-item|d-none|visually-hidden|sr-only)(?:$|[\s_-])", class_id))
        )
        if tag not in AnchorParser.VOID:
            self.stack.append((tag, bool(excluded)))
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and not excluded:
            self.current = tag
            self.parts = []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in AnchorParser.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == self.current:
            label = " ".join("".join(self.parts).split())
            if label:
                self.headings.append((int(tag[1]), label))
            self.current = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.current and self.stack and not self.stack[-1][1]:
            self.parts.append(data)


def find_section_headings(html: str, kind: str, page_section: str) -> list[tuple[str, str]]:
    """Return navigational evidence only; a heading says nothing about completeness."""
    parser = HeadingParser()
    parser.feed(html)
    parser.close()
    evidence = {}
    parents = {}
    for level, label in parser.headings:
        name = words(label)
        parents = {depth: text for depth, text in parents.items() if depth < level}
        role = (GARDEN_SECTIONS if kind == "kindergarten" else SECTION_BY_NAME).get(name)
        if role == "history" and name == "история":
            subject_list = any(text in {"предметы", "учебные предметы", "школьные предметы", "профильные направления"}
                               for text in parents.values())
            if kind != "school" or page_section not in {"about", "history"} or subject_list:
                role = None
        if role and role != "hub" and re.search("[а-я]", name):
            evidence[(role, label)] = None
            if kind == "kindergarten" and name in GARDEN_COMBINED:
                evidence[("events", label)] = None
        parents[level] = name
    return list(evidence)
