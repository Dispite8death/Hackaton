"""Latest completed quick check per active institution, shared by screen and exports."""
from collections import Counter
from contextlib import closing

from . import db

KINDS = {"school": "Школа", "kindergarten": "Детский сад"}
LEVELS = {"CRITICAL": "Критическая", "HIGH": "Высокая", "MEDIUM": "Средняя", "LOW": "Низкая", "INFO": "Информация"}
SECTIONS = {"docs": "Документы", "contacts": "Контакты", "schedule": "Расписание", "staff": "Сотрудники",
            "news": "Новости", "events": "Мероприятия", "events_news": "Новости о мероприятиях",
            "content": "Содержание", "pos": "Обратная связь", "system": "Доступность и загрузка",
            "photo": "Фотографии", "staff_cards": "Карточки сотрудников", "menu": "Меню сайта",
            "sections": "Обязательные разделы", "typo": "Текстовые ошибки", "structure": "Структура сайта", "type": "Тип учреждения"}

CHECKS = {
    "NO_PHONE": "Нет телефона", "NO_EMAIL": "Нет электронной почты", "NO_ADDRESS": "Нет адреса",
    "NO_WORK_HOURS": "Нет режима работы", "NO_SCHEDULE": "Нет расписания", "NO_DOCUMENTS": "Раздел документов не найден",
    "MISSING_DOC": "Отсутствует обязательный документ", "DOC_OUTSIDE_SECTION": "Документ вне своего раздела",
    "MISSING_SECTION": "Отсутствует обязательный раздел", "SECTION_EMPTY": "Пустой раздел",
    "SECTION_PAGE_THIN": "Мало информации в разделе", "SECTION_UNMAPPED": "Раздел не сопоставлен с меню",
    "SECTION_NOT_CRAWLED": "Страницы раздела не загружены", "SECTION_SUBSECTIONS_PARTIAL": "Подразделы проверены частично",
    "NO_DATES": "Не найдены даты новостей", "NEWS_INTERVAL_GAP": "Большой перерыв между новостями",
    "NO_EXPLICIT_PUBLISH_EVENT_DATES": "Не указаны даты публикации и события", "OLD_NEWS": "Новости устарели",
    "NO_UPCOMING_EVENTS": "Нет предстоящих мероприятий", "EVENT_NOT_IN_NEWS": "Нет новости о мероприятии",
    "PUBLISH_DELAY": "Задержка публикации", "PLACEHOLDER_TEXT": "Шаблонный текст", "NO_POS_BANNER": "Нет баннера обратной связи",
    "NO_STAFF_PAGE": "Не найдена страница сотрудников", "NO_STAFF_CONTACTS": "Нет контактов сотрудников",
    "NO_EDUCATION_INFO": "Нет сведений об образовании", "STAFF_EDUCATION_GAP": "Неполные сведения об образовании",
    "STAFF_PHOTO_GAP": "Не хватает фотографий сотрудников", "PHOTO_DUPLICATES": "Повторяющиеся фотографии",
    "PHOTO_ISSUE": "Замечание по фотографии", "PHOTO_PLACEHOLDERS": "Возможные фотозаглушки",
    "SECTION_PHOTO_ISSUES": "Фотографии в разделе", "NO_PIL": "Проверка изображений недоступна",
    "BAD_ORDINAL": "Написание порядкового числительного", "DOU_ON_SCHOOL": "Упоминание ДОУ на сайте школы",
    "TYPE_MISMATCH": "Признаки другого типа учреждения", "NO_CACHE": "Нет сохранённых страниц",
    "UNHANDLED_ERROR": "Ошибка обработки", "CRAWL_FAILED": "Ошибка загрузки сайта",
}


def latest():
    with closing(db.conn()) as conn:
        conn.execute("BEGIN")
        sites = [dict(s) for s in conn.execute("SELECT * FROM sites WHERE active=1 ORDER BY name,id")]
        rows = []
        for site in sites:
            result = conn.execute("""
                SELECT rs.*,r.finished_at FROM run_sites rs JOIN runs r ON rs.run_id=r.id
                WHERE rs.site_id=? AND r.status IN ('done','error')
                ORDER BY rs.run_id DESC,rs.id DESC LIMIT 1""", (site["id"],)).fetchone()
            if result is None:
                continue
            findings = [dict(f) for f in conn.execute("""
                SELECT * FROM findings WHERE run_id=? AND site_id=? ORDER BY
                CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2
                WHEN 'LOW' THEN 3 ELSE 4 END,id""", (result["run_id"], site["id"]))]
            rows.append({"site": site, "result": dict(result), "findings": findings})
    return rows


def select(rows, ids=(), kind=None, level=None, section=None, code=None,
           since=None, until=None, outcome=None):
    selected = []
    for row in rows:
        site = row["site"]
        if (ids and site["id"] not in ids) or (kind and site["type"] != kind):
            continue
        checked = (row["result"].get("finished_at") or "")[:10]
        if (since and checked < since) or (until and checked > until):
            continue
        if (outcome == "clear" and row["findings"]) or (outcome == "issue" and not row["findings"]):
            continue
        findings = [f for f in row["findings"] if (not level or f["severity"] == level)
                    and (not section or f["agent"] == section) and (not code or f["code"] == code)]
        if (level or section or code) and not findings:
            continue
        selected.append({**row, "findings": findings, "partial": bool(level or section or code or since or until)})
    return selected


def stats(rows):
    levels = Counter(f["severity"] for row in rows for f in row["findings"])
    return {"total_sites": len(rows), "severity_counts": levels, "total_findings": sum(levels.values()),
            "avg_score": round(sum(row["result"]["score"] for row in rows) / len(rows), 1) if rows else "—",
            "critical_sites": sum(any(f["severity"] == "CRITICAL" for f in row["findings"]) for row in rows)}
