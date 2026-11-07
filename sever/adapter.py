"""Адаптер данных: результат движка audit/ (SiteResult) → отчёт в формате интерфейса «Севера».

Движок audit/ остаётся единственным источником проверок. Адаптер ничего не перепроверяет и не
добавляет своих правил оценки — он только раскладывает уже полученные факты по четырём состояниям
интерфейса:

* ``fail``    — замечание audit/ (Finding), подтверждённое правилом;
* ``review``  — замечание, которое по смыслу требует решения человека (сверка с эталоном,
                факт-чекинг, расхождение алгоритма и ИИ по дате события, недоступность сайта);
* ``pass``    — пункт, для которого метрики audit/ доказывают, что проверка выполнялась на данных,
                и замечаний по нему нет;
* ``unknown`` — проверка не выполнялась или ей не хватило данных.

Отсутствие замечаний само по себе НЕ превращается в «Проверено»: для каждого пункта ниже указано,
какая метрика audit/ подтверждает выполнение проверки. Охват и оценка не считаются, если сайт
недоступен или подтверждённых пунктов нет.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from audit import config as C
from audit.models import Document, EventItem, Finding, NewsItem, Person, SiteResult

# --------------------------------------------------------------------------- разделы страниц
# Метки разделов, которые SiteAuditor.get() ставит страницам, → ключи разделов интерфейса.
SECTION_KEYS = {
    "Главная страница": [],
    "Новости": ["news"],
    "Мероприятия": ["events"],
    "Документы": ["documents"],
    "Сведения об образовательной организации": ["documents"],
    "Педагогический состав": ["staff"],
    "Наш коллектив": ["staff"],
    "Контакты": ["contacts"],
    "Реализуемые образовательные программы": ["programs"],
    "О школе": ["about"],
    "Структура и органы управления": ["structure"],
    "Расписание": ["schedule"],
    "Педагогам и сотрудникам": ["employees"],
}

# Коды замечаний audit/, которые по смыслу — задача для инспектора, а не подтверждённое нарушение.
REVIEW_CHECKS = {
    "site_unavailable",  # недоступность в момент обхода: повторить проверку, а не фиксировать нарушение
    "redirect",          # перенаправление может быть легитимным
    "news_factcheck",    # расхождение с источниками подтверждает человек
    "ref_docs",          # эталон — ориентир, а не нормативный перечень
    "ref_sections",
}
# Коды, означающие, что проверка не выполнена или данных не хватило.
UNKNOWN_CHECKS = {"internal_error", "news_no_dates"}
CROSSVAL_CHECKS = {"photo_stub", "ref_docs", "ref_sections"}

PAGE_TEXT_LIMIT = 60_000


def _m(r: SiteResult, key: str):
    return r.metrics.get(key)


def _has(r: SiteResult, key: str) -> bool:
    return key in r.metrics and r.metrics[key] is not None


def _fmt_date(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value).strftime("%d.%m.%Y")
        except ValueError:
            return value
    return "—"


# --------------------------------------------------------------------------- каталог пунктов
@dataclass
class Item:
    """Пункт проверки, который может получить «Проверено» без замечания.

    ``codes`` — коды замечаний audit/, которые закрывают этот пункт (тогда пункт показывается
    самим замечанием); ``evidence`` возвращает текст основания, если метрики audit/ доказывают,
    что проверка выполнялась, и ``None`` — если данных нет.
    """
    key: str
    section: str
    title: str
    codes: set[str]
    step: str
    evidence: Callable[[SiteResult], str | None]
    applies: Callable[[SiteResult], bool] = lambda r: True
    unknown_title: str = ""
    unknown_evidence: str = "Проверка не выполнялась или не получила данных."
    recommendation: str = ""
    url_key: str = ""


def _news_fresh(r):
    if not _has(r, "news_days_since_last"):
        return None
    return (f"Последняя новость — {_fmt_date(_m(r, 'news_last'))}, прошло {_m(r, 'news_days_since_last')} дн. "
            f"Норматив: не реже одного раза в {C.NEWS_MAX_GAP_DAYS} дней.")


def _news_gaps(r):
    n = _m(r, "news_last_year") or 0
    if n < 2:
        return None
    return (f"Различных дат публикаций за последний год: {n}; самая ранняя загруженная новость — "
            f"{_fmt_date(_m(r, 'news_oldest_loaded'))}. Интервалов более {C.NEWS_MAX_GAP_DAYS} дней вне каникул нет. "
            "Проверяется загруженная часть архива.")


def _news_late(r):
    if not _m(r, "news_with_event_date"):
        return None
    return (f"Проверено свежих новостей: {_m(r, 'news_checked')}; дата события установлена у "
            f"{_m(r, 'news_with_event_date')}; опубликованных позже суток после события нет.")


def _factcheck(r):
    # «Проверено» — только если хотя бы одно утверждение подтверждено источником; неподтверждённые
    # утверждения не являются ни ошибкой, ни успехом.
    if not _m(r, "news_facts_confirmed"):
        return None
    claims = [c for n in r.news for c in n.factcheck]
    unverified = sum(c.get("status") == "unverified" for c in claims)
    return (f"Утверждений подтверждено источниками: {_m(r, 'news_facts_confirmed')} (в {_m(r, 'news_factchecked')} "
            f"новостях); без внешнего подтверждения: {unverified}; расхождений нет.")


def _events_q(which):
    def f(r):
        if not _m(r, "events_total"):
            return None
        return f"Мероприятий в разделе: {_m(r, 'events_total')}; в {which} квартале: {_m(r, 'events_' + ('current_q' if which == 'текущем' else 'next_q'))}."
    return f


def _events_news(r):
    if not _m(r, "events_past_checked"):
        return None
    return (f"Прошедших мероприятий за {C.NEWS_TIMELINESS_DAYS} дней: {_m(r, 'events_past_checked')}; "
            f"отражены в новостях: {_m(r, 'events_with_news')}.")


def _required_doc(key, label):
    rx = re.compile(C.REQUIRED_DOCS[key][1], re.I)

    def f(r):
        if not _has(r, "docs_required_total") or key not in r.doc_categories:
            return None
        title = next((d.title for d in r.documents if rx.search(d.title)), None)
        if title:
            return f"Найден документ: «{title[:200]}». Соответствие определено по названию; содержимое файла не проверялось."
        if label in (_m(r, "docs_found_by_search") or []):
            return "Найден встроенным поиском сайта. Соответствие определено по тексту результатов поиска."
        return "Упоминание найдено в текстах ссылок раздела «Сведения об образовательной организации»."
    return f


def _staff(key, what):
    def f(r):
        n = _m(r, f"{key}_count")
        if not n:
            return None
        checked = _m(r, f"{key}_detail_checked") or 0
        if what == "present":
            return f"Карточек сотрудников: {n}."
        if what == "photo":
            return (f"Изображение есть во всех {n} карточках. Совпадение фото с человеком и качество снимка "
                    "автоматически не оцениваются.")
        if what == "education":
            return f"Уровень образования указан во всех {checked} открытых карточках (всего карточек {n})."
        return f"Телефон или e-mail указан во всех {checked} открытых карточках (всего карточек {n})."
    return f


def _contacts(r):
    if _m(r, "contacts_missing") is None or _m(r, "contacts_missing"):
        return None
    extra = ", часы приёма граждан" if r.kind == "school" else ""
    return f"В разделе распознаны адрес, телефон, электронная почта, режим работы{extra}. Актуальность значений не проверялась."


def _home_contacts(r):
    if _m(r, "home_contacts_missing") is None or _m(r, "home_contacts_missing"):
        return None
    return "На главной странице распознаны телефон, электронная почта, адрес и режим работы."


def _programs(r):
    if _m(r, "programs_missing") is None or _m(r, "programs_missing"):
        return None
    return "Найдены наименование программы, форма и срок обучения, язык, численность, сведения об ЭО/ДОТ."


def _programs_docs(r):
    if not _m(r, "programs_docs"):
        return None
    return f"Файлов в подразделе «Образование»: {_m(r, 'programs_docs')}."


def _pedagogam(r):
    if not _has(r, "pedagogam_subsections") or _m(r, "pedagogam_empty"):
        return None
    subs = _m(r, "pedagogam_subsections")
    return f"Подразделов проверено: {subs}; пустых нет." if subs else "Раздел содержит текст или документы."


def _about(r):
    n = _m(r, "about_chars")
    if not n:
        return None
    return f"Текст раздела: {n} символов, есть сведения об истории учреждения."


def _structure(r):
    if not _has(r, "structure_chars"):
        return None
    return (f"Текст: {_m(r, 'structure_chars')} символов, карточек руководителей: {_m(r, 'structure_persons')}, "
            f"документов: {_m(r, 'structure_docs')}.")


def _schedule(r):
    if not _has(r, "schedule_docs"):
        return None
    parts = []
    if _m(r, "schedule_widget_filled"):
        parts.append("модуль «Расписание» заполнен")
    if _m(r, "schedule_docs"):
        parts.append(f"файлов: {_m(r, 'schedule_docs')}")
    if _m(r, "schedule_table_rows"):
        parts.append(f"строк таблиц: {_m(r, 'schedule_table_rows')}")
    if _m(r, "schedule_images"):
        parts.append(f"изображений: {_m(r, 'schedule_images')}")
    return "Расписание размещено: " + (", ".join(parts) or "текстом на странице") + ". Учебный год не устарел."


def _placeholders(r, pages_count):
    if not _has(r, "placeholder_pages"):
        return None
    if not pages_count:
        return "Заглушки шаблона Госвеб не найдены в текстах страниц, полученных при проверке."
    return f"Заглушки шаблона Госвеб не найдены в {pages_count} полученных страницах."


def _svedeniya(r):
    if not _has(r, "docs_total"):
        return None
    return "Подразделы «Сведения об образовательной организации» открываются и не пусты."


def catalog(r: SiteResult) -> list[Item]:
    school = r.kind == "school"
    news_base = {"news_missing", "news_empty", "news_no_dates"}
    ev_base = {"events_missing", "events_empty"}
    items = [
        Item("pos", "Главная страница", "Баннер ПОС «Решаем вместе» есть в разметке главной", {"pos_banner"},
             "check_home", lambda r: "Ссылка или виджет ПОС найдены в HTML главной страницы." if r.pages_fetched else None),
        Item("home_contacts", "Главная страница", "Контакты на главной странице", {"home_contacts"}, "check_home",
             _home_contacts, applies=lambda r: r.kind == "kindergarten"),
        Item("news_fresh", "Новости", "Новости обновляются не реже раза в 14 дней", news_base | {"news_stale"},
             "check_news", _news_fresh, url_key="news"),
        Item("news_gaps", "Новости", "Интервалы между новостями за год", news_base | {"news_gaps"}, "check_news",
             _news_gaps, unknown_title="Недостаточно дат публикаций для проверки интервалов", url_key="news"),
        Item("news_late", "Новости", "Новости публикуются в течение суток после события", news_base | {"news_late"},
             "check_news", _news_late, unknown_title="Срок публикации после события не установлен",
             unknown_evidence="Ни в одной из проверенных новостей дата события не установлена однозначно.",
             recommendation="Указывайте в тексте новости дату мероприятия.", url_key="news"),
        Item("news_factcheck", "Новости", "Даты и факты новостей не противоречат открытым источникам",
             news_base | {"news_factcheck"}, "check_news", _factcheck,
             unknown_title="Факты новостей не подтверждены открытыми источниками",
             unknown_evidence="Ни одно утверждение не подтверждено источником: в новостях нет сверяемых фактов "
                              "(официальные даты, юбилеи, дни недели) или ИИ работал без веб-поиска. "
                              "Это не ошибка сайта.",
             url_key="news"),
        Item("events_current", "Мероприятия", "Мероприятия на текущий квартал", ev_base | {"events_current_q"},
             "check_events", _events_q("текущем"), applies=lambda r: school or "events" in r.sections, url_key="events"),
        Item("events_next", "Мероприятия", "Анонсы мероприятий на следующий квартал", ev_base | {"events_next_q"},
             "check_events", _events_q("следующем"), applies=lambda r: school or "events" in r.sections, url_key="events"),
        Item("events_news", "Мероприятия", "Прошедшие мероприятия отражены в новостях", ev_base | {"events_vs_news"},
             "check_events", _events_news, applies=lambda r: school or "events" in r.sections,
             unknown_title="Нет прошедших мероприятий для сопоставления с новостями", url_key="events"),
        Item("svedeniya", "Сведения об образовательной организации", "Подразделы «Сведений» заполнены",
             {"svedeniya_empty"}, "check_documents", _svedeniya, applies=lambda r: "svedeniya" in r.sections,
             url_key="svedeniya"),
    ]
    for key, (label, _rx, _sev, who) in C.REQUIRED_DOCS.items():
        if who not in ("both", r.kind):
            continue
        items.append(Item(f"doc_{key}", "Документы", f"Обязательный документ: {label}", {f"doc_{key}"},
                          "check_documents", _required_doc(key, label), url_key="documents"))
    staff = [("ped_staff", "Педагогический состав", False)] if school else \
        [("kollektiv", "Наш коллектив", True), ("ped_staff", "Педагогический состав", True)]
    for key, label, contacts in staff:
        base = {f"{key}_missing", f"{key}_empty"}
        items += [
            Item(f"{key}_present", label, f"Раздел «{label}» заполнен карточками", base, "check_staff_all",
                 _staff(key, "present"), url_key=key),
            Item(f"{key}_photo", label, "Фото в карточках сотрудников", base | {f"{key}_photo", "photo_stub"},
                 "check_staff_all", _staff(key, "photo"), url_key=key),
            Item(f"{key}_education", label, "Уровень образования в карточках", base | {f"{key}_education"},
                 "check_staff_all", _staff(key, "education"), url_key=key),
        ]
        if contacts:
            items.append(Item(f"{key}_contacts", label, "Контакты в карточках сотрудников",
                              base | {f"{key}_contacts"}, "check_staff_all", _staff(key, "contacts"), url_key=key))
    items += [
        Item("contacts", "Контакты", "Контактные данные полные", {"contacts_missing", "contacts_404", "contacts_incomplete"},
             "check_contacts", _contacts, url_key="contacts"),
        Item("programs", "Реализуемые образовательные программы", "Сведения о реализуемых программах",
             {"programs_missing", "programs_empty", "programs_incomplete"}, "check_programs", _programs,
             url_key="obrazovanie"),
        Item("programs_docs", "Реализуемые образовательные программы", "Тексты образовательных программ размещены",
             {"programs_missing", "programs_no_docs"}, "check_programs", _programs_docs, url_key="obrazovanie"),
        Item("pedagogam", "Педагогам и сотрудникам", "Раздел «Педагогам и сотрудникам» заполнен",
             {"pedagogam_missing", "pedagogam_empty", "pedagogam_sub_empty"}, "check_pedagogam", _pedagogam,
             url_key="pedagogam"),
    ]
    if school:
        items += [
            Item("about", "О школе", "История и описание школы", {"about_missing", "about_history", "about_short"},
                 "check_about", _about, url_key="about"),
            Item("structure", "Структура и органы управления", "Подраздел «Структура и органы управления» заполнен",
                 {"structure_missing", "structure_empty"}, "check_structure", _structure, url_key="struktura"),
            Item("schedule", "Расписание", "Расписание на текущий учебный год",
                 {"schedule_missing", "schedule_404", "schedule_empty", "schedule_old"}, "check_schedule", _schedule,
                 url_key="schedule"),
        ]
    return [i for i in items if i.applies(r)]


# --------------------------------------------------------------------------- построение отчёта
def _fingerprint(org_id, key: str, title: str, evidence: str) -> str:
    return hashlib.sha256((str(org_id) + key + title + evidence).encode()).hexdigest()[:24]


def _finding_check(r: SiteResult, f: Finding, index: int) -> dict:
    code = f.check or "finding"
    status = "unknown" if code in UNKNOWN_CHECKS else "review" if code in REVIEW_CHECKS else "fail"
    method = "crossval" if code in CROSSVAL_CHECKS else "rule"
    if code == "news_factcheck" and any("ИИ (" in e for e in f.evidence):
        method = "llm"
    return {
        "key": f"{code}_{index}", "check": code, "section": f.section, "title": f.problem, "status": status,
        "severity": f.severity, "evidence": "\n".join(f.evidence) or f.problem, "evidence_items": list(f.evidence),
        "url": f.url or r.base + "/", "recommendation": f.recommendation, "method": method, "origin": "audit",
    }


def _failed_steps(r: SiteResult) -> dict[str, str]:
    out = {}
    for f in r.findings:
        if f.check == "internal_error":
            m = re.search(r"«(check_\w+)»", f.problem)
            if m:
                out[m[1]] = f.problem
    return out


def build_checks(r: SiteResult, pages_count: int) -> list[dict]:
    checks = [_finding_check(r, f, i) for i, f in enumerate(r.sorted_findings())]
    if r.status != "ok":
        return checks
    codes = {f.check for f in r.findings}
    failed = _failed_steps(r)
    for item in catalog(r):
        if item.codes & codes:
            continue  # пункт уже представлен замечанием audit/
        url = r.sections.get(item.url_key) or r.base + "/"
        if item.step in failed:
            checks.append(dict(key=item.key, check=item.key, section=item.section, title=item.unknown_title or item.title,
                               status="unknown", severity="medium", evidence=failed[item.step], url=url,
                               recommendation="Повторите проверку; при повторении ошибки сообщите разработчикам.",
                               method="rule", origin="catalog"))
            continue
        ev = item.evidence(r)
        if ev:
            checks.append(dict(key=item.key, check=item.key, section=item.section, title=item.title, status="pass",
                               severity="low", evidence=ev, url=url,
                               recommendation="Замечаний audit/ по этому пункту нет. Подтверждение относится только к проверенным данным.",
                               method="rule", origin="catalog"))
        else:
            checks.append(dict(key=item.key, check=item.key, section=item.section,
                               title=item.unknown_title or item.title + ": нет данных", status="unknown",
                               severity="medium", evidence=item.unknown_evidence, url=url,
                               recommendation=item.recommendation or "Проверьте пункт вручную.", method="rule",
                               origin="catalog"))
    # заглушки — по всем полученным страницам
    if not codes & {"placeholders", "placeholders_more"} and "check_placeholders" not in failed:
        ev = _placeholders(r, pages_count)
        if ev:
            checks.append(dict(key="placeholders", check="placeholders", section="Оформление",
                               title="Демонстрационный контент шаблона не обнаружен", status="pass", severity="low",
                               evidence=ev, url=r.base + "/", method="rule", origin="catalog",
                               recommendation="Проверены только тексты полученных страниц."))
    # расхождение алгоритма и ИИ по дате события: решение инспектора
    disputed = [n for n in r.news if n.event_date_status == "review"]
    if disputed:
        lines = [f"«{n.title[:90]}»: алгоритм — {_fmt_date(n.event_date_candidates.get('algorithm'))}, "
                 f"ИИ — {_fmt_date(n.event_date_candidates.get('model'))}" for n in disputed[:8]]
        checks.append(dict(key="news_event_date_review", check="news_event_date_review", section="Новости",
                           title=f"Дата события определена по-разному алгоритмом и ИИ в {len(disputed)} новостях",
                           status="review", severity="medium", evidence="\n".join(lines), evidence_items=lines,
                           url=disputed[0].url, method="llm", origin="audit",
                           recommendation="Откройте новость и установите дату события вручную. Такие новости не учитываются в проверке срока 24 часа."))
    # визуальные требования памятки — всегда ручная проверка
    checks.append(dict(key="visual", check="visual", section="Оформление",
                       title="Качество фото, деловой стиль, шрифты и наложения блоков", status="review",
                       severity="low", evidence="Текстовый обход не оценивает визуальные характеристики.",
                       url=r.base + "/", method="manual", origin="catalog",
                       recommendation="Откройте сайт в браузере и выполните визуальную проверку по памятке."))
    return checks


def _pages(pages: dict | None) -> list[dict]:
    out = []
    for url, value in (pages or {}).items():
        section, title, text = value
        out.append({"url": url, "title": title or url, "text": (text or "")[:PAGE_TEXT_LIMIT],
                    "sections": SECTION_KEYS.get(section, []), "section_label": section, "fetched_at": None})
    return out


def _news(r: SiteResult) -> list[dict]:
    rows = []
    for n in r.news:
        if not (n.event_date or n.factcheck or n.text):
            continue
        rows.append({"url": n.url, "title": n.title,
                     "published": n.published.isoformat() if n.published else None,
                     "event_date": n.event_date.isoformat() if n.event_date else None,
                     "event_date_status": n.event_date_status, "event_date_source": n.event_date_source,
                     "candidates": n.event_date_candidates, "delay_days": n.delay_days,
                     "factcheck": n.factcheck})
        if len(rows) >= 30:
            break
    return rows


def adapt(org: dict, r: SiteResult, *, pages: dict | None = None, ai: dict | None = None,
          mode: str = "full", preliminary: bool = False, limitations: list[str] | None = None,
          metrics: dict | None = None, checked_at: str | None = None) -> dict:
    """Собирает отчёт интерфейса из результата audit/ и снимков страниц аудитора."""
    page_rows = _pages(pages)
    checks = build_checks(r, len(page_rows))
    for c in checks:
        c["fingerprint"] = _fingerprint(org["id"], c["key"], c["title"], c["evidence"])
    available = r.status == "ok"
    total = len(checks)
    assessed = sum(c["status"] in ("pass", "fail") for c in checks)
    passes = sum(c["status"] == "pass" for c in checks)
    # Без данных охват и оценка не вычисляются: показываются как «—», а не 0 или 100.
    coverage = round(100 * assessed / total) if available and total and assessed else None
    score = round(100 * passes / assessed) if available and assessed else None
    failed_steps = _failed_steps(r)
    status = "unavailable" if not available else "partial" if failed_steps else "complete"
    lims = [
        "Обход движком audit/: меню, карта сайта /map/ и стандартные адреса шаблона Госвеб; содержимое, "
        "появляющееся только после выполнения JavaScript, может быть пропущено.",
        "Документы распознаются по названиям и текстам ссылок; содержимое файлов (PDF, DOCX, сканы) не читается.",
        "Отсутствие сведений в полученной выборке не доказывает их отсутствие на всём сайте.",
        "Охват — доля пунктов с подтверждённым результатом; оценка — доля успешных среди них. "
        "Это не заключение о соблюдении закона.",
    ]
    lims += limitations or []
    if failed_steps:
        lims.append("Часть проверок завершилась ошибкой: " + ", ".join(sorted(failed_steps)) + ".")
    return {
        "organization": {k: org[k] for k in ("id", "name", "url", "kind")},
        "domain": r.domain,
        "checked_at": checked_at or r.checked_at or datetime.now().isoformat(timespec="seconds"),
        "engine": "audit",
        "status": status,
        "score": score,
        "coverage": coverage,
        "audit_score": r.score if available else None,
        "audit_grade": r.grade,
        "is_reference": r.is_reference,
        "checks": checks,
        "pages": page_rows,
        "documents": [{"url": d.url, "label": d.title, "source": d.page or r.base + "/", "state": "listed"}
                      for d in r.documents],
        "doc_categories": list(r.doc_categories),
        "news": _news(r),
        "sections": dict(r.sections),
        "errors": [{"url": r.base + "/", "error": r.error, "kind": "network"}] if r.error else [],
        "limited": False,
        "ai": ai or {"state": "disabled", "findings": [], "events": []},
        "limitations": lims,
        "mode": mode,
        "preliminary": preliminary,
        "metrics": metrics or {},
        "audit_metrics": dict(r.metrics),
        "raw": r.to_dict(),
    }


# --------------------------------------------------------------------------- обратное чтение
def _as_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def site_from_dict(d: dict) -> SiteResult:
    """SiteResult из сохранённого словаря (to_dict / results.json). Повторяет audit.report.load_results."""
    r = SiteResult(**{k: v for k, v in d.items() if k not in ("findings", "news", "events", "documents", "persons")})
    r.findings = [Finding(**f) for f in d.get("findings", [])]
    r.news = [NewsItem(**{**n, "published": _as_dt(n.get("published")),
                          "event_date": date.fromisoformat(n["event_date"]) if n.get("event_date") else None})
              for n in d.get("news", [])]
    r.events = [EventItem(**{**e, "start": date.fromisoformat(e["start"]) if e.get("start") else None,
                             "end": date.fromisoformat(e["end"]) if e.get("end") else None}) for e in d.get("events", [])]
    r.documents = [Document(**x) for x in d.get("documents", [])]
    r.persons = [Person(**x) for x in d.get("persons", [])]
    return r


def summarize(d: dict) -> dict:
    """Краткая сводка для реестра (без загрузки полного отчёта)."""
    checks = d["checks"]
    return {"name": d["organization"]["name"], "score": d["score"], "coverage": d["coverage"],
            "issues": sum(c["status"] == "fail" for c in checks),
            "critical": sum(c["status"] == "fail" and c["severity"] == "critical" for c in checks),
            "review": sum(c["status"] == "review" for c in checks),
            "status": d["status"], "pages": len(d["pages"]), "preliminary": d.get("preliminary", False),
            "audit_score": d.get("audit_score"), "mode": d.get("mode")}
