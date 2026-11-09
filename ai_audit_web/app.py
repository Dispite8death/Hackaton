import time
import csv
import io
import json
import logging
from html import escape
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from nicegui import ui, app

from . import db
from . import core
from . import llm
from . import vision, reports

from .integration import archive_id, ai_status
from Engine.portal_nav import navigation
from Engine import demo_settings

from . import operations
from .operations import audit_lock


def _photo_summary(sm):
    sm = sm or {}
    return {
        "total": sm.get("total", 0),
        "bad": sm.get("bad", 0),
        "duplicates": sm.get("duplicate_pairs", sm.get("duplicates", 0)),
        "placeholders": sm.get("placeholders", 0),
        "vlm_checked": sm.get("vlm_checked", 0),
        "staff_names": sm.get("staff_names", 0),
        "staff_photos": sm.get("staff_photos", 0),
        "non_target_skipped": sm.get("non_target_skipped", 0),
    }


# ============================================================
# Конструктор отчётов
# ============================================================

HTML_BLOCKS = [
    ("summary", "Сводка (KPI: всего сайтов, средний балл, критичные)"),
    ("sites_table", "Таблица сайтов (баллы и количество ошибок)"),
    ("details", "Детальный разбор по каждому сайту"),
    ("llm_summary", "Сводка ИИ по сайту"),
    ("explanation", "ИИ-объяснение ошибок простым языком"),
    ("history", "История изменений (динамика балла)"),
    ("photo_audit", "Фото-аудит (таблица проблемных портретов, без картинок)"),
    ("recommendations", "Рекомендации по исправлению"),
]

CSV_COLS = [
    ("col_site", "Сайт"),
    ("col_url", "URL сайта"),
    ("col_type", "Тип"),
    ("col_score", "Балл"),
    ("col_severity", "Важность"),
    ("col_agent", "Блок"),
    ("col_message", "Проблема"),
    ("col_recommendation", "Рекомендация"),
    ("col_page", "URL страницы с проблемой"),
]

DEFAULT_HTML_BLOCKS = [k for k, _ in HTML_BLOCKS]
DEFAULT_CSV_COLS = [k for k, _ in CSV_COLS]


def _get_report_config():
    s = db.get_settings()
    try:
        html_blocks = json.loads(s.get("report_html_blocks", "") or "null")
    except Exception:
        html_blocks = None
    if not html_blocks:
        html_blocks = DEFAULT_HTML_BLOCKS
    try:
        csv_cols = json.loads(s.get("report_csv_cols", "") or "null")
    except Exception:
        csv_cols = None
    if not csv_cols:
        csv_cols = DEFAULT_CSV_COLS
    include_ok = s.get("report_csv_ok", "1") == "1"
    return html_blocks, csv_cols, include_ok


# ============================================================
# Экспорт HTML (без картинок)
# ============================================================

def report_link(url, label):
    if not url.lower().startswith(("https://", "http://")):
        return ""
    return f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def export_html(blocks=None, selection=None):
    site_results = reports.latest() if selection is None else selection
    if not site_results:
        ui.notify("В выборке нет результатов.", type="warning")
        return
    stats = reports.stats(site_results)
    if blocks is None:
        blocks, _, _ = _get_report_config()
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    parts = []
    parts.append(
        "<!DOCTYPE html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"UTF-8\">\n"
        "<title>Отчет по аудиту сайтов — " + stamp + "</title>\n"
        "<style>\n"
        "body { font-family: 'Segoe UI', Arial, sans-serif; margin: 20px; background: #f5f5f5; color: #222; }\n"
        ".container { max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }\n"
        "h1 { color: #1976d2; border-bottom: 3px solid #1976d2; padding-bottom: 10px; }\n"
        "h2 { color: #1976d2; margin-top: 30px; border-left: 4px solid #1976d2; padding-left: 10px; }\n"
        "h3 { color: #424242; margin-top: 20px; }\n"
        ".stamp { background: #1976d2; color: white; display: inline-block; padding: 6px 14px; border-radius: 4px; font-weight: bold; margin: 10px 0; }\n"
        ".stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }\n"
        ".stat-card { background: #f9f9f9; padding: 15px; border-radius: 6px; text-align: center; border-left: 4px solid #1976d2; }\n"
        ".stat-value { font-size: 28px; font-weight: bold; color: #1976d2; }\n"
        ".stat-label { color: #757575; margin-top: 5px; font-size: 14px; }\n"
        "table { width: 100%; border-collapse: collapse; margin: 15px 0; font-size: 14px; }\n"
        "th, td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; vertical-align: top; }\n"
        "th { background: #1976d2; color: white; }\n"
        "tr:hover { background: #f5f5f5; }\n"
        ".severity-CRITICAL { color: #d32f2f; font-weight: bold; }\n"
        ".severity-HIGH { color: #f57c00; font-weight: bold; }\n"
        ".severity-MEDIUM { color: #fbc02d; font-weight: bold; }\n"
        ".severity-LOW { color: #388e3c; }\n"
        ".site-card { background: #fafafa; border: 1px solid #e0e0e0; border-radius: 6px; padding: 20px; margin: 20px 0; }\n"
        ".site-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e0e0e0; padding-bottom: 10px; margin-bottom: 15px; }\n"
        ".site-score { font-size: 32px; font-weight: bold; color: #1976d2; }\n"
        ".llm-box { background: #e3f2fd; border-left: 4px solid #1976d2; padding: 15px; margin: 15px 0; border-radius: 4px; white-space: pre-wrap; }\n"
        ".explain-box { background: #fff8e1; border-left: 4px solid #ff8f00; padding: 15px; margin: 15px 0; border-radius: 4px; white-space: pre-wrap; }\n"
        ".history-box { background: #f3e5f5; border-left: 4px solid #8e24aa; padding: 15px; margin: 15px 0; border-radius: 4px; }\n"
        ".photo-box { background: #e0f2f1; border-left: 4px solid #00897b; padding: 15px; margin: 15px 0; border-radius: 4px; }\n"
        ".no-errors { color: #388e3c; font-weight: bold; padding: 10px; background: #e8f5e9; border-radius: 4px; }\n"
        ".footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #ddd; color: #757575; font-size: 12px; text-align: center; }\n"
        "a { color: #1976d2; }\n"
        "@media print { body { background: white; } .container { box-shadow: none; } }\n"
        "</style>\n</head>\n<body>\n<div class=\"container\">\n"
        "<h1>Отчет по аудиту сайтов образовательных организаций</h1>\n"
        "<div class=\"stamp\">ОТЧЕТ СФОРМИРОВАН: " + stamp + "</div>\n"
    )

    if "summary" in blocks:
        crit = stats["severity_counts"].get("CRITICAL", 0)
        high = stats["severity_counts"].get("HIGH", 0)
        parts.append(
            "<div class=\"stats\">\n"
            "<div class=\"stat-card\"><div class=\"stat-value\">" + str(stats["total_sites"]) + "</div><div class=\"stat-label\">Всего сайтов</div></div>\n"
            "<div class=\"stat-card\"><div class=\"stat-value\">" + str(stats["avg_score"]) + "</div><div class=\"stat-label\">Средний балл</div></div>\n"
            "<div class=\"stat-card\"><div class=\"stat-value\">" + str(stats["critical_sites"]) + "</div><div class=\"stat-label\">Критичных</div></div>\n"
            "<div class=\"stat-card\"><div class=\"stat-value\">" + str(crit + high) + "</div><div class=\"stat-label\">Критичных + Высоких</div></div>\n"
            "</div>\n"
        )

    if "sites_table" in blocks:
        parts.append(
            "<h2>Сводка по сайтам</h2>\n<table>\n"
            "<tr><th>Сайт</th><th>Тип</th><th>Балл</th><th>Критических</th><th>Высоких</th><th>Средних</th><th>Низких</th><th>Всего ошибок</th></tr>\n"
        )
        for sr in sorted(site_results, key=lambda x: x["result"]["score"]):
            s = sr["site"]
            r = sr["result"]
            counts = reports.stats([sr])["severity_counts"]
            total_err = len(sr["findings"])
            parts.append(
                "<tr><td><b>" + escape(s["name"]) + "</b></td><td>" + escape(reports.KINDS.get(s["type"], s["type"])) + "</td>"
                "<td><b>" + str(r["score"]) + "</b></td>"
                "<td class=\"severity-CRITICAL\">" + str(counts["CRITICAL"]) + "</td>"
                "<td class=\"severity-HIGH\">" + str(counts["HIGH"]) + "</td>"
                "<td class=\"severity-MEDIUM\">" + str(counts["MEDIUM"]) + "</td>"
                "<td class=\"severity-LOW\">" + str(counts["LOW"]) + "</td>"
                "<td><b>" + str(total_err) + "</b></td></tr>"
            )
        parts.append("</table>")

    if "details" in blocks:
        parts.append("<h2>Детальный разбор по сайтам</h2>")
        for sr in sorted(site_results, key=lambda x: x["result"]["score"]):
            s = sr["site"]
            r = sr["result"]
            findings = sr["findings"]
            parts.append(
                "<div class=\"site-card\"><div class=\"site-header\">"
                "<div><h3 style=\"margin:0;\">" + escape(s["name"]) + "</h3>"
                "<p style=\"margin:5px 0 0 0; color:#757575;\">" + escape(s["url"]) + "</p>"
                "<p style=\"margin:2px 0 0 0; color:#757575;\">Тип: " + escape(reports.KINDS.get(s["type"], s["type"])) + "</p></div>"
                "<div class=\"site-score\">" + str(r["score"]) + "</div></div>"
            )

            if not sr.get("partial") and "llm_summary" in blocks and r.get("llm_summary"):
                parts.append("<div class=\"llm-box\"><b>Сводка ИИ:</b><br>" + escape(r["llm_summary"]) + "</div>")

            if not sr.get("partial") and "explanation" in blocks:
                explanation = r.get("ai_explanation")
                if explanation:
                    parts.append("<div class=\"explain-box\"><b>ИИ-объяснение для ответственного:</b><br>" + escape(explanation) + "</div>")

            if not sr.get("partial") and "history" in blocks:
                history = db.get_site_history(s["id"])
                if history:
                    parts.append("<div class=\"history-box\"><b>История проверок:</b><br>")
                    for h in history[-6:]:
                        parts.append(h["started_at"][:16].replace("T", " ") + " — балл: <b>" + str(h["score"]) + "</b><br>")
                    parts.append("</div>")

            if not sr.get("partial") and "photo_audit" in blocks:
                preport = vision.load_photo_report(s["id"])
                if preport and preport.get("images"):
                    sm = _photo_summary(preport.get("summary"))
                    bad = [im for im in preport["images"] if im.get("issues")]
                    parts.append(
                        "<div class=\"photo-box\"><b>Фото-аудит (портреты 714×952):</b> проверено " +
                        str(sm["total"]) + ", проблемных " + str(sm["bad"]) +
                        ", дубликатов " + str(sm["duplicates"]) +
                        ", заглушек " + str(sm["placeholders"]) +
                        ", игнорировано др. размеров " + str(sm["non_target_skipped"]) + "<br>"
                    )
                    if bad:
                        parts.append("<table><tr><th>Файл</th><th>Размер</th><th>Пропорция</th><th>Проблемы</th><th>Где</th></tr>")
                        for im in bad[:30]:
                            link = ""
                            if im.get("page_url"):
                                link = report_link(im["page_url"], "страница")
                            parts.append(
                                "<tr><td>" + escape(str(im.get("file", ""))) + "</td><td>" +
                                str(im.get("width", "?")) + "x" + str(im.get("height", "?")) + "</td><td>" +
                                str(im.get("ratio", "?")) + "</td><td>" + escape("; ".join(im.get("issues", []))) +
                                "</td><td>" + link + "</td></tr>"
                            )
                        parts.append("</table>")
                    parts.append("</div>")

            if not findings:
                parts.append("<div class=\"no-errors\">Проблем не найдено</div>")
            else:
                if "recommendations" in blocks:
                    parts.append("<table><tr><th>Важность</th><th>Блок</th><th>Проблема</th><th>Рекомендация</th><th>Где проблема</th></tr>")
                    for f in findings:
                        page_link = ""
                        if f.get("url"):
                            page_link = report_link(f["url"], "открыть")
                        parts.append(
                            "<tr><td class=\"severity-" + escape(f["severity"]) + "\">" + escape(reports.LEVELS.get(f["severity"], f["severity"])) + "</td>"
                            "<td>" + escape(reports.SECTIONS.get(f["agent"], f["agent"])) + "</td><td>" + escape(f["message"]) + "</td>"
                            "<td>" + escape(f["recommendation"]) + "</td><td>" + page_link + "</td></tr>"
                        )
                else:
                    parts.append("<table><tr><th>Важность</th><th>Блок</th><th>Проблема</th><th>Где проблема</th></tr>")
                    for f in findings:
                        page_link = ""
                        if f.get("url"):
                            page_link = report_link(f["url"], "открыть")
                        parts.append(
                            "<tr><td class=\"severity-" + escape(f["severity"]) + "\">" + escape(reports.LEVELS.get(f["severity"], f["severity"])) + "</td>"
                            "<td>" + escape(reports.SECTIONS.get(f["agent"], f["agent"])) + "</td><td>" + escape(f["message"]) + "</td><td>" + page_link + "</td></tr>"
                        )
                parts.append("</table>")
            parts.append("</div>")

    parts.append("<div class=\"footer\">Сформировано системой AI Audit Web • Штамп: " + stamp + "</div></div></body></html>")

    html = "".join(parts)
    filename = "audit_report_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".html"
    ui.download(html.encode("utf-8"), filename)
    ui.notify("HTML-отчет скачан", type="positive")


# ============================================================
# Экспорт CSV
# ============================================================

def _csv_value(col, s, r, f):
    if col == "col_site":
        return s["name"]
    if col == "col_url":
        return s["url"]
    if col == "col_type":
        return reports.KINDS.get(s["type"], s["type"])
    if col == "col_score":
        return r["score"]
    if col == "col_severity":
        return reports.LEVELS.get(f["severity"], f["severity"]) if f else "Без замечаний"
    if col == "col_agent":
        return reports.SECTIONS.get(f["agent"], f["agent"]) if f else "-"
    if col == "col_message":
        return f.get("message", "Проблем не найдено") if f else "Проблем не найдено"
    if col == "col_recommendation":
        return f.get("recommendation", "-") if f else "-"
    if col == "col_page":
        return (f.get("url", "") if f else "")
    return ""


def export_csv(cols=None, include_ok=None, selection=None):
    selection = reports.latest() if selection is None else selection
    if not selection:
        ui.notify("В выборке нет результатов.", type="warning")
        return
    _, saved_cols, saved_ok = _get_report_config()
    if cols is None:
        cols = saved_cols
    if include_ok is None:
        include_ok = saved_ok
    header_map = dict(CSV_COLS)
    header = [header_map[c] for c in cols if c in header_map]
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')
    writer.writerow(header)
    for row in selection:
        findings = row["findings"] or ([None] if include_ok else [])
        for finding in findings:
            values = [_csv_value(c, row["site"], row["result"], finding) for c in cols]
            writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@"))
                             and v != "-" else v for v in values])
    filename = "audit_errors_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
    ui.download(output.getvalue().encode("utf-8"), filename)
    ui.notify("CSV-таблица скачана (открывайте в Excel)", type="positive")


# ============================================================
# Фоновые задачи
# ============================================================

def job_panel(on_complete=None, site_id=None):
    ui.label("Вкладку можно закрыть. После остановки сервера продолжите последнее задание здесь. Новый запуск заменит его. Незаконченный запрос ИИ при продолжении может быть отправлен повторно.")
    line = ui.label().props("role=status")
    message = ui.label()
    last = None
    async def resume():
        try:
            await operations.start(resume_id=last[0])
            message.set_text("")
        except ValueError as error:
            message.set_text(str(error))
        refresh()
    button = ui.button("Продолжить незаконченный этап", on_click=resume).props("outline")
    def refresh():
        nonlocal last
        job = operations.status()
        state = job["state"]
        button.set_visibility(state in ("error", "interrupted"))
        if state == "idle":
            line.set_text("Заданий ещё не было.")
            last = (None, state)
            return
        elapsed = job["elapsed"] + (max(0, time.time()-job["updated"]) if state == "running" else 0)
        done, total = job["done"], job["total"]
        names = {"running":"Выполняется", "interrupted":"Прервано при остановке сервера",
                 "completed":"Задание завершено", "error":"Остановлено из-за ошибки"}
        parts = [operations.LABELS[job["mode"]], names[state], f"Этапов: {done} из {total}",
                 f"Время работы: {int(elapsed)//60} мин {int(elapsed)%60} с", job["detail"]]
        if state == "running" and 0 < done < total:
            parts.append(f"Осталось ориентировочно {round(job['elapsed'] / done * (total-done))} с по завершённым учреждениям")
        line.set_text(" · ".join(parts + job["notes"]))
        key = (job["id"], state)
        matches = site_id is None or any(step[0] == site_id for step in job["steps"])
        if on_complete and last is not None and last != key and state == "completed" and matches:
            on_complete()
        last = key
    refresh()
    ui.timer(2, refresh)


def has_cache(site_id):
    return bool(core.load_manifest(core.CACHE_DIR / f"site_{site_id}"))


# ============================================================
# Mailto
# ============================================================

def build_email_body(site, result, findings):
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines = []
    lines.append("Здравствуйте!")
    lines.append("")
    lines.append("По результатам автоматического мониторинга сайта " + site["name"] +
                 " (" + site["url"] + ") выявлены следующие замечания.")
    lines.append("Отчёт сформирован: " + stamp + ". Балл сайта: " + str(result["score"]) + " из 100.")
    lines.append("")
    if findings:
        lines.append("СПИСОК ЗАМЕЧАНИЙ (по важности):")
        for i, f in enumerate(findings, 1):
            lines.append(str(i) + ". [" + f["severity"] + "] " + f["message"])
            if f.get("url"):
                lines.append("    Где: " + f["url"])
            if f.get("recommendation"):
                lines.append("    Что сделать: " + f["recommendation"])
        lines.append("")
        lines.append("Просьба устранить замечания в течение 14 календарных дней.")
    else:
        lines.append("Замечаний не выявлено. Сайт соответствует требованиям.")
    lines.append("")
    lines.append("С уважением,")
    lines.append("Система ИИ-аудита сайтов Госвеб")
    return "\n".join(lines)


# ============================================================
# UI helpers
# ============================================================

def layout(title):
    ui.add_head_html("<style>" + (Path(__file__).with_name("nicegui-theme.css")).read_text(encoding="utf-8") + "</style>")
    ui.add_head_html('<link rel="stylesheet" href="/navigation.css">')
    with ui.header().classes("audit-header"):
        ui.html(navigation("/quick", ai_status()), sanitize=False)
    with ui.row().classes("audit-subnav"):
        for label, path in (("Обзор", "/quick"), ("Запуск", "/quick/run"), ("Отчёты", "/report"),
                            ("Разбор с ИИ", "/quick/ai"), ("Учреждения", "/sites"), ("Правила", "/rules"),
                            ("Параметры обхода", "/settings")):
            link = ui.link(label, path)
            if ui.context.client.request.url.path == path:
                link.props('aria-current="page"')
    with ui.row().classes("audit-title-row w-full px-4 py-2"):
        ui.label(title).classes("text-h5 font-semibold mt-4")


def stat_card(label, value):
    with ui.card().classes("audit-kpi flex-1 min-w-40 p-4"):
        ui.label(label).classes("text-caption text-grey-7")
        ui.label(str(value)).classes("text-h5 font-bold text-primary")


# ============================================================
# Дашборд
# ============================================================

def site_options(sites):
    return {s["id"]: f'{s["name"]} · {s["url"]}' for s in sites}


@ui.page("/quick")
def dashboard():
    layout("Экспресс-проверка сайтов")
    sites = db.get_sites()
    stats = reports.stats(reports.latest())
    with ui.column().classes("audit-container w-full gap-4"):
        with ui.row().classes("w-full gap-3"):
            stat_card("В реестре", len(sites))
            stat_card("Есть результат проверки", stats["total_sites"])
            stat_card("Учреждений с критическими замечаниями", stats["critical_sites"])
        with ui.element("div").classes("quick-actions"):
            for title, description, action, path in (
                ("Проверка по правилам", "Проверить сайты или сохранённые страницы. Подключение ИИ не требуется.", "Выбрать учреждения", "/quick/run"),
                ("Отчёты", "Посмотреть замечания по учреждениям, выбрать фильтры и скачать результаты.", "Открыть отчёты", "/report"),
                ("Разбор отчёта с ИИ", "Получить краткую сводку или план исправления по замечаниям алгоритма.", "Перейти к разбору", "/quick/ai")):
                with ui.card().classes("p-5 gap-3"):
                    ui.label(title).classes("text-h6")
                    ui.label(description)
                    ui.link(action, path).classes("quick-action-link")


@ui.page("/quick/run")
def run_page():
    layout("Проверка по правилам")
    sites = db.get_sites()
    with ui.column().classes("audit-container w-full gap-4"):
        ui.label("Выберите одно или несколько учреждений. Проверка сайтов загружает свежие страницы и может занять несколько минут на учреждение.")
        chosen = ui.select(site_options(sites), multiple=True, with_input=True, label="Название или адрес сайта").props("outlined use-chips").classes("w-full")
        status = ui.label().props("role=status")
        def count():
            status.set_text(f"Выбрано учреждений: {len(chosen.value or [])}")
        chosen.on_value_change(count)
        async def run(offline):
            selected = [s for s in db.get_sites() if s["id"] in (chosen.value or [])]
            if not selected or audit_lock.locked():
                status.set_text("Выберите учреждения." if not selected else "Сейчас уже идёт проверка.")
                return
            try:
                await operations.start("cache" if offline else "online", selected)
                status.set_text("Задание принято. Ход проверки показан ниже.")
            except ValueError as error:
                status.set_text(str(error))
        with ui.row():
            ui.button("Выбрать все", on_click=lambda: chosen.set_value(list(site_options(sites)))).props("flat")
            ui.button("Проверить сайты", on_click=lambda: run(False))
            ui.button("Проверить сохранённые страницы", on_click=lambda: run(True)).props("outline")
        count()
        job_panel()
        ui.link("Посмотреть результаты", "/report")


@ui.page("/sites")
def sites_page():
    layout("Учреждения")
    with ui.column().classes("audit-container w-full"):
        sites = db.get_sites()
        selector = ui.select(site_options(sites), with_input=True, label="Название или адрес сайта").props("outlined").classes("w-full")
        selector.on_value_change(lambda e: ui.navigate.to(f"/site/{e.value}") if e.value else None)
        ui.link("Управление списком учреждений", "/admin#registry")


@ui.page("/site/{site_id}")
def site_detail(site_id: int):
    site = db.get_site(site_id)
    if not site:
        layout("Сайт не найден")
        ui.label("Сайт не найден").classes("text-h6 text-red-600 px-4")
        return

    layout(site["name"])
    progress = ui.label().props("role=status").classes("audit-subnav")
    job_status = ui.column().classes("audit-container w-full gap-1")
    container = ui.column().classes("audit-container w-full gap-4 px-4")

    async def run_check(offline):
        try:
            await operations.start("cache" if offline else "online", [site])
            progress.set_text("Задание принято.")
        except ValueError as error:
            progress.set_text(str(error))

    def render():
        container.clear()
        result = db.get_last_site_result(site_id)
        site_now = db.get_site(site_id) or site

        with container:
            with ui.row().classes("w-full gap-2 items-center flex-wrap"):
                ui.link("← К учреждениям", "/sites").classes("text-primary")
                linked_id = archive_id(site["url"])
                if linked_id is not None:
                    ui.link("Сводная карточка учреждения", f"/archive#institution/{linked_id}")
                ui.button("Проверить сайт", on_click=lambda: run_check(False)).props("color=primary icon=cloud_download")
                cached = ui.button("Проверить сохранённые страницы", on_click=lambda: run_check(True)).props("color=teal icon=flash_on")
                if not has_cache(site_id):
                    cached.disable()
                ui.button("Обновить", on_click=render).props("outline icon=refresh")

            with ui.card().classes("w-full p-4"):
                ui.label("Контактное лицо учреждения").classes("text-subtitle1 font-bold mb-2")
                email_input = ui.input("E-mail для отправки отчётов", value=site_now.get("email", "") or "").classes("w-full")

                def save_email():
                    db.update_site_email(site_id, email_input.value.strip())
                    ui.notify("E-mail сохранён", type="positive")

                ui.button("Сохранить e-mail", on_click=save_email).props("color=primary size=sm icon=save")

            if not result:
                ui.label("Результатов пока нет. Нажмите «Проверить сайт».").classes("text-grey-7 mt-4")
                return

            with ui.row().classes("w-full gap-4"):
                stat_card("Балл", result["score"])
                stat_card("Критических", result["critical"])
                stat_card("Высокой важности", result["high"])
                stat_card("Средней важности", result["medium"])
                stat_card("Низкой важности", result["low"])

            try:
                mj = json.loads(result.get("metrics_json") or "{}")
            except Exception:
                mj = {}
            if mj.get("detected_type"):
                same = mj.get("detected_type") == mj.get("declared_type")
                ui.label(("Тип определён верно: " + mj["detected_type"]) if same else
                         ("⚠ Объявлен «" + str(mj.get("declared_type")) + "», по структуре — «" +
                          str(mj.get("detected_type")) + "». Проверьте карточку сайта.")).classes(
                    "text-caption " + ("text-green-7" if same else "text-amber-8"))

            if result.get("llm_summary"):
                with ui.card().classes("w-full p-4 bg-blue-50"):
                    ui.label("Сводка ИИ").classes("text-subtitle1 font-bold mb-2")
                    ui.label(result["llm_summary"]).classes("whitespace-pre-wrap text-grey-800")

            findings = db.get_site_findings(result["run_id"], site_id)

            if site_now.get("email"):
                body = build_email_body(site_now, result, findings)
                subject = "Отчёт по аудиту сайта " + site_now["name"]
                mailto = ("mailto:" + site_now["email"] + "?subject=" + quote(subject) + "&body=" + quote(body))
                with ui.card().classes("w-full p-4 bg-green-50"):
                    ui.label("Отправка отчёта учреждению").classes("text-subtitle1 font-bold mb-2")
                    ui.label("Откроется почтовое приложение с готовым текстом замечаний.").classes("text-caption text-grey-7")
                    ui.link("Написать в " + site_now["email"], mailto).props("color=green icon=mail").classes("text-subtitle1")

            with ui.card().classes("w-full p-4"):
                ui.label("Найденные проблемы").classes("text-subtitle1 font-bold mb-2")
                if not findings:
                    ui.label("Проблем не найдено! Сайт в отличном состоянии.").classes("text-green-7 font-semibold")
                else:
                    rows = [{"id": f.get("id", i), "severity": reports.LEVELS.get(f["severity"], f["severity"]),
                             "agent": reports.SECTIONS.get(f["agent"], f["agent"]),
                             "message": f["message"], "recommendation": f["recommendation"],
                             "page": f.get("url", "") or "—"} for i, f in enumerate(findings)]
                    with ui.table(columns=[
                        {"name": "severity", "label": "Важность", "field": "severity"},
                        {"name": "agent", "label": "Блок", "field": "agent"},
                        {"name": "message", "label": "Проблема", "field": "message", "align": "left"},
                        {"name": "recommendation", "label": "Рекомендация", "field": "recommendation", "align": "left"},
                        {"name": "page", "label": "Где проблема", "field": "page", "align": "left"},
                    ], rows=rows, row_key="id") as table:
                        table.add_slot("page", '''
                            <a v-if="props.row.page !== '—'" :href="props.row.page" target="_blank"
                               style="color:#1976d2; text-decoration:underline;">открыть страницу</a>
                            <span v-else>—</span>
                        ''')
                    ui.label("Столбец «Где проблема» — прямая ссылка на страницу сайта.").classes("text-caption text-grey-7")

            ui.link("Разобрать этот отчёт с ИИ", f"/quick/ai?site_id={site_id}")

            # ---------- Фото-аудит ----------
            with ui.card().classes("w-full p-4 bg-teal-50"):
                ui.label("Фото-аудит (алгоритмический, портреты 714×952)").classes("text-subtitle1 font-bold mb-2")
                preport = vision.load_photo_report(site_id)
                ref = vision.get_reference_info()
                ref_text = ("Эталон загружен: пропорция " + str(ref.get("ratio")) +
                            ", позиция лица " + str(ref.get("face"))) if ref else \
                    "Эталон не загружен — базовый стандарт (портрет 3:4, лицо по центру). Загрузите образец в Правилах."
                ui.label(ref_text).classes("text-caption text-grey-7")

                if preport and preport.get("images") is not None:
                    sm = _photo_summary(preport.get("summary"))
                    ui.label("Портретов 714×952 проверено: " + str(sm["total"]) +
                             " | Проблемных: " + str(sm["bad"]) +
                             " | Дубликатов: " + str(sm["duplicates"]) +
                             " | Заглушек: " + str(sm["placeholders"]) +
                             " | Игнорировано фото др. размеров: " + str(sm["non_target_skipped"])).classes("font-bold mt-2")
                    bad = [im for im in preport["images"] if im.get("issues")]
                    if bad:
                        ui.table(columns=[
                            {"name": "file", "label": "Файл", "field": "file"},
                            {"name": "size", "label": "Размер", "field": "size"},
                            {"name": "ratio", "label": "Пропорция", "field": "ratio"},
                            {"name": "issues", "label": "Проблемы", "field": "issues", "align": "left"},
                            {"name": "page", "label": "Где", "field": "page", "align": "left"},
                        ], rows=[{"file": im.get("file", ""), "size": str(im.get("width", "?")) + "x" + str(im.get("height", "?")),
                                  "ratio": im.get("ratio", "?"), "issues": "; ".join(im.get("issues", [])),
                                  "page": im.get("page_url", "") or "—"} for im in bad], row_key="file").classes("w-full")
                    else:
                        ui.label("Все портреты 714×952 соответствуют стандарту.").classes("text-green-7 font-semibold")
                else:
                    ui.label("Фото-анализ появится после полного аудита (качает и замеряет фото).").classes("text-grey-7")

            # ---------- История изменений ----------
            with ui.card().classes("w-full p-4"):
                ui.label("История изменений").classes("text-subtitle1 font-bold mb-2")
                history = db.get_site_history(site_id)
                if not history:
                    ui.label("История появится после первого аудита.").classes("text-grey-7")
                else:
                    dates = [h["started_at"][:16].replace("T", " ") for h in history]
                    scores = [h["score"] for h in history]
                    ui.echart({"tooltip": {"trigger": "axis"},
                               "xAxis": {"type": "category", "data": dates},
                               "yAxis": {"type": "value", "min": 0, "max": 100},
                               "series": [{"type": "line", "data": scores, "smooth": True,
                                           "areaStyle": {}, "itemStyle": {"color": "#1976d2"}}]}).classes("h-56")
                    rows = [{"idx": i, "date": h["started_at"][:16].replace("T", " "), "score": h["score"],
                             "critical": h["critical"], "high": h["high"], "medium": h["medium"], "low": h["low"]}
                            for i, h in enumerate(reversed(history), 1)]
                    ui.table(columns=[
                        {"name": "date", "label": "Дата проверки", "field": "date"},
                        {"name": "score", "label": "Балл", "field": "score"},
                        {"name": "critical", "label": "Critical", "field": "critical"},
                        {"name": "high", "label": "High", "field": "high"},
                        {"name": "medium", "label": "Medium", "field": "medium"},
                        {"name": "low", "label": "Low", "field": "low"},
                    ], rows=rows, row_key="idx").classes("w-full")
                    diff = db.get_findings_diff(site_id)
                    if diff:
                        fixed, new, unchanged = diff
                        with ui.row().classes("w-full gap-4 mt-2 flex-wrap"):
                            with ui.column().classes("flex-1 min-w-60 gap-0"):
                                ui.label("Исправлено с прошлой проверки: " + str(len(fixed))).classes("text-green-7 font-bold")
                                for f in (fixed[:10] or []):
                                    ui.label("• " + f["message"]).classes("text-caption")
                                if not fixed:
                                    ui.label("• ничего").classes("text-caption text-grey-6")
                            with ui.column().classes("flex-1 min-w-60 gap-0"):
                                ui.label("Появилось нового: " + str(len(new))).classes("text-red-7 font-bold")
                                for f in (new[:10] or []):
                                    ui.label("• " + f["message"]).classes("text-caption")
                                if not new:
                                    ui.label("• ничего").classes("text-caption text-grey-6")
                        ui.label("Без изменений осталось: " + str(unchanged)).classes("text-grey-7 mt-2")
                    else:
                        ui.label("Сравнение «было / стало» появится после второй проверки сайта.").classes("text-grey-7 mt-2")

    render()
    with job_status:
        job_panel(on_complete=render, site_id=site_id)


# ============================================================
# Правила (строки редактируются на месте)
# ============================================================

@ui.page("/rules")
def rules_page():
    layout("Правила проверки")
    container = ui.column().classes("audit-container w-full gap-4 px-4")

    def render():
        container.clear()
        all_rules = db.get_all_audit_rules_raw()
        targets = db.get_photo_targets(active_only=False)
        sections_school = [r for r in all_rules if r["rule_type"] == "section" and r["rule_key"] == "school"]
        sections_kinder = [r for r in all_rules if r["rule_type"] == "section" and r["rule_key"] == "kindergarten"]
        docs_school = [r for r in all_rules if r["rule_type"] == "document" and r["rule_key"] == "school"]
        docs_kinder = [r for r in all_rules if r["rule_type"] == "document" and r["rule_key"] == "kindergarten"]
        placeholders = [r for r in all_rules if r["rule_type"] == "placeholder"]

        with container:
            ui.label("Каждая строка редактируется на месте. Синонимы одного документа/раздела "
                     "перечисляйте через | или ; прямо в строке — достаточно встретить ЛЮБОЙ вариант. "
                     "Проверка ищет указанные вами варианты. "
                     "Изменения применяются при следующем аудите (по кэшу — быстро).").classes("text-grey-7")

            def save_rule(rule_id, input_el):
                val = (input_el.value or "").strip()
                if not val:
                    ui.notify("Значение не может быть пустым", type="negative")
                    return
                db.update_audit_rule(rule_id, val)
                ui.notify("Строка сохранена", type="positive")
                render()

            def add_rule(rule_type, rule_key, input_el):
                val = (input_el.value or "").strip()
                if not val:
                    ui.notify("Введите значение", type="negative")
                    return
                db.add_audit_rule(rule_type, rule_key, val)
                ui.notify("Правило добавлено", type="positive")
                render()

            def delete_rule(rule_id):
                db.delete_audit_rule(rule_id)
                ui.notify("Правило удалено", type="info")
                render()

            def rule_block(title, rules, rule_type, rule_key, add_label, hint=""):
                with ui.card().classes("w-full p-4"):
                    ui.label(title).classes("text-subtitle1 font-bold mb-2")
                    if hint:
                        ui.label(hint).classes("text-caption text-grey-7 mb-2")
                    if not rules:
                        ui.label("Пока пусто.").classes("text-grey-6")
                    for r in rules:
                        with ui.row().classes("w-full items-center gap-2 py-1 border-b"):
                            inp = ui.input("", value=r["rule_value"]).classes("flex-1")
                            ui.button("Сохранить", on_click=lambda rid=r["id"], el=inp: save_rule(rid, el)).props("color=primary size=sm icon=save")
                            ui.button("Удалить", on_click=lambda rid=r["id"]: delete_rule(rid)).props("flat color=red size=sm")
                    new_val = ui.input(add_label).classes("w-full mt-2")
                    ui.button("Добавить", on_click=lambda: add_rule(rule_type, rule_key, new_val)).props("color=primary size=sm")

            doc_hint = ("Синонимы через | или ; в одной строке, например: "
                        "Режим дня | Правила внутреннего распорядка воспитанников | Распорядок дня. "
                        "Чтобы исправить уже существующий тип — отредактируйте его строку кнопкой «Сохранить», "
                        "а не добавляйте новую (иначе старая строка без синонимов останется и даст ложное отсутствие).")

            rule_block("Обязательные разделы — ШКОЛЫ", sections_school, "section", "school", "Новый раздел")
            rule_block("Обязательные разделы — ДЕТСКИЕ САДЫ", sections_kinder, "section", "kindergarten", "Новый раздел")
            rule_block("Обязательные документы — ШКОЛЫ", docs_school, "document", "school", "Новый документ", doc_hint)
            rule_block("Обязательные документы — ДЕТСКИЕ САДЫ", docs_kinder, "document", "kindergarten", "Новый документ", doc_hint)
            rule_block("Маркеры текстовых заглушек", placeholders, "placeholder", "text", "Новый маркер")

            with ui.card().classes("w-full p-4 bg-teal-50"):
                ui.label("📷 Фото: в каких разделах искать + эталон").classes("text-subtitle1 font-bold mb-2")
                ui.label("Проверяются только портреты целевого размера (по умолчанию 714×952, меняется в Настройках). "
                         "«Требовать лица» — сверять число ФИО и число портретов в разделе.").classes("text-caption text-grey-7")

                for t in targets:
                    with ui.row().classes("w-full items-center justify-between py-1 border-b"):
                        with ui.column().classes("gap-0"):
                            ui.label(t["section_label"]).classes("font-bold")
                            ui.label("ключ: " + t["section_key"] + " • маркеры страниц: " +
                                     (t["page_keywords"] or "(главная)") + " • лица: " +
                                     ("да" if t["require_faces"] else "нет")).classes("text-caption text-grey-6")
                        with ui.row().classes("items-center gap-2"):
                            sw = ui.switch("", value=bool(t["active"]))
                            sw.on_value_change(lambda e, tid=t["id"]: db.set_photo_target_active(tid, e.value))
                            ui.button("Удалить", on_click=lambda t=t: delete_target(t["id"])).props("flat color=red size=sm")

                with ui.row().classes("w-full gap-2 mt-2 items-end flex-wrap"):
                    nt_key = ui.input("ключ (lat, напр. staff)").classes("w-40")
                    nt_label = ui.input("Название раздела").classes("w-56")
                    nt_kw = ui.input("Маркеры url/текста (через запятую)").classes("flex-1 min-w-60")
                    nt_faces = ui.checkbox("Требовать лица", value=True)
                    ui.button("Добавить раздел", on_click=lambda: add_target(nt_key, nt_label, nt_kw, nt_faces)).props("color=primary size=sm")

                def add_target(k_el, l_el, kw_el, f_el):
                    if not (k_el.value or "").strip() or not (l_el.value or "").strip():
                        ui.notify("Нужны ключ и название", type="negative")
                        return
                    db.add_photo_target(k_el.value, l_el.value, kw_el.value or "", f_el.value)
                    ui.notify("Раздел для фото добавлен", type="positive")
                    render()

                def delete_target(tid):
                    db.delete_photo_target(tid)
                    ui.notify("Раздел удалён", type="info")
                    render()

                ui.separator().classes("my-3")
                ref = vision.get_reference_info()
                if ref:
                    ui.label("Текущий эталон: пропорция " + str(ref.get("ratio")) +
                             ", ориентация " + str(ref.get("orientation")) +
                             ", позиция лица: " + str(ref.get("face"))).classes("text-caption text-teal-7")
                else:
                    ui.label("Эталон не загружен — базовый стандарт (портрет 3:4, лицо по центру).").classes("text-caption text-grey-7")

                async def handle_ref(e):
                    try:
                        data = await e.file.read()
                        m = vision.save_reference_bytes(data, e.file.name)
                        ui.notify("Эталон сохранён: пропорция " + str(m["ratio"]) + ", лицо: " + str(m["face"]), type="positive")
                        render()
                    except Exception as ex:
                        ui.notify("Не удалось сохранить эталон: " + str(ex), type="negative")

                ui.upload(label="Загрузить эталонное фото (образец стандарта)",
                          auto_upload=True, on_upload=handle_ref).props("accept='image/*'").classes("w-full mt-2")
                ui.link("Повторить проверку сохранённых страниц с новыми правилами", "/quick/run")

    render()


# ============================================================
# Конструктор отчётов
# ============================================================

@ui.page("/report")
def report_page():
    layout("Отчёты по алгоритмической проверке")
    source = reports.latest()
    sites = db.get_sites()
    findings = [f for row in source for f in row["findings"]]
    sections = {f["agent"]: reports.SECTIONS.get(f["agent"], f["agent"]) for f in findings}
    codes = {f["code"]: reports.CHECKS.get(f["code"], f["message"].split(":")[0][:100]) for f in findings}
    saved_html, saved_csv, saved_ok = _get_report_config()
    with ui.column().classes("audit-container w-full gap-4"):
        ui.label("Для каждого учреждения показан последний завершённый результат. Источник замечаний — алгоритм. Балл относится ко всей проверке сайта.")
        with ui.card().classes("w-full p-4"):
            ids = ui.select(site_options(sites), multiple=True, with_input=True, label="Учреждения · название или URL").props("outlined use-chips").classes("w-full")
            with ui.row().classes("w-full gap-3"):
                kind = ui.select(reports.KINDS, label="Тип учреждения").props("outlined clearable").classes("flex-1 min-w-40")
                section = ui.select(sections, label="Раздел проверки").props("outlined clearable").classes("flex-1 min-w-40")
                level = ui.select(reports.LEVELS, label="Серьёзность").props("outlined clearable").classes("flex-1 min-w-40")
                code = ui.select(codes, label="Тип замечания", with_input=True).props("outlined clearable").classes("flex-1 min-w-40")
            with ui.expansion("Период и результат проверки").classes("w-full"):
                with ui.row().classes("w-full"):
                    since = ui.input("Проверено с").props("type=date outlined stack-label")
                    until = ui.input("Проверено по").props("type=date outlined stack-label")
                    outcome = ui.select({"issue":"Есть замечания", "clear":"Без замечаний"}, label="Результат").props("outlined clearable").classes("min-w-48")
        def selection():
            return reports.select(source, ids.value or [], kind.value, level.value, section.value, code.value,
                                  since.value, until.value, outcome.value)

        with ui.expansion("Настроить состав файлов HTML и CSV").classes("w-full"):
            html_checks, csv_checks = {}, {}
            with ui.row():
                with ui.column():
                    ui.label("Разделы HTML")
                    for key, label in HTML_BLOCKS:
                        html_checks[key] = ui.checkbox(label, value=key in saved_html)
                with ui.column():
                    ui.label("Столбцы CSV")
                    for key, label in CSV_COLS:
                        csv_checks[key] = ui.checkbox(label, value=key in saved_csv)
                    ok_switch = ui.checkbox("Включать сайты без замечаний", value=saved_ok)

            def save():
                db.save_settings({"report_html_blocks": json.dumps(keys(html_checks)), "report_csv_cols": json.dumps(keys(csv_checks)),
                                  "report_csv_ok": "1" if ok_switch.value else "0"})
                ui.notify("Настройки выгрузки сохранены.")
            ui.button("Сохранить состав по умолчанию", on_click=save).props("outline")
        def keys(checks):
            return [key for key, checkbox in checks.items() if checkbox.value]
        def download(html):
            chosen = keys(html_checks if html else csv_checks)
            if not chosen:
                ui.notify("Выберите хотя бы один раздел или столбец.", type="warning")
                return
            if html:
                export_html(chosen, selection())
            else:
                export_csv(chosen, ok_switch.value, selection())

        content = ui.column().classes("w-full gap-3")
        def draw():
            content.clear()
            rows = selection()
            stats = reports.stats(rows)
            with content:
                if since.value and until.value and since.value > until.value:
                    ui.label("Начальная дата позже конечной. Исправьте период.").props("role=alert")
                    return
                with ui.row().classes("w-full gap-3"):
                    stat_card("Учреждений в выборке", len(rows))
                    stat_card("Замечаний", stats["total_findings"])
                    stat_card("Средний балл полной проверки", stats["avg_score"])
                if not rows:
                    ui.label("Нет завершённых проверок по выбранным условиям.")
                    ui.link("Перейти к запуску", "/quick/run")
                    return
                with ui.row():
                    ui.button("Скачать HTML", on_click=lambda: download(True)).props("outline")
                    ui.button("Скачать CSV", on_click=lambda: download(False)).props("outline")
                details = []
                for row in rows:
                    for f in row["findings"] or [{}]:
                        details.append({"key": str(row["site"]["id"]) + "/" + str(f.get("id", 0)),
                            "site_id": row["site"]["id"], "name": row["site"]["name"],
                            "date": (row["result"]["finished_at"] or "")[:16].replace("T", " "),
                            "level": reports.LEVELS.get(f.get("severity"), "—"),
                            "section": sections.get(f.get("agent"), "—"), "message": f.get("message", "Без замечаний"),
                            "next": f.get("recommendation", "")})
                columns = [{"name": k, "field": k, "label": label, "align": "left", "sortable": True}
                           for k, label in (("name", "Учреждение"), ("date", "Проверено"), ("level", "Серьёзность"),
                           ("section", "Раздел"), ("message", "Замечание"), ("next", "Что исправить"))]
                table = ui.table(columns=columns, rows=details, row_key="key", pagination=12).classes("quick-report w-full")
                table.add_slot("body-cell-name", """<q-td :props="props"><a :href="'/site/' + props.row.site_id">{{props.value}}</a></q-td>""")
        for widget in (ids, kind, section, level, code, since, until, outcome):
            widget.on_value_change(draw)
        draw()


@ui.page("/quick/ai")
def ai_report_page(site_id: int = 0):
    layout("Разбор отчёта с ИИ")
    rows = {row["site"]["id"]: row for row in reports.latest()}
    with ui.column().classes("audit-container w-full gap-4"):
        ui.label("Модель получает название учреждения и до 25 замечаний последней проверки, начиная с самых серьёзных.")
        selected = ui.select(site_options([row["site"] for row in rows.values()]), value=site_id if site_id in rows else None,
                             label="Учреждение с готовым отчётом", with_input=True).props("outlined clearable").classes("w-full")
        panel = ui.column().classes("w-full")
        def draw():
            panel.clear()
            row = rows.get(selected.value)
            with panel:
                reason = ai_status()
                configured = reason == "ИИ настроен"
                if not configured:
                    ui.label(reason)
                    ui.link("Настроить подключение", "/admin#model")
                if row is None:
                    ui.label("Выберите учреждение." if rows else "Пока нет готовых отчётов.")
                    ui.link("Перейти к алгоритмической проверке", "/quick/run")
                else:
                    result = row["result"]
                    ui.label(f"Проверено {(result['finished_at'] or '')[:16].replace('T',' ')} · Замечаний: {len(row['findings'])}")
                    ui.link("Открыть карточку учреждения", f"/site/{row['site']['id']}")
                status = ui.label().props("role=status")
                async def generate(summary):
                    if not row:
                        return
                    try:
                        await operations.start("summary" if summary else "explain", [row["site"]], result_id=result["id"])
                        status.set_text("Запрос принят. Ответ будет сохранён в этом отчёте.")
                    except ValueError as error:
                        status.set_text(str(error))
                with ui.row():
                    for title, summary in (("Краткая сводка с ИИ", True), ("План исправления с ИИ", False)):
                        with ui.element("span") as wrapper:
                            button = ui.button(title, on_click=lambda summary=summary: generate(summary))
                            if not row or not configured:
                                button.disable()
                                wrapper.props("tabindex=0")
                                ui.tooltip(reason if not configured else "Выберите готовый отчёт.")
                if row:
                    for label, value in (("Сводка ИИ", result.get("llm_summary")), ("План исправления", result.get("ai_explanation"))):
                        if value:
                            with ui.card().classes("w-full p-4"):
                                ui.label(label).classes("text-h6")
                                ui.label(value).classes("whitespace-pre-wrap")
                    legacy = llm.load_explanation(row["site"]["id"])
                    if legacy:
                        with ui.expansion("Архивное пояснение · дата проверки неизвестна"):
                            ui.label(legacy).classes("whitespace-pre-wrap")
        selected.on_value_change(draw)
        draw()
        def refresh_answers():
            rows.update({row["site"]["id"]: row for row in reports.latest()})
            draw()
        job_panel(on_complete=refresh_answers)


@ui.page("/settings")
def settings_page():
    layout("Настройки")
    s = db.get_settings()
    with ui.column().classes("audit-container w-full gap-4 px-4"):
        ui.link("Подключение ИИ для обоих режимов", "/admin#model")
        with ui.card().classes("w-full p-4"):
            ui.label("Настройки краулера (HTTP, без браузера)").classes("text-subtitle1 font-bold mb-2")
            extra_hosts = ui.textarea("Дополнительные разрешённые хосты (через запятую)", value=s.get("extra_hosts", "")).classes("w-full")
            if demo_settings.directory():
                extra_hosts.disable()
            max_pages = ui.number("Максимум страниц на один сайт", value=int(s.get("max_pages", "40")), min=1, max=200).classes("w-full mt-2")
            news_age = ui.number("Через сколько дней новость считается устаревшей",
                                 value=int(s.get("news_max_age_days", "14")), min=1, max=365).classes("w-full mt-2")
            auto_type = ui.switch("Автоопределять тип учреждения по структуре сайта",
                                  value=s.get("auto_detect_type", "1") == "1")
            target_sizes = ui.input("Целевые размеры портретов (напр. 714x952, 800x1067)",
                                    value=s.get("photo_target_sizes", "714x952")).classes("w-full mt-2")
        def save():
            for field, name, limit in ((max_pages, "Максимум страниц", 200), (news_age, "Возраст новости", 365)):
                value = field.value
                if not isinstance(value, (int, float)) or not 1 <= value <= limit or int(value) != value:
                    ui.notify(f"Укажите целое число от 1 до {limit} в поле «{name}».", type="warning")
                    return
            if not vision.parse_target_sizes(target_sizes.value or ""):
                ui.notify("Укажите размеры фотографий, например 714x952, 800x1067.", type="warning")
                return
            db.save_settings({
                "extra_hosts": s.get("extra_hosts","") if demo_settings.directory() else extra_hosts.value.strip(), "max_pages": str(int(max_pages.value)),
                "news_max_age_days": str(int(news_age.value)),
                "auto_detect_type": "1" if auto_type.value else "0",
                "photo_target_sizes": target_sizes.value.strip(),
            })
            ui.notify("Настройки сохранены!", type="positive")

        ui.button("Сохранить", on_click=save).props("color=primary icon=save")


# ============================================================
# API
# ============================================================

@app.post("/api/run")
async def api_run():
    try:
        job_id = await operations.start("online", db.get_sites(active_only=True))
        return {"status": "started", "id": job_id}
    except ValueError as error:
        return {"status": "already_running" if audit_lock.locked() else "unavailable", "detail": str(error)}


@app.post("/api/run_cache")
async def api_run_cache():
    try:
        job_id = await operations.start("cache", db.get_sites(active_only=True))
        return {"status": "started", "id": job_id}
    except ValueError as error:
        return {"status": "already_running" if audit_lock.locked() else "unavailable", "detail": str(error)}


# ============================================================
# Запуск сервера
# ============================================================

logging.getLogger("nicegui").setLevel(logging.ERROR)
logging.getLogger("engineio").setLevel(logging.CRITICAL)
logging.getLogger("socketio").setLevel(logging.CRITICAL)
