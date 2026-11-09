"""Archive receipts are historical facts; availability can change under the retention policy."""

RETENTION_ITEM_SQL="""
CREATE TABLE IF NOT EXISTS retention_item (
    page_id INTEGER NOT NULL REFERENCES page(id),kind TEXT NOT NULL,source TEXT NOT NULL,
    removed_at INTEGER NOT NULL,replacement_page_id INTEGER NOT NULL REFERENCES page(id),
    byte_size INTEGER NOT NULL,sha256 TEXT,PRIMARY KEY(page_id,kind,source)
);
"""
