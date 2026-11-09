import sqlite3
import unittest

from crawl_store import finish_run, initialize_db, record_fetch, start_run
from scan_staff import scan_organization


SITE = "https://garden.example"


def card(name, role):
    return f"""
        <a class="gw-person-card" href="/staff/member">
          <div class="gw-person-card__name">{name}</div>
          <div class="gw-person-card__job">{role}</div>
        </a>"""


class StaffScanTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        initialize_db(self.db)
        with self.db:
            self.db.execute("INSERT INTO organization VALUES (1, 'Сад', ?, 'kindergarten')", (SITE,))
        self.run_id = start_run(self.db, 1)

    def snapshot(self, section, html):
        url = SITE + "/" + section
        return record_fetch(self.db, self.run_id, url, section, int(section != "home"),
                            status=200, html=html, loaded_at=100)

    def test_fallback_and_partial_run_do_not_mix_snapshots(self):
        self.snapshot("home", "<p>Добро пожаловать</p>")
        self.snapshot("about", card("Петрова Мария Ивановна", "Заведующий"))
        finish_run(self.db, self.run_id, "partial")
        result = scan_organization(self.db, 1)
        self.assertEqual((result.head_source, result.head_names), ("inner", ("Петрова Мария Ивановна",)))
        self.assertEqual(self.db.execute("SELECT count(*) FROM staff_observation").fetchone()[0], 1)

        self.run_id = start_run(self.db, 1)
        self.snapshot("home", "<p>Новое состояние сайта</p>")
        finish_run(self.db, self.run_id, "partial")
        self.assertEqual(scan_organization(self.db, 1).head_source, "unresolved")
        self.assertEqual(self.db.execute("SELECT count(*) FROM staff_observation").fetchone()[0], 1)

    def test_home_and_directory_disagreement_is_reported(self):
        self.snapshot("home", card("Петрова Мария Ивановна", "Заведующий"))
        self.snapshot("team", card("Иванова Анна Сергеевна", "Заведующий"))
        finish_run(self.db, self.run_id, "completed")
        result = scan_organization(self.db, 1)
        self.assertEqual(result.head_source, "conflict")
        self.assertEqual(len(result.head_names), 2)

    def test_older_continuation_stored_under_another_section_is_read_from_this_run(self):
        self.snapshot("team", '<nav class="pagination"><a href="/older-page2">2</a></nav>')
        record_fetch(self.db, self.run_id, SITE+"/older-page2", "hub", 2,
                     html=card("Петрова Анна Ивановна", "Воспитатель"), status=200)
        finish_run(self.db, self.run_id, "completed")
        scan_organization(self.db, 1)
        self.assertEqual(self.db.execute("SELECT full_name FROM staff_observation").fetchall(),
                         [("Петрова Анна Ивановна",)])

    def test_contact_page_can_hold_head_in_custom_markup(self):
        self.snapshot("home", "<p>Контактная информация в отдельном разделе</p>")
        self.snapshot("contacts", "<div><b>Директор</b><p>Петрова Анна Ивановна</p></div>")
        finish_run(self.db, self.run_id, "completed")
        result = scan_organization(self.db, 1)
        self.assertEqual((result.head_source, result.head_names), ("inner", ("Петрова Анна Ивановна",)))

    def test_same_full_name_in_different_order_is_not_a_conflict(self):
        self.snapshot("home", card("Петрова Мария Ивановна", "Заведующий"))
        self.snapshot("team", card("Мария Ивановна Петрова", "Заведующий"))
        finish_run(self.db, self.run_id, "completed")
        self.assertEqual(scan_organization(self.db, 1).head_source, "home")
        marks = self.db.execute("""
            SELECT duplicate_card_count, duplicate_page_count FROM staff_observation
        """).fetchall()
        self.assertEqual(marks, [(2, 2), (2, 2)])

    def test_reports_refresh_after_each_scan_and_after_new_audit(self):
        self.snapshot("home", card("Петрова Анна Ивановна", "Заведующий"))
        finish_run(self.db, self.run_id, "completed")
        scan_organization(self.db, 1)
        with self.db:
            self.db.execute("INSERT INTO organization VALUES (2, 'Школа', 'https://school.example', 'school')")
        second = start_run(self.db, 2)
        record_fetch(self.db, second, "https://school.example/", "home", 0, status=200,
                     html=card("Анна Ивановна Петрова", "Директор"))
        finish_run(self.db, second, "completed")
        scan_organization(self.db, 2)
        self.assertEqual(self.db.execute("""
            SELECT organization_id, organization_count, observation_count FROM staff_cross_org_match ORDER BY 1
        """).fetchall(), [(1, 2, 1), (2, 2, 1)])
        newer = start_run(self.db, 2)
        record_fetch(self.db, newer, "https://school.example/", "home", 0, status=200, html="<h1>Школа</h1>")
        finish_run(self.db, newer, "completed")
        self.assertEqual(self.db.execute("SELECT count(*) FROM staff_cross_org_match").fetchone()[0], 0)
        scan_organization(self.db, 2)
        self.assertEqual(self.db.execute("SELECT pages, observations FROM staff_scan_run WHERE run_id=?",
                                         (newer,)).fetchone(), (1, 0))


if __name__ == "__main__":
    unittest.main()
