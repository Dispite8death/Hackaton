"""SQLite-хранилище веб-интерфейса: реестр, запуски, отчёты, решения инспектора, настройки.

Схема таблиц совпадает со схемой «Севера». Реестр учреждений берётся из data/institutions.csv
движка audit/. Сохранённый отчёт не изменяется после завершения запуска; решения инспектора
хранятся отдельно (таблица reviews).
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .adapter import summarize

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
STATIC = PKG / "static"
INSTITUTIONS = ROOT / "data" / "institutions.csv"


def data_dir() -> Path:
    d = Path(os.environ.get("SEVER_DATA_DIR") or ROOT / "data" / "app")
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "sever.sqlite3"


def runs_dir() -> Path:
    d = data_dir() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(db_path(), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


def init() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS organizations(id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE, kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created TEXT NOT NULL, finished TEXT, status TEXT, total INTEGER,
            done INTEGER DEFAULT 0, current TEXT, error TEXT, options TEXT);
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, run_id TEXT, org_id INTEGER, created TEXT, data TEXT,
            summary TEXT, UNIQUE(run_id,org_id));
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS reviews(fingerprint TEXT PRIMARY KEY, state TEXT, note TEXT, updated TEXT);
        CREATE INDEX IF NOT EXISTS reports_org_latest ON reports(org_id,id DESC);
        """)
        if db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0 and INSTITUTIONS.exists():
            with open(INSTITUTIONS, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    db.execute("INSERT OR IGNORE INTO organizations(name,url,kind) VALUES(?,?,?)",
                               (row["name"].strip(), "https://" + row["domain"].strip() + "/", row["kind"].strip()))
        interrupted = [r["id"] for r in db.execute("SELECT id FROM runs WHERE status IN ('queued','running')")]
        db.execute("UPDATE runs SET status='interrupted',finished=?,current=NULL,error=? WHERE status IN ('queued','running')",
                   (now(), "Сервис был остановлен. Готовые отчёты сохранены без сверки между сайтами; запустите новую проверку."))
    for rid in interrupted:
        finish_partial(rid, "Сервис был остановлен до сверки между сайтами запуска; эталон и одинаковые фото не сопоставлялись.")


def finish_partial(run_id: str, note: str) -> None:
    """Промежуточные отчёты прерванного запуска помечаются явно, ничего не удаляется."""
    with connect() as db:
        for row in db.execute("SELECT id,data FROM reports WHERE run_id=?", (run_id,)).fetchall():
            d = json.loads(row["data"])
            if not d.get("preliminary"):
                continue
            d.update(preliminary=False, interrupted=True)
            if d["status"] == "complete":
                d["status"] = "partial"
            d["limitations"].append(note)
            db.execute("UPDATE reports SET data=?,summary=? WHERE id=?",
                       (json.dumps(d, ensure_ascii=False), json.dumps(summarize(d), ensure_ascii=False), row["id"]))


def save_report(run_id: str, org_id: int, data: dict) -> int:
    with connect() as db:
        db.execute("INSERT INTO reports(run_id,org_id,created,data,summary) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(run_id,org_id) DO UPDATE SET data=excluded.data,summary=excluded.summary",
                   (run_id, org_id, now(), json.dumps(data, ensure_ascii=False),
                    json.dumps(summarize(data), ensure_ascii=False)))
        return db.execute("SELECT id FROM reports WHERE run_id=? AND org_id=?", (run_id, org_id)).fetchone()["id"]


# ------------------------------------------------------------------------------ настройки
# Значения по умолчанию совпадают с параметрами CLI audit (python -m audit run).
DEFAULTS = {
    # ИИ (audit.llm.LLMSettings); пустые значения — берутся из окружения / .env (AUDIT_LLM_*)
    "llm_enabled": False, "llm_provider": "", "base_url": "", "api_key": "", "model": "", "web_search": "",
    "json_mode": "", "llm_timeout": None, "llm_max_tokens": None, "reasoning_effort": "", "llm_news": 3,
    # обход (audit.fetcher.Fetcher, audit.checks.Options)
    "concurrency": 6, "http_concurrency": 12, "timeout": 25, "max_news": 15, "max_persons": 40,
    "image_hashes": True, "use_cache": True, "cache_ttl": 12,
    # расписание и эталоны
    "schedule_enabled": False, "schedule_hour": 8, "schedule_interval_days": 7, "next_run": None,
    "benchmark_school": None, "benchmark_kindergarten": None,
}


def settings(secret: bool = False) -> dict:
    result = dict(DEFAULTS)
    with connect() as db:
        result.update({r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")
                       if r["key"] in DEFAULTS})
    if not secret:
        result["has_api_key"] = bool(result.pop("api_key", ""))
    return result


def save_settings(values: dict) -> None:
    with connect() as db:
        for key, value in values.items():
            if key in DEFAULTS:
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


# ------------------------------------------------------------------------------ чтение
def row_summary(db, row) -> dict:
    if row["summary"]:
        return json.loads(row["summary"])
    data = json.loads(db.execute("SELECT data FROM reports WHERE id=?", (row["id"],)).fetchone()["data"])
    s = summarize(data)
    db.execute("UPDATE reports SET summary=? WHERE id=?", (json.dumps(s, ensure_ascii=False), row["id"]))
    return s


def organizations() -> list[dict]:
    with connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM organizations ORDER BY id")]
        for o in rows:
            row = db.execute("SELECT id,created,summary FROM reports WHERE org_id=? ORDER BY id DESC LIMIT 1",
                             (o["id"],)).fetchone()
            o["latest"] = dict(row_summary(db, row), id=row["id"], created=row["created"]) if row else None
    return rows


def report(report_id: int) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        return None
    r = dict(row)
    r["data"] = json.loads(r["data"])
    r.pop("summary", None)
    return r


def latest_reports() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT * FROM reports WHERE id IN (SELECT MAX(id) FROM reports GROUP BY org_id)").fetchall()
    return [dict(r, data=json.loads(r["data"])) for r in rows]


def reviews() -> dict:
    with connect() as db:
        return {r["fingerprint"]: dict(r) for r in db.execute("SELECT * FROM reviews")}
