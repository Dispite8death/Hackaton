"""Разбор русских дат: «25 мая 2026, 16:56», «26 октября 2026 г.», «01.12.2022», «вчера»…"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH_WORD = (r"(январ[яь]|феврал[яь]|марта?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|августа?|"
               r"сентябр[яь]|октябр[яь]|ноябр[яь]|декабр[яь])")
WEEKDAYS = {"понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресень": 6}
_WEEKDAY_WORD = r"(понедельник|вторник|сред[ау]|четверг|пятниц[ау]|суббот[ау]|воскресенье)"


def month_num(word: str) -> int | None:
    w = word.lower()
    if w.startswith("ма") and not w.startswith("март"):
        return 5
    for stem, n in MONTHS.items():
        if stem != "ма" and w.startswith(stem):
            return n
    return None


def weekday_num(word: str) -> int | None:
    w = word.lower()
    for stem, n in WEEKDAYS.items():
        if w.startswith(stem):
            return n
    return None


_DT_PATTERNS = [
    # 2026-05-25 16:56:19 (schema.org datePublished)
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?"), "iso"),
    # 25 мая 2026, 16:56 / 26 октября 2026 г.
    (re.compile(r"(\d{1,2})\s+" + _MONTH_WORD + r"\s+(\d{4})(?:\s*г\.?)?(?:,?\s*(?:в\s*)?(\d{1,2}):(\d{2}))?", re.I), "ru"),
    # 01.12.2022, 15:00 / 01.12.2022 11:26:00
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})(?:,?\s*(\d{1,2}):(\d{2}))?"), "dot"),
]


def parse_datetime(text: str) -> datetime | None:
    """Первая полная дата (с годом) в строке."""
    if not text:
        return None
    best = None
    for rx, kind in _DT_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        try:
            if kind == "iso":
                y, mo, d = int(m[1]), int(m[2]), int(m[3])
                hh, mm = int(m[4] or 0), int(m[5] or 0)
            elif kind == "ru":
                d, mo, y = int(m[1]), month_num(m[2]), int(m[3])
                hh, mm = int(m[4] or 0), int(m[5] or 0)
            else:
                d, mo, y = int(m[1]), int(m[2]), int(m[3])
                hh, mm = int(m[4] or 0), int(m[5] or 0)
            dt = datetime(y, mo, d, hh, mm)
        except (TypeError, ValueError):
            continue
        if best is None or m.start() < best[0]:
            best = (m.start(), dt)
    return best[1] if best else None


@dataclass
class FoundDate:
    value: date
    start: int
    end: int
    has_year: bool
    kind: str            # explicit | relative
    context: str = ""
    end_value: date | None = None   # для диапазонов «21–25 сентября»


_RANGE_RU = re.compile(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+" + _MONTH_WORD + r"(?:\s+(\d{4}))?", re.I)
_DAY_RU = re.compile(r"(?<![\d.])(\d{1,2})\s+" + _MONTH_WORD + r"(?:\s+(\d{4})\s*(?:г(?:ода|\.)?)?)?", re.I)
_DOT = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d.])")
_RELATIVE = re.compile(r"\b(сегодня|вчера|позавчера)\b", re.I)


def infer_year(day: int, month: int, ref: date, weekday: int | None = None,
               prefer: str = "nearest") -> int | None:
    """Год для даты без года. Если известен день недели — выбираем год, где он совпадает."""
    cands = []
    for y in range(ref.year - 2, ref.year + 2):
        try:
            cands.append(date(y, month, day))
        except ValueError:
            continue
    if not cands:
        return None
    if weekday is not None:
        match = [c for c in cands if c.weekday() == weekday]
        if len(match) == 1:
            return match[0].year
        if match:
            cands = match
    if prefer == "past":
        past = [c for c in cands if c <= ref + timedelta(days=1)]
        if past:
            return max(past).year
    if prefer == "future":
        fut = [c for c in cands if c >= ref - timedelta(days=1)]
        if fut:
            return min(fut).year
    return min(cands, key=lambda c: abs((c - ref).days)).year


def find_dates(text: str, ref: date, prefer: str = "past") -> list[FoundDate]:
    """Все даты в тексте. Даты без года достраиваются относительно ref."""
    out: list[FoundDate] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    def add(fd: FoundDate):
        taken.append((fd.start, fd.end))
        lo, hi = max(0, fd.start - 120), min(len(text), fd.end + 120)
        fd.context = text[lo:hi]
        out.append(fd)

    for m in _RANGE_RU.finditer(text):
        mo = month_num(m[3])
        if not mo:
            continue
        y = int(m[4]) if m[4] else infer_year(int(m[1]), mo, ref, prefer=prefer)
        try:
            add(FoundDate(date(y, mo, int(m[1])), m.start(), m.end(), bool(m[4]), "explicit",
                          end_value=date(y, mo, int(m[2]))))
        except (TypeError, ValueError):
            pass
    for m in _DAY_RU.finditer(text):
        if not free(m.start(), m.end()):
            continue
        mo = month_num(m[2])
        if not mo:
            continue
        wd = None
        tail = text[m.end(): m.end() + 25]
        wm = re.match(r"\s*,?\s*\(?\s*" + _WEEKDAY_WORD, tail, re.I)
        if wm:
            wd = weekday_num(wm[1])
        y = int(m[3]) if m[3] else infer_year(int(m[1]), mo, ref, wd, prefer=prefer)
        try:
            add(FoundDate(date(y, mo, int(m[1])), m.start(), m.end(), bool(m[3]), "explicit"))
        except (TypeError, ValueError):
            pass
    for m in _DOT.finditer(text):
        if not free(m.start(), m.end()):
            continue
        d, mo = int(m[1]), int(m[2])
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        if not m[3]:
            # «10.05» без года неотличимо от времени 10:05 — такие строки не считаем датой
            continue
        y = int(m[3])
        y = y + 2000 if y < 100 else y
        try:
            add(FoundDate(date(y, mo, d), m.start(), m.end(), bool(m[3]), "explicit"))
        except (TypeError, ValueError):
            pass
    for m in _RELATIVE.finditer(text):
        shift = {"сегодня": 0, "вчера": 1, "позавчера": 2}[m[1].lower()]
        add(FoundDate(ref - timedelta(days=shift), m.start(), m.end(), True, "relative"))
    out.sort(key=lambda f: f.start)
    return out


def quarter_bounds(d: date) -> tuple[date, date]:
    q0 = (d.month - 1) // 3 * 3 + 1
    start = date(d.year, q0, 1)
    end_month = q0 + 3
    end = date(d.year + (end_month > 12), (end_month - 1) % 12 + 1, 1) - timedelta(days=1)
    return start, end


def next_quarter_bounds(d: date) -> tuple[date, date]:
    _, end = quarter_bounds(d)
    return quarter_bounds(end + timedelta(days=1))


def quarter_label(start: date) -> str:
    return f"{(start.month - 1) // 3 + 1} кв. {start.year}"


def academic_year(d: date) -> tuple[int, int]:
    return (d.year, d.year + 1) if d.month >= 9 else (d.year - 1, d.year)


def fmt(d: date | datetime | None) -> str:
    if d is None:
        return "—"
    return d.strftime("%d.%m.%Y")
