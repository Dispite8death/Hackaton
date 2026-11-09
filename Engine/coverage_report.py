"""Report evidence for the required sections within a single crawl run."""

from collections.abc import Iterable
from sqlite3 import Connection

if __package__:
    from .navigation import REQUIRED_SECTIONS, SECTION_NAMES
else:
    from navigation import REQUIRED_SECTIONS, SECTION_NAMES


STATES = ("page", "heading", "link", "unseen")
SECTION_LABELS = {"home": "Главная страница", **{section: names[0] for section, names in SECTION_NAMES.items()}}
SECTION_LABELS.update(history="История школы", programs="Реализуемые образовательные программы")


def coverage(conn: Connection, run_id: int, kind: str) -> dict[str, str]:
    """Return the strongest observed evidence for each required section."""
    saved_urls = set()
    pages = set()
    for url, final_url, section, depth in conn.execute(
        "SELECT url, final_url, section, depth FROM crawl_fetch WHERE run_id = ? AND page_id IS NOT NULL",
        (run_id,),
    ):
        saved_urls.update((url, final_url))
        if depth == 0:
            pages.add(section)

    links = set()
    for section, url in conn.execute("""
        SELECT DISTINCT l.section, l.url FROM crawl_fetch AS f
        JOIN crawl_link AS l ON l.fetch_id = f.id
        WHERE f.run_id = ? AND f.page_id IS NOT NULL AND l.relation = 'section'
    """, (run_id,)):
        links.add(section)
        if "#" not in url and url in saved_urls:
            pages.add(section)

    headings = {section for (section,) in conn.execute("""
        SELECT DISTINCT h.section FROM crawl_fetch AS f JOIN crawl_heading AS h ON h.fetch_id = f.id
        WHERE f.run_id = ? AND f.page_id IS NOT NULL
    """, (run_id,))}

    states = dict.fromkeys(REQUIRED_SECTIONS[kind], "unseen")
    for evidence, state in ((links, "link"), (headings, "heading"), (pages, "page")):
        for section in evidence.intersection(states):
            states[section] = state
    return states


def coverage_counts(conn: Connection, runs: Iterable[tuple[int, str]]) -> dict[str, dict[str, int]]:
    """Count states per applicable section for the supplied (run_id, kind) pairs."""
    counts = {}
    for run_id, kind in runs:
        for section, state in coverage(conn, run_id, kind).items():
            if section not in counts:
                counts[section] = dict.fromkeys(STATES, 0)
            counts[section][state] += 1
    return counts
