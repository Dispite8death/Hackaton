"""Persistent frontier of required material links from archived school and garden pages."""

QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS material_backfill (
    organization_id INTEGER NOT NULL REFERENCES organization(id),
    url TEXT NOT NULL, source_page_id INTEGER NOT NULL REFERENCES page(id),
    section TEXT NOT NULL, relation TEXT NOT NULL, depth INTEGER NOT NULL, priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', run_id INTEGER REFERENCES crawl_run(id),
    fetch_id INTEGER REFERENCES crawl_fetch(id), lease_until INTEGER, error TEXT,
    PRIMARY KEY(organization_id,url)
);
CREATE INDEX IF NOT EXISTS material_backfill_pending ON material_backfill(state,priority,organization_id);
CREATE TABLE IF NOT EXISTS material_backfill_refresh (
    organization_id INTEGER NOT NULL, url TEXT NOT NULL, cutoff INTEGER NOT NULL,
    PRIMARY KEY(organization_id,url),
    FOREIGN KEY(organization_id,url) REFERENCES material_backfill(organization_id,url)
);
CREATE TABLE IF NOT EXISTS material_backfill_visit (
    fetch_id INTEGER PRIMARY KEY REFERENCES crawl_fetch(id),
    source_page_id INTEGER NOT NULL REFERENCES page(id), relation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_pager_stop (
    page_id INTEGER PRIMARY KEY REFERENCES page(id), parent_page_id INTEGER NOT NULL REFERENCES page(id),
    earlier_page_id INTEGER NOT NULL REFERENCES page(id), reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_worker_cycle (
    id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL DEFAULT(unixepoch()), finished_at INTEGER,
    state TEXT NOT NULL, stage TEXT, settings TEXT NOT NULL, stages TEXT NOT NULL
);
"""
