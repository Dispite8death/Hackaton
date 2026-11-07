import os
import sqlite3

if __package__:
    from .retention import RETENTION_SCHEMA_SQL
    from .backfill_schema import QUEUE_SQL
    from .news_window import WINDOW_SQL
    from .attached_files import ATTACHMENT_SCHEMA_SQL
    from .model_settings import MODEL_SCHEMA_SQL
    from .timeline_audit import TIMELINE_SCHEMA_SQL
    from .factcheck import FACTCHECK_SCHEMA_SQL
    from .institution_audit import INSTITUTION_SCHEMA_SQL
    from .factcheck_batch import BATCH_SCHEMA_SQL
    from .staff_cross_org import CROSS_ORG_SCHEMA_SQL
    from .home_image_files import HOME_IMAGE_SCHEMA_SQL
    from .home_store import HOME_SCHEMA_SQL
    from .home_terms import HOME_TERMS_SCHEMA_SQL
else:
    from retention import RETENTION_SCHEMA_SQL
    from backfill_schema import QUEUE_SQL
    from news_window import WINDOW_SQL
    from attached_files import ATTACHMENT_SCHEMA_SQL
    from model_settings import MODEL_SCHEMA_SQL
    from timeline_audit import TIMELINE_SCHEMA_SQL
    from factcheck import FACTCHECK_SCHEMA_SQL
    from institution_audit import INSTITUTION_SCHEMA_SQL
    from factcheck_batch import BATCH_SCHEMA_SQL
    from staff_cross_org import CROSS_ORG_SCHEMA_SQL
    from home_image_files import HOME_IMAGE_SCHEMA_SQL
    from home_store import HOME_SCHEMA_SQL
    from home_terms import HOME_TERMS_SCHEMA_SQL

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.environ.get("HACKATON_DB_PATH", os.path.join(PROJECT_DIR, "search.sqlite3"))

STAFF_DUPLICATE_COLUMNS = {
    "duplicate_group": "TEXT",
    "duplicate_match": ("TEXT NOT NULL DEFAULT 'unreviewed' CHECK (duplicate_match IN "
                        "('unreviewed', 'single', 'possible', 'same_name', 'same_profile'))"),
    "duplicate_card_count": "INTEGER CHECK (duplicate_card_count >= 1)",
    "duplicate_page_count": "INTEGER CHECK (duplicate_page_count >= 1)",
    "duplicate_cards_on_page": "INTEGER CHECK (duplicate_cards_on_page >= 1)",
    "duplicate_roles_differ": "INTEGER CHECK (duplicate_roles_differ IN (0, 1))",
    "duplicate_needs_review": "INTEGER CHECK (duplicate_needs_review IN (0, 1))",
}
_DUPLICATE_SCHEMA = ",\n    ".join(f"{name} {definition}" for name, definition in STAFF_DUPLICATE_COLUMNS.items())

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS organization (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    site_url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('school', 'kindergarten'))
);
CREATE TABLE IF NOT EXISTS page (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    id_organization INTEGER NOT NULL REFERENCES organization (id),
    date_loaded INTEGER NOT NULL DEFAULT (unixepoch()),
    text_page TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS page_organization_date ON page(id_organization, date_loaded);
CREATE INDEX IF NOT EXISTS page_organization_url_date ON page(id_organization,url,date_loaded,id);

CREATE TABLE IF NOT EXISTS page_render (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    captured_at INTEGER NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
    viewport_width INTEGER NOT NULL CHECK (viewport_width > 0),
    viewport_height INTEGER NOT NULL CHECK (viewport_height > 0),
    css_loaded INTEGER CHECK (css_loaded IN (0, 1)),
    details TEXT NOT NULL,
    rendered_html TEXT,
    screenshot_mime TEXT,
    screenshot BLOB,
    full_screenshot BLOB,
    pdf BLOB
);

CREATE TABLE IF NOT EXISTS crawl_run (
    id INTEGER PRIMARY KEY,
    id_organization INTEGER NOT NULL REFERENCES organization(id),
    started_at INTEGER NOT NULL DEFAULT (unixepoch()),
    finished_at INTEGER,
    state TEXT NOT NULL DEFAULT 'running',
    purpose TEXT NOT NULL DEFAULT 'audit' CHECK (purpose IN ('audit', 'sample')),
    max_depth INTEGER,
    max_pages INTEGER,
    stop_reason TEXT
);
CREATE TABLE IF NOT EXISTS crawl_fetch (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES crawl_run(id),
    url TEXT NOT NULL,
    final_url TEXT,
    section TEXT NOT NULL,
    depth INTEGER NOT NULL CHECK (depth >= 0),
    fetched_at INTEGER NOT NULL,
    http_status INTEGER,
    error TEXT,
    page_id INTEGER UNIQUE REFERENCES page(id)
);
CREATE INDEX IF NOT EXISTS crawl_fetch_run ON crawl_fetch(run_id, section);
CREATE TABLE IF NOT EXISTS page_passport (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    page_kind TEXT NOT NULL, title TEXT NOT NULL, headings TEXT NOT NULL,
    summary TEXT NOT NULL, checks TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page_revisit (
    original_page_id INTEGER NOT NULL REFERENCES page(id),
    fetch_id INTEGER PRIMARY KEY REFERENCES crawl_fetch(id),
    relation TEXT NOT NULL CHECK(relation IN ('revisit','staff_next'))
);
CREATE TABLE IF NOT EXISTS news_page (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    page_kind TEXT NOT NULL, source TEXT NOT NULL, order_status TEXT NOT NULL,
    card_count INTEGER NOT NULL, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_navigator_page (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    is_navigator_page INTEGER NOT NULL CHECK(is_navigator_page IN (0,1)), evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page_index_issue (
    page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE, analyzer TEXT NOT NULL,
    error TEXT NOT NULL, PRIMARY KEY(page_id,analyzer)
);
CREATE TABLE IF NOT EXISTS news_preview (
    page_id INTEGER NOT NULL REFERENCES news_page(page_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK(ordinal>=0),
    url TEXT NOT NULL, title TEXT NOT NULL, date_status TEXT NOT NULL,
    date_text TEXT, date_iso TEXT, evidence TEXT NOT NULL,
    PRIMARY KEY(page_id,ordinal)
);
CREATE VIEW IF NOT EXISTS news_model_page AS
    SELECT n.*, o.id AS organization_id,o.kind,r.id AS run_id,r.purpose,p.url,p.date_loaded,
           f.id AS fetch_id,f.section,f.http_status,v.status AS render_status,v.captured_at,
           v.pdf IS NOT NULL AS has_pdf,v.screenshot IS NOT NULL AS has_viewport,
           v.full_screenshot IS NOT NULL AS has_full,v.rendered_html IS NOT NULL AS has_dom
    FROM news_page n JOIN page p ON p.id=n.page_id JOIN organization o ON o.id=p.id_organization
    JOIN crawl_fetch f ON f.page_id=p.id JOIN crawl_run r ON r.id=f.run_id AND r.id_organization=o.id
    LEFT JOIN page_render v ON v.page_id=n.page_id;

CREATE TABLE IF NOT EXISTS crawl_link (
    fetch_id INTEGER NOT NULL REFERENCES crawl_fetch(id),
    url TEXT NOT NULL,
    label TEXT NOT NULL,
    section TEXT NOT NULL,
    relation TEXT NOT NULL,
    follow INTEGER NOT NULL CHECK (follow IN (0, 1))
);
CREATE INDEX IF NOT EXISTS crawl_link_fetch ON crawl_link(fetch_id);

CREATE TABLE IF NOT EXISTS crawl_heading (
    fetch_id INTEGER NOT NULL REFERENCES crawl_fetch(id),
    section TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (fetch_id, section, label)
);
CREATE INDEX IF NOT EXISTS crawl_heading_section ON crawl_heading(section, fetch_id);

CREATE TABLE IF NOT EXISTS staff_observation (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    card_selector TEXT NOT NULL,
    full_name TEXT,
    role TEXT,
    is_head INTEGER NOT NULL CHECK (is_head IN (0, 1)),
    photo_url TEXT,
    photo_state TEXT NOT NULL,
    education TEXT,
    email TEXT,
    phone TEXT,
    profile_url TEXT,
    method TEXT NOT NULL,
    {_DUPLICATE_SCHEMA},
    UNIQUE (page_id, card_selector)
);
CREATE TABLE IF NOT EXISTS staff_element (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES staff_observation(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    selector TEXT NOT NULL,
    expected_value TEXT NOT NULL,
    visual_status TEXT NOT NULL DEFAULT 'pending',
    visual_details TEXT,
    checked_at INTEGER,
    render_source TEXT CHECK (render_source IN ('html_only', 'live', 'archived_with_assets')),
    render_url TEXT,
    css_loaded INTEGER CHECK (css_loaded IN (0, 1)),
    viewport_width INTEGER CHECK (viewport_width > 0),
    viewport_height INTEGER CHECK (viewport_height > 0),
    UNIQUE (observation_id, field, selector, expected_value)
);
""" + CROSS_ORG_SCHEMA_SQL + HOME_TERMS_SCHEMA_SQL + HOME_SCHEMA_SQL + HOME_IMAGE_SCHEMA_SQL

SCHEMA_SQL += MODEL_SCHEMA_SQL + TIMELINE_SCHEMA_SQL + FACTCHECK_SCHEMA_SQL + INSTITUTION_SCHEMA_SQL + BATCH_SCHEMA_SQL
SCHEMA_SQL += ATTACHMENT_SCHEMA_SQL
SCHEMA_SQL += RETENTION_SCHEMA_SQL

SCHEMA_SQL += QUEUE_SQL + WINDOW_SQL


def connect_db(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH if path is None else path, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn
