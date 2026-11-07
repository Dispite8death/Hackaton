"""Annotate repeated staff cards within a single crawl of one institution."""

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import permutations
from urllib.parse import urljoin, urlsplit, urlunsplit

if __package__:
    from .staff_identity import full_name_key
    from .staff_store import _staff_transaction
else:
    from staff_identity import full_name_key
    from staff_store import _staff_transaction


@dataclass(frozen=True)
class _Observation:
    id: int
    snapshot: int
    url: str
    full_name: str | None
    short_name: tuple[str, ...]
    profile: str | None
    role: str


def _http_url(value: str, base: str = "") -> str:
    try:
        parts = urlsplit(urljoin(base, value) if base else value)
        if not parts.hostname or parts.scheme not in {"http", "https"} or parts.username is not None:
            return ""
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        if parts.port and (parts.scheme, parts.port) not in {("https", 443), ("http", 80)}:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    except ValueError:
        return ""


def _short_name(name):
    text = (name or "").casefold().replace("ё", "е")
    if not re.fullmatch(r"[^\W\d_]+(?:[\s.\-]+[^\W\d_]+)*\.?", text.strip()):
        return ()
    words = re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", text)
    return tuple(sorted(words)) if 2 <= len(words) <= 3 and any(len(w) > 1 for w in words) else ()


def _compatible(short, full):
    # A whole word must match exactly. A single-letter initial can match a prefix.
    return any(all(a == b or (len(a) == 1 and b.startswith(a)) for a, b in zip(short, variant))
               for variant in permutations(full.split(), len(short)))


def _observations(conn, run_id):
    rows = conn.execute("""
        SELECT p.id, p.url, f.url, s.id, s.full_name, s.role, s.profile_url
        FROM crawl_run r JOIN crawl_fetch f ON f.run_id = r.id
        JOIN page p ON p.id = f.page_id AND p.id_organization = r.id_organization
        LEFT JOIN staff_observation s ON s.page_id = p.id
        WHERE r.id = ? ORDER BY f.id, s.id
    """, (run_id,)).fetchall()
    # Redirect aliases recorded by this crawl are stronger evidence than guessing
    # that similarly spelled paths, distinct query strings or different hosts are aliases.
    destinations = defaultdict(set)
    for _, final, requested, *_ in rows:
        destinations[_http_url(requested)].add(_http_url(final))
    aliases = {source: next(iter(targets)) for source, targets in destinations.items() if len(targets) == 1}
    observations = []
    for snapshot, url, _, observation_id, name, role, profile_url in rows:
        if observation_id is None:
            continue
        profile = _http_url(profile_url, url) if profile_url else ""
        profile = aliases.get(profile, profile)
        if profile and urlsplit(profile).path == "/" and not urlsplit(profile).query:
            profile = ""
        full = full_name_key(name)
        observations.append(_Observation(observation_id, snapshot, _http_url(url) or url, full,
                                         () if full else _short_name(name), profile or None,
                                         " ".join((role or "").casefold().replace("ё", "е").split())))
    return observations


def _annotations(rows, run_id):
    by_name, by_profile, by_short = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in rows:
        if row.full_name:
            by_name[row.full_name].append(row)
        if row.profile:
            by_profile[row.profile].append(row)
        if row.short_name:
            by_short[row.short_name].append(row)

    profile_conflicts = {url for url, entries in by_profile.items()
                         if len({r.full_name for r in entries if r.full_name}) > 1}
    profiles_by_name = {name: {r.profile for r in entries if r.profile} for name, entries in by_name.items()}
    review = set()
    for url, entries in by_profile.items():
        if url in profile_conflicts or (len(entries) > 1 and any(not r.full_name for r in entries)):
            review.update(r.id for r in entries)
    for short, entries in by_short.items():
        plausible = [r for name, candidates in by_name.items() if _compatible(short, name) for r in candidates]
        if len(entries) > 1 or plausible:
            review.update(r.id for r in (*entries, *plausible))

    groups = defaultdict(list)
    for row in rows:
        identity = (row.full_name,)
        if not row.full_name:
            identity = (row.id,)
        elif len(profiles_by_name[row.full_name]) > 1:
            # Several personal URLs for the same full name. Unlinked cards cannot
            # be assigned to either namesake, including other unlinked cards.
            review.add(row.id)
            identity = (row.full_name, row.profile or row.id)
        groups[identity].append(row)
    for entries in groups.values():
        group = f"{run_id}:{min(r.id for r in entries)}"
        snapshots = Counter(r.snapshot for r in entries)
        profile_counts = Counter(r.profile for r in entries if r.profile)
        page_count = len({r.url for r in entries})
        roles_differ = len({r.role for r in entries if r.role}) > 1
        for row in entries:
            if len(entries) == 1:
                match = "possible" if row.id in review else "single"
            elif row.profile and profile_counts[row.profile] > 1 and row.profile not in profile_conflicts:
                match = "same_profile"
            else:
                match = "same_name"
            yield (group, match, len(entries), page_count, snapshots[row.snapshot],
                   int(roles_differ), int(row.id in review), row.id)


def refresh_staff_duplicates(conn: sqlite3.Connection, run_id: int) -> None:
    """Recalculate cards, distinct final page URLs, and repeats on each snapshot in this run."""
    with _staff_transaction(conn):
        # Obtain the write lock before reading the set used to calculate the counts.
        conn.execute("UPDATE staff_observation SET duplicate_group = duplicate_group WHERE 0")
        conn.executemany("""
            UPDATE staff_observation SET duplicate_group = ?, duplicate_match = ?, duplicate_card_count = ?,
                duplicate_page_count = ?, duplicate_cards_on_page = ?, duplicate_roles_differ = ?,
                duplicate_needs_review = ? WHERE id = ?
        """, _annotations(_observations(conn, run_id), run_id))
