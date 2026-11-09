"""Local web entry point for both audit engines."""
import argparse
import asyncio
import hmac
import json
import sqlite3
import threading
import webbrowser
from contextlib import closing
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from nicegui import ui
from starlette.datastructures import Headers
from starlette.websockets import WebSocket

from . import config, dashboard_data, site_server as site
from .dashboard_pdf import build_pdf
from .portal_nav import navigation
from ai_audit_web.integration import ai_status, initialize, quick_id


class LocalAccess:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = Headers(scope=scope)
            host = headers.get("host", "")
            port = scope["server"][1]
            origin = headers.get("origin")
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}") or (origin and origin != "http://" + host):
                if scope["type"] == "websocket":
                    await WebSocket(scope, receive, send).close(code=1008)
                else:
                    await Response(status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app():
    from ai_audit_web import app as quick  # registers the original UI and /api/run endpoints

    site.prepare_database()
    initialize()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=8192)
    app.add_middleware(LocalAccess)
    limit = threading.BoundedSemaphore(3)

    def asset_route(filename, media):
        def serve():
            if media == "text/html":
                html = (site.WEB / filename).read_text(encoding="utf8")
                nav = navigation("/admin" if filename == "admin.html" else "/archive", ai_status())
                return Response(html.replace("<!--portal-navigation-->", nav), media_type=media,
                                headers={"Cache-Control": "no-store"})
            return FileResponse(site.WEB / filename, media_type=media, headers={"Cache-Control":"no-cache"})
        return serve

    for path, (filename, media) in {**site.ASSETS, **site.ADMIN_ASSETS, "/navigation.css":("navigation.css","text/css")}.items():
        app.add_api_route(path, asset_route(filename, media), methods=["GET", "HEAD"])

    @app.get("/healthz")
    @app.get("/api/access")
    def access():
        return {"ok":True,"service":"kontur-portal","admin":True,"ai_status":ai_status()}

    @app.get("/api/admin/progress")
    def job_progress():
        with closing(config.connect_db()) as conn:
            return {"job":site.portal_jobs.status(), "cycle":site.audit_worker.last_status(conn), "manual_cycle":site.JOB.copy()}

    @app.get("/api/admin/state")
    def state():
        with closing(config.connect_db()) as conn:
            return JSONResponse(site.admin_state(conn), headers={"Cache-Control":"no-store"})

    @app.post("/api/admin/{action}")
    async def update(request: Request, action: str):
        if (request.headers.get("origin") != "http://" + request.headers["host"] or
                not hmac.compare_digest(request.headers.get("x-audit-token",""), site.TOKEN)):
            return JSONResponse({"error":"Откройте настройки на этом компьютере"},403)
        try:
            size = int(request.headers.get("content-length", 0))
            if not 0 < size <= (4_000_000 if action=="roster" else 16000):
                raise ValueError("Превышен размер запроса или отсутствуют данные")
            data = await request.json()
            if not isinstance(data,dict):
                raise ValueError("Ожидается объект JSON")
            def save():
                with closing(config.connect_db()) as conn:
                    return site.update_settings(conn, action, data)
            if action == "roster":
                if quick.audit_lock.locked():
                    raise ValueError("Дождитесь завершения алгоритмической проверки учреждений.")
                async with quick.audit_lock:
                    return await asyncio.to_thread(save)
            return await asyncio.to_thread(save)
        except (ValueError, KeyError, OSError, RuntimeError, TypeError) as exc:
            return JSONResponse({"error":str(exc)[:350]},400)

    @app.get("/api/dashboard")
    @app.get("/api/institution/{institution_id}")
    @app.get("/report.pdf")
    def archive(request: Request, institution_id: int | None = None):
        if not limit.acquire(timeout=2):
            return JSONResponse({"error":"Готовятся другие отчёты. Повторите запрос."},503)
        try:
            query = request.query_params
            if len(request.url.query)>8192:
                raise ValueError("Слишком длинные параметры")
            filters = {k: query.getlist(k) for k in query}
            with closing(sqlite3.connect(Path(config.DB_PATH).resolve().as_uri()+"?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                if request.url.path == "/report.pdf":
                    return Response(build_pdf(conn,filters),media_type="application/pdf",
                                    headers={"Content-Disposition":'attachment; filename="audit-report.pdf"'})
                if institution_id is None:
                    return dashboard_data.dashboard(conn,filters)
                if not conn.execute("SELECT 1 FROM active_organization WHERE id=?", (institution_id,)).fetchone():
                    return JSONResponse({"error":"Учреждение не найдено"},404)
                detail = dashboard_data.institution(conn,institution_id,filters)
                row = detail.get("institution")
                if row:
                    detail["quick_id"] = quick_id(row["site_url"])
                return detail
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error":str(exc)[:220]},400)
        finally:
            limit.release()

    ui.run_with(app,title="Контур · Аудит сайтов",language="ru",show_welcome_message=False)
    return app


def launch():
    cli = argparse.ArgumentParser(description="Контур — аудит сайтов учреждений")
    cli.add_argument("--port", type=int, default=8765)
    cli.add_argument("--open", action="store_true")
    args = cli.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    if args.open:
        try:
            with build_opener(ProxyHandler({})).open(url+"healthz", timeout=1) as response:
                if json.load(response).get("service") == "kontur-portal":
                    webbrowser.open(url)
                    return
        except (OSError, ValueError):
            pass
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",port=args.port,proxy_headers=False))
    if args.open:
        def open_when_listening():
            import time
            for _ in range(150):
                if server.started:
                    webbrowser.open(url)
                    return
                time.sleep(.1)
        threading.Thread(target=open_when_listening,daemon=True).start()
    server.run()
