"""Report full-name matches across institutions, with evidence from each current audit."""

import sqlite3
from collections import defaultdict

if __package__:
    from .staff_identity import full_name_key
else:
    from staff_identity import full_name_key


CROSS_ORG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS staff_scan_run (
    run_id INTEGER PRIMARY KEY REFERENCES crawl_run(id) ON DELETE CASCADE,
    scanned_at INTEGER NOT NULL DEFAULT (unixepoch()),
    pages INTEGER NOT NULL CHECK (pages >= 0),
    observations INTEGER NOT NULL CHECK (observations >= 0)
);
CREATE TABLE IF NOT EXISTS staff_cross_org_match (
    name_key TEXT NOT NULL,
    organization_id INTEGER NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES crawl_run(id) ON DELETE CASCADE,
    representative_observation_id INTEGER REFERENCES staff_observation(id) ON DELETE SET NULL,
    organization_count INTEGER NOT NULL CHECK (organization_count >= 2),
    observation_count INTEGER NOT NULL CHECK (observation_count >= 1),
    status TEXT NOT NULL DEFAULT 'name_match_needs_review' CHECK (status = 'name_match_needs_review'),
    PRIMARY KEY (name_key, organization_id)
);
CREATE TABLE IF NOT EXISTS staff_cross_org_evidence (
    observation_id INTEGER PRIMARY KEY REFERENCES staff_observation(id) ON DELETE CASCADE,
    name_key TEXT NOT NULL,
    organization_id INTEGER NOT NULL,
    FOREIGN KEY (name_key, organization_id)
        REFERENCES staff_cross_org_match(name_key, organization_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS staff_cross_org_evidence_match ON staff_cross_org_evidence(name_key, organization_id);
CREATE VIEW IF NOT EXISTS staff_cross_org_source AS
    SELECT m.name_key, m.organization_id, org.name AS organization_name, m.run_id,
           m.organization_count, m.observation_count, m.status, s.id AS observation_id,
           s.full_name, s.role, s.card_selector, s.profile_url, p.id AS page_id,
           p.url AS source_url, f.section, p.date_loaded
    FROM staff_cross_org_evidence AS e
    JOIN staff_cross_org_match AS m USING (name_key, organization_id)
    JOIN organization AS org ON org.id = m.organization_id
    JOIN staff_observation AS s ON s.id = e.observation_id
    JOIN page AS p ON p.id = s.page_id AND p.id_organization = m.organization_id
    JOIN crawl_fetch AS f ON f.page_id = p.id AND f.run_id = m.run_id;
"""


def refresh_cross_org_matches(conn: sqlite3.Connection) -> int:
    """Replace the report atomically and return its number of distinct matching full names.

    Use the latest finished audit by run ID for each organization, matching scan_staff.
    A partially scanned audit contributes only its available observations.
    """
    conn.execute("SAVEPOINT refresh_staff_cross_org")
    try:
        # Acquire the write lock before selecting the latest runs and their observations.
        conn.execute("DELETE FROM staff_cross_org_evidence")
        conn.execute("DELETE FROM staff_cross_org_match")
        groups = defaultdict(lambda: defaultdict(list))
        rows = conn.execute("""
            WITH latest AS (
                SELECT id_organization, MAX(id) AS run_id
                FROM crawl_run
                WHERE purpose = 'audit' AND finished_at IS NOT NULL
                GROUP BY id_organization
            )
            SELECT l.id_organization, l.run_id, s.id, s.full_name FROM latest AS l
            JOIN crawl_fetch AS f ON f.run_id = l.run_id
            JOIN page AS p ON p.id = f.page_id AND p.id_organization = l.id_organization
            JOIN staff_observation AS s ON s.page_id = p.id
            WHERE s.full_name IS NOT NULL
        """)
        for organization_id, run_id, observation_id, name in rows:
            name_key = full_name_key(name)
            if name_key is not None:
                groups[name_key][organization_id, run_id].append(observation_id)

        name_count = 0
        for name_key, institutions in groups.items():
            if len(institutions) < 2:
                continue
            name_count += 1
            for (organization_id, run_id), observations in institutions.items():
                conn.execute("""
                    INSERT INTO staff_cross_org_match
                        (name_key, organization_id, run_id, representative_observation_id,
                         organization_count, observation_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (name_key, organization_id, run_id, min(observations), len(institutions), len(observations)))
                conn.executemany("""
                    INSERT INTO staff_cross_org_evidence (observation_id, name_key, organization_id)
                    VALUES (?, ?, ?)
                """, ((obs, name_key, organization_id) for obs in observations))
    except BaseException:
        conn.execute("ROLLBACK TO refresh_staff_cross_org")
        raise
    finally:
        conn.execute("RELEASE refresh_staff_cross_org")
    return name_count
