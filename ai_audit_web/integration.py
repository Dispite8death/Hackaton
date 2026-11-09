from contextlib import closing
from urllib.parse import urlsplit

from Engine import config, model_settings
from Engine.importer import SOURCE, read_organizations
from . import db
from .paths import DATA_DIR


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init()
    with closing(db.conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM settings WHERE key='case_roster_v1'").fetchone():
            return
        hosts = {urlsplit(row[0]).hostname for row in conn.execute("SELECT url FROM sites")}
        for name, url, kind in read_organizations(SOURCE):
            host = urlsplit(url).hostname
            if host not in hosts:
                conn.execute("INSERT INTO sites (name,url,type) VALUES (?,?,?)", (name,url,kind))
                hosts.add(host)
        conn.execute("INSERT INTO settings(key,value) VALUES ('case_roster_v1','1')")


def ai_status():
    with closing(config.connect_db()) as conn:
        return model_settings.portal_connection(conn)[0]


def matching_id(url, rows):
    host = urlsplit(url).hostname
    return next((site_id for site_id, site_url in rows if urlsplit(site_url).hostname == host), None)


def quick_id(url):
    with closing(db.conn()) as conn:
        return matching_id(url, conn.execute("SELECT id,url FROM sites ORDER BY id"))


def archive_id(url):
    with closing(config.connect_db()) as conn:
        return matching_id(url, conn.execute("SELECT id,site_url FROM organization ORDER BY id"))


def audit_settings():
    values = db.get_settings()
    with closing(config.connect_db()) as conn:
        _, profile, key = model_settings.portal_connection(conn)
    values.update(llm_enabled="1" if profile else "0", llm_api_key=key,
                  llm_base_url=profile.endpoint if profile else "",
                  llm_model=profile.model if profile else "", llm_mode="openai")
    return values
