import sqlite3
import unittest

from config import SCHEMA_SQL
from news_order import compare_news_pages


SITE="https://school.example"
FEED=SITE+"/news/"


class NewsPageOrderTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA_SQL)
        self.db.execute("INSERT INTO organization VALUES(1,'Школа',?,'school')",(SITE,))
        self.db.executescript("INSERT INTO crawl_run(id,id_organization) VALUES(1,1),(2,1)")

    def page(self,url,dates,*,run=1,section="news",final=None):
        pid=self.db.execute("INSERT INTO page(url,id_organization,text_page) VALUES(?,1,'') RETURNING id",
                            (final or url,)).fetchone()[0]
        fid=self.db.execute("""INSERT INTO crawl_fetch(run_id,url,final_url,section,depth,fetched_at,http_status,page_id)
                               VALUES(?,?,?,?,1,100,200,?) RETURNING id""",(run,url,final or url,section,pid)).fetchone()[0]
        self.db.execute("INSERT INTO news_page VALUES(?,'news_listing','response_html','partial',?,'{}')",(pid,len(dates)))
        for i,d in enumerate(dates):
            self.db.execute("INSERT INTO news_preview VALUES(?,?,?,'Новость',?,?,?,'{}')",
                            (pid,i,FEED+str(i),"valid" if d else "multiple_dates",d,d))
        return fid,pid

    def link(self,fid,url,label,section="news",rel="pagination"):
        self.db.execute("INSERT INTO crawl_link VALUES(?,?,?,?,?,?)",(fid,url,label,section,rel,int(rel=="pagination")))

    def test_adjacent_offsets_include_ties_and_real_inversion_but_not_jump_or_previous(self):
        second=FEED+"?cur_cc=9&curPos=2"
        third=FEED+"?cur_cc=9&curPos=4"
        a,ap=self.page(FEED,["2026-11-08","2026-11-07"])
        b,bp=self.page(second,["2026-11-07","2026-11-05"])
        _,cp=self.page(third,["2026-11-06"])
        self.link(a,second,"2")
        self.link(a,third,"3")
        self.link(b,FEED,"1")
        self.link(b,third,"3")
        r=compare_news_pages(self.db,1)
        self.assertEqual(r["status"],"out_of_order")
        self.assertEqual([(v["source_page_id"],v["target_page_id"],v["status"]) for v in r["pairs"]],
                         [(ap,bp,"newest_first"),(bp,cp,"out_of_order")])
        self.assertEqual((r["pairs"][1]["previous"]["ordinal"],r["pairs"][1]["current"]["ordinal"]),(1,0))
        self.assertEqual({v["reason"] for v in r["ignored_links"]},{"nonadjacent_position","backward_or_self"})

    def test_other_run_filters_sections_and_host_cannot_supply_a_boundary(self):
        a,_=self.page(FEED,["2026-10-10"])
        self.page(FEED+"?page=2",["2026-11-01"],run=2)
        self.link(a,FEED+"?page=2","2")
        self.link(a,FEED+"?page=3","3")
        self.link(a,FEED+"?page=2&category=9","2")
        self.link(a,SITE+"/events/?page=2","2","events")
        self.link(a,"https://other.example/news/?page=2","2")
        self.link(a,FEED,"Показать ещё",rel="pagination_unresolved")
        r=compare_news_pages(self.db,1)
        self.assertEqual(r["status"],"uncertain")
        self.assertEqual({p["reason"] for p in r["pairs"]},{"target_unvisited","unresolved_control"})
        self.assertEqual({p["reason"] for p in r["ignored_links"]},
                         {"nonadjacent_position","different_feed_or_filter","different_section","different_site"})

    def test_ajax_fragment_uses_exact_boundary_even_when_another_card_has_a_valid_date(self):
        a,_=self.page(FEED,["2026-11-08",None])
        target=FEED+"news.html?cur_cc=545&curPos=2&isNaked=1"
        self.page(target,[None,"2026-10-01"])
        self.link(a,target,"Показать ещё")
        p,=compare_news_pages(self.db,1)["pairs"]
        self.assertEqual(p["reason"],"boundary_date_unresolved")
        self.assertEqual((p["previous"]["date_status"],p["current"]["date_status"]),("multiple_dates","multiple_dates"))

    def test_redirect_back_to_first_and_missing_row_are_uncertain(self):
        a,_=self.page(FEED,["2026-11-08"])
        self.page(FEED+"?page=2",["2026-09-01"],final=FEED)
        self.link(a,FEED+"?page=2","2")
        p,=compare_news_pages(self.db,1)["pairs"]
        self.assertEqual(p["reason"],"target_redirect_position_unknown")
        self.assertEqual(compare_news_pages(self.db,2)["status"],"no_linked_pages")
        with self.assertRaises(ValueError):
            compare_news_pages(self.db,300)


if __name__=="__main__":
    unittest.main()
