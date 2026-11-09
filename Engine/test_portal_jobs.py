import sqlite3
import threading
import unittest
from unittest.mock import patch

from Engine import config, crawl_store, dashboard_data, model_settings, portal_jobs


class PortalJobTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        crawl_store.initialize_db(self.db)
        self.db.executemany("INSERT INTO organization VALUES(?,?,?,?)", [
            (1,"Школа","https://first.example","school"), (2,"Сад","https://second.example","kindergarten")])
        self.db.commit()

    def test_empty_or_disabled_selection_never_sends_model_requests(self):
        with patch.object(portal_jobs.model_gateway, "_api") as api:
            for ids, mode in (([], "ai"), ([99], "ai"), ([1], "ai"), ([2], "algorithm")):
                with self.assertRaises(ValueError):
                    portal_jobs.start(self.db, ids, mode, threading.Lock())
            api.assert_not_called()
        self.assertEqual(portal_jobs.plan(self.db, [2], "download")["empty"], ["Сад"])

    def test_probe_requires_an_answer_in_the_configured_format(self):
        model_settings.set_setting(self.db,"portal_ai_enabled",True)
        with patch.object(portal_jobs.model_gateway, "_api", return_value={"choices":[{"message":{"content":"нет"}}]}):
            with self.assertRaisesRegex(ValueError,"JSON"):
                portal_jobs.verify(self.db)
        with patch.object(portal_jobs.model_gateway, "_api", return_value={"choices":[{"message":{"content":'{"ok":true}'}}]}) as api:
            self.assertEqual(portal_jobs.verify(self.db)["model"], model_settings.DEFAULTS["lmstudio"].model)
            self.assertIn("response_format",api.call_args.args[3])

    def test_download_estimates_preserve_decline_and_respect_limits(self):
        model_settings.set_setting(self.db,"portal_download_1",{"limits":[16,3,12],"seconds":15})
        profile = model_settings.get_profile(self.db)
        portal_jobs.record_benchmark(self.db,profile,None)
        one = portal_jobs.plan(self.db,[1],"download")
        self.assertEqual(one["download_seconds"],15)
        self.assertIn("отказался",one["benchmark_reason"])
        self.assertIsNone(portal_jobs.plan(self.db,[1,2],"download")["download_seconds"])

    def test_detector_does_not_attribute_unchecked_criteria_to_algorithms(self):
        self.assertEqual(dashboard_data.dashboard(self.db,{"detector":"algorithm"})["findings"],[])
        self.assertTrue(dashboard_data.dashboard(self.db,{"detector":"none"})["findings"])

    def test_preview_cannot_start_a_different_scope(self):
        preview = portal_jobs.preview(self.db, [1], "download")
        lock = threading.Lock()
        with self.assertRaisesRegex(ValueError, "план устарел"):
            portal_jobs.start(self.db, [2], "download", lock, preview["ticket"])
        self.assertFalse(lock.locked())
