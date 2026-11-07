"""Определение даты события в новости и факт-чекинг по открытым источникам.

Офлайн-проверки (без ИИ) опираются на общедоступные официальные сведения:
  * даты государственных праздников и памятных дат (ФЗ от 13.03.1995 № 32-ФЗ, Трудовой кодекс РФ,
    указы Президента РФ) и региональную дату — День образования Магаданской области (3 декабря 1953 г.);
  * годы исторических событий и рождения известных персоналий — для проверки «N-летия»;
  * производственный календарь (день недели у даты).
Дополнительно (опционально) новости проверяет ИИ-агент с веб-поиском — см. llm.py.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from .config import FUTURE_MARKERS, PAST_EVENT_VERBS
from .dates import _MONTH_WORD, _WEEKDAY_WORD, find_dates, infer_year, month_num, weekday_num


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_sunday(year: int, month: int) -> date:
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 6) % 7)


# (название, regex упоминания, функция год -> официальная дата)
OFFICIAL_DATES: list[tuple[str, str, callable]] = [
    ("День знаний", r"Д(ень|ня|ню|нём|нем)\s+знаний", lambda y: date(y, 9, 1)),
    ("День учителя", r"Д(ень|ня|ню|нём|нем)\s+учителя", lambda y: date(y, 10, 5)),
    ("День народного единства", r"народного\s+единства", lambda y: date(y, 11, 4)),
    ("День Победы", r"\bД(ень|ня|ню|нём|нем)\s+(Великой\s+)?Победы\b", lambda y: date(y, 5, 9)),
    ("День защитника Отечества", r"защитника?\s+Отечества", lambda y: date(y, 2, 23)),
    ("Международный женский день", r"Международн\w+\s+женск\w+\s+д", lambda y: date(y, 3, 8)),
    ("День космонавтики", r"космонавтики", lambda y: date(y, 4, 12)),
    ("День защиты детей", r"защиты\s+детей", lambda y: date(y, 6, 1)),
    ("День России", r"\bД(ень|ня|ню|нём|нем)\s+России\b", lambda y: date(y, 6, 12)),
    ("День Конституции РФ", r"\bД(ень|ня|ню|нём|нем)\s+Конституции", lambda y: date(y, 12, 12)),
    ("День Героев Отечества", r"Геро\w+\s+Отечества", lambda y: date(y, 12, 9)),
    ("День неизвестного солдата", r"неизвестного\s+солдата", lambda y: date(y, 12, 3)),
    ("Международный день родного языка", r"родного\s+языка", lambda y: date(y, 2, 21)),
    ("День снятия блокады Ленинграда", r"(снятия|освобождения)\s+(Ленинграда\s+от\s+)?(фашистской\s+)?блокады|освобождения\s+Ленинграда", lambda y: date(y, 1, 27)),
    ("День российской науки", r"российской\s+науки", lambda y: date(y, 2, 8)),
    ("День славянской письменности и культуры", r"славянской\s+письменности", lambda y: date(y, 5, 24)),
    ("День воспитателя и дошкольных работников", r"Д(ень|ня|ню)\s+воспитателя|дошкольного\s+работника", lambda y: date(y, 9, 27)),
    ("Международный день пожилых людей", r"пожилых\s+людей|пожилого\s+человека", lambda y: date(y, 10, 1)),
    ("День Государственного флага РФ", r"Государственного\s+флага", lambda y: date(y, 8, 22)),
    ("День семьи, любви и верности", r"семьи,\s+любви\s+и\s+верности", lambda y: date(y, 7, 8)),
    ("День солидарности в борьбе с терроризмом", r"солидарности\s+в\s+борьбе\s+с\s+терроризмом", lambda y: date(y, 9, 3)),
    ("Международный день инвалидов", r"(день|дня)\s+инвалидов|людей\s+с\s+инвалидностью", lambda y: date(y, 12, 3)),
    ("День воссоединения Крыма с Россией", r"воссоединения\s+Крыма", lambda y: date(y, 3, 18)),
    ("День матери", r"\bД(ень|ня|ню|нём|нем)\s+матери\b", lambda y: _last_sunday(y, 11)),
    ("День отца", r"\bД(ень|ня|ню|нём|нем)\s+отца\b", lambda y: _nth_sunday(y, 10, 3)),
    ("День образования Магаданской области", r"(образования|рождения)\s+Магаданской\s+области|День\s+Магаданской\s+области", lambda y: date(y, 12, 3)),
]

# «N-летие …»: (regex объекта, базовый год, описание)
ANNIVERSARY_BASES: list[tuple[str, int, str]] = [
    (r"начала\s+Великой\s+Отечественной", 1941, "начало Великой Отечественной войны — 1941"),
    (r"(Великой\s+)?Победы(\s+в\s+Великой\s+Отечественной)?", 1945, "Победа в ВОВ — 1945"),
    (r"(первого\s+)?пол[её]та\s+(\w\.\s*\w\.\s*)?Гагарина|первого\s+пол[её]та\s+человека\s+в\s+космос", 1961, "полёт Ю. А. Гагарина — 1961"),
    (r"(снятия|освобождения)\s+(Ленинграда\s+от\s+)?блокады|полного\s+освобождения\s+Ленинграда", 1944, "снятие блокады Ленинграда — 1944"),
    (r"Магаданской\s+области", 1953, "образование Магаданской области — 1953"),
    (r"(города\s+)?Магадана", 1939, "Магадану присвоен статус города — 1939"),
    (r"Конституции\s+(Российской\s+Федерации|РФ)", 1993, "принятие Конституции РФ — 1993"),
    (r"(со\s+дня\s+рождения\s+)?(маршала\s+)?(Г\.\s*К\.\s*)?Жукова", 1896, "Г. К. Жуков родился в 1896"),
    (r"(со\s+дня\s+рождения\s+)?(А\.\s*С\.\s*)?Пушкина", 1799, "А. С. Пушкин родился в 1799"),
    (r"(со\s+дня\s+рождения\s+)(Ю\.\s*А\.\s*)?Гагарина", 1934, "Ю. А. Гагарин родился в 1934"),
    (r"(со\s+дня\s+рождения\s+)(Л\.\s*Н\.\s*)?Толстого", 1828, "Л. Н. Толстой родился в 1828"),
    (r"(со\s+дня\s+рождения\s+)(А\.\s*П\.\s*)?Чехова", 1860, "А. П. Чехов родился в 1860"),
    (r"(со\s+дня\s+рождения\s+)(М\.\s*Ю\.\s*)?Лермонтова", 1814, "М. Ю. Лермонтов родился в 1814"),
    (r"(со\s+дня\s+рождения\s+)(Ф\.\s*М\.\s*)?Достоевского", 1821, "Ф. М. Достоевский родился в 1821"),
    (r"(со\s+дня\s+рождения\s+)(М\.\s*В\.\s*)?Ломоносова", 1711, "М. В. Ломоносов родился в 1711"),
    (r"(со\s+дня\s+рождения\s+)(Д\.\s*И\.\s*)?Менделеева", 1834, "Д. И. Менделеев родился в 1834"),
    (r"(со\s+дня\s+рождения\s+)(П\.\s*И\.\s*)?Чайковского", 1840, "П. И. Чайковский родился в 1840"),
    (r"(со\s+дня\s+рождения\s+)(М\.\s*А\.\s*)?Шолохова", 1905, "М. А. Шолохов родился в 1905"),
    (r"(со\s+дня\s+рождения\s+)(С\.\s*А\.\s*)?Есенина", 1895, "С. А. Есенин родился в 1895"),
    (r"(со\s+дня\s+рождения\s+)(С\.\s*П\.\s*)?Королёва|Королева", 1907, "С. П. Королёв родился в 1907"),
    (r"(со\s+дня\s+рождения\s+)(А\.\s*В\.\s*)?Суворова", 1730, "А. В. Суворов родился в 1730"),
    (r"(со\s+дня\s+рождения\s+)(Н\.\s*В\.\s*)?Гоголя", 1809, "Н. В. Гоголь родился в 1809"),
    (r"(со\s+дня\s+рождения\s+)(И\.\s*А\.\s*)?Крылова", 1769, "И. А. Крылов родился в 1769"),
    (r"(со\s+дня\s+рождения\s+)(К\.\s*И\.\s*)?Чуковского", 1882, "К. И. Чуковский родился в 1882"),
    (r"(со\s+дня\s+рождения\s+)(С\.\s*Я\.\s*)?Маршака", 1887, "С. Я. Маршак родился в 1887"),
    (r"(со\s+дня\s+рождения\s+)(С\.\s*В\.\s*)?Михалкова", 1913, "С. В. Михалков родился в 1913"),
    (r"(со\s+дня\s+рождения\s+)(А\.\s*Л\.\s*)?Барто", 1906, "А. Л. Барто родилась в 1906"),
    (r"(со\s+дня\s+рождения\s+)(Н\.\s*Н\.\s*)?Носова", 1908, "Н. Н. Носов родился в 1908"),
    (r"(со\s+дня\s+рождения\s+)(А\.\s*С\.\s*)?Макаренко", 1888, "А. С. Макаренко родился в 1888"),
    (r"(со\s+дня\s+рождения\s+)(И\.\s*С\.\s*)?Тургенева", 1818, "И. С. Тургенев родился в 1818"),
    (r"(со\s+дня\s+рождения\s+)(Н\.\s*А\.\s*)?Некрасова", 1821, "Н. А. Некрасов родился в 1821"),
    (r"(со\s+дня\s+рождения\s+)(П\.\s*П\.\s*)?Бажова", 1879, "П. П. Бажов родился в 1879"),
    (r"(со\s+дня\s+рождения\s+)(В\.\s*В\.\s*)?Маяковского", 1893, "В. В. Маяковский родился в 1893"),
]

_DATE_TOKEN = r"(\d{1,2})\s+" + _MONTH_WORD


def _official(name_rx: str, text: str):
    return re.search(name_rx, text, re.I)


def check_official_dates(text: str, pub: date) -> list[dict]:
    out = []
    for name, rx, fn in OFFICIAL_DATES:
        if not re.search(rx, text, re.I):
            continue
        # дата вплотную к названию: «24 мая — День …» или «День … (24 мая)»
        pats = [
            re.compile(_DATE_TOKEN + r"(?:\s+\d{4}\s*(?:г\.?|года)?)?\s*[–—\-:]\s*(?:это\s+|отмечается\s+|—\s*)?(?:[А-Яа-яё]+\s+){0,3}?" + f"(?:{rx})", re.I),
            re.compile(f"(?:{rx})" + r"[^.\n]{0,25}?[(,–—\-]\s*(?:отмечается\s+)?" + _DATE_TOKEN, re.I),
        ]
        for p in pats:
            m = p.search(text)
            if not m:
                continue
            groups = [g for g in m.groups() if g]
            day = next((int(g) for g in groups if g.isdigit()), None)
            mon = next((month_num(g) for g in groups if not g.isdigit() and month_num(g)), None)
            if not day or not mon:
                continue
            off = fn(pub.year)
            claim = f"{day:02d}.{mon:02d} — {name}"
            if (day, mon) == (off.day, off.month):
                out.append({"claim": claim, "status": "confirmed", "source": "официальный календарь памятных дат",
                            "note": f"официальная дата {off:%d.%m}"})
            elif name in ("День матери", "День отца"):
                off2 = fn(pub.year - 1)
                ok = (day, mon) == (off2.day, off2.month)
                out.append({"claim": claim, "status": "confirmed" if ok else "contradiction",
                            "source": "официальный календарь памятных дат",
                            "note": f"в {pub.year} г. отмечается {off:%d.%m}"})
            else:
                out.append({"claim": claim, "status": "contradiction", "source": "официальный календарь памятных дат",
                            "note": f"официальная дата — {off:%d.%m}"})
            break
    return out


def check_anniversaries(text: str, pub: date) -> list[dict]:
    out = []
    for m in re.finditer(r"(\d{2,3})\s*[-–]?\s*(?:лети[еяюй]\w*|летн\w+\s+юбиле\w+|годовщин\w+)\s+((?:(?!\s\d{2,3}\s*[-–]?\s*(?:лет|годовщ))[^.,;\n]){3,80})", text, re.I):
        n, tail = int(m[1]), m[2]
        for rx, base, descr in ANNIVERSARY_BASES:
            if re.match(r"\s*(?:" + rx + ")", tail, re.I):
                expected = pub.year - base
                status = "confirmed" if abs(n - expected) <= 1 else "contradiction"
                out.append({"claim": m[0].strip()[:120], "status": status, "source": "исторические справочные данные",
                            "note": f"{descr}; в {pub.year} г. — {expected}-летие"})
                break
    return out


def check_weekdays(text: str, pub: date) -> list[dict]:
    out = []
    pats = [
        re.compile(_DATE_TOKEN + r"(?:\s+(\d{4})\s*(?:г\.?|года)?)?\s*,?\s*\(?\s*" + _WEEKDAY_WORD, re.I),
        re.compile(r"в\s+" + _WEEKDAY_WORD + r"\s*,?\s*" + _DATE_TOKEN + r"(?:\s+(\d{4}))?", re.I),
    ]
    for i, p in enumerate(pats):
        for m in p.finditer(text):
            if i == 0:
                day, mon, year, wd = int(m[1]), month_num(m[2]), m[3], weekday_num(m[4])
            else:
                wd, day, mon, year = weekday_num(m[1]), int(m[2]), month_num(m[3]), m[4]
            if not mon or wd is None:
                continue
            y = int(year) if year else infer_year(day, mon, pub)
            try:
                d = date(y, mon, day)
            except (TypeError, ValueError):
                continue
            if not year and abs((d - pub).days) > 90:
                continue
            names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
            ok = d.weekday() == wd
            out.append({"claim": m[0].strip()[:80], "status": "confirmed" if ok else "contradiction",
                        "source": "календарь", "note": f"{d:%d.%m.%Y} — {names[d.weekday()]}"})
    return out


def extract_event_date(text: str, published: datetime) -> tuple[date | None, str]:
    """Дата события, о котором рассказывает новость.

    Берутся даты не позже публикации и не раньше 60 дней до неё; приоритет у дат рядом
    с глаголами прошедшего времени («состоялся», «прошёл», «посетили»…). Даты в контексте
    анонсов («состоится», «приглашаем») пропускаются.
    """
    pub = published.date()
    cands = []
    for f in find_dates(text, pub, prefer="past"):
        if f.value > pub + timedelta(days=1) or f.value < pub - timedelta(days=60):
            continue
        local = text[max(0, f.start - 80): f.end + 80]
        if FUTURE_MARKERS.search(local) and not PAST_EVENT_VERBS.search(local):
            continue
        if re.search(r"\b(до|по|срок\w*|приказ\w*|от)\s*$", text[max(0, f.start - 12): f.start], re.I):
            continue
        near_verb = bool(PAST_EVENT_VERBS.search(local))
        if not near_verb and f.kind != "relative":
            continue   # дата без связи с прошедшим событием (приказ, справка) — не используем
        score = (0 if near_verb else 1, 0 if f.kind == "relative" else 1, f.start)
        cands.append((score, f))
    if not cands:
        return None, ""
    cands.sort(key=lambda x: x[0])
    f = cands[0][1]
    value = f.end_value if f.end_value and f.end_value <= pub + timedelta(days=1) else f.value
    return value, ("относительная дата («вчера/сегодня»)" if f.kind == "relative"
                   else "окончание периода в тексте" if f.end_value else "дата в тексте")


def temporal_consistency(text: str, published: datetime) -> list[dict]:
    """Событие описано как прошедшее, но его дата позже даты публикации."""
    pub = published.date()
    out = []
    for f in find_dates(text, pub, prefer="nearest"):
        if not f.has_year or f.value <= pub + timedelta(days=1):
            continue
        local = text[max(0, f.start - 60): f.end + 60]
        before = text[max(0, f.start - 40): f.start]
        # периоды, сроки и планы («с … по 02.09», «Дата проведения», «Период СПТ») — не описание прошедшего
        if re.search(r"(\b(с|по|до|на)\s*$|период|срок|дат\w*\s+проведени|план|график|запланир)", before, re.I):
            continue
        if PAST_EVENT_VERBS.search(local) and not FUTURE_MARKERS.search(local):
            out.append({"claim": local.strip()[:120], "status": "contradiction", "source": "дата публикации",
                        "note": f"событие {f.value:%d.%m.%Y} описано как прошедшее в новости от {pub:%d.%m.%Y}"})
    return out


def offline_factcheck(text: str, published: datetime | None) -> list[dict]:
    if not text or not published:
        return []
    pub = published.date()
    return (check_official_dates(text, pub) + check_anniversaries(text, pub)
            + check_weekdays(text, pub) + temporal_consistency(text, published))
