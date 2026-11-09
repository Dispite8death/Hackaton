"""Find employee cards in an institution's saved HTML and queue their elements for review."""

import argparse
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from urllib.parse import urldefrag

if __package__:
    from .config import connect_db
    from .crawl_store import initialize_db, select_organizations
    from .render_store import staff_source_html
    from .staff_cross_org import refresh_cross_org_matches
    from .staff_duplicates import refresh_staff_duplicates
    from .staff_extract import extract_staff
    from .staff_identity import full_name_key
    from .staff_store import _staff_transaction, save_staff
    from .navigation import staff_pagination_controls
else:
    from config import connect_db
    from crawl_store import initialize_db, select_organizations
    from render_store import staff_source_html
    from staff_cross_org import refresh_cross_org_matches
    from staff_duplicates import refresh_staff_duplicates
    from staff_extract import extract_staff
    from staff_identity import full_name_key
    from staff_store import _staff_transaction, save_staff
    from navigation import staff_pagination_controls


DIRECTORIES = ("management", "team", "staff")
FALLBACK = ("about", "information", "contacts")


@dataclass(frozen=True)
class Source:
    page_id: int
    url: str
    section: str


@dataclass(frozen=True)
class ScanResult:
    run_id: int | None
    pages: int
    observations: int
    head_source: str
    head_names: tuple[str, ...]


def head_name_key(name: str) -> str:
    return full_name_key(name) or " ".join(name.casefold().replace("ё", "е").split())


def save_snapshot_staff(conn, page_id, page_url, section):
    html = staff_source_html(conn, page_id)
    if html is None:
        return None
    cards = extract_staff(html, page_url, section)
    save_staff(conn, page_id, cards)
    return html, cards


def scan_organization(conn: sqlite3.Connection, organization_id: int) -> ScanResult:
    """Use one finished crawl so old HTML cannot silently fill holes in a newer audit."""
    # Commit card evidence, counts and the report together for this institution.
    with _staff_transaction(conn):
        conn.execute("UPDATE staff_scan_run SET pages = pages WHERE 0")
        return _scan_organization(conn, organization_id)


def _scan_organization(conn: sqlite3.Connection, organization_id: int) -> ScanResult:
    run = conn.execute("""
        SELECT id FROM crawl_run WHERE id_organization = ? AND finished_at IS NOT NULL AND purpose = 'audit'
        ORDER BY id DESC LIMIT 1
    """, (organization_id,)).fetchone()
    if run is None:
        return ScanResult(None, 0, 0, "no_snapshot", ())
    run_id = run[0]

    # A URL can be linked as a directory but have been fetched under a different role.
    aliases = defaultdict(set)
    for url, section in conn.execute("""
        SELECT DISTINCT l.url, l.section FROM crawl_link l JOIN crawl_fetch f ON f.id = l.fetch_id
        WHERE f.run_id = ? AND instr(l.url, '#') = 0
          AND (l.relation='section' OR
               (l.relation='pagination' AND l.section IN ('staff','team','management')))
    """, (run_id,)):
        aliases[url].add(section)

    pages = []
    urls = defaultdict(set)
    for page_id, url, requested, section, depth in conn.execute("""
        SELECT p.id, p.url, f.url, f.section, f.depth
        FROM crawl_fetch f JOIN page p ON p.id = f.page_id
        WHERE f.run_id = ? AND p.id_organization = ? ORDER BY f.id
    """, (run_id, organization_id)):
        roles = aliases[url] | aliases[requested] | {section}
        role = "home" if depth == 0 else next((s for s in (*DIRECTORIES, *FALLBACK) if s in roles), section)
        pages.append(Source(page_id, url, role))
        urls[page_id].update((url, requested))

    visited = set()
    heads = defaultdict(dict)
    profiles = set()
    count = 0

    def inspect(page: Source, section: str):
        nonlocal count
        extracted = save_snapshot_staff(conn, page.page_id, page.url, section)
        if extracted is None:
            return
        html, cards = extracted
        if section in DIRECTORIES:
            # Older snapshots sometimes label a continuation URL under another role.
            profiles.update(staff_pagination_controls(html, page.url, page.url, section)["urls"])
        visited.add(page.page_id)
        count += len(cards)
        for card in cards:
            if card.is_head and card.name:
                key = head_name_key(card.name)
                heads[section][key] = card.name
            if card.profile_url:
                profiles.add(urldefrag(card.profile_url).url)

    # Inspect home first. Directory pages also supply evidence for other employees.
    for section in ("home", *DIRECTORIES):
        for page in pages:
            if page.section == section:
                inspect(page, section)
    if not any(heads.values()):
        for page in pages:
            if page.section in FALLBACK:
                inspect(page, page.section)

    # Some staff cards link to a downloaded profile classified under another section.
    # Only inspect already saved, explicitly linked targets from the same run.
    while targets := [page for page in pages if page.page_id not in visited and urls[page.page_id] & profiles]:
        for page in targets:
            inspect(page, "staff")

    refresh_staff_duplicates(conn, run_id)
    conn.execute("""
        INSERT INTO staff_scan_run(run_id, pages, observations) VALUES (?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET scanned_at = unixepoch(),
            pages = excluded.pages, observations = excluded.observations
    """, (run_id, len(visited), count))
    refresh_cross_org_matches(conn)
    all_names = {key: name for names in heads.values() for key, name in names.items()}
    if len(all_names) > 1:
        source = "conflict"
    elif heads.get("home"):
        source = "home"
    elif all_names:
        source = "inner"
    else:
        source = "unresolved" if visited else "no_snapshot"
    return ScanResult(run_id, len(visited), count, source, tuple(sorted(all_names.values())))


STATUS_LABELS = {
    "home": "карточка руководителя на главной",
    "inner": "руководитель найден во внутренних разделах",
    "unresolved": "в проверенных снимках руководитель однозначно не найден",
    "no_snapshot": "нет подходящих снимков",
    "conflict": "разные кандидаты в руководители, нужна сверка",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Найти карточки сотрудников в последнем завершённом обходе.")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--org", nargs="+", help="ID или полные названия учреждений")
    choice.add_argument("--all", action="store_true", help="обработать все учреждения")
    args = parser.parse_args()
    counts = Counter()
    with closing(connect_db()) as conn:
        initialize_db(conn)
        try:
            selected = select_organizations(conn, args.org)
        except ValueError as error:
            parser.error(str(error))
        for org in selected:
            result = scan_organization(conn, org["id"])
            counts[result.head_source] += 1
            names = ", ".join(result.head_names)
            print(f"ID {org['id']} · {STATUS_LABELS[result.head_source]} · "
                  f"снимков {result.pages}, карточек {result.observations}" + (f" · {names}" if names else ""),
                  flush=True)
    if len(selected) > 1:
        for state, number in counts.items():
            print(f"{STATUS_LABELS[state]}: {number}")


if __name__ == "__main__":
    main()
