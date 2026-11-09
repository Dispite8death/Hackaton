"""SQLite evidence for school homepage content and event pages from the same audit."""

import json
from urllib.parse import urldefrag

if __package__:
    from .render_store import incomplete_document
    from .home_terms import get_terms
else:
    from render_store import incomplete_document
    from home_terms import get_terms


_PHOTO_FIELDS = """
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('found','ambiguous','not_found')),
    image_url TEXT, alt TEXT, evidence TEXT NOT NULL
"""

HOME_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS home_main_photo ({_PHOTO_FIELDS});
CREATE TABLE IF NOT EXISTS home_canteen_photo ({_PHOTO_FIELDS});
CREATE TABLE IF NOT EXISTS garden_building_photo (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('candidate','ambiguous','not_identified')),
    image_url TEXT, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS garden_home_contact (
    page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    field TEXT NOT NULL CHECK(field IN ('address','phone','email','hours')),
    status TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(page_id,field)
);
CREATE TABLE IF NOT EXISTS garden_home_section (
    page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    section TEXT NOT NULL CHECK(section IN ('team','news','events')), heading_found INTEGER NOT NULL,
    block_status TEXT NOT NULL, navigation_count INTEGER NOT NULL, listing_count INTEGER NOT NULL,
    card_count INTEGER NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(page_id,section)
);
CREATE TABLE IF NOT EXISTS home_school_description (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('found','ambiguous','not_found')),
    text TEXT, char_count INTEGER, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS home_event_block (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    page_kind TEXT NOT NULL, block_status TEXT NOT NULL, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS home_event_link (
    page_id INTEGER NOT NULL REFERENCES home_event_block(page_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, kind TEXT NOT NULL, url TEXT, title TEXT,
    date_text TEXT, date_start TEXT, date_end TEXT, image_url TEXT, evidence TEXT NOT NULL,
    PRIMARY KEY(page_id, ordinal)
);
CREATE INDEX IF NOT EXISTS home_event_link_url ON home_event_link(url);
CREATE VIEW IF NOT EXISTS school_event_date_issue AS
    SELECT f.run_id,e.page_id,e.ordinal,e.url,e.title,e.date_text AS calendar_text,
           json_extract(e.evidence,'$.date_evidence.conflict_reason') AS reason,
           json_extract(e.evidence,'$.date_evidence.text_dates') AS text_dates,
           json_extract(e.evidence,'$.date_evidence.sources') AS sources
    FROM home_event_link e JOIN crawl_fetch f ON f.page_id=e.page_id
    WHERE e.kind='event' AND json_extract(e.evidence,'$.date_evidence.reason')='possible_date_error';
CREATE TABLE IF NOT EXISTS home_analysis_problem (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    attempted_at INTEGER NOT NULL, error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_event_quarter (
    run_id INTEGER NOT NULL REFERENCES crawl_run(id) ON DELETE CASCADE,
    period TEXT NOT NULL CHECK(period IN ('current','next')),
    start_date TEXT NOT NULL, end_date TEXT NOT NULL, status TEXT NOT NULL,
    dated_events INTEGER NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(run_id, period)
);
CREATE VIEW IF NOT EXISTS home_image_reference AS
    SELECT page_id, 'main_photo' AS category, 'main' AS item_key, image_url FROM home_main_photo
        WHERE image_url IS NOT NULL
    UNION ALL SELECT page_id, 'canteen_photo', 'canteen', image_url FROM home_canteen_photo
        WHERE image_url IS NOT NULL
    UNION ALL SELECT e.page_id, 'event', coalesce(e.url,p.url || '#' || e.ordinal), e.image_url
        FROM home_event_link e JOIN page p ON p.id=e.page_id WHERE e.kind='event' AND e.image_url IS NOT NULL
    UNION ALL SELECT s.page_id, 'staff', coalesce(nullif(s.full_name,''), s.profile_url, p.url || '#' || s.card_selector),
        s.photo_url FROM staff_observation s JOIN page p ON p.id=s.page_id WHERE s.photo_url IS NOT NULL;
CREATE VIEW IF NOT EXISTS home_current_image_use AS
    WITH latest AS (
        SELECT id_organization, max(id) AS run_id FROM crawl_run
        WHERE purpose='audit' AND finished_at IS NOT NULL GROUP BY id_organization
    ), refs AS (
        SELECT l.id_organization, l.run_id, i.page_id, i.image_url, i.category,
               CASE WHEN i.category='event' AND instr(i.item_key, '#')>0
                    THEN substr(i.item_key, 1, instr(i.item_key, '#')-1) ELSE i.item_key END AS item_key
        FROM latest l JOIN crawl_fetch f USING(run_id)
        JOIN page p ON p.id=f.page_id AND p.id_organization=l.id_organization
        JOIN home_image_reference i ON i.page_id=p.id
        WHERE i.image_url != '' AND (i.category!='event' OR EXISTS (
            SELECT 1 FROM home_event_link e WHERE e.page_id=i.page_id AND e.kind='event' AND e.url=i.item_key
        ))
    ), aliases AS (
        SELECT f.run_id, f.url AS requested, min(p.url) AS target
        FROM latest l JOIN crawl_fetch f USING(run_id)
        JOIN page p ON p.id=f.page_id AND p.id_organization=l.id_organization
        WHERE p.url!=f.url GROUP BY f.run_id, f.url HAVING min(p.url)=max(p.url)
    )
    SELECT r.id_organization, r.run_id, r.page_id, r.image_url, r.category, coalesce(a.target,r.item_key) AS item_key
    FROM refs r LEFT JOIN aliases a ON r.category='event' AND a.run_id=r.run_id AND a.requested=r.item_key;
DROP VIEW IF EXISTS home_reused_image;
CREATE VIEW home_reused_image AS
    WITH uses AS (
        SELECT DISTINCT id_organization, image_url, category, item_key FROM home_current_image_use
    )
    SELECT image_url, count(*) AS distinct_uses, count(DISTINCT id_organization) AS organizations,
           count(DISTINCT category) AS categories
    FROM uses GROUP BY image_url HAVING count(*)>1;
CREATE VIEW IF NOT EXISTS home_reused_asset AS
    WITH files AS (
        SELECT DISTINCT r.id_organization, r.category, d.sha256, d.image_url
        FROM home_current_image_use r JOIN home_image_download d USING(page_id,image_url)
        JOIN home_image_asset a ON a.sha256=d.sha256
        WHERE r.category IN ('main_photo','canteen_photo') AND d.status='saved'
    ), uses AS (
        SELECT DISTINCT id_organization, category, sha256 FROM files
    )
    SELECT u.sha256, count(*) AS distinct_uses, count(DISTINCT u.id_organization) AS organizations,
           count(DISTINCT u.category) AS categories,
           (SELECT count(DISTINCT f.image_url) FROM files f WHERE f.sha256=u.sha256) AS distinct_urls,
           'same_bytes_multiple_uses' AS status
    FROM uses u GROUP BY u.sha256 HAVING count(*)>1;
"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _image_url(value):
    return urldefrag(value).url if value else None


def analyze_saved_page(conn, page_id):
    """Replace category observations in the caller's transaction; retain original page and render."""
    conn.execute("SAVEPOINT home_extraction")
    try:
        changed = _extract_page(conn, page_id)
        conn.execute("DELETE FROM home_analysis_problem WHERE page_id=?", (page_id,))
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError) as exc:
        conn.execute("ROLLBACK TO home_extraction")
        conn.execute("""
            INSERT INTO home_analysis_problem(page_id,attempted_at,error) VALUES(?,unixepoch(),?)
            ON CONFLICT(page_id) DO UPDATE SET attempted_at=excluded.attempted_at,error=excluded.error
        """, (page_id, f"{type(exc).__name__}: {str(exc)[:500]}"))
        return False
    except BaseException:
        conn.execute("ROLLBACK TO home_extraction")
        raise
    finally:
        conn.execute("RELEASE home_extraction")
    return changed


def _save_photo(conn, page_id, category, item, source):
    conn.execute(f"""
        INSERT INTO home_{category}(page_id,status,image_url,alt,evidence) VALUES(?,?,?,?,?)
        ON CONFLICT(page_id) DO UPDATE SET status=excluded.status, image_url=excluded.image_url,
            alt=excluded.alt, evidence=excluded.evidence
    """, (page_id, item["status"], _image_url(item.get("image_url")), item.get("alt"),
          encoded(dict(item["evidence"], source=source))))


def _garden(conn, page_id, html, url, name, source):
    from bs4 import BeautifulSoup
    if __package__:
        from .garden_contacts import extract_garden_contacts
        from .garden_home import extract_garden_home
    else:
        from garden_contacts import extract_garden_contacts
        from garden_home import extract_garden_home
    soup = BeautifulSoup(html, "html.parser")
    garden = extract_garden_home(soup, url, name, get_terms(conn))
    for field, item in extract_garden_contacts(soup, url).items():
        conn.execute("""
            INSERT INTO garden_home_contact(page_id,field,status,evidence) VALUES(?,?,?,?)
            ON CONFLICT(page_id,field) DO UPDATE SET status=excluded.status,evidence=excluded.evidence
        """, (page_id, field, item["status"], encoded(dict(item, source=source))))
    for section in ("news", "team", "events"):
        item = garden[section]
        conn.execute("""
            INSERT INTO garden_home_section VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(page_id,section) DO UPDATE SET heading_found=excluded.heading_found,
                block_status=excluded.block_status,navigation_count=excluded.navigation_count,
                listing_count=excluded.listing_count,card_count=excluded.card_count,evidence=excluded.evidence
        """, (page_id, section, item["heading_found"], item["block_status"], len(item["navigation_links"]),
              len(item["listing_links"]), len(item["cards"]), encoded(dict(item, source=source))))
    photo = garden["main_photo"]
    _save_photo(conn, page_id, "main_photo", photo, source)
    building = photo["evidence"]["building_candidate"]
    conn.execute("""
        INSERT INTO garden_building_photo VALUES(?,?,?,?)
        ON CONFLICT(page_id) DO UPDATE SET status=excluded.status,image_url=excluded.image_url,evidence=excluded.evidence
    """, (page_id, building["status"], _image_url(building["image_url"]), encoded(dict(building, source=source))))
    return True


def _extract_page(conn, page_id):
    row = conn.execute("""
        SELECT p.url, p.text_page, f.depth, f.section, o.kind, o.name, r.rendered_html, r.details
        FROM page p JOIN crawl_fetch f ON f.page_id=p.id JOIN organization o ON o.id=p.id_organization
        LEFT JOIN page_render r ON r.page_id=p.id WHERE p.id=?
    """, (page_id,)).fetchone()
    if row is None:
        return False
    url, html, depth, section, kind, name, dom, details = row
    if depth != 0 and (kind != "school" or section != "events"):
        return False
    if __package__:
        from .home_content import extract_home_content
        from .home_events import extract_home_events
    else:
        from home_content import extract_home_content
        from home_events import extract_home_events
    source = "response_html"
    if dom and not incomplete_document(json.loads(details)):
        html, source = dom, "rendered_dom"

    if kind == "kindergarten":
        return _garden(conn, page_id, html, url, name, source)
    if depth == 0:
        content = extract_home_content(html, url, get_terms(conn))
        for category in ("main_photo", "canteen_photo"):
            _save_photo(conn, page_id, category, content[category], source)
        desc = content["school_description"]
        value = desc.get("text")
        conn.execute("""
            INSERT INTO home_school_description(page_id,status,text,char_count,evidence) VALUES(?,?,?,?,?)
            ON CONFLICT(page_id) DO UPDATE SET status=excluded.status, text=excluded.text,
                char_count=excluded.char_count, evidence=excluded.evidence
        """, (page_id, desc["status"], value, len(value) if value else None, encoded(dict(desc["evidence"], source=source))))

    events = extract_home_events(html, url)
    meta = dict(events["evidence"], source=source)
    meta.pop("cards", None)  # the ordered card rows below hold the complete evidence once
    page_kind = "home" if depth == 0 else meta["page_kind"]
    conn.execute("""
        INSERT INTO home_event_block(page_id,page_kind,block_status,evidence) VALUES (?,?,?,?)
        ON CONFLICT(page_id) DO UPDATE SET page_kind=excluded.page_kind,
            block_status=excluded.block_status,evidence=excluded.evidence
    """, (page_id, page_kind, events["block_status"], encoded(meta)))
    conn.execute("DELETE FROM home_event_link WHERE page_id=?", (page_id,))
    entries = list(events["links"])
    entries.extend(card for card in events["evidence"].get("cards", []) if not card.get("url") and card not in entries)
    for index, item in enumerate(entries):
        conn.execute("""
            INSERT INTO home_event_link(page_id,ordinal,kind,url,title,date_text,date_start,date_end,image_url,evidence)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (page_id, index, item.get("kind","event"), item.get("url"), item.get("title"),
              item.get("date_text"), item.get("date_start"), item.get("date_end"),
              _image_url(item.get("image_url")), encoded(item)))
    return True

