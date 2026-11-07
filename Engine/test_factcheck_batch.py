import json
import sqlite3
import sys
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

import factcheck
import factcheck_batch as queue
import loadPage
from config import SCHEMA_SQL
from factcheck_sources import add_allowed_source, allowlist_fingerprint, remove_allowed_source
from model_settings import set_setting


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.uri = f"file:factcheck-{uuid4().hex}?mode=memory&cache=shared"
        self.db = self.enterContext(closing(self.connect()))
        self.db.executescript(SCHEMA_SQL)
        with self.db:
            add_allowed_source(self.db, "https://journal-example.ru/")
            self.db.execute("INSERT INTO organization VALUES(1,'Школа №7 Магадана','https://school.example','school')")
            self.db.executemany("INSERT INTO crawl_run(id,id_organization,state) VALUES(?,1,'complete')",
                                [(1,), (2,), (3,)])
            for page_id, role, run_id in [(1, "news_detail", 1), (2, "event_detail", 1), (3, "news_listing", 1),
                                          (4, "news_detail", 2), (5, "news_detail", 3)]:
                # IDs 1, 4 and 5 are distinct visits to the same news URL.
                url = "https://school.example/" + role
                self.db.execute("INSERT INTO page VALUES(?,?,1,0,'<article>Публикация</article>')", (page_id, url))
                self.db.execute("INSERT INTO page_passport VALUES(?,?,'Городской фестиваль','[]','','[]')",
                                (page_id, role))
                self.db.execute("""INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,http_status,page_id)
                                   VALUES(?,?,'news',1,0,200,?)""", (run_id, url, page_id))

    def connect(self):
        conn = sqlite3.connect(self.uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def empty_search(self, url, *_):
        self.assertFalse(self.db.in_transaction)
        if "bing.com" in url:
            return url, "application/xml", b"<rss><channel/></rss>"
        return url, "application/json", b'{"articles":[]}' if "gdelt" in url else b"[]"

    def test_scope_limit_zero_idempotence_and_separate_snapshots(self):
        ready = queue.batch_check(self.db, [1, 1, 2], limit=0)
        self.assertEqual((ready["queued"], ready["pending_ids"], ready["processed"]), (3, [1, 2, 4], 0))
        with self.db:
            set_setting(self.db, "factcheck_batch_limit", 1)
        first = queue.batch_check(self.db, [1, 2], fetch=self.empty_search)
        self.assertEqual((first["processed_ids"], first["errors"], first["remaining"]), ([1], 0, 2))
        second = queue.batch_check(self.db, [1, 2], limit=10, fetch=self.empty_search)
        self.assertEqual(second["processed_ids"], [2, 4])
        self.assertEqual(second["state_counts"], {"done":3})
        self.assertEqual(self.db.execute("SELECT count(*) FROM factcheck_run WHERE status='insufficient'").fetchone()[0], 3)
        self.assertEqual(queue.batch_check(self.db, [1, 2], fetch=self.empty_search)["processed"], 0)

    def test_errors_have_diagnostics_and_deferred_retry(self):
        def unavailable(*_):
            raise OSError("HTTP 503 from provider")

        result = queue.batch_check(self.db, [2], fetch=unavailable)
        self.assertEqual((result["error_ids"], result["remaining"]), ([4], 1))
        state, last_error, after = self.db.execute("SELECT state,error,retry_after FROM factcheck_job").fetchone()
        self.assertEqual(state, "error")
        self.assertIn("503", last_error)
        self.assertIsNotNone(after)
        self.assertEqual(queue.batch_check(self.db, [2], fetch=self.empty_search)["processed"], 0)
        with self.db:
            self.db.execute("UPDATE factcheck_job SET retry_after=0")
        self.assertEqual(queue.batch_check(self.db, [2], fetch=self.empty_search)["processed_ids"], [4])

    def test_live_lease_and_recovery_after_factcheck_evidence_committed(self):
        def check_while_fetching(*args):
            with closing(self.connect()) as other:
                nested = queue.batch_check(other, [2], limit=1, fetch=self.empty_search)
            self.assertEqual((nested["processed"], nested["state_counts"]), (0, {"leased":1}))
            return self.empty_search(*args)

        self.assertEqual(queue.batch_check(self.db, [2], fetch=check_while_fetching)["processed_ids"], [4])
        snapshot = self.db.execute("SELECT check_id FROM factcheck_job WHERE page_id=4").fetchone()[0]
        # Simulate an exit between the commit of the external evidence and the queue update.
        with self.db:
            self.db.execute("""UPDATE factcheck_job SET state='leased',lease_until=0,lease_token='stale',check_id=NULL
                               WHERE page_id=4""")
        with patch.object(queue, "run_factcheck", side_effect=AssertionError) as check:
            resumed = queue.batch_check(self.db, [2])
            check.assert_not_called()
        self.assertEqual(resumed["reused_ids"], [4])
        self.assertEqual(self.db.execute("SELECT check_id FROM factcheck_job").fetchone()[0], snapshot)

    def test_abandoned_lease_without_stored_evidence_is_retried(self):
        queue.batch_check(self.db, [2], limit=0)
        with self.db:
            self.db.execute("""UPDATE factcheck_job SET state='leased',lease_token='stale',lease_until=0,attempts=1""")
        self.assertEqual(queue.batch_check(self.db, [2], fetch=self.empty_search)["processed_ids"], [4])
        self.assertEqual(self.db.execute("SELECT attempts FROM factcheck_job").fetchone()[0], 2)

    def test_partial_manual_check_does_not_consume_the_snapshot(self):
        factcheck.run_factcheck(self.db, 4, fetch=self.empty_search, search=False)
        report = queue.batch_check(self.db, [2], fetch=self.empty_search)
        self.assertEqual((report["processed_ids"], report["reused"]), ([4], 0))

    def test_old_insufficient_after_search_outage_is_retryable(self):
        with self.db:
            self.db.execute("""INSERT INTO factcheck_run(page_id,checked_at,query,minimum_sources,source_count,
                              status,claim,searches) VALUES(4,unixepoch(),'query',2,0,'insufficient',?,?)""",
                            (json.dumps({"allowlist_fingerprint":allowlist_fingerprint(self.db)}),
                             json.dumps([{"provider":"index","status":"error","error":"timeout"}])))
        report = queue.batch_check(self.db, [2], fetch=self.empty_search)
        self.assertEqual(report["state_counts"], {"error":1})
        self.assertEqual(report["reused"], 0)

    def test_legacy_jobs_and_unversioned_evidence_are_retained_but_not_reused(self):
        # Before allowlists, the schema only had factcheck_job, with no policy mapping.
        self.db.execute("DROP TABLE factcheck_job_policy")
        self.db.execute("DROP TABLE factcheck_job_history")
        with self.db:
            legacy = self.db.execute("""INSERT INTO factcheck_run(page_id,checked_at,query,minimum_sources,
                     source_count,status,claim,searches) VALUES(1,12,'q',2,3,'corroborated','{}','[]')""").lastrowid
            self.db.execute("""INSERT INTO factcheck_job(page_id,run_id,state,attempts,check_id,outcome)
                               VALUES(1,1,'done',1,?,'corroborated')""", (legacy,))
            self.db.execute("""INSERT INTO factcheck_job(page_id,run_id,state,attempts,outcome,error,retry_after)
                               VALUES(2,1,'error',2,'worker_error','old error',9999999999)""")
        self.db.executescript(SCHEMA_SQL)
        with patch.object(queue, "run_factcheck") as check:
            report = queue.batch_check(self.db, [1], limit=0)
            queue.batch_check(self.db, [1], limit=0)
            check.assert_not_called()
        self.assertEqual(report["pending_ids"], [1,2])
        history = self.db.execute("""SELECT page_id,state,attempts,allowlist_fingerprint,check_id,error
                                     FROM factcheck_job_history ORDER BY page_id""").fetchall()
        self.assertEqual([tuple(r) for r in history], [(1,"done",1,None,legacy,None),(2,"error",2,None,None,"old error")])
        self.assertEqual(self.db.execute("SELECT status FROM factcheck_run WHERE id=?", (legacy,)).fetchone()[0],
                         "corroborated")

    def test_allowlist_change_invalidates_done_and_error_and_keeps_both_attempts(self):
        first = queue.batch_check(self.db, [1], limit=1, fetch=self.empty_search)
        def unavailable(*_):
            raise OSError("HTTP 503")
        failed = queue.batch_check(self.db, [1], limit=1, fetch=unavailable)
        self.assertEqual((first["processed_ids"], failed["error_ids"]), ([1], [2]))
        old_fp = allowlist_fingerprint(self.db)
        old_checks = [r[0] for r in self.db.execute("SELECT check_id FROM factcheck_job ORDER BY page_id")]
        with self.db, patch.object(queue, "run_factcheck") as check:
            add_allowed_source(self.db, "https://second-example.ru/news/")
            check.assert_not_called()
        new_fp = allowlist_fingerprint(self.db)
        self.assertNotEqual(old_fp, new_fp)
        with patch.object(queue, "run_factcheck") as check:
            ready = queue.batch_check(self.db, [1], limit=0)
            check.assert_not_called()
        self.assertEqual((ready["pending_ids"],ready["reused"]), ([1,2],0))
        history = self.db.execute("""SELECT state,check_id,allowlist_fingerprint FROM factcheck_job_history
                                     ORDER BY page_id""").fetchall()
        self.assertEqual([tuple(r) for r in history],
                         [("done",old_checks[0],old_fp),("error",old_checks[1],old_fp)])
        second = queue.batch_check(self.db, [1], limit=2, fetch=self.empty_search)
        self.assertEqual(second["processed_ids"], [1,2])
        self.assertEqual(self.db.execute("SELECT count(*) FROM factcheck_run").fetchone()[0], 4)
        with self.db:
            remove_allowed_source(self.db, "https://second-example.ru/news/")
        # There are newer attempts for the second list, but only the first list's evidence fits now.
        restored = queue.batch_check(self.db, [1], limit=0)
        self.assertEqual(restored["reused_ids"], [1])
        self.assertEqual([r[0] for r in self.db.execute("SELECT check_id FROM factcheck_job ORDER BY page_id")],
                         old_checks)

    def test_change_during_lease_never_finishes_current_job_or_starts_another_search(self):
        for nested_worker, selected_run, page in [(False,2,4),(True,3,5)]:
            with self.subTest(nested_worker=nested_worker):
                initial_fp = allowlist_fingerprint(self.db)
                changed = False
                def change_policy(*args):
                    nonlocal changed
                    if not changed:
                        changed = True
                        with closing(self.connect()) as other:
                            with other:
                                add_allowed_source(other, f"https://second-example.ru/section-{page}/")
                            if nested_worker:
                                self.assertEqual(queue.batch_check(other,[selected_run],limit=1,
                                    fetch=self.empty_search)["processed_ids"], [page])
                    return self.empty_search(*args)
                outer = queue.batch_check(self.db, [selected_run], limit=3, fetch=change_policy)
                self.assertTrue(outer["policy_changed"])
                self.assertEqual(outer["processed_ids"], [])
                row = self.db.execute("SELECT state,check_id,lease_token FROM factcheck_job WHERE page_id=?",
                                      (page,)).fetchone()
                self.assertEqual((row[0],row[2]), ("done" if nested_worker else "pending",None))
                runs = dict(self.db.execute("""SELECT json_extract(claim,'$.allowlist_fingerprint'),id
                                               FROM factcheck_run WHERE page_id=?""",(page,)))
                self.assertIn(initial_fp,runs)
                self.assertEqual(len(runs), 2 if nested_worker else 1)
                if nested_worker:
                    self.assertEqual(row[1], runs[allowlist_fingerprint(self.db)])
                else:
                    self.assertIsNone(row[1])

    def test_result_persisted_for_temporary_policy_cannot_finish_restored_policy(self):
        def switched_search(conn, page_id, **kwargs):
            # The policy was B when evidence was collected and is A again when the worker returns.
            with conn:
                add_allowed_source(conn, "https://temporary-example.ru/news/")
                other_fp = allowlist_fingerprint(conn)
                other = conn.execute("""INSERT INTO factcheck_run(page_id,checked_at,query,minimum_sources,
                       source_count,status,claim,searches) VALUES(?,100,'query',2,2,'corroborated',?,'[]')""",
                       (page_id,json.dumps({"allowlist_fingerprint":other_fp}))).lastrowid
                remove_allowed_source(conn, "https://temporary-example.ru/news/")
            return {"status":"corroborated","searches":[],"sources":[],"check_id":other}
        with patch.object(queue,"run_factcheck",side_effect=switched_search) as worker:
            report = queue.batch_check(self.db, [2], limit=3)
            worker.assert_called_once()
        self.assertEqual((report["pending_ids"],report["processed_ids"],report["error_ids"]), ([4],[],[]))
        self.assertEqual(queue.batch_check(self.db, [2], limit=0)["reused"], 0)
        self.assertEqual(tuple(self.db.execute("SELECT check_id,lease_token FROM factcheck_job").fetchone()),
                         (None,None))
        self.assertEqual(self.db.execute("SELECT count(*) FROM factcheck_run").fetchone()[0], 1)

    def test_cli_disabled_flag_and_auto_setting(self):
        self.enterContext(patch.object(loadPage, "connect_db", self.connect))
        self.enterContext(patch.object(loadPage, "organizations", return_value=[{"id":1}]))
        self.enterContext(patch.object(loadPage, "crawl_organization", side_effect=[
            loadPage.Result(i, 2, 0, "complete", "test") for i in (1, 2, 3)]))
        for flags, expected_network in [([], False), (["--factcheck"], True), ([], True)]:
            if not flags and expected_network:
                with self.db:
                    set_setting(self.db, "factcheck_auto", True)
            with patch.object(sys, "argv", ["loadPage.py", "--org", "1", "--news-only", *flags]), \
                 patch.object(factcheck, "_fetch", side_effect=AssertionError) as external, \
                 patch.object(self, "empty_search", wraps=self.empty_search) as fetch, redirect_stdout(StringIO()):
                self.assertEqual(loadPage.main(factcheck_fetch=fetch), 0)
                self.assertEqual(fetch.called, expected_network)
                external.assert_not_called()


if __name__ == "__main__":
    unittest.main()
