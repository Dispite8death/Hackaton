import json
import sqlite3
import unittest

from config import SCHEMA_SQL
from page_passport import index_all, index_page


class PassportTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.executescript("""
            INSERT INTO organization VALUES(1,'Школа','https://school.test/','school'),(2,'Сад','https://garden.test/','kindergarten');
            INSERT INTO crawl_run(id,id_organization) VALUES(1,1),(2,2);
        """)

    def page(self, html, url, section, depth=1, org=1):
        with self.db:
            pid = self.db.execute("INSERT INTO page(url,text_page,id_organization) VALUES(?,?,?)",
                                  (url,html,org)).lastrowid
            self.db.execute("INSERT INTO crawl_fetch(run_id,url,depth,section,fetched_at,page_id) VALUES(?,?,?,?,1,?)",
                            (org,url,depth,section,pid))
        return pid

    def result(self, pid):
        row = dict(self.db.execute("SELECT * FROM page_passport WHERE page_id=?", (pid,)).fetchone())
        row["checks"] = {c["id"]: c for c in json.loads(row["checks"])}
        row["headings"] = json.loads(row["headings"])
        return row

    def test_observations_are_sourced_and_eligible_checks_are_not_findings(self):
        html = """<head><title>Главная &amp; новости</title></head><nav><h2>Меню</h2></nav>
            <main><header><h1>Наша <b>школа</b></h1></header><p>Здесь учатся дети.</p>
            <aside>Чужой текст</aside><h2 aria-hidden="true">Скрытый</h2><script>log()</script></main>"""
        pid = self.page(html, "https://school.test/", "home", 0)
        self.assertTrue(index_page(self.db, pid))
        row = self.result(pid)
        self.assertEqual(row["title"], "Главная & новости")
        self.assertEqual(row["headings"], [{"level": 1, "text": "Наша школа"}])
        self.assertEqual(row["summary"], "Наша школа Здесь учатся дети.")
        self.assertIn("image", row["checks"]["school_main_photo"]["material_kinds"])
        self.assertNotIn("столовая", row["summary"].lower())
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT count(*) FROM page_passport").fetchone()[0], 0)
        self.assertFalse(index_page(self.db, -17))

    def test_current_scope_detail_and_empty_values(self):
        ids = [self.page("<main><h1>Воспитатели</h1></main>", "/team/", "team", org=2),
               self.page('<main><div class="gw-person-detail">Воспитатель группы</div></main>', "/people/1", "team", org=2),
               self.page("<p>Прием с 7:00 до 19:00</p>", "/contacts", "contacts", org=2),
               self.page("", "/events/item.html", "events"), self.page("", "/news/", "news")]
        self.assertEqual(index_all(self.db), len(ids))
        self.assertEqual(index_all(self.db), len(ids))
        rows = [self.result(pid) for pid in ids]
        self.assertEqual([r["page_kind"] for r in rows], ["staff_directory","staff_profile","contacts","event_detail","news_listing"])
        self.assertTrue({"garden_address","garden_phone","garden_email","garden_hours"} <= rows[2]["checks"].keys())
        self.assertEqual((rows[3]["title"],rows[3]["summary"],rows[3]["headings"]), ("","",[]))
        self.assertIn("events_evidence", rows[3]["checks"])
        self.assertIn("news_evidence", rows[4]["checks"])
        self.assertIn("staff_directory_coverage", rows[0]["checks"])

    def test_binary_checks_are_scoped_to_kind_and_visual_evidence(self):
        school = self.page("", "https://school.test/", "home", 0)
        garden = self.page("", "https://garden.test/", "home", 0, 2)
        index_all(self.db)
        s, g = self.result(school)["checks"], self.result(garden)["checks"]
        self.assertIn("events_quarter_next", s)
        self.assertNotIn("events_quarter_current", g)
        self.assertTrue({"garden_building_photo","garden_team_cards","garden_staff_list_link","garden_event_cards"} <= g.keys())
        for code in ("screen_layout","feedback_banner_overlap","canteen_photo"):
            self.assertFalse({"html","dom"} & set(s[code]["material_kinds"]))


if __name__ == "__main__":
    unittest.main()
