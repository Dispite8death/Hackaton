import re
import json
import hashlib
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, urljoin, urldefrag
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from . import vision

from .paths import DATA_DIR as ROOT
CACHE_DIR = ROOT / "cache"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

SECTIONS = {
    "school": ["Новости", "Мероприятия", "Документы", "Расписание", "О школе",
               "Контакты", "Педагогам и сотрудникам",
               "Сведения об образовательной организации"],
    "kindergarten": ["Новости", "Наш коллектив", "Педагогический состав",
                     "Сведения об образовательной организации",
                     "Педагогам и сотрудникам", "Контакты"],
}

SUPPRESS_MISSING_SECTIONS = {"новости"}

CRAWL_KEYWORDS = [
    "новост", "мероприят", "событ", "документ", "dokument", "document",
    "контакт", "сведени", "svedeni", "расписан", "сотрудник", "педагог",
    "коллектив", "состав", "полезн", "о школе", "о нас", "рук-во", "руководств",
    "питани", "безопасн", "лицензи", "устав", "образован", "ваканс", "стипенд",
    "международн", "финансов", "структур", "материальн", "стандарт",
    "nash-detskiy-sad", "gruppy", "kollektiv", "pedagogam", "poleznaya",
    "meropriyatiya", "novosti", "kontakty", "raspisanie",
]

PLACEHOLDER_MARKERS = [
    "заглушка", "введите текст", "введите описание", "добавьте описание",
    "добавьте текст", "укажите цель", "укажите цели и задачи", "укажите описание",
    "измените текст", "измените описание", "измените заголовок",
    "демонстрационный контент", "демонстрационный", "текст будет добавлен",
    "информация будет добавлена", "фото будет добавлено",
    "раздел находится в разработке", "раздел в разработке", "lorem ipsum",
]

POS_MARKERS = ["госуслуги", "платформа обратной связи", "пос", "gosuslugi", "opendata"]

# Fallback только если список документов в БД пуст. Имена литеральные, без синонимов.
REQUIRED_DOCS = {
    "school": ["Устав", "Лицензия", "Аккредитация", "Образовательная программа",
               "Прием в школу", "Платные услуги", "Режим занятий", "Питание", "Безопасность"],
    "kindergarten": ["Устав", "Лицензия", "Образовательная программа", "Режим дня",
                     "Питание", "Медицинское обслуживание", "Безопасность",
                     "Платные услуги", "Прием в детский сад"],
}

PUBLISH_LABELS = ["опубликовано", "дата публикации", "размещено", "дата размещения"]
EVENT_LABELS = ["дата проведения", "дата мероприятия", "мероприятие прошло", "дата события"]

PHONE_RE = re.compile(r"(\+7|8)?[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
EMAIL_RE = re.compile(r"[\w\.\-]+@[\w\.\-]+\.\w+")

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "марта": 3, "апрел": 4, "мая": 5, "май": 5,
          "июн": 6, "июл": 7, "августа": 8, "август": 8, "сентябр": 9, "октябр": 10,
          "ноябр": 11, "декабр": 12}

DMY_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
RU_DATE_RE = re.compile(
    r"(\d{1,2})\s+(январ|феврал|март|марта|апрел|ма[яй]|июн|июл|августа|август|сентябр|октябр|ноябр|декабр)"
    r"[а-яё]*\s+(\d{4})", re.IGNORECASE)
ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
LOC_RE = re.compile(r"<loc>(?:<!\[CDATA\[)?\s*([^<\s]+)\s*(?:\]\]>)?</loc>")

ITEMPROP_DATE = ("datepublished", "startdate", "enddate", "datecreated", "datemodified")

DOU_ON_SCHOOL_RE = re.compile(r"(оплачивают\s+ДОУ|ДОУ\s+в\s+размере)", re.IGNORECASE)


# ============================================================
# Кэш
# ============================================================

def get_cache_dir(site_id):
    d = CACHE_DIR / ("site_" + str(site_id))
    d.mkdir(parents=True, exist_ok=True)
    (d / "pages").mkdir(exist_ok=True)
    return d


def md5_bytes(data):
    return hashlib.md5(data).hexdigest()


def load_manifest(cache_dir):
    p = cache_dir / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_manifest(cache_dir, manifest):
    (cache_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def get_page_from_cache(cache_dir, url):
    p = cache_dir / "pages" / (hashlib.md5(url.encode()).hexdigest() + ".html")
    if p.exists():
        return p.read_text(encoding="utf-8")
    return None


def save_page_to_cache(cache_dir, url, html):
    p = cache_dir / "pages" / (hashlib.md5(url.encode()).hexdigest() + ".html")
    p.write_text(html, encoding="utf-8")


def clean_cache(cache_dir, current_urls):
    current_hashes = {hashlib.md5(u.encode()).hexdigest() for u in current_urls}
    pages_dir = cache_dir / "pages"
    if pages_dir.exists():
        for f in pages_dir.glob("*.html"):
            if f.stem not in current_hashes:
                try:
                    f.unlink()
                except Exception:
                    pass


def http_get(url, timeout=15):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_html(html, url, site_id):
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    h1 = soup.find("h1")
    if h1:
        h1t = h1.get_text(" ", strip=True)
        if h1t and h1t not in title:
            title = (title + " | " + h1t) if title else h1t

    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if href.startswith("http"):
            links.append({"href": href, "text": a.get_text(" ", strip=True)[:300]})
    images = []
    for img in soup.find_all("img"):
        src = img.get("src", "") or ""
        if not src or src.startswith("data:"):
            continue
        full = urljoin(url, src)
        low = full.lower()
        if ".svg" in low or any(x in low for x in ("logo", "icon", "sprite", "favicon")):
            continue
        images.append({"src": full, "alt": (img.get("alt", "") or "").strip()[:200]})
    return {"url": url, "html": html, "text": text, "title": title, "links": links,
            "images": images, "_site_id": site_id, "from_cache": False}


# ============================================================
# Хосты и приоритеты
# ============================================================

def host_allowed(url, allowed_hosts):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for allowed in allowed_hosts:
        if not allowed:
            continue
        allowed = allowed.lower().strip()
        if allowed.startswith("."):
            if host.endswith(allowed) or host == allowed[1:]:
                return True
        else:
            if host == allowed or host.endswith("." + allowed):
                return True
    return False


def should_crawl(href, text):
    try:
        path = urlparse(href).path.lower()
    except Exception:
        path = ""
    if path.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".rtf",
                      ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")):
        return False
    low = (href + " " + text).lower()
    return any(k in low for k in CRAWL_KEYWORDS)


PAGE_PRIORITY = ["svedeni", "dokument", "pedagog", "kollektiv", "sostav", "novost",
                 "meropriyat", "raspisan", "kontakty", "o-shkole", "polezn",
                 "nash-detskiy-sad", "gruppy", "pedagogam"]


def priority_score(url):
    low = url.lower()
    for i, kw in enumerate(PAGE_PRIORITY):
        if kw in low:
            return len(PAGE_PRIORITY) - i
    return 0


# ============================================================
# Sitemap
# ============================================================

def fetch_sitemap(sitemap_url, allowed_hosts, depth=0, seen=None):
    if seen is None:
        seen = set()
    if depth > 3 or sitemap_url in seen:
        return []
    seen.add(sitemap_url)
    urls = []
    try:
        xml = http_get(sitemap_url).decode("utf-8", errors="ignore")
    except Exception:
        return []
    for u in LOC_RE.findall(xml):
        if "sitemap" in u.lower() and u != sitemap_url:
            if host_allowed(u, allowed_hosts):
                urls.extend(fetch_sitemap(u, allowed_hosts, depth + 1, seen))
        else:
            urls.append(u)
    return urls


# ============================================================
# HTTP-краулер
# ============================================================

def crawl_http(start_url, allowed_hosts, settings, site_id):
    max_pages = int(settings.get("max_pages", "40"))
    cache_dir = get_cache_dir(site_id)
    manifest = load_manifest(cache_dir)
    pages = []
    skipped_external = []

    sitemap_urls = []
    try:
        sitemap_urls = fetch_sitemap(urljoin(start_url, "/sitemap.xml"), allowed_hosts)
    except Exception:
        pass

    doc_locs, other_locs = [], []
    for loc in sitemap_urls:
        if not host_allowed(loc, allowed_hosts):
            continue
        low = loc.lower()
        if any(k in low for k in ["dokument", "document", "svedeni", "документ", "сведени"]):
            doc_locs.append(loc)
        elif should_crawl(loc, ""):
            other_locs.append(loc)

    other_locs.sort(key=priority_score, reverse=True)
    queue = [start_url] + doc_locs + other_locs
    visited = set()
    new_manifest = {}

    for raw_url in list(queue):
        if len(pages) >= max_pages:
            break
        url, _ = urldefrag(raw_url)
        if url in visited:
            continue
        if not host_allowed(url, allowed_hosts):
            skipped_external.append(url)
            continue
        visited.add(url)

        raw_md5 = None
        try:
            body = http_get(url)
            raw_md5 = md5_bytes(body)
        except Exception:
            pages.append({"url": url, "error": "не удалось скачать", "html": "",
                          "text": "", "title": "", "links": [], "images": [], "_site_id": site_id})
            continue

        cached = manifest.get(url)
        if cached and raw_md5 and cached.get("raw_md5") == raw_md5:
            cached_html = get_page_from_cache(cache_dir, url)
            if cached_html:
                pg = parse_html(cached_html, url, site_id)
                pg["from_cache"] = True
                pages.append(pg)
                new_manifest[url] = cached
                continue

        html = body.decode("utf-8", errors="ignore")
        save_page_to_cache(cache_dir, url, html)
        new_manifest[url] = {"raw_md5": raw_md5, "cached_at": datetime.now().isoformat()}
        pages.append(parse_html(html, url, site_id))

        for link in pages[-1].get("links", []):
            href = link.get("href", "")
            if not href or not host_allowed(href, allowed_hosts):
                continue
            if should_crawl(href, link.get("text", "")) and href not in visited:
                if len(queue) < 800:
                    queue.append(href)

    clean_cache(cache_dir, list(new_manifest.keys()))
    save_manifest(cache_dir, new_manifest)

    try:
        (ROOT / "storage" / str(site_id)).mkdir(parents=True, exist_ok=True)
        (ROOT / "storage" / str(site_id) / "sitemap.txt").write_text(
            "\n".join(sitemap_urls) if sitemap_urls else "SITEMAP NOT FOUND", encoding="utf-8")
        if skipped_external:
            (ROOT / "storage" / str(site_id) / "skipped_external.txt").write_text(
                "\n".join(skipped_external[:500]), encoding="utf-8")
    except Exception:
        pass

    return pages


def load_cached_pages(site_id):
    cache_dir = get_cache_dir(site_id)
    manifest = load_manifest(cache_dir)
    pages = []
    for url in manifest.keys():
        html = get_page_from_cache(cache_dir, url)
        if html:
            pg = parse_html(html, url, site_id)
            pg["from_cache"] = True
            pages.append(pg)
    return pages


# ============================================================
# Utils
# ============================================================

def make_finding(agent, severity, code, message, url="", recommendation=""):
    return {"agent": agent, "severity": severity, "code": code,
            "message": message, "url": url, "recommendation": recommendation}


def valid_pages(pages):
    return [p for p in pages if not p.get("error")]


def month_num(month):
    m = month.lower()
    if m.startswith("ма") and not m.startswith("мар"):
        return 5
    for k, v in MONTHS.items():
        if m.startswith(k):
            return v
    return None


def safe_date(y, m, d):
    try:
        return datetime(y, m, d)
    except Exception:
        return None


def _push_date(out, y, m, d):
    try:
        y, m, d = int(y), int(m), int(d)
    except Exception:
        return
    if y < 1990 or y > 2100 or m < 1 or m > 12 or d < 1 or d > 31:
        return
    dt = safe_date(y, m, d)
    if dt:
        out.append(dt)


def extract_dates(text):
    if not text:
        return []
    out = []
    for d, m, y in DMY_RE.findall(text):
        _push_date(out, y, m, d)
    for d, m, y in RU_DATE_RE.findall(text):
        mon = month_num(m)
        if mon:
            _push_date(out, y, mon, d)
    return sorted(set(out))


def extract_dates_from_page(page):
    out = extract_dates(page.get("text", "") or "")
    html = page.get("html", "") or ""
    if not html:
        return sorted(set(out))
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return sorted(set(out))
    for t in soup.find_all("time"):
        dt = (t.get("datetime") or "").strip()
        m = ISO_RE.match(dt)
        if m:
            _push_date(out, m.group(1), m.group(2), m.group(3))
        else:
            for d in extract_dates(t.get_text(" ", strip=True)):
                out.append(d)
    for el in soup.find_all(attrs={"itemprop": True}):
        ip = (el.get("itemprop") or "").lower()
        if ip not in ITEMPROP_DATE:
            continue
        v = (el.get("content") or el.get("datetime") or el.get_text(strip=True) or "").strip()
        m = ISO_RE.match(v)
        if m:
            _push_date(out, m.group(1), m.group(2), m.group(3))
        else:
            for d in extract_dates(v):
                out.append(d)
    return sorted(set(out))


def find_labeled_date(text, labels):
    if not text or not labels:
        return None
    low = text.lower()
    for label in labels:
        ll = label.lower()
        idx = low.find(ll)
        while idx != -1:
            dates = extract_dates(text[idx:idx + 250])
            if dates:
                return dates[0]
            idx = low.find(ll, idx + len(ll))
    return None


def find_page_url_with(pages, keywords):
    for p in valid_pages(pages):
        low = (p.get("url", "") + " " + p.get("text", "")).lower()
        if any(k in low for k in keywords):
            return p.get("url", "")
    return ""


def find_page_with_date(pages, target):
    for p in valid_pages(pages):
        if target in extract_dates_from_page(p):
            return p.get("url", "")
    return ""


# ============================================================
# Разделы
# ============================================================

def sections_agent(pages, site_type, site_url, audit_rules):
    findings = []
    required = audit_rules.get("sections", {}).get(site_type, SECTIONS.get(site_type, []))
    if not required:
        return findings

    text_parts = []
    link_parts = []
    for p in valid_pages(pages):
        text_parts.append((p.get("text", "") or "").lower())
        try:
            text_parts.append(urlparse(p.get("url", "")).path.lower())
        except Exception:
            pass
        for l in p.get("links", []):
            link_parts.append((l.get("text", "") or "").lower())
            link_parts.append((l.get("href", "") or "").lower())
    big_text = " ".join(text_parts)
    big_links = " ".join(link_parts)

    for section in required:
        sl = section.lower()
        if sl in SUPPRESS_MISSING_SECTIONS:
            continue
        if sl in big_text or sl in big_links:
            continue
        findings.append(make_finding(
            "sections", "CRITICAL", "MISSING_SECTION",
            "Не найден обязательный раздел: " + section,
            site_url, "Добавить раздел и наполнить его."))
    return findings


# ============================================================
# Автодетект типа
# ============================================================

def detect_site_type(pages):
    kg_score = 0
    school_score = 0
    for p in valid_pages(pages):
        try:
            path = urlparse(p.get("url", "")).path.lower()
        except Exception:
            path = ""
        if "nash-detskiy-sad" in path:
            kg_score += 3
        if "gruppy-detskogo-sada" in path or "nash-kollektiv" in path:
            kg_score += 2
        if "nasha-shkola" in path:
            school_score += 3
        if "glavnoe/raspisanie" in path or "roditelyam-i-uchenikam" in path:
            school_score += 2
        if "glavnoe/meropriyatiya" in path:
            school_score += 1
    if kg_score > school_score:
        return "kindergarten", kg_score, school_score
    if school_score > kg_score:
        return "school", kg_score, school_score
    return "", kg_score, school_score


# ============================================================
# Документы: синонимы ТОЛЬКО те, что ты вписал в строку через | или ;
# ============================================================

def _doc_candidates(entry):
    return [p.strip().lower() for p in re.split(r"[|;]", entry or "") if p.strip()]


def docs_agent(pages, site_type, site_url, audit_rules):
    findings = []
    required = audit_rules.get("documents", {}).get(site_type, REQUIRED_DOCS.get(site_type, []))
    vp = valid_pages(pages)
    doc_pages = []
    for p in vp:
        ctx = (p.get("url", "") + " " + " ".join(l.get("text", "") for l in p.get("links", []))).lower()
        if "документ" in ctx or "dokument" in ctx or "document" in ctx or "svedeni" in p.get("url", "").lower():
            doc_pages.append(p)
    primary_parts, secondary_parts = [], []
    for p in vp:
        link_texts = " ".join(l.get("text", "") for l in p.get("links", []))
        link_hrefs = " ".join(l.get("href", "") for l in p.get("links", []))
        secondary_parts.append(p.get("text", "") + " " + link_texts)
        if p in doc_pages:
            primary_parts.append(p.get("text", "") + " " + link_texts + " " + link_hrefs)
        for l in p.get("links", []):
            href = l.get("href", "").lower()
            if href.endswith((".pdf", ".doc", ".docx", ".rtf")) or "/document" in href or "sveden" in href:
                primary_parts.append(l.get("text", "") + " " + href)
    primary = " ".join(primary_parts).lower()
    secondary = " ".join(secondary_parts).lower()
    if not doc_pages and not primary.strip():
        return [make_finding("docs", "CRITICAL", "NO_DOCUMENTS",
                             "Не найден раздел документов и файлы документов.", site_url,
                             "Добавить раздел документов и обязательные файлы.")]
    for doc_name in required:
        cands = _doc_candidates(doc_name)
        if not cands:
            continue
        page_with_doc = find_page_url_with(pages, cands)
        if any(c in primary for c in cands):
            continue
        if any(c in secondary for c in cands):
            findings.append(make_finding("docs", "LOW", "DOC_OUTSIDE_SECTION",
                                         "Документ «" + doc_name + "» найден вне раздела «Документы».",
                                         page_with_doc or site_url,
                                         "Разместить документ «" + doc_name + "» в разделе «Документы»."))
            continue
        tried = " | ".join(cands)
        findings.append(make_finding("docs", "HIGH", "MISSING_DOC",
                                     "Не найден обязательный документ: " + doc_name +
                                     " (искали варианты: " + tried + ")",
                                     site_url,
                                     "Добавить документ или дописать синоним через | в строке правила."))
    return findings


# ============================================================
# Прочие агенты
# ============================================================

def contacts_agent(pages, site_url):
    findings = []
    text = " ".join(p.get("text", "") for p in valid_pages(pages)).lower()
    if not PHONE_RE.search(text):
        findings.append(make_finding("contacts", "HIGH", "NO_PHONE",
                                     "Не найден контактный телефон.", site_url,
                                     "Добавить актуальный телефон учреждения."))
    if not EMAIL_RE.search(text):
        findings.append(make_finding("contacts", "HIGH", "NO_EMAIL",
                                     "Не найден email.", site_url, "Добавить актуальный email."))
    if not any(x in text for x in ["адрес", "ул.", "улица", "дом", "корпус"]):
        findings.append(make_finding("contacts", "MEDIUM", "NO_ADDRESS",
                                     "Не найден адрес учреждения.", site_url, "Добавить адрес."))
    if not any(x in text for x in ["режим работы", "часы приема", "часы работы"]):
        findings.append(make_finding("contacts", "MEDIUM", "NO_WORK_HOURS",
                                     "Не найден режим работы или часы приема.", site_url,
                                     "Добавить режим работы и часы приема."))
    return findings


def schedule_agent(pages, site_type, site_url):
    if site_type != "school":
        return []
    text = " ".join(p.get("text", "") for p in valid_pages(pages)).lower()
    if "расписан" not in text:
        return [make_finding("schedule", "HIGH", "NO_SCHEDULE",
                             "Не найден раздел или данные расписания.", site_url,
                             "Заполнить раздел «Расписание».")]
    return []


def staff_agent(pages, site_type, site_url):
    findings = []
    staff_pages = []
    for p in valid_pages(pages):
        low_url = p.get("url", "").lower()
        text_low = p.get("text", "").lower()
        if any(x in low_url or x in text_low for x in ["педагог", "сотрудник", "коллектив", "состав"]):
            staff_pages.append(p)
    if not staff_pages:
        return [make_finding("staff", "HIGH", "NO_STAFF_PAGE",
                             "Не найден раздел с педагогическим составом / коллективом.",
                             site_url, "Добавить раздел с сотрудниками.")]
    first_staff_url = staff_pages[0].get("url", site_url)
    corpus = " ".join(p.get("text", "") for p in staff_pages)
    corpus_low = corpus.lower()
    if "образован" not in corpus_low:
        findings.append(make_finding("staff", "MEDIUM", "NO_EDUCATION_INFO",
                                     "Не найдены сведения об образовании сотрудников.",
                                     first_staff_url, "Добавить уровень образования по каждому сотруднику."))
    if not (PHONE_RE.search(corpus_low) or EMAIL_RE.search(corpus_low)):
        findings.append(make_finding("staff", "MEDIUM", "NO_STAFF_CONTACTS",
                                     "Не найдены контактные данные сотрудников.",
                                     first_staff_url, "Добавить контактные данные сотрудников."))
    return findings


def news_events_agent(pages, settings):
    findings = []
    now = datetime.now()
    max_age = int(settings.get("news_max_age_days", "14") or "14")
    all_dates = []
    for p in valid_pages(pages):
        all_dates.extend(extract_dates_from_page(p))
    all_dates = [d for d in all_dates if 2000 < d.year <= now.year + 2]
    past = sorted([d for d in all_dates if d <= now], reverse=True)
    future = [d for d in all_dates if now < d <= now + timedelta(days=183)]
    latest_news = past[0] if past else None
    if not all_dates:
        findings.append(make_finding("news", "HIGH", "NO_DATES",
                                     "Не найдены даты новостей или мероприятий.", "",
                                     "Добавить новости и мероприятия с датами."))
    else:
        if latest_news:
            age = (now - latest_news).days
            if age > max_age:
                page_url = find_page_with_date(pages, latest_news)
                findings.append(make_finding("news", "HIGH", "OLD_NEWS",
                                             "Последняя новость старше " + str(max_age) + " дней: " +
                                             latest_news.date().isoformat() + " (" + str(age) + " дн.)",
                                             page_url, "Добавить актуальные новости."))
        if len(past) >= 2:
            gaps = []
            for i in range(min(len(past) - 1, 12)):
                gap = (past[i] - past[i + 1]).days
                if 0 <= gap <= 120:
                    gaps.append(gap)
            if gaps and max(gaps) > max_age:
                findings.append(make_finding("news", "MEDIUM", "NEWS_INTERVAL_GAP",
                                             "Интервал между публикациями больше " + str(max_age) +
                                             " дней: " + str(max(gaps)) + " дн.",
                                             "", "Соблюдать интервал между новостями."))
        if not future:
            findings.append(make_finding("events", "HIGH", "NO_UPCOMING_EVENTS",
                                         "Не найдены мероприятия на текущий и предстоящий период.",
                                         "", "Добавить мероприятия на текущий и следующий квартал."))
    violations = 0
    for p in valid_pages(pages):
        text = p.get("text", "")
        pub_date = find_labeled_date(text, PUBLISH_LABELS)
        event_date = find_labeled_date(text, EVENT_LABELS)
        if pub_date and event_date and pub_date - event_date > timedelta(hours=24):
            violations += 1
            if violations <= 5:
                findings.append(make_finding("news", "HIGH", "PUBLISH_DELAY",
                                             "Публикация позже 24 часов после мероприятия: событие " +
                                             event_date.date().isoformat() + ", публикация " +
                                             pub_date.date().isoformat(),
                                             p.get("url", ""),
                                             "Публиковать новость не позже 24 часов после события."))
    latest_iso = latest_news.date().isoformat() if latest_news else None
    return findings, latest_iso, len(future)


def events_news_agent(pages, site_url):
    findings = []
    now = datetime.now()
    events_pages, news_pages = [], []
    for p in valid_pages(pages):
        u = p.get("url", "").lower()
        if any(k in u for k in ["meropriy", "sobyti", "event", "мероприят"]):
            events_pages.append(p)
        elif any(k in u for k in ["novost", "news", "новост"]):
            news_pages.append(p)
    if not events_pages or not news_pages:
        return findings
    event_items = []
    for p in events_pages:
        for d in extract_dates_from_page(p):
            if now - timedelta(days=45) <= d <= now - timedelta(days=2):
                event_items.append((d, p.get("url", site_url)))
    if not event_items:
        return findings
    news_dates = []
    for p in news_pages:
        news_dates.extend(extract_dates_from_page(p))
    seen = set()
    for d, page_url in sorted(event_items):
        if d in seen:
            continue
        seen.add(d)
        if not any(abs((nd - d).days) <= 10 for nd in news_dates):
            findings.append(make_finding("events_news", "MEDIUM", "EVENT_NOT_IN_NEWS",
                                         "Мероприятие от " + d.date().isoformat() +
                                         " проведено, но не освещено в разделе «Новости».",
                                         page_url, "Опубликовать новость об итогах мероприятия."))
        if len(findings) >= 5:
            break
    return findings


def placeholder_agent(pages, site_url, audit_rules):
    markers = audit_rules.get("placeholders", PLACEHOLDER_MARKERS)
    found_map = {}
    for p in valid_pages(pages):
        low = (p.get("text", "") or "").lower()
        for marker in markers:
            if marker in low:
                found_map.setdefault(marker, []).append(p.get("url", ""))
    findings = []
    for marker, urls in found_map.items():
        findings.append(make_finding("content", "HIGH", "PLACEHOLDER_TEXT",
                                     "Найдена заглушка «" + marker + "» (" + str(len(urls)) + " стр.): " + urls[0],
                                     urls[0], "Удалить служебный текст и заменить реальным контентом."))
    return findings


def pos_agent(pages, site_url):
    text_corpus = " ".join(p.get("text", "") for p in valid_pages(pages)).lower()
    html_corpus = " ".join((p.get("html", "") or "")[:200000] for p in valid_pages(pages)).lower()
    for marker in POS_MARKERS:
        ml = marker.lower()
        if ml == "пос":
            pattern = r"(?<![а-яёa-z0-9])пос(?![а-яёa-z0-9])"
            if re.search(pattern, text_corpus) or re.search(pattern, html_corpus):
                return []
        else:
            if ml in text_corpus or ml in html_corpus:
                return []
    return [make_finding("pos", "MEDIUM", "NO_POS_BANNER",
                         "Не найден баннер ПОС / Госуслуги.", site_url, "Добавить баннер ПОС.")]


def typo_agent(pages, site_type, site_url):
    findings = []
    if site_type != "school":
        return findings
    dou_pages = []
    for p in valid_pages(pages):
        text = p.get("text", "") or ""
        if DOU_ON_SCHOOL_RE.search(text):
            dou_pages.append(p.get("url", site_url))
    if dou_pages:
        uniq = sorted(set(dou_pages))
        findings.append(make_finding(
            "typo", "MEDIUM", "DOU_ON_SCHOOL",
            "На школьном сайте в условиях оплаты упомянута аббревиатура «ДОУ» "
            "(детское дошкольное учреждение) — остаток шаблона детсада. Страниц: " +
            str(len(uniq)) + ". Пример: " + uniq[0],
            uniq[0],
            "Убрать «ДОУ»: оплачивается услуга, а не учреждение."))
    return findings


# ============================================================
# Score
# ============================================================

def score_findings(findings):
    weights = {"CRITICAL": 20, "HIGH": 10, "MEDIUM": 4, "LOW": 1, "INFO": 0}
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0}
    score = 100
    for f in findings:
        sev = f.get("severity", "LOW")
        counts[sev] = counts.get(sev, 0) + 1
        score -= weights.get(sev, 1)
    return max(score, 0), counts


# ============================================================
# Главный аудит
# ============================================================

async def audit_site(site, settings, offline=False):
    url = site["url"]
    declared_type = site.get("type", "school")
    site_id = site.get("id", "site")

    try:
        host = urlparse(url).hostname.lower()
    except Exception:
        host = ""
    allowed_hosts = {host, ".gosweb.gosuslugi.ru"}
    extra = settings.get("extra_hosts", "") or ""
    for item in extra.replace("\n", ",").split(","):
        item = item.strip()
        if item:
            allowed_hosts.add(item.lower())

    from . import db
    audit_rules = db.get_audit_rules()
    photo_targets = db.get_photo_targets(active_only=True)

    if offline:
        pages = load_cached_pages(site_id)
        if not pages:
            findings = [make_finding("system", "CRITICAL", "NO_CACHE",
                                     "Кэш пуст. Сначала запустите полный аудит.",
                                     url, "Нажмите «Полный аудит».")]
            score, counts = score_findings(findings)
            return {"score": score, "counts": counts, "findings": findings,
                    "metrics": {"pages": 0, "latest_news": None, "future_events": 0, "offline": True},
                    "llm_summary": ""}
    else:
        try:
            pages = await asyncio.to_thread(crawl_http, url, allowed_hosts, settings, site_id)
        except Exception as e:
            findings = [make_finding("system", "CRITICAL", "CRAWL_FAILED",
                                     "Не удалось открыть сайт: " + str(e), url,
                                     "Проверить доступность сайта и настройки сети.")]
            score, counts = score_findings(findings)
            return {"score": score, "counts": counts, "findings": findings,
                    "metrics": {"pages": 0, "latest_news": None, "future_events": 0},
                    "llm_summary": ""}
        try:
            await asyncio.to_thread(vision.build_photo_cache, site_id, pages)
        except Exception as e:
            print("[PHOTO CACHE] " + str(e))

    detected, kg_s, sch_s = detect_site_type(pages)
    effective_type = declared_type
    findings = []
    if detected and detected != declared_type:
        if settings.get("auto_detect_type") == "1":
            effective_type = detected
        findings.append(make_finding(
            "system", "MEDIUM", "TYPE_MISMATCH",
            "Объявлен тип «" + declared_type + "», но по структуре сайта определён «" + detected +
            "» (баллы: ДОУ " + str(kg_s) + ", школа " + str(sch_s) + "). Правила применяются по типу «" +
            effective_type + "».",
            url, "Исправить тип в карточке сайта или включить автоопределение в Настройках."))

    findings.extend(sections_agent(pages, effective_type, url, audit_rules))
    findings.extend(contacts_agent(pages, url))
    findings.extend(schedule_agent(pages, effective_type, url))
    findings.extend(staff_agent(pages, effective_type, url))

    news_findings, latest_news, future_events = news_events_agent(pages, settings)
    findings.extend(news_findings)
    findings.extend(events_news_agent(pages, url))
    findings.extend(docs_agent(pages, effective_type, url, audit_rules))
    findings.extend(placeholder_agent(pages, url, audit_rules))
    findings.extend(pos_agent(pages, url))
    findings.extend(typo_agent(pages, effective_type, url))

    try:
        findings.extend(vision.photo_agent(pages, effective_type, url, site_id, photo_targets))
    except Exception as e:
        print("[PHOTO AGENT] " + str(e))

    score, counts = score_findings(findings)
    metrics = {"pages": len(valid_pages(pages)), "latest_news": latest_news,
               "future_events": future_events, "offline": offline,
               "declared_type": declared_type, "detected_type": detected,
               "effective_type": effective_type}
    return {"score": score, "counts": counts, "findings": findings,
            "metrics": metrics, "llm_summary": ""}
