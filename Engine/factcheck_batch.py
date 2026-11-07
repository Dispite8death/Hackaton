"""Persistent, sequential fact-check queue for immutable publication snapshots."""

import argparse
import json
from collections import Counter
from contextlib import closing
from uuid import uuid4

if __package__:
    from .factcheck import run_factcheck
    from .factcheck_sources import allowlist_fingerprint
    from .model_settings import setting
else:
    from factcheck import run_factcheck
    from factcheck_sources import allowlist_fingerprint
    from model_settings import setting


BATCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS factcheck_job (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES crawl_run(id),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','leased','done','error')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until INTEGER,
    retry_after INTEGER,
    check_id INTEGER REFERENCES factcheck_run(id),
    outcome TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS factcheck_job_run ON factcheck_job(run_id,state,page_id);
CREATE TABLE IF NOT EXISTS factcheck_job_policy (
    page_id INTEGER PRIMARY KEY REFERENCES factcheck_job(page_id) ON DELETE CASCADE,
    allowlist_fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factcheck_job_history (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES factcheck_job(page_id) ON DELETE CASCADE,
    allowlist_fingerprint TEXT,
    archived_at INTEGER NOT NULL DEFAULT (unixepoch()),
    state TEXT NOT NULL, attempts INTEGER NOT NULL, check_id INTEGER REFERENCES factcheck_run(id),
    outcome TEXT, error TEXT, retry_after INTEGER, lease_until INTEGER
);
"""

LEASE_SECONDS = 3600
RETRY_SECONDS = 900


def _search_failed(report):
    """Support previous attempts recorded before the checker distinguished provider outages."""
    if report["status"] == "retryable":
        return True
    searches = report["searches"]
    sources = report["sources"]
    if report["status"] == "corroborated":
        return False
    if sources and all(source["status"] == "unreadable" for source in sources):
        return True
    return bool(searches and not sources and all(item["status"] == "error" for item in searches))


def _outcome(report):
    if _search_failed(report):
        reasons = [s.get("error") or s.get("provider") for s in report["searches"] if s["status"] == "error"]
        reasons.extend(s.get("error") for s in report["sources"] if s["status"] == "unreadable")
        message = "; ".join(dict.fromkeys(r for r in reasons if r)) or "Поисковые сервисы или публикации недоступны"
        return "error", message[:1500]
    return "done", None


def _save(conn, page_id, check_id, outcome, state, error, token=None, checked_at=None):
    return conn.execute("""
        UPDATE factcheck_job SET state=?, check_id=?, outcome=?, error=?, lease_token=NULL, lease_until=NULL,
               retry_after=CASE WHEN ?='error' THEN coalesce(?,unixepoch())+? END
        WHERE page_id=? AND (lease_token=? OR ? IS NULL)
    """, (state, check_id, outcome, error, state, checked_at, RETRY_SECONDS, page_id, token, token)).rowcount


def _align_policy(conn, scope, run_ids, fingerprint):
    """Start a fresh queue entry when its allowlist changes, keeping past attempts and leases."""
    rows = conn.execute(f"""
        SELECT j.page_id,p.allowlist_fingerprint,j.state,j.attempts,j.check_id,j.outcome,j.error,
               j.retry_after,j.lease_until
        FROM factcheck_job j LEFT JOIN factcheck_job_policy p USING(page_id)
        WHERE j.run_id IN ({scope}) AND p.allowlist_fingerprint IS NOT ?
    """, (*run_ids, fingerprint)).fetchall()
    if not rows:
        return
    old_attempts = [row for row in rows if row[2] != "pending" or row[3] or row[4] is not None]
    conn.executemany("""INSERT INTO factcheck_job_history(page_id,allowlist_fingerprint,state,attempts,
                        check_id,outcome,error,retry_after,lease_until) VALUES(?,?,?,?,?,?,?,?,?)""", old_attempts)
    conn.executemany("""UPDATE factcheck_job SET state='pending',attempts=0,check_id=NULL,outcome=NULL,
                        error=NULL,retry_after=NULL,lease_token=NULL,lease_until=NULL WHERE page_id=?""",
                     ((r[0],) for r in old_attempts))
    conn.executemany("""INSERT INTO factcheck_job_policy(page_id,allowlist_fingerprint) VALUES(?,?)
                        ON CONFLICT(page_id) DO UPDATE SET allowlist_fingerprint=excluded.allowlist_fingerprint""",
                     ((r[0],fingerprint) for r in rows))


def _prepare(conn, scope, run_ids):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        queued = conn.execute(f"""
            INSERT INTO factcheck_job(page_id,run_id)
            SELECT f.page_id,f.run_id FROM crawl_fetch f JOIN page_passport p ON p.page_id=f.page_id
            WHERE f.run_id IN ({scope}) AND p.page_kind IN ('news_detail','event_detail')
            ON CONFLICT(page_id) DO NOTHING
        """, run_ids).rowcount
        fingerprint = allowlist_fingerprint(conn)
        _align_policy(conn, scope, run_ids, fingerprint)
        reused_ids = _recover(conn, scope, run_ids, fingerprint)
    return queued, reused_ids, fingerprint


def _recover(conn, scope, run_ids, fingerprint):
    # A process can exit after committing the evidence and before releasing its lease.
    # Adopt that recorded result, including compatible checks made manually.
    records = conn.execute(f"""
        SELECT j.page_id,r.id,r.status,r.searches,r.checked_at
        FROM factcheck_job j JOIN factcheck_run r ON r.id=(
            SELECT max(id) FROM factcheck_run WHERE page_id=j.page_id
              AND json_extract(claim,'$.allowlist_fingerprint')=?
              AND (status='corroborated' OR json_array_length(searches)>0))
        WHERE j.run_id IN ({scope}) AND j.state!='done'
          AND (j.state!='leased' OR j.lease_until<=unixepoch())
          AND r.id>coalesce(j.check_id,0)
    """, (fingerprint,*run_ids)).fetchall()
    reused_ids = []
    for page_id, check_id, status, searches, checked_at in records:
        searches = json.loads(searches)
        sources = [json.loads(r[0]) for r in conn.execute(
            "SELECT evidence FROM factcheck_source WHERE check_id=?", (check_id,))]
        state, error = _outcome({"status":status,"searches":searches,"sources":sources})
        _save(conn, page_id, check_id, status, state, error, checked_at=checked_at)
        if state == "done":
            reused_ids.append(page_id)
    return reused_ids


def _claim(conn, scope, run_ids, reused_ids, fingerprint):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if allowlist_fingerprint(conn) != fingerprint:
            return None
        _align_policy(conn, scope, run_ids, fingerprint)
        reused_ids.extend(_recover(conn, scope, run_ids, fingerprint))
        row = conn.execute(f"""
            SELECT page_id,check_id FROM factcheck_job WHERE run_id IN ({scope})
              AND (state='pending' OR (state='error' AND retry_after<=unixepoch())
                   OR (state='leased' AND lease_until<=unixepoch()))
            ORDER BY attempts,page_id LIMIT 1
        """, run_ids).fetchone()
        if row is None:
            return None
        page_id, previous_check = row
        token = uuid4().hex
        conn.execute("""UPDATE factcheck_job SET state='leased',lease_token=?,lease_until=unixepoch()+?,
                     attempts=attempts+1,retry_after=NULL WHERE page_id=?""", (token, LEASE_SECONDS, page_id))
    return page_id, previous_check, token


def batch_check(conn, run_ids, *, limit=None, fetch=None):
    """Enqueue selected runs and process up to limit snapshot attempts. Zero only prepares the queue."""
    if conn.in_transaction:
        raise ValueError("Перед проверкой завершите транзакцию записи")
    run_ids = tuple(dict.fromkeys(run_ids))
    if any(type(run_id) is not int or run_id < 1 for run_id in run_ids):
        raise ValueError("ID обходов должны быть положительными целыми числами")
    limit = setting(conn, "factcheck_batch_limit", 3) if limit is None else limit
    if type(limit) is not int or limit < 0:
        raise ValueError("Лимит проверок должен быть неотрицательным целым числом")
    scope = ",".join("?" for _ in run_ids)
    queued, reused_ids, fingerprint = _prepare(conn, scope, run_ids) if run_ids else (0, [], None)
    processed_ids, error_ids = [], []
    for _ in range(limit if run_ids else 0):
        job = _claim(conn, scope, run_ids, reused_ids, fingerprint)
        if job is None:
            break
        page_id, check_id, token = job
        report = None
        try:
            report = run_factcheck(conn, page_id, fetch=fetch)
            check_id, outcome = report["check_id"], report["status"]
            state, error = _outcome(report)
        except Exception as exc:
            conn.rollback()
            outcome, state, error = "worker_error", "error", str(exc)[:1500]
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if allowlist_fingerprint(conn) != fingerprint:
                break
            if report is not None:
                stored = conn.execute("""SELECT json_extract(claim,'$.allowlist_fingerprint')
                    FROM factcheck_run WHERE id=? AND page_id=?""",(check_id,page_id)).fetchone()
                if not stored or stored[0] != fingerprint:
                    # A change between the claim and the search can yield evidence for a different policy.
                    _save(conn, page_id, None, None, "pending", None, token)
                    break
            if _save(conn, page_id, check_id, outcome, state, error, token):
                (processed_ids if state == "done" else error_ids).append(page_id)
    jobs = []
    policy_changed = False
    if run_ids:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current = allowlist_fingerprint(conn)
            policy_changed = current != fingerprint
            if policy_changed:
                _align_policy(conn, scope, run_ids, current)
                reused_ids = _recover(conn, scope, run_ids, current)
                processed_ids, error_ids = [], []
            jobs = conn.execute(f"SELECT page_id,state FROM factcheck_job WHERE run_id IN ({scope}) ORDER BY page_id",
                                run_ids).fetchall()
    remaining_ids = [pid for pid, state in jobs if state != "done"]
    return {"queued":queued, "processed":len(processed_ids), "remaining":len(remaining_ids),
            "errors":len(error_ids), "reused":len(reused_ids), "reused_ids":reused_ids,
            "processed_ids":processed_ids, "error_ids":error_ids, "remaining_ids":remaining_ids,
            "pending_ids":[pid for pid,state in jobs if state == "pending"],
            "state_counts":dict(Counter(state for _,state in jobs)), "policy_changed":policy_changed}


def main():
    if __package__:
        from .config import connect_db
        from .crawl_store import initialize_db
    else:
        from config import connect_db
        from crawl_store import initialize_db
    parser = argparse.ArgumentParser(description="Очередь внешней проверки отдельных новостей и мероприятий")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", type=int, nargs="+", help="ID выбранных обходов")
    mode.add_argument("--all", action="store_true", help="публикации всех сохранённых обходов")
    parser.add_argument("--limit", type=int,
                        help="число публикаций за запуск (по настройке администратора, изначально 3). "
                             "При 0 очередь заполняется без обращения в сеть")
    args = parser.parse_args()
    with closing(connect_db()) as conn:
        initialize_db(conn)
        run_ids = args.run if args.run is not None else [r[0] for r in conn.execute("SELECT id FROM crawl_run")]
        try:
            result = batch_check(conn, run_ids, limit=args.limit)
        except ValueError as error:
            parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
