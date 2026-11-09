"""Staff observations belong to a particular page snapshot."""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable
from contextlib import contextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if __package__:
    from .render_store import incomplete_document
else:
    from render_store import incomplete_document

if TYPE_CHECKING:
    from browser_capture import RenderEvidence
    from staff_extract import ElementEvidence, StaffCard


@contextmanager
def _staff_transaction(conn: sqlite3.Connection):
    # A savepoint also works inside the caller's existing transaction.
    conn.execute("SAVEPOINT staff_write")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO staff_write")
        raise
    finally:
        conn.execute("RELEASE staff_write")


def _save_elements(conn: sqlite3.Connection, observation_id: int, elements: Iterable["ElementEvidence"]) -> None:
    # The queue ID identifies an immutable selector, field and expected value.
    # Re-running the HTML parser must not discard a check of unchanged evidence.
    old = {
        (field, selector, value): element_id
        for element_id, field, selector, value in conn.execute(
            "SELECT id, field, selector, expected_value FROM staff_element WHERE observation_id = ?",
            (observation_id,),
        )
    }
    wanted = {(element.field, element.selector, element.text) for element in elements}
    conn.executemany(
        "INSERT INTO staff_element (observation_id, field, selector, expected_value) VALUES (?, ?, ?, ?)",
        ((observation_id, *key) for key in sorted(wanted - old.keys())),
    )
    conn.executemany("DELETE FROM staff_element WHERE id = ?", ((old[key],) for key in old.keys() - wanted))


def save_staff(conn: sqlite3.Connection, page_id: int, cards: Iterable["StaffCard"]) -> None:
    """Atomically replace extracted observations for one snapshot; keep other snapshots intact."""
    with _staff_transaction(conn):
        keep = set()
        for card in cards:
            card_selectors = {el.selector for el in card.elements if el.field == "card" and el.selector}
            if len(card_selectors) != 1:
                raise ValueError("Для карточки нужен один CSS-селектор контейнера")
            (selector,) = card_selectors
            observation_id = conn.execute("""
                INSERT INTO staff_observation
                    (page_id, card_selector, full_name, role, is_head, photo_url, photo_state,
                     education, email, phone, profile_url, method)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (page_id, card_selector) DO UPDATE SET
                    full_name = excluded.full_name, role = excluded.role, is_head = excluded.is_head,
                    photo_url = excluded.photo_url, photo_state = excluded.photo_state,
                    education = excluded.education, email = excluded.email, phone = excluded.phone,
                    profile_url = excluded.profile_url, method = excluded.method
                RETURNING id
                """, (page_id, selector, card.name, card.role, card.is_head, card.photo_url, card.photo_state,
                      card.education, card.email, card.phone, card.profile_url, card.method)).fetchone()[0]
            if observation_id in keep:
                raise ValueError(f"Две карточки получили один селектор: {selector}")
            keep.add(observation_id)
            _save_elements(conn, observation_id, card.elements)

        if keep:
            outdated = [
                (row[0],) for row in conn.execute("SELECT id FROM staff_observation WHERE page_id = ?", (page_id,))
                if row[0] not in keep
            ]
            conn.executemany("DELETE FROM staff_observation WHERE id = ?", outdated)
        else:
            conn.execute("DELETE FROM staff_observation WHERE page_id = ?", (page_id,))


def record_visual_result(
    conn: sqlite3.Connection, element_id: int, status: str, details: str, *, expected_value: str,
    render_source: str = "html_only", render_url: str | None = None, css_loaded: bool | None = None,
    viewport_width: int | None = None, viewport_height: int | None = None, checked_at: int | None = None,
) -> bool:
    """Save a browser observation for this source and viewport, or unknown for incomplete evidence.

    css_loaded means the stylesheets required for this render were verified. Live results
    describe the rendered URL at checked_at; the older snapshot only supplies the candidate.
    Return False if a newer extraction replaced its evidence.
    """
    if status not in {"visible", "hidden", "partial", "occluded", "out_of_view", "not_found", "unknown"}:
        raise ValueError(f"Неизвестный результат визуальной проверки: {status}")
    if render_source not in {"html_only", "live", "archived_with_assets"}:
        raise ValueError(f"Неизвестный источник рендера: {render_source}")
    if render_source == "live":
        url = urlsplit(render_url or "")
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("Для живой проверки укажите фактический HTTP(S)-адрес страницы")
    if any(v is not None and (type(v) is not int or v <= 0) for v in (viewport_width, viewport_height)):
        raise ValueError("Размеры окна указываются в положительных целых CSS-пикселях")
    if css_loaded is not None and type(css_loaded) is not bool:
        raise ValueError("Признак проверки CSS должен быть True, False или None")
    if render_source == "html_only" or css_loaded is not True or viewport_width is None or viewport_height is None:
        status = "unknown"
    with _staff_transaction(conn):
        updated = conn.execute("""
            UPDATE staff_element SET visual_status = ?, visual_details = ?, checked_at = COALESCE(?, unixepoch()),
                render_source = ?, render_url = ?, css_loaded = ?, viewport_width = ?, viewport_height = ?
            WHERE id = ? AND expected_value = ?
        """, (status, details, checked_at, render_source, render_url, css_loaded, viewport_width, viewport_height,
              element_id, expected_value))
    return updated.rowcount == 1


def save_render_staff(conn: sqlite3.Connection, page_id: int, render: "RenderEvidence") -> None:
    """Link same-visit measurements only to matching evidence in this new snapshot."""
    cards = getattr(render, "staff_cards", None)
    if cards is None:
        return
    issues = Counter()
    try:
        save_staff(conn, page_id, cards)
    except ValueError as error:
        # An invalid set of detected cards rolled back its own savepoint; keep the HTTP capture.
        issues[str(error)] += 1
    else:
        measurements = render.details.get("staff_measurements", [])
        if not isinstance(measurements, list):
            issues["invalid_measurements"] += 1
        elif (measurements and not incomplete_document(render.details)
              and (not render.rendered_html or render.details.get("staff_source") != "rendered_dom")):
            issues["no_matching_rendered_dom"] += len(measurements)
        elif measurements:
            elements = {
                (card, field, selector, value): element_id
                for card, field, selector, value, element_id in conn.execute("""
                    SELECT s.card_selector, e.field, e.selector, e.expected_value, e.id
                    FROM staff_observation AS s JOIN staff_element AS e ON e.observation_id = s.id
                    WHERE s.page_id = ?
                """, (page_id,))
            }
            for item in measurements:
                if (not isinstance(item, dict) or not isinstance(item.get("details"), dict)
                        or not isinstance(item.get("status"), str)):
                    issues["invalid_measurement"] += 1
                    continue
                key = tuple(item.get(k) for k in ("card_selector", "field", "selector", "expected_value"))
                element_id = elements.get(key) if all(isinstance(v, str) for v in key) else None
                if element_id is None:
                    issues["unmatched_element"] += 1
                    continue
                facts = item["details"]
                status = item.get("status")
                if incomplete_document(render.details) or facts.get("hit_test_uncertain") is not False or (
                    facts.get("expected_match") is False and status != "not_found"
                ):
                    status = "unknown"
                try:
                    record_visual_result(
                        conn, element_id, status, json.dumps(facts, ensure_ascii=False), expected_value=key[3],
                        render_source="live", render_url=render.url, css_loaded=render.css_loaded,
                        viewport_width=render.viewport_width, viewport_height=render.viewport_height,
                        checked_at=render.captured_at,
                    )
                except ValueError as error:
                    issues[str(error)] += 1
    if issues:
        conn.execute("""
            UPDATE page_render SET details = json_set(details, '$.staff_storage_issues', json(?))
            WHERE page_id = ?
        """, (json.dumps(issues, ensure_ascii=False), page_id))


def staff_summary(conn: sqlite3.Connection, organization_id: int) -> list:
    """Group historical observations by person profile and role within one institution.

    Without a profile the snapshots must refer to the same card on the same page.
    Different roles remain separate and source observations are always retained.
    """
    return conn.execute("""
        SELECT s.full_name, s.role, s.profile_url, COUNT(DISTINCT p.url) AS source_pages,
               COUNT(DISTINCT p.id) AS snapshots, MAX(p.date_loaded) AS last_seen,
               MIN(s.id) AS example_observation_id
        FROM staff_observation AS s JOIN page AS p ON p.id = s.page_id
        WHERE p.id_organization = ?
        GROUP BY s.full_name, s.role, s.profile_url,
                 CASE WHEN s.profile_url IS NULL OR s.full_name IS NULL THEN p.url END,
                 CASE WHEN s.profile_url IS NULL OR s.full_name IS NULL THEN s.card_selector END
        ORDER BY s.full_name, s.role, s.profile_url
    """, (organization_id,)).fetchall()
