import json
import sqlite3
import unittest
from unittest.mock import patch

from backfill_materials import capture, queue_failed_document_cards, seed, target_priority
from backfill_pagers import repeating_pager
from browser_capture import RenderEvidence
from config import SCHEMA_SQL
from http_page import PageResponse
from navigation import Link


class MaterialBackfillTest(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory=sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.org=(1,"https://school.example/","school")
        self.site=self.org[1]
        self.conn.executescript("""
            INSERT INTO organization VALUES(1,'School','https://school.example/','school');
            INSERT INTO crawl_run(id,id_organization,state,purpose) VALUES(1,1,'partial','audit'),(2,1,'running','sample');
            INSERT INTO page VALUES(1,'https://school.example/about/',1,100,'<p>Сохранено</p>');
            INSERT INTO crawl_fetch(id,run_id,url,final_url,section,depth,fetched_at,http_status,page_id)
               VALUES(1,1,'https://school.example/school/','https://school.example/about/','about',1,100,200,1);
        """)

    def link(self,path,section="documents",relation="pagination"):
        return Link(self.site+path,"Следующая",section,relation,True)

    def test_scope_uses_published_profiles_files_and_unfiltered_lists(self):
        profile=self.link("people/ivanova","staff","detail")
        wrapper=self.link("documents/charter.html","documents","detail")
        source="staff"
        args=(self.site,source,{profile.url},{wrapper.url},"school")
        self.assertEqual(target_priority(profile,*args),3)
        self.assertEqual(target_priority(self.link("docs/?curPos=20"),*args),1)
        for link in (wrapper,self.link("documents/other.pdf"),self.link("staff/holiday-news","staff","detail"),
                     self.link("news/?filter%5B2%5D=67","history","section"),
                     Link("https://elsewhere.example/","another","information","section",True)):
            with self.subTest(link=link):
                self.assertIsNone(target_priority(link,*args))

    def test_capture_keeps_pdf_parent_and_next_page_then_seed_is_idempotent(self):
        first=self.link("documents/?curPos=20")
        for link in (first,self.link("school/","about","section"),self.link("about/","about","section")):
            self.conn.execute("INSERT INTO crawl_link VALUES(1,?,?,?,?,?)",(link.url,link.label,link.section,link.relation,1))
        seed(self.conn,[self.org])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM material_backfill").fetchone()[0],1)
        with self.conn:
            self.conn.execute("UPDATE material_backfill SET run_id=2,state='working'")
        html='<main><nav class="pagination"><a href="?curPos=40">3</a></nav></main>'
        render=RenderEvidence(101,first.url,800,500,html,b"window",b"full",b"%PDF",True,
                              {"requested_artifacts":["dom","screenshot","full_screenshot","pdf"]})
        with patch("backfill_materials.BrowserCapture") as browser:
            browser.fetch.return_value=PageResponse(first.url,200,html,loaded_at=101,render=render)
            self.assertEqual(capture(self.conn,browser,2,self.org,(first.url,"documents",2),set(),set(),set(),1),(200,2))
        seed(self.conn,[self.org])
        items=self.conn.execute("SELECT state,depth,source_page_id FROM material_backfill ORDER BY depth").fetchall()
        self.assertEqual(list(map(tuple,items)),[("saved",2,1),("pending",3,2)])
        self.assertEqual(self.conn.execute("SELECT pdf,full_screenshot FROM page_render WHERE page_id=2").fetchone()[:],
                         (b"%PDF",b"full"))

    def test_expired_worker_with_committed_fetch_does_not_download_again(self):
        with self.conn:
            self.conn.execute("""INSERT INTO material_backfill(organization_id,url,source_page_id,section,relation,
                depth,priority,state,run_id,lease_until) VALUES(1,?,1,'documents','detail',2,3,'working',2,0)""",(self.site+"gone",))
            self.conn.execute("""INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,http_status,error)
                VALUES(2,?,'documents',2,101,404,'HTTP 404')""",(self.site+"gone",))
        seed(self.conn,[self.org])
        row=self.conn.execute("SELECT state,fetch_id,error FROM material_backfill").fetchone()
        self.assertEqual(row[:],("failed",2,"HTTP 404"))

    def test_three_identical_empty_offsets_stop_only_that_pager(self):
        for i in range(2,5):
            url=self.site+f"team/?offset={10*i}"
            self.conn.execute("INSERT INTO page VALUES(?,?,1,100,?)",(i,url,"<main>Список пока пуст</main>"))
            self.conn.execute("""INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,page_id)
                VALUES(?,2,?,'team',?,100,?)""",(i,url,i,i))
            self.conn.execute("""INSERT INTO material_backfill(organization_id,url,source_page_id,section,relation,
                depth,priority,state,run_id,fetch_id) VALUES(1,?,?,'team','pagination',?,1,'saved',2,?)""",
                (url,i-1,i,i))
            self.conn.execute("""INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
                details,rendered_html) VALUES(?,100,?,'complete',800,600,'{}','<main>Список пока пуст</main>')""",(i,url))
        self.conn.commit()
        self.assertFalse(repeating_pager(self.conn,3))
        self.conn.execute("UPDATE page_render SET rendered_html='<main>Другой текст</main>' WHERE page_id=4")
        self.assertFalse(repeating_pager(self.conn,4))
        self.conn.execute("UPDATE page_render SET rendered_html='<main>Список пока пуст</main>' WHERE page_id=4")
        self.assertTrue(repeating_pager(self.conn,4))
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM material_pager_stop").fetchone()),
                         (4,3,2,"three_empty_offsets"))

    def test_missing_file_queues_its_html_card_once_and_keeps_the_failed_original(self):
        file_url=self.site+"files/charter.pdf"
        card_url=self.site+"documents/charter.html"
        good_url=self.site+"files/plan.pdf"
        self.conn.execute("INSERT INTO institution_document_page VALUES(1,'documents',1,'response_html',?)",
            (json.dumps({"documents":[{"title":"Устав","url":file_url,"detail_url":card_url},
                                      {"title":"План","url":good_url,"detail_url":self.site+"documents/plan.html"}]}),))
        self.conn.execute("""INSERT INTO page_attachment(page_id,url,title,role,format_hint,state,http_status,error)
            VALUES(1,?,'Устав','catalog','pdf','error',404,'404'),(1,?,'План','catalog','pdf','pending',NULL,NULL)""",
            (file_url,good_url))
        self.conn.commit()
        self.assertEqual(queue_failed_document_cards(self.conn),1)
        self.assertEqual(queue_failed_document_cards(self.conn),0)
        self.assertEqual(tuple(self.conn.execute("SELECT url,source_page_id,relation FROM material_backfill").fetchone()),
                         (card_url,1,"document_fallback"))
        self.conn.execute("UPDATE material_backfill SET state='failed'")
        self.conn.commit()
        self.assertEqual(queue_failed_document_cards(self.conn),0)
        self.assertEqual(self.conn.execute("SELECT state FROM page_attachment WHERE url=?",(file_url,)).fetchone()[0],"error")
