import asyncio
import csv
import io
import json
import logging
from html import escape
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote

from nicegui import ui, app

from . import db
from . import core
from . import llm
from . import vision

from .integration import archive_id, audit_settings, ai_status
from Engine.portal_nav import navigation

audit_lock = asyncio.Lock()


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
    ("llm_summary", "LLM-сводка по сайту"),
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


def export_html(blocks=None):
    stats = db.dashboard_stats()
    if not stats["has_run"]:
        ui.notify("Сначала запустите аудит.", type="warning")
        return
    if blocks is None:
        blocks, _, _ = _get_report_config()

    last_run_id = stats["last_run"]["id"]
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    site_results = []
    for s in db.get_sites():
        r = db.get_last_site_result(s["id"])
        if r and r.get("run_id") == last_run_id:
            findings = db.get_site_findings(last_run_id, s["id"])
            site_results.append({"site": s, "result": r, "findings": findings})

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
            "<tr><th>Сайт</th><th>Тип</th><th>Балл</th><th>Critical</th><th>High</th><th>Medium</th><th>Low</th><th>Всего ошибок</th></tr>\n"
        )
        for sr in sorted(site_results, key=lambda x: x["result"]["score"]):
            s = sr["site"]
            r = sr["result"]
            total_err = r["critical"] + r["high"] + r["medium"] + r["low"]
            parts.append(
                "<tr><td><b>" + escape(s["name"]) + "</b></td><td>" + escape(s["type"]) + "</td>"
                "<td><b>" + str(r["score"]) + "</b></td>"
                "<td class=\"severity-CRITICAL\">" + str(r["critical"]) + "</td>"
                "<td class=\"severity-HIGH\">" + str(r["high"]) + "</td>"
                "<td class=\"severity-MEDIUM\">" + str(r["medium"]) + "</td>"
                "<td class=\"severity-LOW\">" + str(r["low"]) + "</td>"
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
                "<p style=\"margin:2px 0 0 0; color:#757575;\">Тип: " + escape(s["type"]) + "</p></div>"
                "<div class=\"site-score\">" + str(r["score"]) + "</div></div>"
            )

            if "llm_summary" in blocks and r.get("llm_summary"):
                parts.append("<div class=\"llm-box\"><b>LLM-сводка:</b><br>" + escape(r["llm_summary"]) + "</div>")

            if "explanation" in blocks:
                explanation = llm.load_explanation(s["id"])
                if explanation:
                    parts.append("<div class=\"explain-box\"><b>ИИ-объяснение для ответственного:</b><br>" + escape(explanation) + "</div>")

            if "history" in blocks:
                history = db.get_site_history(s["id"])
                if history:
                    parts.append("<div class=\"history-box\"><b>История проверок:</b><br>")
                    for h in history[-6:]:
                        parts.append(h["started_at"][:16].replace("T", " ") + " — балл: <b>" + str(h["score"]) + "</b><br>")
                    parts.append("</div>")

            if "photo_audit" in blocks:
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
                            "<tr><td class=\"severity-" + escape(f["severity"]) + "\">" + escape(f["severity"]) + "</td>"
                            "<td>" + escape(f["agent"]) + "</td><td>" + escape(f["message"]) + "</td>"
                            "<td>" + escape(f["recommendation"]) + "</td><td>" + page_link + "</td></tr>"
                        )
                else:
                    parts.append("<table><tr><th>Важность</th><th>Блок</th><th>Проблема</th><th>Где проблема</th></tr>")
                    for f in findings:
                        page_link = ""
                        if f.get("url"):
                            page_link = report_link(f["url"], "открыть")
                        parts.append(
                            "<tr><td class=\"severity-" + escape(f["severity"]) + "\">" + escape(f["severity"]) + "</td>"
                            "<td>" + escape(f["agent"]) + "</td><td>" + escape(f["message"]) + "</td><td>" + page_link + "</td></tr>"
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
        return s["type"]
    if col == "col_score":
        return r["score"]
    if col == "col_severity":
        return f.get("severity", "OK") if f else "OK"
    if col == "col_agent":
        return f.get("agent", "-") if f else "-"
    if col == "col_message":
        return f.get("message", "Проблем не найдено") if f else "Проблем не найдено"
    if col == "col_recommendation":
        return f.get("recommendation", "-") if f else "-"
    if col == "col_page":
        return (f.get("url", "") if f else "")
    return ""


def export_csv(cols=None, include_ok=None):
    stats = db.dashboard_stats()
    if not stats["has_run"]:
        ui.notify("Сначала запустите аудит.", type="warning")
        return
    _, saved_cols, saved_ok = _get_report_config()
    if cols is None:
        cols = saved_cols
    if include_ok is None:
        include_ok = saved_ok
    last_run_id = stats["last_run"]["id"]
    header_map = dict(CSV_COLS)
    header = [header_map[c] for c in cols if c in header_map]
    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')
    writer.writerow(header)
    for s in db.get_sites():
        r = db.get_last_site_result(s["id"])
        if r and r.get("run_id") == last_run_id:
            findings = db.get_site_findings(last_run_id, s["id"])
            if not findings:
                if include_ok:
                    writer.writerow([_csv_value(c, s, r, None) for c in cols])
            else:
                for f in findings:
                    writer.writerow([_csv_value(c, s, r, f) for c in cols])
    filename = "audit_errors_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
    ui.download(output.getvalue().encode("utf-8"), filename)
    ui.notify("CSV-таблица скачана (открывайте в Excel)", type="positive")


# ============================================================
# Фоновые задачи
# ============================================================

async def do_audit(offline=False, sites=None):
    async with audit_lock:
        settings = db.get_settings()
        if sites is None:
            sites = db.get_sites(active_only=True)
        if not sites:
            print("[AUDIT] Нет активных сайтов для проверки.")
            return
        mode = "ПО КЭШУ" if offline else "ПОЛНЫЙ"
        print("\n[AUDIT] === " + mode + " АУДИТ " + str(len(sites)) + " САЙТОВ ===")
        run_id = db.start_run(len(sites))
        scores = []
        for site in sites:
            print("\n[AUDIT] Обрабатываю: " + site["name"])
            try:
                result = await core.audit_site(site, settings, offline=offline)
                print("  -> ошибок: " + str(len(result.get("findings", []))) + ", балл: " + str(result.get("score")))
                result["llm_summary"] = await llm.summarize(site, result["findings"], audit_settings())
                db.save_site_result(run_id, site, result)
                scores.append(result["score"])
            except Exception as e:
                print("  -> [ОШИБКА] " + site["name"] + ": " + str(e))
                import traceback
                traceback.print_exc()
                fallback = {
                    "score": 0,
                    "counts": {"CRITICAL": 1, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0},
                    "findings": [{"agent": "system", "severity": "CRITICAL", "code": "UNHANDLED_ERROR",
                                  "message": "Ошибка обработки: " + str(e), "url": site["url"],
                                  "recommendation": "Проверить доступность сайта и настройки сети."}],
                    "metrics": {}, "llm_summary": "",
                }
                db.save_site_result(run_id, site, fallback)
                scores.append(0)
        avg = sum(scores) / len(scores) if scores else 0
        print("\n[AUDIT] === ЗАВЕРШЕН. Средний балл: " + str(round(avg, 1)) + " ===\n")
        db.finish_run(run_id, "done", avg)


async def audit_single_site(site_id, offline=False):
    async with audit_lock:
        settings = db.get_settings()
        site = db.get_site(site_id)
        if not site:
            return
        mode = "ПО КЭШУ" if offline else "ПОЛНЫЙ"
        print("\n[AUDIT SINGLE] === " + mode + ": " + site["name"] + " ===")
        run_id = db.start_run(1)
        try:
            result = await core.audit_site(site, settings, offline=offline)
            result["llm_summary"] = await llm.summarize(site, result["findings"], audit_settings())
            db.save_site_result(run_id, site, result)
            db.finish_run(run_id, "done", result["score"])
            print("[AUDIT SINGLE] === ЗАВЕРШЕНО. Балл: " + str(result["score"]) + " ===\n")
        except Exception as e:
            print("  -> [ОШИБКА] " + site["name"] + ": " + str(e))
            import traceback
            traceback.print_exc()
            fallback = {
                "score": 0,
                "counts": {"CRITICAL": 1, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "INFO": 0},
                "findings": [{"agent": "system", "severity": "CRITICAL", "code": "UNHANDLED_ERROR",
                              "message": "Ошибка обработки: " + str(e), "url": site["url"],
                              "recommendation": "Проверить доступность сайта и настройки сети."}],
                "metrics": {}, "llm_summary": "",
            }
            db.save_site_result(run_id, site, fallback)
            db.finish_run(run_id, "error", 0)


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
        for label, path in (("Обзор", "/quick"), ("Учреждения", "/sites"), ("Правила", "/rules"),
                            ("Отчёты", "/report"), ("Параметры обхода", "/settings")):
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

@ui.page("/quick")
def dashboard():
    layout("Обзор проверки")
    progress = ui.label().props("role=status").classes("audit-subnav")
    container = ui.column().classes("audit-container w-full gap-4 px-4")

    async def start_audit_ui(offline):
        if audit_lock.locked():
            ui.notify("Проверка уже выполняется", type="warning")
            return
        sites = db.get_sites(active_only=True)
        if offline:
            sites = [site for site in sites if has_cache(site["id"])]
        if not sites:
            progress.set_text("Сначала выберите учреждение и нажмите «Проверить сайт».")
            return
        progress.set_text(f"Идёт проверка учреждений ({len(sites)}).")
        await do_audit(offline, sites)
        progress.set_text("Проверка завершена. Результаты обновлены.")
        render()

    def render():
        container.clear()
        stats = db.dashboard_stats()
        with container:
            sites = db.get_sites()
            by_id = {site["id"]: site["name"] for site in sites}
            with ui.card().classes("w-full p-5"):
                ui.label("Проверить учреждение").classes("text-h6")
                ui.label("Выберите школу или детский сад. Проверка сайта по правилам доступна без ИИ.")
                choice = ui.select(by_id, label="Найти по названию", with_input=True).classes("w-full")
                ui.button("Открыть учреждение", on_click=lambda: ui.navigate.to(f"/site/{choice.value}")).bind_enabled_from(choice, "value", backward=bool)
            with ui.row().classes("audit-actions w-full gap-2 flex-wrap"):
                ui.button("Проверить все учреждения", on_click=lambda: start_audit_ui(False)).props("color=primary icon=cloud_download")
                ui.button("Повторить по кэшу", on_click=lambda: start_audit_ui(True)).props("color=teal icon=flash_on")
                ui.button("Обновить данные", on_click=render).props("outline icon=refresh")
                ui.button("Скачать HTML-отчет", on_click=export_html).props("color=green icon=description")
                ui.button("Скачать CSV (Excel)", on_click=export_csv).props("color=orange icon=table_chart")

            if not stats["has_run"]:
                ui.label("Результаты появятся после первой проверки учреждения.").classes("text-grey-7 mt-4")
                return

            with ui.row().classes("w-full gap-4"):
                stat_card("Всего сайтов", stats["total_sites"])
                stat_card("Средний балл", stats["avg_score"])
                stat_card("Критичных сайтов", stats["critical_sites"])
                last_run_time = stats["last_run"]["started_at"][:16].replace("T", " ") if stats["last_run"] else "-"
                stat_card("Последний запуск", last_run_time)

            sev = stats["severity_counts"]
            with ui.row().classes("w-full gap-4"):
                with ui.card().classes("flex-1 p-4"):
                    ui.label("Ошибки по важности").classes("text-subtitle1 font-bold mb-2")
                    ui.echart({"tooltip": {"trigger": "item"}, "series": [{"type": "pie", "radius": "70%", "data": [
                        {"name": "CRITICAL", "value": sev.get("CRITICAL", 0)},
                        {"name": "HIGH", "value": sev.get("HIGH", 0)},
                        {"name": "MEDIUM", "value": sev.get("MEDIUM", 0)},
                        {"name": "LOW", "value": sev.get("LOW", 0)},
                    ]}]}).classes("h-72")
                with ui.card().classes("flex-1 p-4"):
                    ui.label("Проблемы по блокам").classes("text-subtitle1 font-bold mb-2")
                    ui.echart({"tooltip": {"trigger": "axis"},
                               "xAxis": {"type": "category", "data": [x["name"] for x in stats["agent_counts"]], "axisLabel": {"rotate": 30}},
                               "yAxis": {"type": "value"},
                               "series": [{"type": "bar", "data": [x["value"] for x in stats["agent_counts"]]}]}).classes("h-72")

            with ui.card().classes("w-full p-4"):
                ui.label("Рейтинг сайтов (от худшего к лучшему)").classes("text-subtitle1 font-bold mb-2")
                ui.table(columns=[
                    {"name": "site_name", "label": "Сайт", "field": "site_name", "align": "left"},
                    {"name": "score", "label": "Балл", "field": "score"},
                    {"name": "critical", "label": "Critical", "field": "critical"},
                    {"name": "high", "label": "High", "field": "high"},
                    {"name": "medium", "label": "Medium", "field": "medium"},
                    {"name": "low", "label": "Low", "field": "low"},
                ], rows=stats["top_bad"], row_key="site_name").classes("w-full")

    render()


# ============================================================
# Сайты
# ============================================================

@ui.page("/sites")
def sites_page():
    layout("Сайты")
    container = ui.column().classes("audit-container w-full gap-4 px-4")

    def render():
        container.clear()
        with container:
            with ui.expansion("Добавить учреждение").classes("w-full"):
                name = ui.input("Название").classes("w-full")
                url = ui.input("URL").classes("w-full")
                email = ui.input("E-mail учреждения (для отправки отчётов)").classes("w-full")
                site_type = ui.select(["school", "kindergarten"], value="school", label="Тип учреждения").classes("w-full")

                async def add_site():
                    clean_url = url.value.strip()
                    if not clean_url:
                        ui.notify("Введите URL", type="negative")
                        return
                    if not clean_url.startswith("http"):
                        clean_url = "https://" + clean_url
                    parsed = urlparse(clean_url)
                    if not parsed.hostname:
                        ui.notify("Некорректный URL", type="negative")
                        return
                    db.add_site(name=name.value.strip() or parsed.hostname,
                                url=clean_url, site_type=site_type.value,
                                email=email.value.strip())
                    ui.notify("Сайт добавлен!", type="positive")
                    name.value, url.value, email.value = "", "", ""
                    render()

                ui.button("Добавить", on_click=add_site).props("color=primary icon=add")

            with ui.card().classes("w-full p-4"):
                ui.label("Список ресурсов").classes("text-subtitle1 font-bold mb-2")
                sites = db.get_sites()
                if not sites:
                    ui.label("Пока нет добавленных сайтов.").classes("text-grey-7")
                    return
                for s in sites:
                    with ui.row().classes("w-full items-center justify-between py-2 border-b"):
                        with ui.column().classes("gap-0"):
                            ui.label(s["name"]).classes("font-bold")
                            ui.label(s["url"]).classes("text-caption text-grey-7")
                            ui.label("Тип: " + s["type"] + " • E-mail: " + (s.get("email") or "не указан")).classes("text-caption text-grey-6")
                        with ui.row():
                            ui.link("Детали", "/site/" + str(s["id"])).props("color=primary").classes("mr-2")
                            ui.button("Удалить", on_click=lambda s=s: delete_site(s["id"])).props("flat color=red")

    def delete_site(site_id):
        db.delete_site(site_id)
        ui.notify("Сайт удален", type="info")
        render()

    render()


# ============================================================
# Карточка сайта
# ============================================================

@ui.page("/site/{site_id}")
def site_detail(site_id: int):
    site = db.get_site(site_id)
    if not site:
        layout("Сайт не найден")
        ui.label("Сайт не найден").classes("text-h6 text-red-600 px-4")
        return

    layout(site["name"])
    container = ui.column().classes("audit-container w-full gap-4 px-4")

    progress = ui.label().props("role=status").classes("audit-subnav")

    async def run_check(offline):
        if audit_lock.locked():
            ui.notify("Дождитесь окончания текущей проверки.", type="warning")
            return
        progress.set_text("Проверяем сохранённые страницы…" if offline else "Загружаем и проверяем страницы сайта…")
        await audit_single_site(site_id, offline=offline)
        progress.set_text("Готово. Результат проверки показан в карточке.")
        render()

    def render():
        container.clear()
        result = db.get_last_site_result(site_id)
        site_now = db.get_site(site_id) or site

        with container:
            with ui.row().classes("w-full gap-2 items-center flex-wrap"):
                ui.link("← К списку сайтов", "/sites").classes("text-primary")
                linked_id = archive_id(site["url"])
                if linked_id is not None:
                    ui.link("Сводная карточка учреждения", f"/#institution/{linked_id}")
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
                stat_card("Critical", result["critical"])
                stat_card("High", result["high"])
                stat_card("Medium", result["medium"])
                stat_card("Low", result["low"])

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
                    ui.label("LLM-сводка").classes("text-subtitle1 font-bold mb-2")
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
                    rows = [{"id": f.get("id", i), "severity": f["severity"], "agent": f["agent"],
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

            with ui.card().classes("w-full p-4 bg-amber-50"):
                ui.label("ИИ-объяснение ошибок простым языком").classes("text-subtitle1 font-bold mb-2")
                explanation = llm.load_explanation(site_id)
                if explanation:
                    ui.label(explanation).classes("whitespace-pre-wrap text-grey-800")
                else:
                    ui.label("Объяснения еще нет. Нажмите кнопку — нейросеть объяснит ошибки простыми словами.").classes("text-grey-7")

                async def make_explanation():
                    s = audit_settings()
                    if s.get("llm_enabled") != "1":
                        ui.notify("Сначала включите LLM в разделе «Настройки».", type="warning")
                        return
                    r = db.get_last_site_result(site_id)
                    if not r:
                        ui.notify("Сначала запустите аудит этого сайта.", type="warning")
                        return
                    fnds = db.get_site_findings(r["run_id"], site_id)
                    ui.notify("Нейросеть думает... 1–2 минуты.", type="info")
                    try:
                        text = await llm.explain(site, fnds, s)
                        if text is None:
                            ui.notify("LLM выключен в настройках.", type="warning")
                            return
                        llm.save_explanation(site_id, text)
                        ui.notify("Объяснение готово!", type="positive")
                        render()
                    except Exception as e:
                        ui.notify("Ошибка LLM: " + str(e), type="negative")

                explain_button = ui.button("Объяснить с помощью ИИ", on_click=make_explanation).props("color=primary icon=psychology")
                if audit_settings()["llm_enabled"] != "1":
                    explain_button.disable()
                    ui.link("Настроить ИИ для объяснения", "/admin#model")

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
                     "Встроенного словаря синонимов нет: ищется строго то, что ты вписал. "
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
                ui.label("После смены эталона, разделов или целевого размера — «⚡ По кэшу» (быстро, без сети).").classes("text-caption text-grey-7")

    render()


# ============================================================
# Конструктор отчётов
# ============================================================

@ui.page("/report")
def report_page():
    layout("Конструктор отчётов")
    saved_html, saved_csv, saved_ok = _get_report_config()

    with ui.column().classes("audit-container w-full gap-4 px-4"):
        ui.label("Отметьте галочками, какие блоки попадут в отчёт. «Сохранить по умолчанию» влияет на кнопки на Дашборде.").classes("text-grey-7")

        html_checks = {}
        with ui.card().classes("w-full p-4"):
            ui.label("Блоки HTML-отчёта").classes("text-subtitle1 font-bold mb-2")
            for key, label in HTML_BLOCKS:
                html_checks[key] = ui.checkbox(label, value=key in saved_html)

        csv_checks = {}
        with ui.card().classes("w-full p-4"):
            ui.label("Колонки CSV-таблицы (Excel)").classes("text-subtitle1 font-bold mb-2")
            for key, label in CSV_COLS:
                csv_checks[key] = ui.checkbox(label, value=key in saved_csv)
            ok_switch = ui.switch("Включать сайты без ошибок (строки «ОК»)", value=saved_ok)

        def save_defaults():
            db.save_settings({
                "report_html_blocks": json.dumps([k for k in html_checks if html_checks[k].value], ensure_ascii=False),
                "report_csv_cols": json.dumps([k for k in csv_checks if csv_checks[k].value], ensure_ascii=False),
                "report_csv_ok": "1" if ok_switch.value else "0",
            })
            ui.notify("Конфигурация сохранена по умолчанию!", type="positive")

        def gen_html_now():
            export_html(blocks=[k for k in html_checks if html_checks[k].value])

        def gen_csv_now():
            export_csv(cols=[k for k in csv_checks if csv_checks[k].value], include_ok=ok_switch.value)

        with ui.row().classes("gap-2 flex-wrap"):
            ui.button("Сохранить по умолчанию", on_click=save_defaults).props("color=primary icon=save")
            ui.button("Сформировать HTML сейчас", on_click=gen_html_now).props("color=green icon=description")
            ui.button("Сформировать CSV сейчас", on_click=gen_csv_now).props("color=orange icon=table_chart")


# ============================================================
# Настройки
# ============================================================

@ui.page("/settings")
def settings_page():
    layout("Настройки")
    s = db.get_settings()
    with ui.column().classes("audit-container w-full gap-4 px-4"):
        ui.link("Подключение ИИ для обоих режимов", "/admin#model")
        with ui.card().classes("w-full p-4"):
            ui.label("Настройки краулера (HTTP, без браузера)").classes("text-subtitle1 font-bold mb-2")
            extra_hosts = ui.textarea("Дополнительные разрешённые хосты (через запятую)", value=s.get("extra_hosts", "")).classes("w-full")
            max_pages = ui.number("Максимум страниц на один сайт", value=int(s.get("max_pages", "40")), min=1, max=200).classes("w-full mt-2")
            news_age = ui.number("Порог старины новости (дней) — старше = OLD_NEWS",
                                 value=int(s.get("news_max_age_days", "14")), min=1, max=365).classes("w-full mt-2")
            auto_type = ui.switch("Автоопределять тип учреждения по структуре сайта",
                                  value=s.get("auto_detect_type", "1") == "1")
            target_sizes = ui.input("Целевые размеры портретов (напр. 714x952, 800x1067)",
                                    value=s.get("photo_target_sizes", "714x952")).classes("w-full mt-2")
        def save():
            db.save_settings({
                "extra_hosts": extra_hosts.value.strip(), "max_pages": str(int(max_pages.value)),
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
    if audit_lock.locked():
        return {"status": "already_running"}
    asyncio.create_task(do_audit(offline=False))
    return {"status": "started"}


@app.post("/api/run_cache")
async def api_run_cache():
    if audit_lock.locked():
        return {"status": "already_running"}
    asyncio.create_task(do_audit(offline=True))
    return {"status": "started"}


# ============================================================
# Запуск сервера
# ============================================================

logging.getLogger("nicegui").setLevel(logging.ERROR)
logging.getLogger("engineio").setLevel(logging.CRITICAL)
logging.getLogger("socketio").setLevel(logging.CRITICAL)
