"""Configuration baseline for an isolated jury instance. Never apply to the production database."""
import json
import os
from pathlib import Path

from . import config, model_settings

ARCHIVE = ("model_profile","retention_policy","home_term","factcheck_allowed_source")
QUICK = ("settings","audit_rules","photo_targets","section_markers")
SCOPE = "name IN ('active_provider','portal_ai_enabled','audit_worker_settings','factcheck_auto') OR name LIKE 'last_probe_%' OR name LIKE 'portal_benchmark_%'"


def directory():
    if not (raw := os.environ.get("KONTUR_DEMO_DIR")):
        return None
    root = Path(raw).resolve()
    if Path(config.DB_PATH).resolve() != root/"search.sqlite3":
        raise ValueError("Демонстрация требует отдельной копии архива.")
    return root


def baseline(conn, *, restore=False):
    root = directory()
    if root is None:
        raise ValueError("Сброс доступен в демонстрационной копии.")
    path = root/"baseline.json"
    from ai_audit_web import db
    if Path(db.DB_PATH).resolve() != root/"data/quick/audit.db":
        raise ValueError("Требуется отдельная база экспресс-аудита.")
    if not restore and path.exists():
        return
    conn.execute("ATTACH DATABASE ? AS quick_demo",(str(db.DB_PATH),))
    try:
        tables = [(t, "") for t in ARCHIVE]+[("app_setting"," WHERE "+SCOPE)]
        tables += [("quick_demo."+t, "") for t in QUICK]
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if restore:
                saved=json.loads(path.read_text(encoding="utf8"))
                for table,where in tables:
                    conn.execute("DELETE FROM "+table+where)
                    rows=saved[table]
                    if rows:
                        conn.executemany("INSERT INTO "+table+" VALUES("+",".join("?" for _ in rows[0])+")",rows)
            else:
                saved={table:[list(r) for r in conn.execute("SELECT * FROM "+table+where)] for table,where in tables}
                tmp=path.with_suffix(".tmp")
                tmp.write_text(json.dumps(saved,ensure_ascii=False),encoding="utf8");tmp.replace(path)
    finally:
        conn.execute("DETACH DATABASE quick_demo")


def validate(action, data):
    if not directory():
        return
    if action in ("roster","schedule","sources"):
        raise ValueError("На публичном показе реестр, сетевые источники и расписание ПК закреплены владельцем.")
    if action=="profile":
        p=data.get("profile",{})
        fixed=model_settings.DEFAULTS["lmstudio"]
        if (p.get("provider"),p.get("endpoint","").rstrip("/"),p.get("model"))!=(fixed.provider,fixed.endpoint,fixed.model) or data.get("secret"):
            raise ValueError("В показе подключена локальная Qwen. Можно менять формат, тайм-аут и режим рассуждений.")
