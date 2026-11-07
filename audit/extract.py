"""Извлечение структурированных данных из HTML страниц Госвеба.

Шаблоны школ и детских садов свёрстаны по-разному, поэтому парсеры по возможности
не завязаны на CSS-классы: «карточка» элемента списка определяется как наибольший
родитель ссылки, внутри которого нет ссылок на другие элементы того же списка.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Callable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .config import (EDUCATION_RE, EMAIL_RE, EMPTY_DOCS_MARK, PERSON_NAME_RE, PHONE_RE,
                     PLACEHOLDER_PATTERNS)
from .dates import find_dates, parse_datetime
from .models import Document, EventItem, NewsItem, Person

FILE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|rtf|odt|ods|odp|pptx?|zip|rar|7z|sig)$", re.I)
_DOC_NOISE_PREFIX = re.compile(
    r"^(Скачать|Открыть|Документ подписан|Дата и время|Фамилия, имя|Должность лица|Электронная подпись|"
    r"Закрыть|Подробнее|Документы не выбраны)", re.I)
_DOC_NOISE_FULL = re.compile(
    r"^(\d+([,.]\d+)?(\s*(МБ|Мб|мб|КБ|Кб|кб|Б|MB|KB|байт))?|[0-9A-Fa-f]{16,}|\d{2}\.\d{2}\.\d{4}.*|[|•·]|pdf|docx?)$",
    re.I)


class _Noise:
    @staticmethod
    def match(s: str) -> bool:
        s = s.strip()
        return bool(_DOC_NOISE_PREFIX.match(s) or _DOC_NOISE_FULL.match(s))


DOC_NOISE = _Noise()
_DATE_LINE = re.compile(r"^\d{1,2}(\s+[а-яё]+|\.\d{1,2}\.)\s*\d{4}\s*(г\.?|года)?\s*(,?\s*\d{1,2}:\d{2})?$", re.I)
NOISE_TAGS = ["script", "style", "svg", "noscript", "form", "template", "iframe", "button"]


# ---------------------------------------------------------------- базовые утилиты
def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "lxml")


def main_content(soup: BeautifulSoup) -> Tag:
    """Основной контент страницы без меню, подвала, скриптов и модальных окон."""
    main = soup.select_one("main") or soup.select_one(".gw-content") or soup.body or soup
    main = BeautifulSoup(str(main), "lxml")
    for t in main(NOISE_TAGS):
        t.decompose()
    for sel in ["header", "footer", "nav", ".modal", ".gw-breadcrumb", ".breadcrumb",
                "[class*=breadcrumb]", ".gw-analytics-agreement", "[class*=share]"]:
        for t in main.select(sel):
            t.decompose()
    return main


def text_of(el: Tag | None, sep: str = " ") -> str:
    if el is None:
        return ""
    return re.sub(r"[ \t ]+", " ", el.get_text(sep, strip=True))


def lines_of(el: Tag) -> list[str]:
    return [ln.strip() for ln in el.get_text("\n", strip=True).split("\n") if ln.strip()]


def page_title(soup: BeautifulSoup) -> str:
    h = soup.select_one("main h1") or soup.select_one("h1")
    return text_of(h)


def norm_url(href: str, base: str) -> str:
    u = urljoin(base, href.strip())
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}{p.path}"


def same_host(u: str, base: str) -> bool:
    return urlparse(u).netloc in ("", urlparse(base).netloc)


def card_of(a: Tag, own_url: str, is_item_link: Callable[[str], bool], base: str) -> Tag:
    """Наибольший родитель ссылки, не содержащий ссылок на другие элементы списка."""
    node = a
    while node.parent is not None and node.parent.name not in ("[document]", "html", "body"):
        par = node.parent
        urls = {norm_url(x["href"], base) for x in par.find_all("a", href=True)
                if is_item_link(norm_url(x["href"], base))}
        urls.discard(own_url)
        if urls:
            break
        node = par
    return node


def _title_from_card(card: Tag, a: Tag) -> str:
    for sel in ["[itemprop=headline]", "[class*=title]", "[class*=name]", "h2", "h3", "h4", "h5", ".h5"]:
        el = card.select_one(sel)
        t = text_of(el)
        if t and len(t) > 3 and not parse_datetime(t):
            return t[:200]
    t = (a.get("title") or "").strip() or text_of(a)
    if not t:
        img = card.find("img")
        t = (img.get("alt") or "").strip(" .") if img else ""
    return t[:200]


# ---------------------------------------------------------------- новости
def extract_news(html: str, page_url: str) -> list[NewsItem]:
    soup = soup_of(html)
    main = main_content(soup)
    list_path = urlparse(page_url).path.rstrip("/") + "/"

    def is_item(u: str) -> bool:
        p = urlparse(u)
        return same_host(u, page_url) and p.path.startswith(list_path) and p.path.endswith(".html")

    items: dict[str, NewsItem] = {}
    for a in main.find_all("a", href=True):
        u = norm_url(a["href"], page_url)
        if not is_item(u) or u in items:
            continue
        card = card_of(a, u, is_item, page_url)
        published = None
        meta = card.select_one("[itemprop=datePublished]")
        if meta and meta.get("content"):
            published = parse_datetime(meta["content"])
        if not published:
            date_el = card.select_one("[class*=date], time, [class*=subtitle]")
            published = parse_datetime(text_of(date_el)) if date_el else None
        if not published:
            published = parse_datetime(text_of(card)[:400])
        snippet_el = card.select_one("[itemprop=description], [class*=body], [class*=text], p")
        items[u] = NewsItem(url=u, title=_title_from_card(card, a), published=published,
                            snippet=text_of(snippet_el)[:500])
    return list(items.values())


def extract_article_text(html: str) -> tuple[str, str]:
    """(заголовок, текст) страницы новости/мероприятия."""
    soup = soup_of(html)
    main = main_content(soup)
    # блок «Другие новости» / «Посмотреть всех» не относится к самой статье
    # блоки «Другие новости», а также дата публикации — не часть текста о событии
    for t in main.select("[class*=other], [class*=related], [class*=more], [class*=date], time, "
                         "[itemprop=datePublished], [class*=author], [class*=category], [class*=tag]"):
        t.decompose()
    title = page_title(soup)
    text = text_of(main, "\n")
    if title and text.startswith(title):
        text = text[len(title):].lstrip()
    # отдельные строки, состоящие только из даты, — это служебная дата публикации
    text = "\n".join(ln for ln in text.split("\n") if not _DATE_LINE.match(ln.strip()))
    return title, text


# ---------------------------------------------------------------- мероприятия
def extract_events(html: str, page_url: str, ref: date) -> list[EventItem]:
    soup = soup_of(html)
    main = main_content(soup)
    list_path = urlparse(page_url).path.rstrip("/") + "/"

    def is_item(u: str) -> bool:
        p = urlparse(u)
        return same_host(u, page_url) and p.path.startswith(list_path) and p.path.endswith(".html")

    out: dict[str, EventItem] = {}
    for a in main.find_all("a", href=True):
        u = norm_url(a["href"], page_url)
        if not is_item(u) or u in out:
            continue
        card = card_of(a, u, is_item, page_url)
        date_el = card.select_one("[class*=object-date], [class*=event-date], [class*=date]")
        raw = text_of(date_el) if date_el else ""
        found = [f.value for f in find_dates(raw, ref, prefer="nearest")] if raw else []
        if not found:
            meta = card.select_one("[itemprop=startDate]")
            dt = parse_datetime(meta["content"]) if meta and meta.get("content") else None
            found = [dt.date()] if dt else []
        if not found:
            body = text_of(card)
            found = [f.value for f in find_dates(body[:300], ref, prefer="nearest")]
            raw = raw or body[:80]
        start = found[0] if found else None
        end = None
        if date_el is not None and raw:
            rng = re.search(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})", raw)
            if rng and start:
                try:
                    end = start.replace(day=int(rng[2]))
                except ValueError:
                    end = None
        out[u] = EventItem(url=u, title=_title_from_card(card, a), start=start, end=end, raw_date=raw[:80])
    return list(out.values())


# ---------------------------------------------------------------- документы
def extract_documents(html: str, page_url: str) -> list[Document]:
    soup = soup_of(html)
    main = main_content(soup)
    docs: dict[str, Document] = {}

    def add(title: str, url: str):
        title = re.sub(r"\s+", " ", title).strip(" .|")
        if not title or len(title) < 3 or DOC_NOISE.match(title):
            return
        key = title.lower()
        if key not in docs:
            docs[key] = Document(title=title[:300], url=url, page=page_url)

    for item in main.select(".gw-document-item, [class*=document-item], [class*=documents__item]"):
        t_el = item.select_one("[class*=title]")
        link = item.select_one("a[href]")
        title = text_of(t_el) or (link.get("title", "") if link else "")
        file_a = next((x for x in item.find_all("a", href=True) if FILE_EXT.search(urlparse(x["href"]).path)), None)
        add(title, norm_url((file_a or link)["href"], page_url) if (file_a or link) else page_url)

    def is_file(u: str) -> bool:
        return bool(FILE_EXT.search(urlparse(u).path))

    for a in main.find_all("a", href=True):
        u = norm_url(a["href"], page_url)
        if not is_file(u):
            continue
        title = text_of(a)
        if not title or DOC_NOISE.match(title) or title.lower().startswith("скачать"):
            card = card_of(a, u, is_file, page_url)
            cand = [ln for ln in lines_of(card) if not DOC_NOISE.match(ln) and len(ln) > 3]
            title = cand[0] if cand else ""
        if not title:
            title = urlparse(u).path.rsplit("/", 1)[-1]
        add(title, u)
    return list(docs.values())


def has_empty_docs_block(html: str) -> bool:
    return bool(EMPTY_DOCS_MARK.search(text_of(main_content(soup_of(html)))))


# ---------------------------------------------------------------- персоны
PERSON_CARD_SEL = (".gw-person-info-card, .gw-person-card, [class*=person-card], [class*=person-info-card], "
                   "[class*=teacher-card], [class*=employee-card], [class*=staff-card]")
PHOTO_STUB_URL = re.compile(r"(no[-_]?photo|no[-_]?image|placeholder|default|stub|avatar|zaglush|noavatar|empty)", re.I)


def _name_in(text: str) -> str:
    for ln in re.split(r"[\n|]", text):
        ln = ln.strip()
        if PERSON_NAME_RE.match(ln):
            return ln
    m = re.search(r"[А-ЯЁ][а-яё\-]+\s+[А-ЯЁ][а-яё\-]+\s+[А-ЯЁ][а-яё\-]+(вна|вич|чна|ична|оглы|кызы)\b", text)
    return m[0] if m else ""


def extract_persons(html: str, page_url: str) -> list[Person]:
    soup = soup_of(html)
    main = main_content(soup)
    people: dict[str, Person] = {}
    cards = main.select(PERSON_CARD_SEL)
    # убираем вложенные карточки (оставляем внешние)
    cards = [c for c in cards if not any(p in cards for p in c.parents)]
    for card in cards:
        name_el = card.select_one("[class*=name]")
        name = text_of(name_el) if name_el else ""
        if not PERSON_NAME_RE.match(name):
            name = _name_in(card.get_text("\n", strip=True))
        if not name:
            continue
        pos_el = card.select_one("[class*=position], [class*=job], [class*=post]")
        link = card if card.name == "a" and card.get("href") else card.select_one("a[href]")
        href = link.get("href") if link else None
        url = norm_url(href, page_url) if href and not href.startswith(("tel:", "mailto:", "#")) else None
        img = card.find("img")
        photo = None
        if img is not None:
            src = img.get("src") or img.get("data-src") or ""
            photo = norm_url(src, page_url) if src and not src.startswith("data:") else None
        ctext = text_of(card)
        people.setdefault(name, Person(
            name=name, position=text_of(pos_el)[:200], url=url, photo=photo,
            has_phone=bool(card.select_one("a[href^='tel:']")) or bool(PHONE_RE.search(ctext)),
            has_email=bool(card.select_one("a[href^='mailto:']")) or bool(EMAIL_RE.search(ctext)),
            has_education=bool(EDUCATION_RE.search(ctext)), list_page=page_url))
    return list(people.values())


def person_detail(html: str) -> dict:
    soup = soup_of(html)
    main = main_content(soup)
    for t in main.select("[class*=other], [class*=related]"):
        t.decompose()
    txt = text_of(main, "\n")
    # «Другие учителя …» — хвост с чужими карточками
    txt = re.split(r"\n(Другие|Посмотреть всех)", txt)[0]
    img = main.find("img")
    return {
        "has_education": bool(EDUCATION_RE.search(txt)),
        "has_phone": bool(main.select_one("a[href^='tel:']")) or bool(PHONE_RE.search(txt)),
        "has_email": bool(main.select_one("a[href^='mailto:']")) or bool(EMAIL_RE.search(txt)),
        "photo": (img.get("src") if img is not None else None),
        "text": txt[:3000],
    }


# ---------------------------------------------------------------- заглушки
def find_placeholders(text: str) -> list[tuple[str, str]]:
    hits = []
    for label, rx in PLACEHOLDER_PATTERNS:
        m = rx.search(text)
        if m:
            lo, hi = max(0, m.start() - 40), min(len(text), m.end() + 40)
            hits.append((label, text[lo:hi].replace("\n", " ").strip()))
    return hits


def internal_links(html: str, base: str) -> list[tuple[str, str]]:
    """(url, текст) всех внутренних ссылок страницы, включая меню."""
    soup = soup_of(html)
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        u = norm_url(href, base)
        if not same_host(u, base) or "/netcat_files/" in u:
            continue
        out.append((u, text_of(a)))
    return out
