"""HTTP API веб-интерфейса «Север» поверх движка audit/.

Контракт API повторяет приложение «Север» (sever-audit.zip), чтобы интерфейс работал без переделки;
вместо собственного краулера «Севера» запуски выполняет sever.runner (движок audit/).
Реализовано на Starlette (на нём же построен FastAPI), чтобы не добавлять зависимостей.
"""
from __future__ import annotations

import csv
import html
import io
import ipaddress
import json
import secrets
import socket
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from audit.crossval import choose_reference, doc_label, site_doc_set

from . import runner, store
from .adapter import site_from_dict

VERSION = "1.1.0-audit"
SESSION = secrets.token_urlsafe(32)


class HTTPError(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


def error(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


async def body(request: Request, model: type[BaseModel]):
    try:
        raw = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPError(422, "Некорректный JSON")
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        first = e.errors()[0]
        raise HTTPError(422, f"Некорректное поле {'.'.join(map(str, first['loc']))}: {first['msg']}")


# ------------------------------------------------------------------------------ модели запросов
class Organization(BaseModel):
    name: str = Field(min_length=3, max_length=350)
    url: str = Field(max_length=2000)
    kind: str


class RunInput(BaseModel):
    organization_ids: list[int] = Field(default_factory=list, max_length=300)
    mode: str = "full"


class SettingsInput(BaseModel):
    model_config = {"extra": "ignore"}
    llm_enabled: bool = False
    llm_provider: str = Field(default="", pattern=r"^(|auto|openai|anthropic)$")
    base_url: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=250)
    api_key: str | None = None
    web_search: str = Field(default="", pattern=r"^(|auto|google|off)$")
    json_mode: str = Field(default="", pattern=r"^(|json_object|json_schema|off)$")
    llm_timeout: float | None = Field(default=None, ge=5, le=1800)
    llm_max_tokens: int | None = Field(default=None, ge=256, le=64000)
    reasoning_effort: str = Field(default="", max_length=20)
    llm_news: int = Field(default=3, ge=0, le=15)
    concurrency: int = Field(default=6, ge=1, le=12)
    http_concurrency: int = Field(default=12, ge=1, le=32)
    timeout: float = Field(default=25, ge=5, le=120)
    max_news: int = Field(default=15, ge=1, le=60)
    max_persons: int = Field(default=40, ge=1, le=200)
    image_hashes: bool = True
    use_cache: bool = True
    cache_ttl: float = Field(default=12, ge=0, le=720)
    schedule_enabled: bool = False
    schedule_hour: int = Field(default=8, ge=0, le=23)
    schedule_interval_days: int = Field(default=7, ge=1, le=30)
    benchmark_school: int | None = None
    benchmark_kindergarten: int | None = None


def safe_settings(s: SettingsInput) -> dict:
    vals = s.model_dump(exclude={"api_key"})
    if vals["base_url"]:
        p = urlsplit(vals["base_url"])
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise HTTPError(422, "Некорректный адрес API. Укажите http(s)://сервер:порт/v1")
        vals["base_url"] = vals["base_url"].rstrip("/")
    if s.api_key is not None:
        vals["api_key"] = s.api_key
    return vals


def public_site(url: str) -> str:
    """Адрес нового учреждения: только корень сайта (движок audit/ проверяет домен целиком), общедоступная сеть."""
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise HTTPError(422, "Нужен HTTP(S)-адрес сайта без логина и пароля")
    if p.port and p.port not in (80, 443):
        raise HTTPError(422, "Для сайтов разрешены порты 80 и 443")
    if p.path not in ("", "/") or p.query:
        raise HTTPError(422, "Укажите адрес главной страницы сайта, без пути: проверяется весь сайт")
    host = p.hostname.lower()
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        raise HTTPError(422, "Не удалось найти сайт по этому адресу")
    if not infos or any(not ipaddress.ip_address(i[4][0]).is_global for i in infos):
        raise HTTPError(422, "Адрес сайта должен вести в общедоступную сеть")
    return f"https://{host}/"


# ------------------------------------------------------------------------------ состояние
async def state(request: Request):
    with store.connect() as db:
        runs = [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY created DESC LIMIT 30")]
    for r in runs:
        opts = json.loads(r.pop("options") or "{}")
        r["mode"] = opts.get("mode", "full")
        r["outputs"] = (store.runs_dir() / r["id"] / "summary.xlsx").exists()
    cfg = store.settings()
    cfg["llm_effective"] = runner.llm_settings(store.settings(secret=True)).describe()
    return JSONResponse({"organizations": store.organizations(), "runs": runs, "settings": cfg,
                         "session": SESSION, "reviews": store.reviews(), "time": store.now(), "engine": "audit"})


async def add_org(request: Request):
    b = await body(request, Organization)
    if b.kind not in ("school", "kindergarten"):
        raise HTTPError(422, "Выберите тип учреждения")
    url = await run_in_threadpool(public_site, b.url)
    try:
        with store.connect() as db:
            cur = db.execute("INSERT INTO organizations(name,url,kind) VALUES(?,?,?)", (b.name.strip(), url, b.kind))
            return JSONResponse({"id": cur.lastrowid})
    except Exception:
        raise HTTPError(409, "Учреждение с таким адресом уже существует")


async def create_run(request: Request):
    b = await body(request, RunInput)
    try:
        return JSONResponse(runner.start_run(b.organization_ids, b.mode))
    except runner.RunConflict as e:
        raise HTTPError(409, str(e))
    except ValueError as e:
        raise HTTPError(422, str(e))


async def cancel(request: Request):
    try:
        runner.cancel_run(request.path_params["rid"])
    except runner.RunConflict as e:
        raise HTTPError(409, str(e))
    return JSONResponse({"status": "cancelling"})


# ------------------------------------------------------------------------------ отчёты
async def reports(request: Request):
    org_id = request.query_params.get("org_id")
    with store.connect() as db:
        sql = "SELECT id,run_id,org_id,created,summary FROM reports " + ("WHERE org_id=? " if org_id else "") + \
              "ORDER BY id DESC LIMIT 400"
        rows = db.execute(sql, (int(org_id),) if org_id else ()).fetchall()
        result = [dict(store.row_summary(db, r), id=r["id"], org_id=r["org_id"], run_id=r["run_id"],
                       created=r["created"]) for r in rows]
    return JSONResponse(result)


def load_report(request: Request) -> dict:
    r = store.report(int(request.path_params["report_id"]))
    if not r:
        raise HTTPError(404, "Отчёт не найден")
    return r


async def get_report(request: Request):
    r = load_report(request)
    r["data"].pop("raw", None)          # исходный результат audit/ доступен в выгрузке JSON
    r["reviews"] = store.reviews()      # отчёт неизменяем, решения инспектора хранятся отдельно
    return JSONResponse(r)


class Review(BaseModel):
    state: str
    note: str = Field(default="", max_length=2000)


async def review(request: Request):
    b = await body(request, Review)
    if b.state not in ("open", "confirmed", "dismissed", "fixed"):
        raise HTTPError(422, "Некорректный статус")
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?)",
                   (request.path_params["fingerprint"][:64], b.state, b.note, store.now()))
    return JSONResponse({"ok": True})


async def update_settings(request: Request):
    b = await body(request, SettingsInput)
    values = safe_settings(b)
    current = store.settings()
    if b.schedule_enabled and any(current[k] != values[k] for k in ("schedule_enabled", "schedule_hour", "schedule_interval_days")):
        values["next_run"] = runner.next_schedule(values)
    elif not b.schedule_enabled:
        values["next_run"] = None
    store.save_settings(values)
    cfg = store.settings()
    cfg["llm_effective"] = runner.llm_settings(store.settings(secret=True)).describe()
    return JSONResponse(cfg)


TEST_NEWS = ("День отца в школе",
             "16 октября в нашей школе прошёл праздник, посвящённый Дню отца, который в 2026 году отмечается "
             "16 октября. Ребята подготовили концерт для пап.")


def _llm_test(cfg: dict) -> dict:
    """Тот же тестовый запрос, что `python -m audit llm-test`: новость с заведомой ошибкой в дате."""
    import requests
    checker = runner.make_llm(cfg)
    models = []
    s = checker.settings
    if s.provider == "openai" and runner.LLM_FACTORY is None:
        try:
            headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
            r = requests.get(s.base_url.rstrip("/") + "/models", headers=headers, timeout=15)
            models = [m["id"] for m in r.json().get("data", [])] if r.ok else []
        except Exception:  # noqa: BLE001 — список моделей необязателен
            models = []
    res = checker.check_news(TEST_NEWS[0], TEST_NEWS[1], datetime(2026, 10, 19, 10, 0), "Тестовая школа")
    if res is None:
        raise ValueError("ответ не получен или не разобран как JSON")
    ev = res.get("event_date")
    return {"ok": True, "models": models, "model": s.model, "settings": s.describe(),
            "message": f"Модель отвечает, JSON получен. Дата события: {ev.isoformat() if ev else 'не определена'}; "
                       f"утверждений: {len(res.get('claims', []))}."}


async def llm_test(request: Request):
    b = await body(request, SettingsInput)
    cfg = store.settings(secret=True)
    cfg.update(safe_settings(b))
    try:
        return JSONResponse(await run_in_threadpool(_llm_test, cfg))
    except Exception as e:  # noqa: BLE001
        raise HTTPError(502, "Не удалось получить ответ модели: " + str(e)[:250])


async def comparison(request: Request):
    """Сравнение с эталоном по правилам audit.crossval: категории документов и выбор эталона по баллу audit."""
    cfg = store.settings()
    records = [r for r in store.latest_reports() if r["data"].get("raw") and not r["data"].get("preliminary")
               and r["data"]["status"] != "unavailable"]
    sites = {r["org_id"]: site_from_dict(r["data"]["raw"]) for r in records}
    auto = choose_reference(list(sites.values()))
    groups = []
    for kind in ("school", "kindergarten"):
        rows_k = [r for r in records if r["data"]["organization"]["kind"] == kind]
        pinned = cfg["benchmark_" + kind]
        ref = next((r for r in rows_k if r["org_id"] == pinned), None)
        if not ref and kind in auto:
            ref = next((r for r in rows_k if sites[r["org_id"]] is auto[kind]), None)
        base = site_doc_set(sites[ref["org_id"]]) if ref else set()
        rows = []
        for r in rows_k:
            mine = site_doc_set(sites[r["org_id"]])
            d = r["data"]
            rows.append({"id": r["id"], "org_id": r["org_id"], "name": d["organization"]["name"], "score": d["score"],
                         "coverage": d["coverage"], "audit_score": d.get("audit_score"), "documents": len(d["documents"]),
                         "categories": sorted(doc_label(k) for k in mine),
                         "missing_compared_to_reference": sorted(doc_label(k) for k in base - mine), "created": r["created"]})
        groups.append({"kind": kind, "reference": {
            "id": ref["id"], "org_id": ref["org_id"], "name": ref["data"]["organization"]["name"],
            "categories": sorted(doc_label(k) for k in base), "manual": ref["org_id"] == pinned, "created": ref["created"],
            "audit_score": ref["data"].get("audit_score")} if ref else None,
            "rows": sorted(rows, key=lambda x: (x["audit_score"] or 0, x["coverage"] or 0), reverse=True)})
    return JSONResponse(groups)


# ------------------------------------------------------------------------------ выгрузки
def csv_value(value) -> str:
    s = str(value if value is not None else "")
    return "'" + s if s.lstrip().startswith(("=", "+", "-", "@")) else s


def pct(v) -> str:
    return "—" if v is None else f"{v}%"


async def export_summary(request: Request):
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["Учреждение", "Тип", "Сайт", "Дата проверки", "Оценка проверенных пунктов", "Охват %",
                "Балл audit", "Проблемы", "Из них критичных", "Требуют проверки"])
    for o in store.organizations():
        l = o["latest"] or {}
        w.writerow([csv_value(v) for v in [o["name"], o["kind"], o["url"], l.get("created"), l.get("score"),
                                           l.get("coverage"), l.get("audit_score"), l.get("issues"), l.get("critical"),
                                           l.get("review")]])
    return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=sever-summary.csv"})


STATUSES = {"pass": "Проверено", "fail": "Проблема", "review": "Требует проверки", "unknown": "Нет данных"}
PRIORITY = {"critical": "Критичный", "high": "Высокий", "medium": "Средний", "low": "Низкий"}
SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
DECISIONS = {"open": "Открыто", "confirmed": "Подтверждено", "dismissed": "Не подтвердилось", "fixed": "Исправлено"}
MODES = {"quick": "Быстрый обзор", "import": "Импорт результата CLI", "full": "Полная проверка"}


async def export_report(request: Request):
    r = load_report(request)
    fmt = request.path_params["fmt"]
    marks = store.reviews()
    d = r["data"]
    r["reviews"] = {c["fingerprint"]: marks[c["fingerprint"]] for c in d["checks"] if c["fingerprint"] in marks}
    filename = f"sever-report-{r['id']}"
    if fmt == "json":
        return Response(json.dumps(r, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename={filename}.json"})
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Раздел", "Вывод", "Статус", "Приоритет", "Доказательство", "Страница", "Рекомендация", "Метод",
                    "Код проверки audit", "Решение инспектора", "Комментарий", "Дата решения", "Режим проверки"])
        for c in d["checks"]:
            m = marks.get(c["fingerprint"], {})
            w.writerow([csv_value(x) for x in [c["section"], c["title"], STATUSES[c["status"]], PRIORITY[c["severity"]],
                                               c["evidence"], c["url"], c["recommendation"], c["method"], c.get("check", ""),
                                               DECISIONS.get(m.get("state"), ""), m.get("note", ""), m.get("updated", ""),
                                               MODES.get(d.get("mode"), d.get("mode"))]])
        return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={filename}.csv"})
    if fmt != "html":
        raise HTTPError(404, "Неизвестный формат")
    e = lambda s: html.escape(str(s or ""))  # noqa: E731
    note = MODES.get(d.get("mode"), "") + ". Движок проверок: audit/."
    if d.get("preliminary"):
        note = "ПРОМЕЖУТОЧНЫЙ ОТЧЁТ: сверка между сайтами запуска ещё не выполнена. " + note

    def decision(c):
        m = marks.get(c["fingerprint"])
        return ("<br><b>Решение инспектора: " + e(DECISIONS.get(m["state"], m["state"])) + "</b><br>" + e(m.get("note"))
                + "<br>" + e(m.get("updated"))) if m else ""

    rows = "".join(
        f'<tr><td>{e(c["section"])}</td><td><b>{e(c["title"])}</b><br>{STATUSES[c["status"]]} · {PRIORITY[c["severity"]]}'
        f'{decision(c)}</td><td>{e(c["evidence"]).replace(chr(10), "<br>")}<br><a href="{e(c["url"])}">Страница</a></td>'
        f'<td>{e(c["recommendation"])}</td></tr>'
        for c in sorted(d["checks"], key=lambda c: (SEV_ORDER[c["severity"]], c["status"] == "pass")))
    docs = "".join(f'<li><a href="{e(x["url"])}">{e(x.get("label") or x["url"])}</a></li>' for x in d["documents"])
    score = "—" if d["score"] is None else f"{d['score']}/100"
    content = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Отчёт Север</title><style>body{{font:15px/1.6 Arial,sans-serif;color:#192e3b;margin:40px auto;max-width:1100px;padding:20px}}h1{{font-size:25px}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border:1px solid #cbd2d5;padding:10px;text-align:left;vertical-align:top}}a{{color:#156b69;overflow-wrap:anywhere}}button{{padding:12px;background:#123d43;color:white;border:0}}@media print{{button{{display:none}}body{{margin:0;padding:0}}thead{{display:table-header-group}}tr{{break-inside:avoid}}}}@page{{size:A4 landscape;margin:12mm}}</style><button onclick="window.print()">Печать / Сохранить PDF</button><p>СЕВЕР / МОНИТОРИНГ САЙТОВ ОБРАЗОВАНИЯ</p><h1>{e(d['organization']['name'])}</h1><p>{e(d['organization']['url'])}<br>Проверка: {e(d['checked_at'])}<br>Охват подтверждённых проверок: {pct(d['coverage'])}. Оценка проверенных пунктов: {score}. Балл audit: {e(d.get('audit_score') if d.get('audit_score') is not None else '—')} ({e(d.get('audit_grade'))}). Страниц: {len(d['pages'])}.</p><p>{e(note)}</p><p>Оценка относится только к подтверждённым пунктам. Вопросы для проверки и недоступные данные не означают нарушение.</p><table><thead><tr><th>Раздел</th><th>Вывод и приоритет</th><th>Основание</th><th>Что сделать</th></tr></thead><tbody>{rows}</tbody></table><h2>Перечень документов</h2><ul>{docs}</ul><h2>Ограничения</h2><ul>{"".join("<li>" + e(x) + "</li>" for x in d["limitations"])}</ul></html>'''
    return HTMLResponse(content, headers={"Content-Disposition": f"inline; filename={filename}.html"})


RUN_FILES = {"summary.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
             "findings.csv": "text/csv; charset=utf-8", "results.json": "application/json"}


async def run_file(request: Request):
    """Выгрузки audit/ по запуску: те же файлы, что строит CLI (index.html, sites/*.html, xlsx, csv, json)."""
    rid = request.path_params["rid"]
    rel = request.path_params["path"] or "index.html"
    if not rid.isalnum():
        raise HTTPError(404, "Запуск не найден")
    root = (store.runs_dir() / rid).resolve()
    target = (root / rel).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPError(404, "Файл не найден")
    if target.name in RUN_FILES:
        return FileResponse(target, media_type=RUN_FILES[target.name], filename=f"{rid}-{target.name}")
    return FileResponse(target)


async def health(request: Request):
    return JSONResponse({"status": "ok", "version": VERSION, "engine": "audit"})


async def index(request: Request):
    return FileResponse(store.STATIC / "index.html")


# ------------------------------------------------------------------------------ middleware
class LocalGuard(BaseHTTPMiddleware):
    """Локальный сервис: изменяющие запросы только со страницы приложения (токен сессии + Origin)."""

    async def dispatch(self, request, call_next):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return Response("Недопустимый источник запроса", status_code=403)
            if request.headers.get("x-audit-session") != SESSION:
                return Response("Обновите страницу приложения", status_code=403)
        try:
            response = await call_next(request)
        except HTTPError as e:
            response = error(e.status, e.detail)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response


async def http_error(request, exc: HTTPError):
    return error(exc.status, exc.detail)


@asynccontextmanager
async def lifespan(app):
    store.init()
    stop = threading.Event()
    runner.start_scheduler(stop)
    yield
    stop.set()
    for job in list(runner.JOBS.values()):
        job.cancel_event.set()


routes = [
    Route("/", index),
    Route("/api/state", state),
    Route("/api/organizations", add_org, methods=["POST"]),
    Route("/api/runs", create_run, methods=["POST"]),
    Route("/api/runs/{rid}/cancel", cancel, methods=["POST"]),
    Route("/api/reports", reports),
    Route("/api/reports/{report_id:int}", get_report),
    Route("/api/reports/{report_id:int}/export/{fmt}", export_report),
    Route("/api/reviews/{fingerprint}", review, methods=["PUT"]),
    Route("/api/settings", update_settings, methods=["PUT"]),
    Route("/api/llm/test", llm_test, methods=["POST"]),
    Route("/api/comparison", comparison),
    Route("/api/export/summary.csv", export_summary),
    Route("/runs/{rid}/{path:path}", run_file),
    Route("/api/health", health),
    Mount("/static", StaticFiles(directory=store.STATIC), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPError: http_error}, middleware=[
    Middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"]),
    Middleware(LocalGuard),
])
