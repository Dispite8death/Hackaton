import json
import sqlite3
import unittest
from datetime import date

from config import SCHEMA_SQL
from navigation import Link
from news_window import bounds, filter_publication_links, skip_queued_url


class NewsWindowTest(unittest.TestCase):
    day=date(2026,11,8)
    site="https://school.example/"
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.execute("INSERT INTO organization VALUES(1,'School',?,'school')",(self.site,))
        self.db.execute("INSERT INTO crawl_run(id,id_organization) VALUES(1,1)")
        self.links=[]

    def listing(self, days, *, order="newest_first", pinned=False):
        html='<main><article class="pinned-post"><a href="/old">Закреплено</a></article></main>' if pinned else "<main>Новости</main>"
        url=self.site+"news/"
        self.db.execute("INSERT INTO page VALUES(1,?,1,1,?)",(url,html))
        self.db.execute("INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,page_id) VALUES(1,1,?,'news',1,1,1)",(url,))
        self.db.execute("INSERT INTO news_page VALUES(1,'news_listing','response_html',?,?,'{}')",(order,len(days)))
        for i,iso in enumerate(days):
            target=self.site+f"news/{i}.html"
            evidence={"date_evidence":{"sources":[{"role":"date_field","text":iso}]}} if iso else {}
            self.db.execute("INSERT INTO news_preview VALUES(1,?,?,? ,?,?,?,?)",
                (i,target,str(i),"valid" if iso else "multiple_dates",iso,iso,json.dumps(evidence)))
            self.links.append(Link(target,str(i),"news","detail",True))
        self.pager=Link(url+"?cur_cc=1&curPos=9","2","news","pagination",True)
        self.db.execute("INSERT INTO crawl_link VALUES(1,?,'2','news','pagination',1)",(self.pager.url,))
        self.links.append(self.pager)
        self.db.commit()

    def available(self, months=12):
        return filter_publication_links(self.db,source_page_id=1,links=self.links,organization_id=1,
                                        months=months,as_of=self.day)

    def test_inclusive_dates_and_leap_months(self):
        self.assertEqual(bounds(12,self.day),("2025-11-08","2026-11-08"))
        self.assertEqual(bounds(12,date(2024,2,29))[0],"2023-02-28")
        self.listing(["2026-11-08","2025-11-08","2025-11-07"])
        self.assertEqual(self.available(),self.links[:2])
        self.assertEqual(self.db.execute("SELECT boundary_url FROM news_window_page").fetchone()[0],self.links[2].url)
        self.assertFalse(skip_queued_url(self.db,1,self.links[2].url,"events",as_of=self.day))
        self.assertTrue(skip_queued_url(self.db,1,self.pager.url,"news",as_of=self.day))
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.available(24),self.links)

    def test_ambiguous_preview_keeps_article_and_continuation(self):
        self.listing(["2026-04-01",None,"2025-04-01"],order="partial")
        self.assertEqual(self.available(),[self.links[i] for i in (0,1,3)])
        self.assertEqual(self.db.execute("SELECT reason FROM news_window_date WHERE ordinal=1").fetchone()[0],
                         "preview_multiple_dates")

    def test_pinned_list_does_not_close_pagination_and_another_component_is_independent(self):
        self.listing(["2025-04-02","2025-04-01"],pinned=True)
        self.assertEqual(self.available(),[self.pager])
        self.assertFalse(skip_queued_url(self.db,1,self.pager.url.replace("cur_cc=1","cur_cc=77"),"news",as_of=self.day))
