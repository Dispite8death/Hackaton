import json
import sqlite3
import unittest
from types import SimpleNamespace

from crawl_store import initialize_db, record_fetch, start_run
from render_store import render_artifact, render_info, staff_source_html
from staff_extract import extract_staff
from staff_store import save_staff


def sample_render(**changes):
    fields = dict(
        captured_at=123, url="https://school.test/", viewport_width=1280, viewport_height=720,
        rendered_html="<p>Отрисованный текст</p>", screenshot=b"viewport", full_screenshot=b"long image",
        pdf=b"%PDF-test", css_loaded=True,
        details={"requested_artifacts": ["dom", "screenshot", "full_screenshot", "pdf"],
                 "artifact_errors": {}, "pos": {"selector": "#pos", "covered_points": 3}},
    )
    fields.update(changes)
    return SimpleNamespace(**fields)


class RenderStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize_db(self.db)
        with self.db:
            self.db.execute("INSERT INTO organization(name,site_url,kind) VALUES ('Школа','https://school.test/','school')")
        self.run_id = start_run(self.db, 1)

    def save(self, render=None, **kwargs):
        fetch_id = record_fetch(self.db, self.run_id, "https://school.test/", "home", 0,
                                status=200, html="<p>Ответ сервера</p>", render=render, **kwargs)
        return self.db.execute("SELECT page_id FROM crawl_fetch WHERE id=?", (fetch_id,)).fetchone()[0]

    def test_artifacts_belong_to_one_snapshot_and_export_exact_bytes(self):
        first = self.save()
        second = self.save(sample_render())
        initialize_db(self.db)
        self.assertIsNone(render_info(self.db, first))
        self.assertEqual(render_artifact(self.db, second, "dom"), "<p>Отрисованный текст</p>".encode("utf-8"))
        self.assertEqual(render_artifact(self.db, second, "screenshot"), b"viewport")
        self.assertEqual(render_artifact(self.db, second, "full-screenshot"), b"long image")
        self.assertEqual(render_artifact(self.db, second, "pdf"), b"%PDF-test")
        info = json.loads(render_artifact(self.db, second, "details"))
        self.assertEqual(info["page_id"], second)
        self.assertEqual(info["status"], "complete")
        self.assertTrue(info["css_loaded"])
        self.assertEqual(info["details"]["pos"]["covered_points"], 3)
        self.assertEqual(self.db.execute("SELECT text_page FROM page WHERE id=?", (second,)).fetchone()[0],
                         "<p>Ответ сервера</p>")

    def test_partial_failed_and_unrequested_artifacts(self):
        details = {"requested_artifacts": ["dom", "screenshot", "full_screenshot", "pdf"],
                   "artifact_errors": {"pdf": "timeout", "full_screenshot": "timeout"}}
        page = self.save(sample_render(pdf=None, full_screenshot=None, css_loaded=None, details=details))
        info = render_info(self.db, page)
        self.assertEqual(info["status"], "partial")
        self.assertEqual(info["available_artifacts"], ["dom", "screenshot"])
        self.assertEqual(info["details"]["artifact_errors"]["pdf"], "timeout")
        self.assertIsNone(render_artifact(self.db, page, "pdf"))
        self.assertIsNone(info["css_loaded"])
        page = self.save(sample_render(pdf=None, full_screenshot=None, screenshot=None, rendered_html=None,
                                       css_loaded=False, details=details))
        self.assertEqual(render_info(self.db, page)["status"], "failed")
        page = self.save(sample_render(pdf=None, details={"requested_artifacts": ["dom", "screenshot", "full_screenshot"]}))
        self.assertEqual(render_info(self.db, page)["status"], "complete")

    def test_atomic_write_and_http_error(self):
        class BrokenLink:
            pass

        with self.assertRaises(AttributeError):
            self.save(sample_render(), links=[BrokenLink()])
        for table in ("page", "page_render", "crawl_fetch"):
            self.assertEqual(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        record_fetch(self.db, self.run_id, "https://school.test/missing", "news", 1,
                     status=404, error="Not found", render=sample_render())
        self.assertEqual(self.db.execute("SELECT count(*) FROM page_render").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM crawl_fetch").fetchone()[0], 1)

    def test_staff_live_measurements_match_the_rendered_snapshot(self):
        dom = """<a class="gw-person-card" href="/person/7">
            <strong class="gw-person-card__name">Петрова Анна Алексеевна</strong>
            <span class="gw-person-card__job">Заведующая детским садом</span></a>"""
        render = sample_render(rendered_html=dom)
        render.staff_cards = extract_staff(dom, render.url)
        card, = render.staff_cards
        card_selector = next(e.selector for e in card.elements if e.field == "card")
        render.details.update(staff_source="rendered_dom", staff_measurements=[
            dict(card_selector=card_selector, field=e.field, selector=e.selector, expected_value=e.text,
                 status="visible", details={"hit_test_uncertain": e.field == "role", "expected_match": True})
            for e in card.elements
        ])
        first = self.save(render)
        self.assertEqual(staff_source_html(self.db, first), dom)
        save_staff(self.db, first, extract_staff(staff_source_html(self.db, first), render.url))
        query = """SELECT e.field,e.visual_status,e.checked_at,e.render_url,e.viewport_width
                   FROM staff_element e JOIN staff_observation s ON s.id=e.observation_id
                   WHERE s.page_id=? ORDER BY e.field"""
        states = self.db.execute(query, (first,)).fetchall()
        self.assertIn(("name", "visible", 123, render.url, 1280), states)
        self.assertIn(("role", "unknown", 123, render.url, 1280), states)
        render.css_loaded = False
        second = self.save(render)
        self.assertTrue(all(row[1] == "unknown" for row in self.db.execute(query, (second,))))
        self.assertEqual(self.db.execute(query, (first,)).fetchall(), states)
        render.css_loaded = True
        for marker in ({"document_ready_state": "loading"}, {"document_complete_for_extraction": False}):
            with self.subTest(marker=marker):
                render.details.update(marker)
                early = self.save(render)
                self.assertEqual(render_info(self.db, early)["status"], "partial")
                early_rows = self.db.execute(query, (early,)).fetchall()
                self.assertEqual({row[1] for row in early_rows}, {"unknown"})
                self.assertEqual(staff_source_html(self.db, early), "<p>Ответ сервера</p>")
                render.details.pop(next(iter(marker)))
        raw = self.save()
        self.assertEqual(staff_source_html(self.db, raw), "<p>Ответ сервера</p>")

    def test_different_print_geometry_is_independent_of_document_readiness(self):
        for state in ("complete", "interactive"):
            with self.subTest(state=state):
                render = sample_render()
                render.details.update(document_ready_state=state, print_comparison={"overflow_changed": True})
                page = self.save(render)
                self.assertEqual(render_info(self.db, page)["status"], "complete")

    def test_staff_measurement_errors_preserve_capture_but_db_errors_roll_back(self):
        render = sample_render()
        render.staff_cards = []
        render.details.update(staff_source="rendered_dom", staff_measurements=[{}])
        page = self.save(render)
        self.assertEqual(render_info(self.db, page)["details"]["staff_storage_issues"], {"invalid_measurement": 1})
        self.assertIsNotNone(render_artifact(self.db, page, "dom"))
        self.db.execute("""CREATE TEMP TRIGGER deny_staff BEFORE INSERT ON staff_observation
                           BEGIN SELECT RAISE(ABORT, 'store error'); END""")
        render.staff_cards = extract_staff("""<a class="gw-person-card" href="/p">
            <strong class="gw-person-card__name">Петрова Анна Алексеевна</strong>
            <span class="gw-person-card__job">Директор</span></a>""", render.url)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "store error"):
            self.save(render)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM page_render").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
