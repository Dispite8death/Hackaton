import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

import staff_report
from crawl_store import initialize_db
from staff_cross_org import CROSS_ORG_SCHEMA_SQL, refresh_cross_org_matches
from staff_duplicates import refresh_staff_duplicates


class StaffReportTest(unittest.TestCase):
    def setUp(self):
        with NamedTemporaryFile(prefix="staff report ", suffix=".sqlite", delete=False) as temp:
            self.path = Path(temp.name)
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.enterContext(patch.object(staff_report, "DB_PATH", self.path))
        self.db = self.enterContext(closing(sqlite3.connect(self.path)))
        initialize_db(self.db)
        self.db.executescript(CROSS_ORG_SCHEMA_SQL)
        for org in (58, 59, 60):
            self.db.execute("INSERT INTO organization(id,name,kind,site_url) VALUES (?,?,'school',?)",
                            (org, f"Учреждение {org}", f"https://{org}.example/"))
            self.db.execute("INSERT INTO crawl_run(id,id_organization,finished_at) VALUES (?,?,5)", (org, org))
        for pid, org, suffix, role, name in [
            (1, 58, "one", "Воспитатель", "Иванова Ирина Юрьевна"),
            (2, 58, "two", "Логопед", "Иванова Ирина Юрьевна"),
            (3, 59, "other", "Методист", "Ирина Юрьевна Иванова"),
            (4, 58, "initials", "Логопед", "Иванова И.Ю."),
            (5, 58, "single", "Методист", "Котова Лилия Петровна"),
        ]:
            url = f"https://{org}.example/{suffix}"
            self.db.execute("INSERT INTO page(id,url,id_organization,text_page) VALUES (?,?,?,'')", (pid,url,org))
            self.db.execute("""
                INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,page_id)
                VALUES (?,?,'staff',1,1,?)""", (org,url,pid))
            self.db.execute("""
                INSERT INTO staff_observation(page_id,card_selector,full_name,role,is_head,photo_state,method)
                VALUES (?,'#one',?,?,0,'absent','template')""", (pid,name,role))
        for org, count in [(58, 4), (59, 1)]:
            refresh_staff_duplicates(self.db, org)
            self.db.execute("INSERT INTO staff_scan_run(run_id,pages,observations) VALUES (?,?,?)", (org,count,count))
        refresh_cross_org_matches(self.db)
        self.db.commit()

    def report(self, *args):
        stream = StringIO()
        with redirect_stdout(stream):
            self.assertEqual(staff_report.main(list(args)), 0)
        return stream.getvalue()

    def test_cross_shows_both_roles_and_urls_and_ignores_outdated_index(self):
        result = self.report("--pair", "58", "59", "--limit", "1")
        for value in ["Иванова Ирина Юрьевна", "Ирина Юрьевна Иванова", "Методист", "Воспитатель",
                      "https://58.example/two", "https://59.example/other", "требуется сверка"]:
            self.assertIn(value, result)
        self.assertIn("https://58.example/one", self.report("--cross"))
        self.assertNotIn("initials", result)
        self.db.execute("INSERT INTO crawl_run(id,id_organization,finished_at) VALUES (70,58,9)")
        self.db.commit()
        newer = self.report("--pair", "58", "59")
        self.assertIn("обработка карточек ещё не отмечена", newer)
        self.assertNotIn("https://59.example/other", newer)

    def test_local_includes_possible_cards_and_skips_singletons_and_samples(self):
        self.db.execute("INSERT INTO crawl_run(id,id_organization,finished_at,purpose) VALUES (70,58,9,'sample')")
        self.db.commit()
        local = self.report("--org", "58")
        for value in ["карточек: 2", "адресов страниц: 2", "должности различаются",
                      "возможное совпадение", "https://58.example/initials"]:
            self.assertIn(value, local)
        self.assertNotIn("https://58.example/single", local)
        self.assertNotIn("https://59.example/other", local)
        one_group = self.report("--org", "58", "--limit", "1")
        self.assertIn("https://58.example/two", one_group)
        self.assertNotIn("https://58.example/initials", one_group)

    def test_processed_empty_audit_is_distinct_from_missing_scan(self):
        self.assertIn("ещё не отмечена", self.report("--pair", "58", "60"))
        self.db.execute("INSERT INTO staff_scan_run(run_id,pages,observations) VALUES (60,2,0)")
        self.db.commit()
        empty = self.report("--pair", "58", "60")
        self.assertIn("обработано карточек: 0", empty)
        self.assertIn("В сохранённых карточках", empty)
        self.assertNotIn("ещё не отмечена", empty)


if __name__ == "__main__":
    unittest.main()
