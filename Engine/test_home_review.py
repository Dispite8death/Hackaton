import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from config import SCHEMA_SQL
import home_review
from home_review import review_home


class HomeReviewTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.executescript("""
            INSERT INTO organization(id,name,site_url,kind) VALUES(1,'School','https://school.test/','school');
            INSERT INTO crawl_run(id,id_organization,started_at,finished_at,purpose)
                VALUES(1,1,100,200,'sample'),(2,1,300,400,'audit');
            INSERT INTO page(id,url,id_organization,date_loaded,text_page) VALUES
                (11,'https://school.test/',1,101,'<main>sample</main>'),
                (12,'https://school.test/events/',1,110,'<main>listing</main>'),
                (13,'https://school.test/event-1',1,111,'<main>event</main>'),
                (21,'https://school.test/',1,301,'<main>other run</main>');
            INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,page_id) VALUES
                (1,'https://school.test/','home',0,101,11),(1,'https://school.test/list','events',1,110,12),
                (1,'https://school.test/event-1','events',2,111,13),(2,'https://school.test/','home',0,301,21);
            INSERT INTO home_main_photo VALUES
                (11,'found','https://school.test/img','building','{"source":"rendered_dom"}'),
                (21,'found','https://school.test/img','building','{}');
            INSERT INTO home_canteen_photo VALUES
                (11,'ambiguous','https://school.test/unavailable',NULL,'{"score":1}'),
                (21,'found','https://school.test/img',NULL,'{}');
            INSERT INTO home_school_description VALUES(11,'found','Школа',5,'{}');
            INSERT INTO home_event_block VALUES(11,'home','empty','{}'),(12,'listing','cards','{}'),
                (13,'detail','cards','{}'),(21,'home','not_found','{}');
            INSERT INTO home_event_link(page_id,ordinal,kind,url,title,evidence) VALUES
                (11,0,'catalog','https://school.test/list','Мероприятия','{"selector":"a"}'),
                (12,0,'event','https://school.test/event-1','Заголовок мероприятия','{"evidence":{"heuristics":["template_title"]}}'),
                (13,0,'event',NULL,'Мероприятие без ссылки','{}'),(21,0,'event',NULL,'Other run','{}');
            INSERT INTO school_event_quarter VALUES
                (1,'current','2026-10-01','2026-12-31','none_in_saved_pages',0,'{"source":"calendar_fields"}'),
                (1,'next','2027-01-01','2027-03-31','dates_unresolved',0,'{}');
        """)
        self.data = b"image bytes"
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.db.execute("INSERT INTO home_image_asset VALUES(?, 'image/jpeg', 100, 50, ?)", (self.sha, self.data))
        self.db.execute("""
            INSERT INTO home_image_download VALUES(11,'https://school.test/img',150,'https://school.test/new',
                                                   200,'saved',NULL,?)
        """, (self.sha,))
        self.db.execute("""
            INSERT INTO home_image_download VALUES(21,'https://school.test/img',350,'https://school.test/new',
                                                   200,'saved',NULL,?)
        """, (self.sha,))
        self.db.execute("""
            CREATE VIEW IF NOT EXISTS home_reused_asset AS
            SELECT sha256,2 AS distinct_uses,1 AS organizations,2 AS categories,
                   1 AS distinct_urls,'same_bytes_multiple_uses' AS status FROM home_image_asset
        """)
        self.db.execute("""
            INSERT INTO home_image_download VALUES(11,'https://school.test/unavailable',151,'https://school.test/unavailable',
                                                   404,'error','HTTP 404',NULL)
        """)
        self.db.execute("""
            INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
                                    details,rendered_html,screenshot_mime,screenshot,full_screenshot)
            VALUES(11,102,'https://school.test/','partial',900,600,?,'<main>DOM</main>','image/jpeg',?,?)
        """, (json.dumps({"peer_styles":{"status":"measured","groups":[]}, "full_screenshot_coverage":"size_capped",
                          "unnecessary_large_field":"discard_this"}), b"viewport", b"full"))
        self.db.commit()
        self.folder = Path(tempfile.gettempdir()) / f"home-review-test-{uuid.uuid4().hex}"
        self.folder.mkdir()
        self.addCleanup(shutil.rmtree, self.folder)

    def test_review_only_groups_same_run_distinguishes_event_from_navigation(self):
        r = review_home(self.db, 11)
        self.assertEqual([p["page_id"] for p in r["pages"]], [11, 12, 13])
        self.assertEqual(r["pages"][0]["event_links"][0]["target_page_ids_in_bundle"], [12])
        self.assertEqual(r["pages"][0]["event_links"][0]["event_truth"], "not_an_event_record")
        event = r["pages"][1]["event_links"][0]
        self.assertEqual(event["template_assessment"]["signals"], ["template_title"])
        self.assertEqual(event["event_truth"], "independent_evidence_required")
        self.assertEqual(r["quarters"]["current"]["evidence"]["source"], "calendar_fields")
        self.assertEqual(r["quarters"]["next"]["status"], "dates_unresolved")
        self.assertFalse(r["reused_image_urls"]["selected_run_in_scope"])
        self.assertEqual(r["reused_image_urls"]["matching_urls_from_this_bundle"][0]["distinct_uses"], 2)
        self.assertEqual(r["reused_image_bytes"]["matching_hashes_from_this_bundle"][0]["sha256"], self.sha)
        self.assertEqual(r["source"]["date_loaded"], 101)
        self.assertEqual(r["image_downloads"][0]["fetched_at"], 150)
        self.assertEqual(r["criteria"]["canteen_photo"]["archive"]["status"], "error")
        self.assertIsNone(r["criteria"]["main_photo"]["archive"]["file"])
        self.assertEqual(r["criteria"]["main_photo"]["original_dimension_check"]["result"], "different_dimensions")
        self.assertEqual(r["criteria"]["school_description"]["length_check"]["result"], "outside_range")
        self.assertNotIn("discard_this", json.dumps(r))
        with self.assertRaises(ValueError):
            review_home(self.db, 12)

    def test_criteria_uncertainty_ranges_and_optional_analysis_problem(self):
        self.db.execute("UPDATE home_image_asset SET width=1200,height=900")
        self.db.execute("UPDATE home_school_description SET char_count=300,text=? WHERE page_id=11", ("а"*300,))
        self.db.execute("INSERT INTO home_analysis_problem VALUES(12,155,'ошибка извлечения')")
        report = review_home(self.db, 11)
        self.assertEqual(report["pages"][1]["analysis_problem"]["attempted_at"], 155)
        self.assertEqual(report["criteria"]["school_description"]["length_check"]["result"], "within_range")
        self.assertEqual(report["criteria"]["main_photo"]["original_dimension_check"]["result"], "exact_match")
        self.db.execute("UPDATE home_main_photo SET status='ambiguous' WHERE page_id=11")
        self.db.execute("UPDATE home_school_description SET status='ambiguous' WHERE page_id=11")
        self.db.execute("DROP TABLE home_analysis_problem")
        report = review_home(self.db, 11)
        self.assertFalse(report["analysis_problem_table_available"])
        self.assertEqual(report["criteria"]["school_description"]["length_check"]["result"], "needs_review")
        check = report["criteria"]["main_photo"]["original_dimension_check"]
        self.assertEqual((check["width"],check["height"],check["result"]), (1200,900,"unknown"))

    def test_package_contains_original_html_dom_images_and_screenshots(self):
        folder = self.folder / "review"
        r = review_home(self.db, 11, folder)
        self.assertEqual(r, json.loads((folder / "review.json").read_text(encoding="utf-8")))
        self.assertEqual((folder / r["criteria"]["main_photo"]["archive"]["file"]).read_bytes(), self.data)
        self.assertEqual((folder / r["pages"][1]["response_html_file"]).read_text(), "<main>listing</main>")
        render = r["pages"][0]["render"]
        self.assertEqual((folder / render["rendered_html"]["file"]).read_text(), "<main>DOM</main>")
        self.assertEqual((folder / render["screenshot"]["file"]).read_bytes(), b"viewport")
        self.assertEqual((folder / render["full_screenshot"]["file"]).read_bytes(), b"full")
        self.assertEqual(render["peer_styles"]["status"], "measured")
        self.assertIsNone(r["pages"][1]["render"])
        with self.assertRaises(FileExistsError):
            review_home(self.db, 11, folder)
        self.assertEqual(list(self.folder.iterdir()), [folder])

    def test_failing_write_removes_only_own_staging_directory(self):
        with patch("home_review._write", side_effect=OSError("disk full")), self.assertRaises(OSError):
            review_home(self.db, 11, self.folder / "review")
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertFalse(self.db.in_transaction)

    def test_concurrently_created_empty_destination_is_preserved(self):
        folder = self.folder / "review"
        publish = home_review._publish

        def racing_writer(staging, destination):
            destination.mkdir()
            publish(staging, destination)

        with patch("home_review._publish", side_effect=racing_writer), self.assertRaises(FileExistsError):
            review_home(self.db, 11, folder)
        self.assertTrue(folder.is_dir())
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual(list(self.folder.iterdir()), [folder])


if __name__ == "__main__":
    unittest.main()
