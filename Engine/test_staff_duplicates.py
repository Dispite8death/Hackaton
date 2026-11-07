import sqlite3
import unittest

from config import STAFF_DUPLICATE_COLUMNS
from crawl_store import initialize_db, record_fetch, start_run
from staff_duplicates import refresh_staff_duplicates


class StaffDuplicatesTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        initialize_db(self.conn)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executemany("INSERT INTO organization(name,site_url,kind) VALUES ('Школа',?,'school')",
                              [('https://school.test/',), ('https://other.test/',)])
        self.run = start_run(self.conn, 1)

    def page(self, path="/", *, run=None, requested=None):
        base = "https://school.test"
        fetch = record_fetch(self.conn, run or self.run, base + (requested or path), "staff", 1,
                             status=200, html="", final_url=base + path)
        return self.conn.execute("SELECT page_id FROM crawl_fetch WHERE id=?", (fetch,)).fetchone()[0]

    def card(self, page, name="Семёнова Ольга Петровна", role="Учитель", profile=None):
        selector = self.conn.execute("SELECT count(*) FROM staff_observation WHERE page_id=?", (page,)).fetchone()[0]
        return self.conn.execute("""
            INSERT INTO staff_observation(page_id,card_selector,full_name,role,is_head,photo_state,profile_url,method)
            VALUES(?,?,?,?,0,'absent',?,'template')
        """, (page, str(selector), name, role, profile)).lastrowid

    def observation(self, row):
        return self.conn.execute("SELECT * FROM staff_observation WHERE id=?", (row,)).fetchone()

    def test_counts_renamed_profile_and_unchanged_rerun(self):
        home = self.page()
        first = self.card(home, profile="https://SCHOOL.TEST:443/old#photo")
        second = self.card(home, name="Ольга  Петровна СЕМЕНОВА", role="Директор", profile="/person")
        third = self.card(self.page("/person", requested="/old"))
        # The same final page was fetched from another entry. It is a new snapshot but the same source URL.
        fourth = self.card(self.page("/person", requested="/another"), role=" УЧИТЕЛЬ ")
        refresh_staff_duplicates(self.conn, self.run)
        results = [self.observation(i) for i in (first, second, third, fourth)]
        self.assertEqual(len({r["duplicate_group"] for r in results}), 1)
        self.assertEqual([r["duplicate_match"] for r in results], ["same_profile"]*2 + ["same_name"]*2)
        self.assertEqual([r["duplicate_cards_on_page"] for r in results], [2, 2, 1, 1])
        for r in results:
            self.assertEqual((r["duplicate_card_count"], r["duplicate_page_count"], r["duplicate_roles_differ"],
                              r["duplicate_needs_review"]), (4, 2, 1, 0))
        refresh_staff_duplicates(self.conn, self.run)
        self.assertEqual([tuple(self.observation(i)) for i in (first, second, third, fourth)],
                         list(map(tuple, results)))
        self.conn.execute("DELETE FROM staff_observation WHERE id=?", (second,))
        refresh_staff_duplicates(self.conn, self.run)
        r = self.observation(first)
        self.assertEqual((r["duplicate_card_count"], r["duplicate_cards_on_page"], r["duplicate_roles_differ"]),
                         (3, 1, 0))

    def test_namesakes_and_ambiguous_profile_are_not_merged(self):
        p = self.page()
        a = self.card(p, profile="/staff/a")
        alias = self.card(p, profile="/staff/a")
        b = self.card(p, profile="/staff/b")
        unlinked = self.card(p)
        different = self.card(p, name="Семёнова Ольга Павловна", profile="/staff/a")
        refresh_staff_duplicates(self.conn, self.run)
        rows = [self.observation(i) for i in (a, alias, b, unlinked, different)]
        self.assertEqual(len({r["duplicate_group"] for r in rows}), 4)
        self.assertEqual(rows[0]["duplicate_group"], rows[1]["duplicate_group"])
        self.assertEqual([r["duplicate_match"] for r in rows], ["same_name"] * 2 + ["possible"] * 3)
        self.assertTrue(all(r["duplicate_needs_review"] for r in rows))

    def test_initials_with_shared_url_are_only_a_possible_match(self):
        p = self.page()
        full = self.card(p, profile="/p")
        short1 = self.card(p, name="Семенова О.П.", profile="/p")
        short2 = self.card(p, name="О. П. Семёнова")
        other = self.card(p, name="Семёнова Ирина Михайловна", profile="/other")
        missing = self.card(p, name=None)
        refresh_staff_duplicates(self.conn, self.run)
        rows = [self.observation(i) for i in (full, short1, short2, other, missing)]
        self.assertEqual(len({r["duplicate_group"] for r in rows}), 5)
        self.assertEqual([r["duplicate_match"] for r in rows], ["possible"]*3 + ["single"]*2)

    def test_different_runs_dates_and_institutions_remain_separate(self):
        later, other = start_run(self.conn, 1), start_run(self.conn, 2)
        self.conn.execute("UPDATE crawl_run SET started_at=started_at+86400 WHERE id=?", (later,))
        rows = [self.card(self.page(run=run)) for run in (self.run, later, other)]
        self.conn.commit()
        self.conn.execute("BEGIN")
        refresh_staff_duplicates(self.conn, self.run)
        self.conn.rollback()
        self.assertIsNone(self.observation(rows[0])["duplicate_group"])
        refresh_staff_duplicates(self.conn, self.run)
        self.assertEqual([self.observation(row)["duplicate_match"] for row in rows],
                         ["single", "unreviewed", "unreviewed"])
        for run in (later, other):
            refresh_staff_duplicates(self.conn, run)
        self.assertEqual(len({self.observation(row)["duplicate_group"] for row in rows}), 3)

    def test_migration_preserves_observations_and_is_idempotent(self):
        i = self.card(self.page())
        for name in STAFF_DUPLICATE_COLUMNS:
            self.conn.execute(f"ALTER TABLE staff_observation DROP COLUMN {name}")
        initialize_db(self.conn)
        self.assertEqual(self.observation(i)["duplicate_match"], "unreviewed")
        refresh_staff_duplicates(self.conn, self.run)
        initialize_db(self.conn)
        self.assertEqual(self.observation(i)["duplicate_match"], "single")


if __name__ == "__main__":
    unittest.main()
