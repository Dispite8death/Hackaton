import sqlite3
import unittest

from config import SCHEMA_SQL
from staff_cross_org import CROSS_ORG_SCHEMA_SQL, refresh_cross_org_matches
from staff_identity import full_name_key


class StaffCrossOrgTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA_SQL + CROSS_ORG_SCHEMA_SQL)
        self.db.executemany("INSERT INTO organization (id, name, site_url, kind) VALUES (?, ?, ?, 'school')",
                            ((i, f"Школа {i}", f"https://s{i}.example") for i in (1, 2, 3)))
        self.db.commit()

    def audit(self, org, purpose="audit", finished=True):
        return self.db.execute("""
            INSERT INTO crawl_run (id_organization, purpose, state, finished_at)
            VALUES (?, ?, ?, ?) RETURNING id
        """, (org, purpose, "completed" if finished else "running", 100 if finished else None)).fetchone()[0]

    def card(self, run_id, name, role="Учитель", section="staff"):
        org = self.db.execute("SELECT id_organization FROM crawl_run WHERE id=?", (run_id,)).fetchone()[0]
        url = f"https://s{org}.example/{section}"
        page_id = self.db.execute("""
            INSERT INTO page (url, id_organization, text_page) VALUES (?, ?, '') RETURNING id
        """, (url, org)).fetchone()[0]
        self.db.execute("""
            INSERT INTO crawl_fetch (run_id, url, section, depth, fetched_at, page_id)
            VALUES (?, ?, ?, 1, 100, ?)
        """, (run_id, url, section, page_id))
        return self.db.execute("""
            INSERT INTO staff_observation (page_id, card_selector, full_name, role, is_head, photo_state, method)
            VALUES (?, '#card', ?, ?, 0, 'missing', 'fixture') RETURNING id
        """, (page_id, name, role)).fetchone()[0]

    def test_only_unabbreviated_full_names(self):
        self.assertEqual(full_name_key("  ПЁТРОВА-ТЁМНАЯ   Ольга Ильинична "),
                         full_name_key("Ольга Ильинична Петрова‑Темная"))
        for value in (None, "", "Иванов", "Петров И. В.", "Петров И В", "Петров Игорь",
                      "Петров Игорь 2000", "Петров И. Васильевич", "Петров! Игорь Васильевич"):
            with self.subTest(value=value):
                self.assertIsNone(full_name_key(value))

    def test_each_institution_has_one_row_and_keeps_all_sources(self):
        first, second, third = (self.audit(org) for org in (1, 2, 3))
        a = self.card(first, "Семёнова Мария Юрьевна", "Директор", "home")
        b = self.card(first, "Мария Юрьевна Семенова", "Учитель", "management")
        c = self.card(second, "СЕМЕНОВА МАРИЯ ЮРЬЕВНА")
        self.card(third, "Семенова М. Ю.")
        self.card(third, "Семенова Марина Юрьевна")
        self.card(first, "Петров Иван Иванович", section="team")
        self.card(first, "Петров Иван Иванович", section="profile")
        # No offline scan marker exists: live observations still form usable evidence.
        self.assertEqual(refresh_cross_org_matches(self.db), 1)
        self.assertEqual(refresh_cross_org_matches(self.db), 1)
        rows = self.db.execute("""
            SELECT organization_id, run_id, representative_observation_id, organization_count,
                   observation_count, status FROM staff_cross_org_match ORDER BY organization_id
        """).fetchall()
        self.assertEqual(rows, [(1, first, a, 2, 2, "name_match_needs_review"),
                                (2, second, c, 2, 1, "name_match_needs_review")])
        evidence = self.db.execute("""
            SELECT observation_id, section, role, source_url FROM staff_cross_org_source ORDER BY observation_id
        """).fetchall()
        self.assertEqual(evidence, [(a, "home", "Директор", "https://s1.example/home"),
                                    (b, "management", "Учитель", "https://s1.example/management"),
                                    (c, "staff", "Учитель", "https://s2.example/staff")])

    def test_newer_finished_audit_replaces_old_evidence_per_institution(self):
        name = "Климова Анна Ильинична"
        first, second = self.audit(1), self.audit(2)
        self.card(first, name)
        self.card(second, name)
        self.assertEqual(refresh_cross_org_matches(self.db), 1)
        self.card(self.audit(3, purpose="sample"), name)
        self.card(self.audit(3, finished=False), name)
        self.audit(2, purpose="sample")
        self.assertEqual(refresh_cross_org_matches(self.db), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM staff_cross_org_evidence").fetchone()[0], 2)
        # Losing a later match means there is no current evidence for this pair.
        self.audit(2)
        self.assertEqual(refresh_cross_org_matches(self.db), 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM staff_cross_org_source").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM staff_observation").fetchone()[0], 4)

    def test_failed_refresh_retains_prior_report_and_outer_transaction(self):
        name = "Климова Анна Ильинична"
        for org in (1, 2):
            self.card(self.audit(org), name)
        refresh_cross_org_matches(self.db)
        self.db.commit()
        self.db.execute("""CREATE TEMP TRIGGER fail_refresh BEFORE INSERT ON staff_cross_org_evidence
                           BEGIN SELECT RAISE(ABORT, 'test abort'); END""")
        self.card(self.audit(3), name)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test abort"):
            refresh_cross_org_matches(self.db)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.db.execute("SELECT count(*) FROM staff_cross_org_source").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM staff_observation").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()
