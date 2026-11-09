import sqlite3
from contextlib import closing
import json
import re
from datetime import datetime

from .paths import DATA_DIR
from Engine import operation_log

DB_PATH = DATA_DIR / "audit.db"


def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with closing(conn()) as c, c:
        operation_log.initialize(c)
        c.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'school',
            active INTEGER NOT NULL DEFAULT 1,
            email TEXT DEFAULT ''
        )
        """)
        try:
            c.execute("ALTER TABLE sites ADD COLUMN email TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            sites_total INTEGER NOT NULL DEFAULT 0,
            avg_score REAL
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS run_sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            site_name TEXT,
            site_url TEXT,
            score INTEGER,
            critical INTEGER,
            high INTEGER,
            medium INTEGER,
            low INTEGER,
            latest_news TEXT,
            future_events INTEGER,
            metrics_json TEXT,
            llm_summary TEXT
        )
        """)

        columns = {r[1] for r in c.execute("PRAGMA table_info(run_sites)")}
        if "ai_explanation" not in columns:
            c.execute("ALTER TABLE run_sites ADD COLUMN ai_explanation TEXT")
        if "checked_at" not in columns:
            c.execute("ALTER TABLE run_sites ADD COLUMN checked_at TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS quick_site_runs ON run_sites(site_id,run_id DESC)")
        c.execute("""
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            site_id INTEGER NOT NULL,
            agent TEXT,
            severity TEXT,
            code TEXT,
            message TEXT,
            url TEXT,
            recommendation TEXT
        )
        """)

        c.execute("CREATE INDEX IF NOT EXISTS quick_run_findings ON findings(run_id,site_id)")
        c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS audit_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_type TEXT NOT NULL,
            rule_key TEXT NOT NULL,
            rule_value TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS photo_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_key TEXT NOT NULL,
            section_label TEXT NOT NULL,
            page_keywords TEXT NOT NULL DEFAULT '',
            require_faces INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1
        )
        """)

        default_rules = [
            ("section", "school", "Новости"), ("section", "school", "Мероприятия"),
            ("section", "school", "Документы"), ("section", "school", "Расписание"),
            ("section", "school", "О школе"), ("section", "school", "Контакты"),
            ("section", "school", "Педагогам и сотрудникам"),
            ("section", "school", "Сведения об образовательной организации"),
            ("section", "kindergarten", "Новости"), ("section", "kindergarten", "Наш коллектив"),
            ("section", "kindergarten", "Педагогический состав"),
            ("section", "kindergarten", "Сведения об образовательной организации"),
            ("section", "kindergarten", "Педагогам и сотрудникам"),
            ("section", "kindergarten", "Контакты"),
            ("document", "school", "Устав"), ("document", "school", "Лицензия"),
            ("document", "school", "Аккредитация"), ("document", "school", "Образовательная программа"),
            ("document", "school", "Прием в школу"), ("document", "school", "Платные услуги"),
            ("document", "school", "Режим занятий"), ("document", "school", "Питание"),
            ("document", "school", "Безопасность"),
            ("document", "kindergarten", "Устав"), ("document", "kindergarten", "Лицензия"),
            ("document", "kindergarten", "Образовательная программа"),
            ("document", "kindergarten", "Режим дня"),
            ("document", "kindergarten", "Питание"), ("document", "kindergarten", "Медицинское обслуживание"),
            ("document", "kindergarten", "Безопасность"), ("document", "kindergarten", "Платные услуги"),
            ("document", "kindergarten", "Прием в детский сад"),
            ("placeholder", "text", "заглушка"), ("placeholder", "text", "введите текст"),
            ("placeholder", "text", "введите описание"), ("placeholder", "text", "добавьте описание"),
            ("placeholder", "text", "добавьте текст"), ("placeholder", "text", "укажите цель"),
            ("placeholder", "text", "укажите цели и задачи"), ("placeholder", "text", "измените текст"),
            ("placeholder", "text", "измените описание"), ("placeholder", "text", "демонстрационный контент"),
            ("placeholder", "text", "текст будет добавлен"), ("placeholder", "text", "информация будет добавлена"),
            ("placeholder", "text", "фото будет добавлено"), ("placeholder", "text", "раздел находится в разработке"),
            ("placeholder", "text", "lorem ipsum"),
        ]

        count = c.execute("SELECT COUNT(*) FROM audit_rules").fetchone()[0]
        if count == 0:
            for rt, rk, rv in default_rules:
                c.execute(
                    "INSERT INTO audit_rules (rule_type, rule_key, rule_value, active) VALUES (?, ?, ?, 1)",
                    (rt, rk, rv))

        pt_count = c.execute("SELECT COUNT(*) FROM photo_targets").fetchone()[0]
        if pt_count == 0:
            default_targets = [
                ("staff", "Педагогический состав", "pedagog,sostav,педагог,состав", 1),
                ("leadership", "Руководство / Администрация", "rukovodstvo,admin,руководств,директор", 1),
                ("collective", "Наш коллектив", "kollektiv,коллектив", 1),
            ]
            for sk, sl, kw, rf in default_targets:
                c.execute(
                    "INSERT INTO photo_targets (section_key, section_label, page_keywords, require_faces, active) "
                    "VALUES (?, ?, ?, ?, 1)", (sk, sl, kw, rf))

        defaults = {
            "llm_enabled": "0", "llm_mode": "ollama",
            "llm_url": "http://127.0.0.1:11434/api/generate",
            "llm_base_url": "http://127.0.0.1:11434/v1",
            "llm_model": "qwen2.5:7b-instruct", "llm_api_key": "",
            "extra_hosts": "", "max_pages": "40",
            "news_max_age_days": "14",
            "auto_detect_type": "1",
            "photo_target_sizes": "714x952",
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


def add_site(name, url, site_type, email=""):
    with closing(conn()) as c, c:
        c.execute("INSERT INTO sites (name, url, type, active, email) VALUES (?, ?, ?, 1, ?)",
                  (name, url, site_type, email))


def update_site_email(site_id, email):
    with closing(conn()) as c, c:
        c.execute("UPDATE sites SET email=? WHERE id=?", (email, site_id))


def get_sites(active_only=True):
    query = "SELECT * FROM sites"
    if active_only:
        query += " WHERE active=1"
    query += " ORDER BY id DESC"
    with closing(conn()) as c, c:
        return [dict(r) for r in c.execute(query).fetchall()]


def get_site(site_id):
    with closing(conn()) as c, c:
        row = c.execute("SELECT * FROM sites WHERE id=? AND active=1", (site_id,)).fetchone()
        return dict(row) if row else None


def delete_site(site_id):
    with closing(conn()) as c, c:
        c.execute("DELETE FROM sites WHERE id=?", (site_id,))


def get_settings():
    defaults = {
        "llm_enabled": "0", "llm_mode": "ollama",
        "llm_url": "http://127.0.0.1:11434/api/generate",
        "llm_base_url": "http://127.0.0.1:11434/v1",
        "llm_model": "qwen2.5:7b-instruct", "llm_api_key": "",
        "extra_hosts": "", "max_pages": "40",
        "news_max_age_days": "14",
        "auto_detect_type": "1",
        "photo_target_sizes": "714x952",
    }
    with closing(conn()) as c, c:
        for r in c.execute("SELECT key, value FROM settings").fetchall():
            defaults[r["key"]] = r["value"]
    return defaults


def save_settings(data):
    with closing(conn()) as c, c:
        for k, v in data.items():
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)))


def get_audit_rules():
    with closing(conn()) as c, c:
        rows = c.execute("SELECT * FROM audit_rules WHERE active=1 ORDER BY rule_type, rule_key").fetchall()
        rules = {"sections": {"school": [], "kindergarten": []},
                 "documents": {"school": [], "kindergarten": []}, "placeholders": []}
        for r in rows:
            r = dict(r)
            if r["rule_type"] == "section":
                rules["sections"][r["rule_key"]].append(r["rule_value"])
            elif r["rule_type"] == "document":
                rules["documents"][r["rule_key"]].append(r["rule_value"])
            elif r["rule_type"] == "placeholder":
                rules["placeholders"].append(r["rule_value"])
        return rules


def add_audit_rule(rule_type, rule_key, rule_value):
    with closing(conn()) as c, c:
        c.execute("INSERT INTO audit_rules (rule_type, rule_key, rule_value, active) VALUES (?, ?, ?, 1)",
                  (rule_type, rule_key, rule_value))


def update_audit_rule(rule_id, rule_value):
    with closing(conn()) as c, c:
        c.execute("UPDATE audit_rules SET rule_value=? WHERE id=?", (rule_value.strip(), rule_id))


def delete_audit_rule(rule_id):
    with closing(conn()) as c, c:
        c.execute("DELETE FROM audit_rules WHERE id=?", (rule_id,))


def get_all_audit_rules_raw():
    with closing(conn()) as c, c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM audit_rules ORDER BY rule_type, rule_key").fetchall()]


def get_photo_targets(active_only=True):
    q = "SELECT * FROM photo_targets"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY id"
    with closing(conn()) as c, c:
        return [dict(r) for r in c.execute(q).fetchall()]


def add_photo_target(section_key, section_label, page_keywords, require_faces):
    with closing(conn()) as c, c:
        c.execute(
            "INSERT INTO photo_targets (section_key, section_label, page_keywords, require_faces, active) "
            "VALUES (?, ?, ?, ?, 1)",
            (section_key.strip(), section_label.strip(), page_keywords.strip(), 1 if require_faces else 0))


def delete_photo_target(target_id):
    with closing(conn()) as c, c:
        c.execute("DELETE FROM photo_targets WHERE id=?", (target_id,))


def set_photo_target_active(target_id, active):
    with closing(conn()) as c, c:
        c.execute("UPDATE photo_targets SET active=? WHERE id=?", (1 if active else 0, target_id))


def start_run(sites_total):
    with closing(conn()) as c, c:
        cur = c.execute("INSERT INTO runs (started_at, status, sites_total) VALUES (?, ?, ?)",
                        (datetime.now().isoformat(), "running", sites_total))
        return cur.lastrowid


def finish_run(run_id, status, avg_score):
    with closing(conn()) as c, c:
        c.execute("UPDATE runs SET finished_at=?, status=?, avg_score=? WHERE id=?",
                  (datetime.now().isoformat(), status, avg_score, run_id))


def last_run():
    with closing(conn()) as c, c:
        row = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def save_site_result(run_id, site, result):
    counts = result.get("counts", {})
    metrics = result.get("metrics", {})
    with closing(conn()) as c, c:
        c.execute("""
            INSERT INTO run_sites (run_id, site_id, site_name, site_url, score,
                critical, high, medium, low, latest_news, future_events, metrics_json, llm_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (run_id, site["id"], site["name"], site["url"], result.get("score", 0),
                   counts.get("CRITICAL", 0), counts.get("HIGH", 0), counts.get("MEDIUM", 0),
                   counts.get("LOW", 0), metrics.get("latest_news", ""),
                   metrics.get("future_events", 0), json.dumps(metrics, ensure_ascii=False),
                   result.get("llm_summary", "")))
        for f in result.get("findings", []):
            c.execute("""
                INSERT INTO findings (run_id, site_id, agent, severity, code, message, url, recommendation)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                      (run_id, site["id"], f.get("agent", ""), f.get("severity", "LOW"),
                       f.get("code", ""), f.get("message", ""), f.get("url", ""),
                       f.get("recommendation", "")))


def get_last_site_result(site_id):
    with closing(conn()) as c, c:
        row = c.execute("""
            SELECT rs.* FROM run_sites rs JOIN runs r ON rs.run_id = r.id
            WHERE rs.site_id=? AND (r.status IN ('done','error') OR rs.checked_at IS NOT NULL)
            ORDER BY r.id DESC LIMIT 1""", (site_id,)).fetchone()
        return dict(row) if row else None


def save_summary(result_id, text):
    with closing(conn()) as c, c:
        c.execute("UPDATE run_sites SET llm_summary=? WHERE id=?", (text, result_id))


def save_explanation(result_id, text):
    with closing(conn()) as c, c:
        c.execute("UPDATE run_sites SET ai_explanation=? WHERE id=?", (text, result_id))


def get_site_findings(run_id, site_id):
    with closing(conn()) as c, c:
        rows = c.execute("""
            SELECT * FROM findings WHERE run_id=? AND site_id=?
            ORDER BY CASE severity WHEN 'CRITICAL' THEN 4 WHEN 'HIGH' THEN 3
                WHEN 'MEDIUM' THEN 2 WHEN 'LOW' THEN 1 ELSE 0 END DESC""",
                         (run_id, site_id)).fetchall()
        return [dict(r) for r in rows]


def get_site_history(site_id):
    with closing(conn()) as c, c:
        rows = c.execute("""
            SELECT rs.run_id, rs.score, rs.critical, rs.high, rs.medium, rs.low,
                   r.started_at, r.finished_at
            FROM run_sites rs JOIN runs r ON r.id = rs.run_id
            WHERE rs.site_id = ? AND r.status = 'done' ORDER BY r.id ASC""",
                         (site_id,)).fetchall()
        return [dict(r) for r in rows]


def _finding_key(f):
    msg = re.sub(r"\d+", "#", f.get("message", ""))
    return (f.get("agent", ""), f.get("code", ""), msg)


def get_findings_diff(site_id):
    with closing(conn()) as c, c:
        runs = c.execute("""
            SELECT rs.run_id FROM run_sites rs JOIN runs r ON r.id = rs.run_id
            WHERE rs.site_id=? AND r.status='done' ORDER BY r.id DESC LIMIT 2""",
                         (site_id,)).fetchall()
        if len(runs) < 2:
            return None
        cur_id, prev_id = runs[0]["run_id"], runs[1]["run_id"]
        cur = [dict(r) for r in c.execute("SELECT * FROM findings WHERE run_id=? AND site_id=?", (cur_id,site_id))]
        prev = [dict(r) for r in c.execute("SELECT * FROM findings WHERE run_id=? AND site_id=?", (prev_id,site_id))]
    cur_keys = {_finding_key(f): f for f in cur}
    prev_keys = {_finding_key(f): f for f in prev}
    fixed = [prev_keys[k] for k in prev_keys if k not in cur_keys]
    new = [cur_keys[k] for k in cur_keys if k not in prev_keys]
    unchanged = len([k for k in cur_keys if k in prev_keys])
    return fixed, new, unchanged


def dashboard_stats():
    with closing(conn()) as c, c:
        total_sites = c.execute("SELECT COUNT(*) AS c FROM sites WHERE active=1").fetchone()["c"]
        last = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if not last:
            return {"has_run": False, "total_sites": total_sites, "last_run": None,
                    "avg_score": 0, "critical_sites": 0, "severity_counts": {},
                    "agent_counts": [], "top_bad": []}
        run_sites = c.execute("SELECT * FROM run_sites WHERE run_id=? ORDER BY score ASC",
                              (last["id"],)).fetchall()
        severity_rows = c.execute("SELECT severity, COUNT(*) AS c FROM findings WHERE run_id=? GROUP BY severity",
                                  (last["id"],)).fetchall()
        agent_rows = c.execute("SELECT agent, COUNT(*) AS c FROM findings WHERE run_id=? GROUP BY agent ORDER BY c DESC",
                               (last["id"],)).fetchall()
        severity_counts = {r["severity"]: r["c"] for r in severity_rows}
        agent_counts = [{"name": r["agent"], "value": r["c"]} for r in agent_rows]
        critical_sites = sum(1 for r in run_sites if r["critical"] > 0)
        top_bad = [{"site_name": r["site_name"], "site_url": r["site_url"], "score": r["score"],
                    "critical": r["critical"], "high": r["high"], "medium": r["medium"], "low": r["low"]}
                   for r in run_sites[:10]]
        return {"has_run": True, "total_sites": total_sites, "last_run": dict(last),
                "avg_score": round(last["avg_score"] or 0, 1), "critical_sites": critical_sites,
                "severity_counts": severity_counts, "agent_counts": agent_counts, "top_bad": top_bad}
