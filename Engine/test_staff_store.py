import sqlite3
import unittest
from types import SimpleNamespace

from crawl_store import initialize_db
from staff_store import record_visual_result, save_staff, staff_summary


def card(selector="article", name="Анна Петрова", role="Директор", profile="https://school.test/staff/1"):
    return SimpleNamespace(
        name=name, role=role, is_head=role == "Директор", photo_url=None, photo_state="absent",
        education=None, email=None, phone=None, profile_url=profile, method="semantic",
        elements=(
            SimpleNamespace(field="card", selector=selector, text=name),
            SimpleNamespace(field="role", selector=selector + " .role", text=role),
        ),
    )


class StaffStoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA foreign_keys = ON")
        initialize_db(self.conn)
        with self.conn:
            self.conn.execute("INSERT INTO organization (name, site_url, kind) VALUES ('Школа', '/', 'school')")
            self.conn.executemany("INSERT INTO page (id_organization, url, text_page) VALUES (1, ?, '')",
                                  [("/",), ("/team/",)])

    def test_repeat_replace_and_preserve_element_checks(self):
        save_staff(self.conn, 1, [card()])
        original = self.conn.execute("SELECT id, expected_value FROM staff_element WHERE field='role'").fetchone()
        self.assertTrue(record_visual_result(
            self.conn, original[0], "occluded", "Баннер", expected_value=original[1],
            render_source="live", render_url="https://school.test/", css_loaded=True,
            viewport_width=1280, viewport_height=720,
        ))
        save_staff(self.conn, 1, [card()])
        self.assertEqual(self.conn.execute("SELECT id, visual_status FROM staff_element WHERE field='role'").fetchone(),
                         (original[0], "occluded"))
        save_staff(self.conn, 2, [card(selector=".person")])
        self.assertEqual(staff_summary(self.conn, 1)[0][3:5], (2, 2))
        save_staff(self.conn, 1, [card(role="Учитель")])
        self.assertFalse(record_visual_result(self.conn, original[0], "visible", "", expected_value=original[1]))
        self.assertEqual(self.conn.execute("SELECT DISTINCT visual_status FROM staff_element").fetchall(),
                         [("pending",)])
        self.assertEqual(len(staff_summary(self.conn, 1)), 2)
        save_staff(self.conn, 1, [])
        self.assertEqual(self.conn.execute("SELECT page_id FROM staff_observation").fetchall(), [(2,)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM staff_element").fetchone(), (2,))
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_duplicate_rolls_back_and_respects_outer_transaction(self):
        save_staff(self.conn, 1, [card()])
        with self.assertRaises(ValueError):
            save_staff(self.conn, 1, [card(role="Учитель"), card()])
        self.assertEqual(self.conn.execute("SELECT role FROM staff_observation").fetchall(), [("Директор",)])
        self.conn.execute("BEGIN")
        save_staff(self.conn, 1, [])
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM staff_observation").fetchone(), (1,))

    def test_names_without_profiles_stay_separate(self):
        save_staff(self.conn, 1, [card(profile=None), card(selector=".another", profile=None)])
        self.assertEqual(len(staff_summary(self.conn, 1)), 2)

    def test_visibility_requires_a_rendered_source_verified_css_and_viewport(self):
        save_staff(self.conn, 1, [card()])
        element_id, value = self.conn.execute("SELECT id, expected_value FROM staff_element LIMIT 1").fetchone()
        for source, css, width, height, result in [
            ("html_only", True, 1024, 768, "unknown"), ("archived_with_assets", None, 1024, 768, "unknown"),
            ("live", False, 1024, 768, "unknown"), ("live", True, None, None, "unknown"),
            ("archived_with_assets", True, 1024, 768, "visible"), ("live", True, 1280, 720, "visible"),
        ]:
            with self.subTest(source=source, css=css, width=width):
                url = "https://school.test/actual/" if source == "live" else None
                record_visual_result(self.conn, element_id, "visible", "", expected_value=value, render_source=source,
                                     render_url=url, css_loaded=css, viewport_width=width, viewport_height=height)
                self.assertEqual(self.conn.execute("""
                    SELECT visual_status, render_source, render_url, css_loaded, viewport_width, viewport_height
                    FROM staff_element WHERE id = ?
                """, (element_id,)).fetchone(), (result, source, url, css, width, height))
        with self.assertRaises(ValueError):
            record_visual_result(self.conn, element_id, "visible", "", expected_value=value, render_source="live")

    def test_migrate_earlier_visual_results_with_no_render_context(self):
        save_staff(self.conn, 1, [card()])
        for column in ("render_source", "render_url", "css_loaded", "viewport_width", "viewport_height"):
            self.conn.execute(f"ALTER TABLE staff_element DROP COLUMN {column}")
        with self.conn:
            self.conn.execute("UPDATE staff_element SET visual_status='visible' WHERE field='role'")
        initialize_db(self.conn)
        initialize_db(self.conn)
        self.assertEqual(self.conn.execute("SELECT field, visual_status FROM staff_element ORDER BY field").fetchall(),
                         [("card", "pending"), ("role", "unknown")])


if __name__ == "__main__":
    unittest.main()
