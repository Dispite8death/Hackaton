import json
import sqlite3
import unittest
from unittest.mock import patch

from crawl_store import finish_run, initialize_db, record_fetch, start_run
from home_store import analyze_saved_page
from home_terms import add_term
from browser_capture import RenderEvidence


class HomePipelineTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        initialize_db(self.db)
        with self.db:
            self.db.execute("INSERT INTO organization VALUES(1,'Школа','https://s.example/','school')")
            self.db.execute("INSERT INTO organization VALUES(2,'Сад','https://k.example/','kindergarten')")

    def save(self, run, html, render=None):
        fetch = record_fetch(self.db,run,"https://s.example/","home",0,status=200,html=html,render=render)
        return self.db.execute("SELECT page_id FROM crawl_fetch WHERE id=?", (fetch,)).fetchone()[0]

    def test_normal_entrypoint_and_reread_after_term_edit(self):
        html = "<main><figure><img src='/room.jpg'><figcaption>Трапезная</figcaption></figure></main>"
        run = start_run(self.db, 1)
        pid = self.save(run, html)
        self.assertEqual(self.db.execute("SELECT status FROM home_canteen_photo").fetchone()[0], "not_found")
        with self.db:
            add_term(self.db, "canteen_photo", "трапезная")
            for _ in range(2):
                self.assertTrue(analyze_saved_page(self.db, pid))
        self.assertEqual(self.db.execute("SELECT status,image_url FROM home_canteen_photo").fetchall(),
                         [("found","https://s.example/room.jpg")])
        finish_run(self.db,run,"completed")
        self.assertEqual(self.db.execute("SELECT count(*) FROM school_event_quarter").fetchone()[0], 2)
        self.save(start_run(self.db, 2), html)
        self.assertEqual(self.db.execute("SELECT count(*) FROM home_school_description").fetchone()[0], 1)

    def test_raw_survives_extraction_failure_and_retry_clears_error(self):
        html = "<main><p>Наша школа открыта в 1991 году.</p></main>"
        with patch("home_content.extract_home_content", side_effect=ValueError("invalid markup")):
            pid = self.save(start_run(self.db, 1), html)
        self.assertEqual(self.db.execute("SELECT text_page FROM page WHERE id=?", (pid,)).fetchone()[0], html)
        self.assertIn("invalid markup",self.db.execute("SELECT error FROM home_analysis_problem").fetchone()[0])
        with self.db:
            self.assertTrue(analyze_saved_page(self.db, pid))
        self.assertEqual(self.db.execute("SELECT count(*) FROM home_analysis_problem").fetchone()[0], 0)

    def test_incomplete_browser_document_uses_response_html(self):
        raw = "<main><p>Наша школа открылась в 1989 году и находится в районном центре.</p></main>"
        render = RenderEvidence(120,"https://s.example/",1280,800,"<html></html>",None,None,None,None,
                                {"document_complete_for_extraction":False})
        self.save(start_run(self.db, 1),raw,render)
        text,evidence = self.db.execute("SELECT text,evidence FROM home_school_description").fetchone()
        self.assertIn("1989", text)
        self.assertEqual(json.loads(evidence)["source"],"response_html")

