import json
import sqlite3
import unittest

from config import SCHEMA_SQL
from timeline_audit import TIMELINE_SCHEMA_SQL, refresh_timeline
from timeline_dates import article_sources, dated_source, latency


SITE="https://school.example"


class DateRulesTest(unittest.TestCase):
    def test_day_precision_midnight_boundaries_and_timezone(self):
        def check(pub,event):
            return latency([dated_source(pub,"publication")],[dated_source(event,"event")])[0]
        self.assertEqual(check("04 ноября 2026","04 ноября 2026"),"within_24h")
        self.assertEqual(check("05 ноября 2026","04 ноября 2026"),"uncertain")
        self.assertEqual(check("06 ноября 2026","04 ноября 2026"),"over_24h")
        self.assertEqual(check("03 ноября 2026","04 ноября 2026"),"published_before_event")
        self.assertEqual(check("2026-11-05T00:00:00Z","04 ноября 2026 в 11:00"),"within_24h")
        self.assertEqual(check("2026-11-05T00:01:00Z","04 ноября 2026 в 11:00"),"over_24h")
        self.assertEqual(check("05 ноября 2026","04 ноября"),"uncertain")

    def test_article_only_occurrence_dates_and_preview_disagreement(self):
        f=article_sources("""<main><h1>Праздник</h1><p class="gw-content-small-title">5 ноября 2026 г.</p>
           <article><p>4 ноября 2026 года состоялся конкурс школьных театров.</p>
           <p>К 23 февраля 2026 года ребята приготовили открытки.</p>
           <p>10 декабря 2026 года пройдёт финал.</p></article>
           <aside>1 ноября 2026 года прошла выставка.</aside></main>""",101)
        self.assertEqual(len(f["event"]),1)
        self.assertEqual(f["event"][0]["bounds"][0][:10],"2026-11-04")
        self.assertEqual(latency(f["published"]+[dated_source("2 ноября 2026","preview")],f["event"])[1]["publication_resolution"],
                         "conflicting_dates")
        future=article_sources("<article><p>4 ноября 2026 года встреча не состоялась.</p></article>",77)
        self.assertEqual(future["event"],[])
        self.assertEqual(future["reports"],[])

    def test_lower_bound_for_an_unstated_year_does_not_assign_it(self):
        fields=[dated_source("23 октября","reported_event_sentence")]
        status,evidence=latency([dated_source("26 октября 2026","published")],fields)
        self.assertEqual(status,"over_24h")
        self.assertEqual(evidence["reason"],"over_24h_for_every_possible_past_year")
        self.assertIsNone(fields[0]["bounds"])
        self.assertEqual(latency([dated_source("24 октября 2026","published")],fields)[0],"uncertain")
        self.assertEqual(latency([dated_source("25 октября 2026","published")],fields+
                                 [dated_source("14 сентября","reported_event_sentence")])[0],"uncertain")


class TimelineArchiveTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA_SQL + TIMELINE_SCHEMA_SQL)
        self.db.execute("INSERT INTO organization VALUES(1,'Школа',?,'school')",(SITE,))
        self.db.executemany("INSERT INTO crawl_run(id,id_organization,started_at,state) VALUES(?,1,1800000000,'completed')",
                            [(1,),(2,)])

    def page(self,run,url,kind,body="",cards=()):
        pid=self.db.execute("INSERT INTO page(url,id_organization,text_page) VALUES(?,1,?) RETURNING id",(url,body)).fetchone()[0]
        fid=self.db.execute("""INSERT INTO crawl_fetch(run_id,url,final_url,section,depth,fetched_at,page_id)
                             VALUES(?,?,?,'news',1,42,?) RETURNING id""",(run,url,url,pid)).fetchone()[0]
        if kind:
            self.db.execute("INSERT INTO news_page VALUES(?,?, 'response_html','newest_first',?,'{}')",(pid,kind,len(cards)))
        for i,(path,iso,status) in enumerate(cards):
            evidence=json.dumps({"date_evidence":{"sources":[{"role":"date_field","text":iso}]}})
            self.db.execute("INSERT INTO news_preview VALUES(?,?,?,?,?,?,?,?)",
                            (pid,i,SITE+path,path,status,iso,iso if status=="valid" else None,evidence))
        return pid,fid

    def test_verified_pairs_14d_missing_dates_incomplete_capture(self):
        url=SITE+"/news"
        one,fid=self.page(1,url,"news_listing",cards=[("/a","2026-07-01","valid"),("/b","2026-06-17","valid")])
        two,_=self.page(1,url+"?page=2","news_listing",cards=[("/c","2026-06-02","valid"),("/d","02 июня","incomplete_date")])
        for suffix,label in (("?page=2","2"),("?page=3","3")):
            self.db.execute("INSERT INTO crawl_link VALUES(?,?,?,'news','pagination',1)",(fid,url+suffix,label))
        self.page(2,url+"?page=3","news_listing",cards=[("/outside","2026-06-10","valid")])
        self.page(1,SITE+"/","home",cards=[("/featured","2026-06-11","valid")])
        self.db.execute("UPDATE crawl_run SET state='partial',stop_reason='page_limit' WHERE id=1")
        result=refresh_timeline(self.db,1)
        self.assertEqual(result["gaps"],{"within_14d":1,"over_14d":1,"uncertain":1})
        self.assertEqual(result["run_state"],"partial")
        self.assertEqual(self.db.execute("SELECT page_id,next_page_id,days FROM timeline_gap WHERE status='over_14d'").fetchone(),
                         (one,two,15))
        self.assertEqual(refresh_timeline(self.db,1),result)
        self.assertEqual(self.db.execute("SELECT count(*) FROM timeline_gap").fetchone()[0],3)

    def test_pinned_duplicate_and_known_intermediate_never_become_global_gaps(self):
        self.page(1,SITE+"/news","news_listing",'<a class="pinned" href="/a">Закреплено</a>',[
            ("/a","2026-10-01","valid"),("/b","2026-09-01","valid"),("/b","2026-09-01","valid"),
            ("/c","2026-08-01","valid"),("/old","2026-08-20","valid")])
        refresh_timeline(self.db,1)
        rows=[(status,json.loads(raw)["reason"]) for status,raw in self.db.execute("SELECT status,evidence FROM timeline_gap ORDER BY ordinal")]
        self.assertEqual(rows,[("uncertain","pinned_preview"),("uncertain","same_publication_repeated"),
                               ("uncertain","other_dated_publication_between_pair"),("uncertain","inverted_publication_order")])

    def test_publication_and_event_matched_with_local_provenance(self):
        title="Олимпиада северных театров"
        url=SITE+"/news/result.html"
        pid,_=self.page(1,url,"news_detail",f"""<article><h1>{title}</h1><time itemprop="datePublished"
           datetime="2026-09-06T15:00:00+11:00"></time><p>4 сентября 2026 года состоялась олимпиада северных театров.</p></article>""")
        ev,_=self.page(1,SITE+"/events",None)
        self.db.execute("INSERT INTO home_event_block VALUES(?,'listing','cards','{}')",(ev,))
        self.db.execute("""INSERT INTO home_event_link(page_id,ordinal,kind,url,title,date_start,date_end,evidence)
                         VALUES(?,0,'event',?,?, '2026-09-04','2026-09-04','{}')""",(ev,SITE+"/olimp",title))
        refresh_timeline(self.db,1)
        row=self.db.execute("SELECT page_id,status,event_start,published_start FROM timeline_publication").fetchone()
        self.assertEqual((row[0],row[1],row[2][:10],row[3][:10]),(pid,"over_24h","2026-09-04","2026-09-06"))
        status,raw=self.db.execute("SELECT status,evidence FROM timeline_event_news").fetchone()
        self.assertEqual(status,"reported_on_site")
        self.assertEqual(json.loads(raw)["news_candidates"][0]["page_id"],pid)

    def test_date_in_only_the_teaser_is_not_a_publication_date(self):
        pid,_=self.page(1,SITE+"/news","news_listing",cards=[
            ("/a","2026-12-01","valid"),("/b","2026-01-01","valid")])
        self.db.execute("UPDATE news_preview SET evidence='{}' WHERE page_id=? AND ordinal=0",(pid,))
        result=refresh_timeline(self.db,1)
        self.assertEqual(result["gaps"],{"uncertain":1})
        raw=self.db.execute("SELECT evidence FROM timeline_gap").fetchone()[0]
        self.assertEqual(json.loads(raw)["reason"],"publication_date_not_established")


if __name__=="__main__":
    unittest.main()
