from contextlib import closing

from Engine import config, model_settings
from Engine.roster import address_key
from . import db
from .paths import DATA_DIR


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db.init()
    with closing(config.connect_db()) as archive:
        sites = archive.execute("SELECT name,site_url,kind FROM active_organization ORDER BY id").fetchall()
    with closing(db.conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM settings WHERE key='case_roster_v1'").fetchone():
            return
        hosts = {address_key(row[0]) for row in conn.execute("SELECT url FROM sites")}
        for name, url, kind in sites:
            host = address_key(url)
            if host not in hosts:
                conn.execute("INSERT INTO sites (name,url,type) VALUES (?,?,?)", (name,url,kind))
                hosts.add(host)
        conn.execute("INSERT INTO settings(key,value) VALUES ('case_roster_v1','1')")


def ai_status():
    with closing(config.connect_db()) as conn:
        return model_settings.portal_connection(conn)[0]


def matching_id(url, rows):
    key = address_key(url)
    return next((site_id for site_id, site_url in rows if address_key(site_url) == key), None)


def quick_id(url):
    with closing(db.conn()) as conn:
        return matching_id(url, conn.execute("SELECT id,url FROM sites WHERE active=1 ORDER BY id"))


def archive_id(url):
    with closing(config.connect_db()) as conn:
        return matching_id(url, conn.execute("SELECT id,site_url FROM active_organization ORDER BY id"))


def audit_settings():
    with closing(config.connect_db()) as conn:
        _, profile, key = model_settings.portal_connection(conn)
    return {"llm_enabled": "1" if profile else "0", "llm_profile": profile, "llm_api_key": key}
