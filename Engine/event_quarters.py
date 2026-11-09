"""Compare distinct dated event entries with the two quarters of a saved Magadan audit."""

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit

if __package__:
    from .home_events import _printed_dates
    from .home_store import encoded
else:
    from home_events import _printed_dates
    from home_store import encoded


MAGADAN = timezone(timedelta(hours=11))


def quarter_bounds(day):
    first = date(day.year, ((day.month - 1) // 3) * 3 + 1, 1)
    for label in ("current", "next"):
        following = date(first.year + (first.month == 10), first.month + 3 if first.month < 10 else 1, 1)
        yield label, first, following - timedelta(days=1)
        first = following


def _listing_coverage(conn, run_id, organization_id):
    advertised = {urldefrag(url).url for url, in conn.execute("""
        SELECT DISTINCT l.url FROM home_event_link l JOIN page p ON p.id=l.page_id
        JOIN crawl_fetch f ON f.page_id=p.id WHERE f.run_id=? AND p.id_organization=?
        AND l.kind IN ('section','navigation') AND l.url IS NOT NULL
    """, (run_id, organization_id))}
    aliases, snapshots, listings = {}, {}, {}
    for page, requested, final, kind, status, evidence in conn.execute("""
        SELECT p.id,f.url,p.url,b.page_kind,b.block_status,b.evidence
        FROM crawl_fetch f JOIN page p ON p.id=f.page_id LEFT JOIN home_event_block b ON b.page_id=p.id
        WHERE f.run_id=? AND p.id_organization=? ORDER BY f.id
    """, (run_id, organization_id)):
        aliases[urldefrag(requested).url] = urldefrag(final).url
        snapshots[urldefrag(final).url] = status
        if kind == "listing" or (kind not in {"home","detail"} and ({requested, final} & advertised)):
            info = json.loads(evidence) if evidence else {}
            reasons = []
            if (status not in {"cards", "empty"}
                    or any(block.get("status") == "ambiguous" for block in info.get("blocks", []))):
                reasons.append("unrecognized_listing_content")
            keys = (key.casefold() for url in (requested, final)
                    for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))
            if info.get("listing_filters_active") or any(key == "filter" or key.startswith("filter[") for key in keys):
                reasons.append("filtered_listing")
            targets = {urljoin(final, url) for url in info.get("pagination_urls", [])}
            listings[page] = {"page_id": page, "url": final, "block_status": status, "reasons": reasons,
                              "pagination": targets}
    for page, url in conn.execute("""
        SELECT f.page_id,l.url FROM crawl_link l JOIN crawl_fetch f ON f.id=l.fetch_id
        WHERE f.run_id=? AND l.relation='pagination'
    """, (run_id,)):
        if page in listings:
            listings[page]["pagination"].add(url)
    for info in listings.values():
        unresolved = []
        for url in sorted(info.pop("pagination")):
            target = urldefrag(url).url
            if snapshots.get(aliases.get(target, target)) not in {"cards", "empty"}:
                unresolved.append(url)
        if unresolved:
            info["reasons"].append("uncaptured_or_unparsed_pagination")
            info["unresolved_pagination"] = unresolved
    missing = sorted(url for url in advertised if aliases.get(url, url) not in snapshots)
    return aliases, listings, missing


def refresh_quarters(conn, run_id):
    run = conn.execute("""
        SELECT r.started_at,r.state,r.stop_reason,r.finished_at,r.id_organization FROM crawl_run r
        JOIN organization o ON o.id=r.id_organization WHERE r.id=? AND o.kind='school'
    """, (run_id,)).fetchone()
    if run is None:
        return
    fetched_day = datetime.fromtimestamp(run[0], MAGADAN).date()
    aliases, listings, missing_listings = _listing_coverage(conn, run_id, run[4])
    coverage_incomplete = (run[1] != "completed" or run[3] is None or bool(missing_listings)
                           or any(info["reasons"] for info in listings.values()))
    entries = defaultdict(list)
    for page_id, ordinal, link, title, start, end, evidence in conn.execute("""
        SELECT l.page_id,l.ordinal,l.url,l.title,l.date_start,l.date_end,l.evidence FROM home_event_link l
        JOIN crawl_fetch f ON f.page_id=l.page_id JOIN page p ON p.id=f.page_id
        WHERE f.run_id=? AND p.id_organization=? AND l.kind='event'
        ORDER BY l.page_id,l.ordinal
    """, (run_id, run[4])):
        url = urldefrag(link).url if link else None
        key = aliases.get(url, url) or f"page:{page_id},card:{ordinal}"
        entries[key].append((page_id, ordinal, title, start, end, json.loads(evidence)))
    dated, undated, templates = [], [], []
    for key, rows in entries.items():
        refs = [{"page_id": r[0], "ordinal": r[1], "date_text": r[5].get("date_text"),
                 "date_start": r[3], "date_end": r[4], "date_evidence": r[5].get("date_evidence", {})} for r in rows]
        facts = {"event": key, "title": rows[0][2], "sources": refs}
        signals = {signal for r in rows for signal in r[5].get("evidence", {}).get("heuristics", [])}
        if signals & {"template_title", "template_description"}:
            templates.append(dict(facts, signals=sorted(signals)))
            continue
        ranges = {(r[3], r[4] or r[3]) for r in rows if r[3]}
        reasons = {r[5].get("date_evidence", {}).get("reason") for r in rows}
        if ("possible_date_error" in reasons or any(ref["date_evidence"].get("possible_date_error") for ref in refs)):
            undated.append(dict(facts, reason="possible_date_error"))
            continue
        conflicts = any(reason and "conflict" in reason for reason in reasons)
        if len(ranges) == 1:
            lo, hi = next(iter(ranges))
            try:
                first, last = date.fromisoformat(lo), date.fromisoformat(hi)
                for row in rows:
                    if row[5].get("date_evidence", {}).get("reason") == "no_year_in_date_field":
                        points = _printed_dates(row[5].get("date_text") or "")
                        if points and (points[0][1:] != (first.month, first.day) or
                                       len(points) > 1 and points[-1][1:] != (last.month, last.day)):
                            conflicts = True
                if first > last:
                    reasons.add("invalid_date_or_range")
            except (TypeError, ValueError):
                reasons.add("invalid_date_or_range")
        if "invalid_date_or_range" in reasons:
            undated.append(dict(facts, reason="invalid_date_or_range"))
            continue
        if conflicts or len(ranges) != 1:
            reason = "conflicting_dates" if conflicts or len(ranges) > 1 else "no_complete_calendar_date"
            undated.append(dict(facts, reason=reason))
            continue
        start, end = next(iter(ranges))
        dated.append((start, end, dict(facts, start=start, end=end)))
    for period, start, end in quarter_bounds(fetched_day):
        matches = [ev for lo, hi, ev in dated if lo <= end.isoformat() and hi >= start.isoformat()]
        if matches:
            status = "dated_candidates"
        elif undated:
            status = "dates_unresolved"
        elif not listings:
            status = "listing_not_captured"
        elif coverage_incomplete:
            status = "listing_unresolved"
        else:
            status = "none_in_saved_pages"
        evidence = dict(reference_date=fetched_day.isoformat(), time_zone="Asia/Magadan", source="calendar_fields",
                        run_state=run[1], stop_reason=run[2], listing_page_ids=list(listings),
                        listings=list(listings.values()), missing_listing_urls=missing_listings,
                        coverage_incomplete=bool(coverage_incomplete or not listings),
                        dated_candidates=matches, unresolved=undated, template_signals=templates)
        conn.execute("""
            INSERT INTO school_event_quarter(run_id,period,start_date,end_date,status,dated_events,evidence)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,period) DO UPDATE SET start_date=excluded.start_date,
                end_date=excluded.end_date,status=excluded.status,dated_events=excluded.dated_events,
                evidence=excluded.evidence
        """, (run_id, period, start.isoformat(), end.isoformat(), status, len(matches), encoded(evidence)))

