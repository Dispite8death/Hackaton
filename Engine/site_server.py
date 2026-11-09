"""Serve the audit dashboard and its loopback-only administration panel."""

import secrets
import threading
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from keyring.errors import KeyringError

from . import audit_worker, config, portal_jobs, retention, worker_schedule
from .crawl_store import initialize_db
from .factcheck_sources import add_allowed_source, get_allowed_sources, remove_allowed_source
from .home_terms import CATEGORY_LABELS, add_term, get_terms, remove_term
from .model_settings import DEFAULTS, Profile, get_profile, save_profile, save_secret, set_setting, setting, portal_connection


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = {"/":("home.html","text/html"),"/system.css":("system.css","text/css"),"/archive":("index.html","text/html"),"/app.js":("app.js","text/javascript"),"/style.css":("style.css","text/css")}
ADMIN_ASSETS = {"/admin":("admin.html","text/html"),"/admin.js":("admin.js","text/javascript")}
TOKEN = secrets.token_urlsafe(32)
WORK = threading.Lock()
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
    return {"organizations":[dict(row) for row in conn.execute("SELECT id,name,site_url FROM organization ORDER BY name,id")],
            "token":TOKEN,"settings":audit_worker.get_settings(conn),"retention":retention.get_policy(conn),
            "sources":get_allowed_sources(conn),"words":get_terms(conn),"word_labels":CATEGORY_LABELS,
            "provider":get_profile(conn).provider,"profiles":{k:asdict(get_profile(conn,k)) for k in DEFAULTS},
            "ai_enabled":setting(conn,"portal_ai_enabled",False),"ai_status":portal_connection(conn)[0],
            "factcheck_auto":setting(conn,"factcheck_auto",False),"job":JOB.copy(),"cycle":audit_worker.last_status(conn)}


def start_work():
    if not WORK.acquire(blocking=False):
        raise ValueError("Цикл уже выполняется")
    JOB.update(state="running",error=None)
    def execute():
        try:
            with closing(config.connect_db()) as conn:
                result = audit_worker.run_once(conn, overrides={"model_checks":0})
            JOB.update(state=result["state"])
        except Exception as error:
            JOB.update(state="error",error=str(error)[:400])
        finally:
            WORK.release()
    threading.Thread(target=execute,daemon=True).start()
    return {"state":"running"}


def update_settings(conn, action, data):
    if action == "plan":
        return portal_jobs.preview(conn, data.get("ids"), data.get("mode"))
    if action == "analyze":
        return portal_jobs.start(conn, data.get("ids"), data.get("mode"), WORK, data.get("ticket"))
    if action == "verify":
        return portal_jobs.verify(conn)
    if action == "benchmark":
        if data.get("decline") is True:
            profile, _ = portal_jobs.profile_for_job(conn)
            portal_jobs.record_benchmark(conn, profile, None)
            return {"message":"Отказ от теста сохранён для этого профиля"}
        return portal_jobs.verify(conn, benchmark=True)
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
    if action=="disable-ai":
        with conn:
            set_setting(conn,"portal_ai_enabled",False)
        return {"ai_status":"ИИ выключен"}
    if action=="profile":
        p = Profile(**data["profile"])
        with conn:
            save_profile(conn,p)
            if data.get("secret"):
                try:
                    save_secret(conn,p,data["secret"])
                except KeyringError as error:
                    raise ValueError("Не удалось сохранить ключ в хранилище учётных данных ОС.") from error
            set_setting(conn,"portal_ai_enabled",data.get("enabled") is True)
        return {"provider":p.provider,"model":p.model,"ai_status":portal_connection(conn)[0]}
    if action=="factcheck" and type(data.get("enabled")) is bool:
        with conn:
            set_setting(conn,"factcheck_auto",data["enabled"])
        return {"enabled":data["enabled"]}
    raise ValueError("Неизвестная операция")


def main():
    from .portal import launch
    launch()


if __name__ == "__main__":
    main()
