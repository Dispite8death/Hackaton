import json
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from browser_capture import RenderEvidence
from crawl_store import initialize_db, start_run, record_fetch, finish_run
from page_review import export_pages
from review_contract import validate_response


class PageReviewTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        initialize_db(self.conn)
        self.conn.execute("INSERT INTO organization VALUES(1,'Сад','https://garden.test','kindergarten')")
        self.run = start_run(self.conn, 1, purpose="sample")
        parent = Path(tempfile.gettempdir()) / f"page-review-test-{uuid4().hex}"
        parent.mkdir()
        self.addCleanup(shutil.rmtree, parent)
        self.out = parent / "review"

    def test_review_is_page_scoped_and_evidence_is_exported(self):
        root = "https://garden.test/"
        doc = "<main><h1>Главная</h1><a href='/news/'>Все новости</a></main>"
        record_fetch(self.conn, self.run, root, "home", 0, status=200, html=doc)
        article = "<main><h1>Праздник</h1><p>Рассказ о празднике</p></main>"
        render = RenderEvidence(300, root+"news/story.html", 1280, 800, article,
                                b"screen", b"full", b"%PDF-example", True, {})
        record_fetch(self.conn, self.run, render.url, "news", 2, status=200, html=article, render=render)
        record_fetch(self.conn, self.run, root+"missing", "news", 2, status=404, error="HTTP 404")
        finish_run(self.conn, self.run, "partial")
        data = export_pages(self.conn, None, self.out, news_run=self.run)
        self.assertEqual(len(data["pages"]), 2)
        self.assertEqual(data["news_run"]["attempts"][-1]["http_status"], 404)
        for page in data["pages"]:
            req = json.loads((self.out / page["request_file"]).read_text(encoding="utf-8"))
            self.assertEqual(req["identity"]["page_id"], page["page_id"])
            self.assertTrue((self.out / page["schema_file"]).exists())
            for m in req["materials"]:
                self.assertTrue((self.out / m["path"]).is_file())
            if page["section"] == "news":
                self.assertEqual({m["kind"] for m in req["materials"]}, {"html","dom","viewport","full","pdf"})
                self.assertEqual((self.out / page["render"]["pdf"]["file"]).read_bytes(), render.pdf)
            else:
                self.assertEqual([m["kind"] for m in req["materials"]], ["html"])
                answer = {"identity":req["identity"], "results":[
                    {"check_id":c["id"], "outcome":"uncertain", "material_ids":[], "evidence":"Материала недостаточно"}
                    for c in req["checks"]
                ]}
                validate_response(req, answer)
                screen = next(r for r in answer["results"] if r["check_id"] == "screen_layout")
                screen.update(outcome="present",material_ids=[req["materials"][0]["id"]])
                with self.assertRaises(ValueError):
                    validate_response(req, answer)
        with self.assertRaises(FileExistsError):
            export_pages(self.conn, None, self.out, news_run=self.run)

    def test_failed_news_run_still_has_a_manifest(self):
        record_fetch(self.conn, self.run, "https://garden.test/", "home", 0, status=503, error="HTTP 503")
        finish_run(self.conn, self.run, "failed")
        result = export_pages(self.conn, [], self.out, news_run=self.run)
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["news_run"]["attempts"][0]["http_status"], 503)
