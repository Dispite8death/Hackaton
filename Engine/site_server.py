"""Serve the audit dashboard and its loopback-only administration panel."""

import argparse
import gzip
import hmac
import json
import secrets
import sqlite3
import threading
import webbrowser
from contextlib import closing
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, build_opener

from . import audit_worker, config, retention, worker_schedule
from .crawl_store import initialize_db
from .factcheck_sources import add_allowed_source, get_allowed_sources, remove_allowed_source
from .home_terms import CATEGORY_LABELS, add_term, get_terms, remove_term
from .model_settings import DEFAULTS, Profile, get_profile, save_profile, save_secret, set_setting, setting


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = {"/":("index.html","text/html"),"/app.js":("app.js","text/javascript"),"/style.css":("style.css","text/css")}
ADMIN_ASSETS = {"/admin":("admin.html","text/html"),"/admin.js":("admin.js","text/javascript")}
TOKEN = secrets.token_urlsafe(32)
WORK = threading.Lock()
EXPENSIVE = threading.BoundedSemaphore(3)
JOB = {"state":"idle"}


def prepare_database():
    from .importer import SOURCE, read_organizations
    with closing(config.connect_db()) as conn:
        initialize_db(conn)
        if not conn.execute("SELECT 1 FROM organization LIMIT 1").fetchone():
            with conn:
                conn.executemany("INSERT OR IGNORE INTO organization(name,site_url,kind) VALUES(?,?,?)",
                                 read_organizations(SOURCE))


def admin_state(conn):
    return {"token":TOKEN,"settings":audit_worker.get_settings(conn),"retention":retention.get_policy(conn),
            "sources":get_allowed_sources(conn),"words":get_terms(conn),"word_labels":CATEGORY_LABELS,
            "provider":get_profile(conn).provider,"profiles":{k:asdict(get_profile(conn,k)) for k in DEFAULTS},
            "factcheck_auto":setting(conn,"factcheck_auto",False),"job":JOB.copy(),"cycle":audit_worker.last_status(conn)}


def start_work():
    if not WORK.acquire(blocking=False):
        raise ValueError("Цикл уже выполняется")
    JOB.update(state="running",error=None)
    def execute():
        try:
            with closing(config.connect_db()) as conn:
                result = audit_worker.run_once(conn)
            JOB.update(state=result["state"])
        except Exception as error:
            JOB.update(state="error",error=str(error)[:400])
        finally:
            WORK.release()
    threading.Thread(target=execute,daemon=True).start()
    return {"state":"running"}


def update_settings(conn, action, data):
    if action=="settings":
        previous = audit_worker.get_settings(conn)["interval_minutes"]
        interval = data["settings"].get("interval_minutes", previous)
        if interval != previous and worker_schedule.schedule_info(conn).get("enabled"):
            validated = audit_worker._validated({**audit_worker.get_settings(conn), **data["settings"]})
            worker_schedule.enable_schedule(conn, validated["interval_minutes"])
        audit_worker.save_settings(conn, data["settings"])
        return {"message":"Параметры обновления сохранены"}
    if action=="retention":
        if data.get("preview"):
            return retention.maintain(conn,apply=False)
        return retention.save_policy(conn,data)
    if action=="schedule":
        operation = data["operation"]
        if operation=="enable":
            return worker_schedule.enable_schedule(conn,audit_worker.get_settings(conn)["interval_minutes"])
        if operation=="disable":
            return worker_schedule.disable_schedule(conn)
        if operation=="status":
            return worker_schedule.schedule_info(conn)
    if action=="cycle":
        return start_work()
    if action=="sources" and data["operation"] in ("add","remove"):
        with conn:
            (add_allowed_source if data["operation"]=="add" else remove_allowed_source)(conn,data["url"])
        return {"sources":get_allowed_sources(conn)}
    if action=="words" and data["operation"] in ("add","remove"):
        with conn:
            (add_term if data["operation"]=="add" else remove_term)(conn,data["category"],data["word"])
        return {"words":get_terms(conn)}
    if action=="profile":
        p = Profile(**data["profile"])
        with conn:
            save_profile(conn,p)
            if data.get("secret"):
                save_secret(conn,p,data["secret"])
        return {"provider":p.provider,"model":p.model}
    if action=="factcheck" and type(data.get("enabled")) is bool:
        with conn:
            set_setting(conn,"factcheck_auto",data["enabled"])
        return {"enabled":data["enabled"]}
    raise ValueError("Неизвестная операция")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "Audit"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self,*args):
        pass

    def local_admin(self):
        port = self.server.server_address[1]
        return (self.server.admin and self.client_address[0]=="127.0.0.1" and
                self.headers.get("Host","").casefold() in {f"127.0.0.1:{port}",f"localhost:{port}"})

    def send_bytes(self,code,body,content_type,*,download=None):
        packed = (len(body)>8192 and content_type.startswith(("application/json","text/"))
                  and "gzip" in self.headers.get("Accept-Encoding",""))
        if packed:
            body = gzip.compress(body,compresslevel=2)
        self.send_response(code)
        self.send_header("Content-Type",content_type)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("Cache-Control","no-store")
        self.send_header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if packed:
            self.send_header("Content-Encoding","gzip")
            self.send_header("Vary","Accept-Encoding")
        if download:
            self.send_header("Content-Disposition",f'attachment; filename="{download}"')
        self.end_headers()
        if self.command!="HEAD":
            self.wfile.write(body)

    def json(self,value,code=200):
        self.send_bytes(code,json.dumps(value,ensure_ascii=False,allow_nan=False).encode(),"application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.read_route()
        except (BrokenPipeError,ConnectionError):
            pass
        except (ValueError,KeyError,TypeError) as exc:
            self.json({"error":str(exc)[:220]},400)
        except Exception:
            self.json({"error":"Не удалось прочитать архив. Проверьте состояние локального сервера."},500)

    def read_route(self):
        target = urlsplit(self.path)
        path = target.path
        asset = ASSETS.get(path) or (ADMIN_ASSETS.get(path) if self.local_admin() else None)
        if asset:
            name,mime = asset
            self.send_bytes(200,(WEB/name).read_bytes(),mime+"; charset=utf-8")
            return
        if path in ("/healthz","/api/access"):
            self.json({"ok":True,"service":"gosweb-audit","admin":self.local_admin()})
            return
        if path=="/api/admin/state" and self.local_admin():
            with closing(config.connect_db()) as conn:
                self.json(admin_state(conn))
            return
        parts = path.strip("/").split("/")
        detail = len(parts)==3 and parts[:2]==["api","institution"] and parts[2].isdigit()
        if path not in ("/api/dashboard","/report.pdf") and not detail:
            self.json({"error":"Страница не найдена"},404)
            return
        if not EXPENSIVE.acquire(timeout=2):
            self.json({"error":"Сервер готовит другие выборки. Повторите запрос через несколько секунд."},503)
            return
        try:
            filters = {k:v if len(v)>1 else v[0] for k,v in parse_qs(target.query,max_num_fields=120).items()}
            if len(target.query)>8192:
                raise ValueError("Слишком длинные параметры")
            from . import dashboard_data
            uri = Path(config.DB_PATH).resolve().as_uri()+"?mode=ro"
            with closing(sqlite3.connect(uri,uri=True,timeout=20)) as conn:
                conn.row_factory = sqlite3.Row
                if path=="/report.pdf":
                    from .dashboard_pdf import build_pdf
                    self.send_bytes(200,build_pdf(conn,filters),"application/pdf",download="audit-report.pdf")
                elif detail:
                    if not conn.execute("SELECT 1 FROM organization WHERE id=?",(int(parts[2]),)).fetchone():
                        self.json({"error":"Учреждение не найдено"},404)
                    else:
                        self.json(dashboard_data.institution(conn,int(parts[2]),filters))
                else:
                    self.json(dashboard_data.dashboard(conn,filters))
        finally:
            EXPENSIVE.release()

    def do_POST(self):
        if not self.local_admin() or not self.path.startswith("/api/admin/"):
            self.json({"error":"Администрирование доступно только на этом компьютере"},403)
            return
        if (self.headers.get("Origin") != "http://"+self.headers.get("Host","")
                or not hmac.compare_digest(self.headers.get("X-Audit-Token",""),TOKEN)):
            self.json({"error":"Откройте настройки с локальной страницы"},403)
            return
        try:
            count = int(self.headers.get("Content-Length",0))
            if not 0<count<=16000 or self.headers.get_content_type()!="application/json":
                raise ValueError("Ожидается JSON до 16 КБ")
            data = json.loads(self.rfile.read(count))
            if not isinstance(data,dict):
                raise ValueError("Неверные параметры")
            with closing(config.connect_db()) as conn:
                self.json(update_settings(conn,self.path.removeprefix("/api/admin/"),data))
        except (KeyError,ValueError,RuntimeError,OSError,TypeError) as exc:
            self.json({"error":str(exc)[:350]},400)


def main():
    cli = argparse.ArgumentParser(description="Сайт аудита и единая панель")
    cli.add_argument("--port",type=int,default=8765)
    cli.add_argument("--public-port",type=int,default=8766)
    cli.add_argument("--open",action="store_true")
    args = cli.parse_args()
    local_url = f"http://127.0.0.1:{args.port}/"
    if args.open:
        try:
            with build_opener(ProxyHandler({})).open(local_url + "healthz", timeout=1) as reply:
                info = json.load(reply)
            if info.get("service") == "gosweb-audit" and info.get("admin"):
                webbrowser.open(local_url)
                return
        except (OSError, ValueError):
            pass
    prepare_database()
    with Server(("127.0.0.1",args.port),Handler) as local, Server(("127.0.0.1",args.public_port),Handler) as public:
        local.admin,public.admin = True,False
        threading.Thread(target=public.serve_forever,daemon=True).start()
        if args.open:
            webbrowser.open(local_url)
        try:
            local.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            public.shutdown()


if __name__=="__main__":
    main()
