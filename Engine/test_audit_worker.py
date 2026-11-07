import os
import sqlite3
import time
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import audit_worker as worker
from backfill_materials import known_urls, seed
from config import SCHEMA_SQL
from loadPage import Result


class AuditWorkerTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory=sqlite3.Row
        self.db.executescript(SCHEMA_SQL)
        self.db.executescript("""
            INSERT INTO organization VALUES(1,'Old','https://old.example/','school'),
                       (2,'Recent','https://recent.example/','kindergarten');
            INSERT INTO crawl_run(id,id_organization,started_at,finished_at,state,purpose)
                VALUES(1,1,100,200,'partial','audit'),(2,2,unixepoch(),unixepoch(),'completed','audit');
        """)
        self.off=dict(organizations_per_tick=0,frontier_pages=0,attachment_files=0,model_checks=0,factcheck_items=0)

    def test_stale_organizations_are_chosen_without_ids_and_files_share_one_tick_budget(self):
        with patch("loadPage.crawl_organization",return_value=Result(18,5,0,"partial","page_limit")) as crawl:
            status=worker.run_once(self.db,overrides={**self.off,"organizations_per_tick":2})
        self.assertEqual(status["state"],"completed")
        self.assertEqual([x["organization_id"] for x in status["stages"]["crawl"]["visits"]],[1])
        self.assertIs(crawl.call_args.kwargs["db"],self.db)
        self.assertIs(crawl.call_args.kwargs["archive_files"],False)
        self.assertEqual(worker.last_status(self.db),status)
        self.assertEqual(worker.get_settings(self.db)["frontier_pages"],6)

    def test_busy_stage_failure_does_not_stop_models_or_factcheck(self):
        def unavailable(conn,settings):
            self.assertEqual(worker.run_once(conn,overrides=self.off)["state"],"busy")
            raise OSError("server offline")
        with patch.object(worker,"_frontier",side_effect=unavailable),patch("local_model_service.prepare_local_service",
                return_value={"ready":True,"status":"ready","detail":""}),patch("model_batch.batch_assess",
                return_value={"processed":1,"errors":0}) as model,patch("factcheck_batch.batch_check") as fact:
            report=worker.run_once(self.db,overrides={**self.off,"frontier_pages":2,"model_checks":1,"factcheck_items":1})
        self.assertEqual(report["state"],"partial")
        self.assertIn("server offline",report["stages"]["frontier"]["error"])
        model.assert_called_once_with(self.db,limit=1,include_photos=True,rotate=True,latest_only=True)
        fact.assert_not_called()
        self.assertFalse(report["stages"]["factcheck"]["enabled"])

    def test_model_wait_leaves_questions_available_and_retention_requires_explicit_policy(self):
        with patch("local_model_service.prepare_local_service",
                return_value={"ready":False,"status":"waiting","detail":"Модель ещё загружается."}), \
                patch("model_batch.batch_assess") as assess,patch("retention.maintain") as cleanup:
            status=worker.run_once(self.db,overrides={**self.off,"model_checks":1})
        self.assertEqual(status["state"],"partial")
        self.assertEqual(status["stages"]["models"]["service"]["status"],"waiting")
        self.assertEqual(status["stages"]["retention"]["state"],"disabled")
        assess.assert_not_called()
        cleanup.assert_not_called()

    def test_cli_explicit_db_supersedes_already_imported_default(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/"selected.sqlite3"
            with patch.dict(os.environ),redirect_stdout(StringIO()):
                self.assertEqual(worker.main(["--db",str(path),"--status"]),0)
            with closing(sqlite3.connect(path)) as db:
                self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='audit_worker_cycle'").fetchone())
                with worker._cycle_lock(db) as acquired,closing(sqlite3.connect(path)) as second:
                    self.assertTrue(acquired)
                    with worker._cycle_lock(second) as duplicate:
                        self.assertFalse(duplicate)
                with worker._cycle_lock(db) as released:
                    self.assertTrue(released)

    def test_settings_persist_and_interrupted_cycle_retains_progress(self):
        with self.assertRaises(ValueError):
            worker.save_settings(self.db,{"model_checks":-1})
        worker.save_settings(self.db,{"model_checks":12})
        self.assertEqual(worker.get_settings(self.db)["model_checks"],12)
        self.db.execute("INSERT INTO audit_worker_cycle(state,stage,settings,stages) VALUES('running','models','{}','{}')")
        self.db.commit()
        worker.run_once(self.db,overrides=self.off)
        self.assertEqual(self.db.execute("SELECT state FROM audit_worker_cycle WHERE id=1").fetchone()[0],"interrupted")

    def test_fresh_link_reopens_old_page_without_losing_previous_parent(self):
        now=int(time.time())
        self.db.executescript("""
            INSERT INTO page VALUES (10,'https://old.example/',1,100,'old home'),
                                    (11,'https://old.example/schedule/',1,101,'old schedule');
            INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,http_status,page_id)
                VALUES(10,1,'https://old.example/','home',0,100,200,10),(11,1,'https://old.example/schedule/','schedule',1,101,200,11);
            INSERT INTO material_backfill(organization_id,url,source_page_id,section,relation,depth,priority,state,run_id,fetch_id)
                VALUES(1,'https://old.example/schedule/',10,'schedule','section',1,0,'saved',1,11);
            INSERT INTO crawl_run(id,id_organization,purpose) VALUES(3,1,'audit');
            INSERT INTO page VALUES(12,'https://old.example/',1,unixepoch(),'<p>current</p>');
            INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,http_status,page_id)
                VALUES(12,3,'https://old.example/','home',0,unixepoch(),200,12);
            INSERT INTO crawl_link VALUES(12,'https://old.example/schedule/','Расписание','schedule','section',1);
        """)
        seed(self.db,[(1,"https://old.example/","school")],since=now-3600)
        self.assertEqual(tuple(self.db.execute("SELECT state,source_page_id,fetch_id FROM material_backfill").fetchone()),
                         ("pending",12,None))
        self.assertNotIn("https://old.example/schedule/",known_urls(self.db,1))
        self.assertEqual(tuple(self.db.execute("SELECT fetch_id,source_page_id FROM material_backfill_visit").fetchone()),(11,10))

    def test_transient_frontier_errors_retry_with_backoff_but_404_remains_recorded(self):
        self.db.executescript("""
            INSERT INTO page VALUES(10,'https://old.example/',1,100,'old home');
            INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,http_status,error)
                VALUES(11,1,'https://old.example/a','staff',1,100,404,'missing'),(12,1,'https://old.example/b','staff',1,100,503,'busy'),
                    (13,1,'https://old.example/c','staff',1,unixepoch(),500,'busy');
            INSERT INTO material_backfill(organization_id,url,source_page_id,section,relation,depth,priority,state,run_id,fetch_id)
                SELECT 1,url,10,section,'detail',depth,3,'failed',run_id,id FROM crawl_fetch;
        """)
        with patch("backfill_materials.process_org",return_value=1):
            result=worker._frontier(self.db,{**worker.DEFAULTS,"frontier_pages":1})
        self.assertEqual(result["frontier"],{"failed":2,"pending":1})
        self.assertEqual(self.db.execute("SELECT url FROM material_backfill WHERE state='pending'").fetchone()[0],
                         "https://old.example/b")
