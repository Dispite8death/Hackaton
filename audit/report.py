"""Отчёты: index.html (сводный рейтинг), sites/<домен>.html (карточка учреждения), summary.xlsx, findings.csv."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import SEVERITY_LABEL, SEVERITY_ORDER
from .dates import fmt
from .models import Document, EventItem, Finding, NewsItem, Person, SiteResult

TEMPLATES = Path(__file__).parent / "templates"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["d"] = lambda v: fmt(_as_date(v)) if v else "—"
    env.filters["sev"] = lambda s: SEVERITY_LABEL.get(s, s)
    env.filters["plural"] = _plural
    return env


def _as_date(v):
    if isinstance(v, (date, datetime)):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _slug(domain: str) -> str:
    return domain.split(".")[0]


def priorities(r: SiteResult, n: int = 5) -> list[Finding]:
    return [f for f in r.sorted_findings() if f.severity in ("critical", "high")][:n]


def render_all(sites: list[SiteResult], summary: dict, out: Path) -> None:
    out = Path(out)
    (out / "sites").mkdir(parents=True, exist_ok=True)
    env = _env()
    ranked = sorted(sites, key=lambda r: (-r.score, r.domain))
    place = {r.domain: i + 1 for i, r in enumerate(ranked)}
    for r in sites:
        r.metrics["place"] = place[r.domain]
    ok = [r for r in sites if r.status == "ok"]
    kpi = {
        "total": len(sites),
        "schools": sum(r.kind == "school" for r in sites),
        "kindergartens": sum(r.kind == "kindergarten" for r in sites),
        "unavailable": len(sites) - len(ok),
        "avg_score": round(sum(r.score for r in ok) / len(ok)) if ok else 0,
        "critical": sum(r.count("critical") for r in sites),
        "high": sum(r.count("high") for r in sites),
        "stale_news": sum(1 for r in ok if (r.metrics.get("news_days_since_last") or 999) > 14),
        "no_events_q": sum(1 for r in ok if r.kind == "school" and not r.metrics.get("events_current_q")),
        "placeholders": sum(1 for r in ok if r.metrics.get("placeholder_pages")),
        "photo_stub": sum(1 for r in ok if any(f.check == "photo_stub" for f in r.findings)),
    }
    refs = {k: next((r for r in sites if r.domain == d), None) for k, d in summary.get("references", {}).items()}
    common = summary.get("common_problems", [])
    (out / "index.html").write_text(env.get_template("index.html").render(
        sites=ranked, kpi=kpi, refs=refs, common=common, summary=summary, slug=_slug,
        priorities=priorities), encoding="utf-8")
    for r in sites:
        by_section = defaultdict(list)
        for f in r.sorted_findings():
            by_section[f.section].append(f)
        sections = sorted(by_section.items(), key=lambda kv: (min(f.rank for f in kv[1]), kv[0]))
        ref = refs.get(r.kind)
        news_checked = [n for n in r.news if n.event_date or n.factcheck or n.text][:20]
        (out / "sites" / f"{_slug(r.domain)}.html").write_text(env.get_template("site.html").render(
            r=r, sections=sections, prio=priorities(r, 7), ref=ref, summary=summary, total=len(sites),
            news_checked=news_checked, sev_order=SEVERITY_ORDER), encoding="utf-8")
    _write_csv(sites, out / "findings.csv")
    _write_xlsx(ranked, out / "summary.xlsx", summary)


def _write_csv(sites: list[SiteResult], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Домен", "Учреждение", "Тип", "Балл", "Раздел", "Критичность", "Проблема", "Рекомендация",
                    "Ссылка", "Подробности"])
        for r in sites:
            for x in r.sorted_findings():
                w.writerow([r.domain, r.name, r.kind_label, r.score, x.section, x.severity_label, x.problem,
                            x.recommendation, x.url or "", " | ".join(x.evidence)])


def _write_xlsx(ranked: list[SiteResult], path: Path, summary: dict) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return
    wb = Workbook()
    ws = wb.active
    ws.title = "Рейтинг"
    head = ["Место", "Учреждение", "Домен", "Тип", "Балл", "Оценка", "Эталон", "Критично", "Высокая", "Средняя",
            "Низкая", "Дней с последней новости", "Нарушений интервала новостей", "Мероприятий в тек. квартале",
            "Обязательных документов", "Педагогов", "Без фото", "Без уровня образования", "Главная проблема"]
    ws.append(head)
    fills = {"good": "C6EFCE", "ok": "FFF2CC", "warn": "FCE4D6", "bad": "F8CBAD"}
    for i, r in enumerate(ranked, 1):
        m = r.metrics
        staff = m.get("ped_staff_count", 0)
        top = priorities(r, 1)
        ws.append([i, r.name, r.domain, r.kind_label, r.score, r.grade, "да" if r.is_reference else "",
                   r.count("critical"), r.count("high"), r.count("medium"), r.count("low"),
                   m.get("news_days_since_last"), m.get("news_gaps"),
                   m.get("events_current_q") if r.kind == "school" else None,
                   f"{m.get('docs_required_present', 0)}/{m.get('docs_required_total', 0)}",
                   staff, m.get("ped_staff_no_photo"), m.get("ped_staff_no_education"),
                   top[0].problem if top else ""])
        ws.cell(row=i + 1, column=5).fill = PatternFill("solid", fgColor=fills.get(r.grade_css, "FFFFFF"))
    ws2 = wb.create_sheet("Замечания")
    ws2.append(["Учреждение", "Домен", "Раздел", "Критичность", "Проблема", "Рекомендация", "Ссылка", "Подробности"])
    for r in ranked:
        for x in r.sorted_findings():
            ws2.append([r.name, r.domain, x.section, x.severity_label, x.problem, x.recommendation, x.url or "",
                        "\n".join(x.evidence)])
    ws3 = wb.create_sheet("Частые проблемы")
    ws3.append(["Раздел", "Проблема (пример)", "Критичность", "Число сайтов"])
    for c in summary.get("common_problems", []):
        ws3.append([c["section"], c["example"], SEVERITY_LABEL[c["severity"]], c["sites"]])
    for sheet in (ws, ws2, ws3):
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for col in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(col)].width = 18
        sheet.freeze_panes = "A2"
    ws.column_dimensions["B"].width = 60
    ws.column_dimensions["S"].width = 70
    ws2.column_dimensions["A"].width = 45
    ws2.column_dimensions["E"].width = 70
    ws2.column_dimensions["F"].width = 60
    ws2.column_dimensions["H"].width = 60
    wb.save(path)


# ---------------------------------------------------------------- загрузка results.json
def load_results(path: Path) -> tuple[list[SiteResult], dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    sites = []
    for d in data["sites"]:
        r = SiteResult(**{k: v for k, v in d.items() if k not in
                          ("findings", "news", "events", "documents", "persons")})
        r.findings = [Finding(**f) for f in d["findings"]]
        r.news = [NewsItem(**{**n, "published": _as_date(n["published"]) if n["published"] else None,
                              "event_date": date.fromisoformat(n["event_date"]) if n.get("event_date") else None})
                  for n in d["news"]]
        r.events = [EventItem(**{**e, "start": date.fromisoformat(e["start"]) if e.get("start") else None,
                                 "end": date.fromisoformat(e["end"]) if e.get("end") else None}) for e in d["events"]]
        r.documents = [Document(**x) for x in d["documents"]]
        r.persons = [Person(**x) for x in d["persons"]]
        sites.append(r)
    return sites, data["summary"]
