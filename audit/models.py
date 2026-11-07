from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from .config import SEVERITY_LABEL, SEVERITY_ORDER

# порядок разделов внутри одной критичности: сначала то, что видят граждане и что требует кейс
SECTION_PRIORITY = {s: i for i, s in enumerate([
    "Сайт", "Главная страница", "Новости", "Мероприятия", "Контакты", "Расписание", "Документы",
    "Педагогический состав", "Наш коллектив", "Педагогический состав / коллектив",
    "Сведения об образовательной организации", "Реализуемые образовательные программы", "О школе",
    "Структура и органы управления", "Педагогам и сотрудникам", "Сравнение с эталоном", "Служебное"])}


@dataclass
class Finding:
    section: str
    problem: str
    severity: str                  # critical | high | medium | low
    recommendation: str
    url: str | None = None
    evidence: list[str] = field(default_factory=list)
    check: str = ""                # машинный код проверки

    @property
    def severity_label(self) -> str:
        return SEVERITY_LABEL[self.severity]

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER[self.severity]


@dataclass
class NewsItem:
    url: str
    title: str
    published: datetime | None
    snippet: str = ""
    text: str = ""
    event_date: date | None = None
    event_date_source: str = ""     # regex | llm | ""
    event_date_status: str = "unknown"  # algorithm | agreement | review | unknown
    event_date_candidates: dict[str, str | None] = field(default_factory=dict)
    delay_days: int | None = None
    factcheck: list[dict] = field(default_factory=list)


@dataclass
class EventItem:
    url: str
    title: str
    start: date | None
    end: date | None = None
    raw_date: str = ""


@dataclass
class Document:
    title: str
    url: str
    page: str = ""


@dataclass
class Person:
    name: str
    position: str = ""
    url: str | None = None
    photo: str | None = None
    photo_hash: str | None = None
    has_phone: bool = False
    has_email: bool = False
    has_education: bool = False
    detail_checked: bool = False
    list_page: str = ""


@dataclass
class SiteResult:
    domain: str
    name: str
    kind: str                      # school | kindergarten
    status: str = "ok"             # ok | unavailable
    error: str = ""
    checked_at: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    news: list[NewsItem] = field(default_factory=list)
    events: list[EventItem] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    doc_categories: list[str] = field(default_factory=list)
    persons: list[Person] = field(default_factory=list)
    score: int = 0
    grade: str = ""
    grade_css: str = ""
    is_reference: bool = False
    pages_fetched: int = 0

    @property
    def base(self) -> str:
        return f"https://{self.domain}"

    @property
    def kind_label(self) -> str:
        return "Школа" if self.kind == "school" else "Детский сад"

    def add(self, *findings: Finding) -> None:
        self.findings.extend(f for f in findings if f)

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: (f.rank, SECTION_PRIORITY.get(f.section, 50), f.section))

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    def to_dict(self) -> dict:
        d = asdict(self)
        return _jsonable(d)


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return o
