import sqlite3
import unittest
from contextlib import contextmanager

from browser_capture import RenderEvidence
from config import SCHEMA_SQL
from http_page import PageResponse
from revisit_pages import revisit_pages


class RevisitTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA_SQL)
        self.db.executescript("""
            INSERT INTO organization VALUES(1,'Garden','https://one.example/','kindergarten'),
                                           (2,'School','https://two.example/','school');
            INSERT INTO crawl_run(id,id_organization,finished_at,state) VALUES(8,1,10,'completed'),(9,2,10,'completed');
            INSERT INTO page VALUES(11,'https://one.example/staff/?page=2',1,7,'old snapshot'),
                                   (12,'https://two.example/news/',2,8,'old news');
            INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,page_id) VALUES
                (8,'https://one.example/staff/?page=2','team',2,7,11),(9,'https://two.example/news/','news',1,8,12);
        """)
        self.calls, self.sessions = [], []
        self.pages = {
            "https://one.example/staff/?page=2": """<nav class="pagination">
                <a href="?page=1">1</a><a class="active" href="?page=2">2</a>
                <a href="?page=4">4</a><a href="?page=3">3</a>
                <a href="https://external.example/staff/?page=5">Следующая</a></nav>""",
            "https://one.example/staff/?page=3": '<nav class="pagination"><a href="?page=4">4</a></nav>',
        }

    @contextmanager
    def capture(self, **opts):
        self.sessions.append(opts)
        yield self

    def fetch(self,url,site,timeout,*,section):
        self.calls.append((url,section))
        html=self.pages.get(url)
        if html is None:
            return PageResponse(url,404,None,"HTTP 404",100)
        render=RenderEvidence(100,url,800,500,html,b"window",b"full",b"%PDF",True,
                              {"requested_artifacts":["dom","screenshot","full_screenshot","pdf"]})
        return PageResponse(url,200,html,loaded_at=100,render=render)

    def test_revisits_only_requested_and_one_observed_forward_link(self):
        reports=revisit_pages(self.db,[11,11,12],staff_next=True,render_wait=3,capture_factory=self.capture)
        self.assertEqual(self.sessions,[{"pdf":True,"ready_timeout":3}]*2)
        self.assertEqual(self.calls,[("https://one.example/staff/?page=2","team"),
                                    ("https://one.example/staff/?page=3","team"),("https://two.example/news/","news")])
        self.assertEqual([r["relation"] for r in reports],["revisit","staff_next","revisit"])
        self.assertEqual([r["original_page_id"] for r in reports],[11,11,12])
        stored=self.db.execute("""
            SELECT v.original_page_id,v.relation,f.depth,f.section,f.http_status,f.fetched_at,r.purpose,r.state
            FROM page_revisit v JOIN crawl_fetch f ON f.id=v.fetch_id JOIN crawl_run r ON r.id=f.run_id ORDER BY f.id
        """).fetchall()
        self.assertEqual(stored,[(11,"revisit",2,"team",200,100,"sample","completed"),
                                 (11,"staff_next",3,"team",200,100,"sample","completed"),
                                 (12,"revisit",1,"news",404,100,"sample","failed")])
        self.assertEqual(self.db.execute("SELECT count(*) FROM page_render WHERE pdf=? AND full_screenshot=?",
                                         (b"%PDF",b"full")).fetchone()[0],2)
        self.assertEqual(self.db.execute("SELECT date_loaded,text_page FROM page WHERE id=11").fetchone(),
                         (7,"old snapshot"))
        self.assertIsNone(reports[-1]["page_id"])

    def test_unresolved_control_does_not_generate_a_url_and_invalid_id_fails_before_visits(self):
        self.pages["https://one.example/staff/?page=2"]='<button onclick="more()">Показать ещё</button>'
        with self.assertRaises(ValueError):
            revisit_pages(self.db,[11,999],staff_next=True,capture_factory=self.capture)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.db.execute("SELECT count(*) FROM crawl_run WHERE purpose='sample'").fetchone()[0],0)
        reports=revisit_pages(self.db,[11],staff_next=True,capture_factory=self.capture)
        self.assertEqual(len(reports),1)

    def test_default_first_page_does_not_revisit_an_unmarked_page_one_alias(self):
        root = "https://one.example/staff/"
        self.db.execute("UPDATE page SET url=? WHERE id=11",(root,))
        self.pages[root] = '<nav class="pagination"><a href="?page=1">1</a><a href="?page=2">2</a></nav>'
        reports = revisit_pages(self.db,[11],staff_next=True,capture_factory=self.capture)
        self.assertEqual([r["url"] for r in reports], [root,root+"?page=2"])


if __name__ == "__main__":
    unittest.main()
