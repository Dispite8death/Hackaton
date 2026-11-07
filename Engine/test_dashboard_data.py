import json
import sqlite3
import unittest

from config import SCHEMA_SQL
from dashboard_data import dashboard, institution


class DashboardCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA_SQL)
        self.conn.executemany("INSERT INTO organization VALUES (?,?,?,?)", [
            (1, "Школа Олимп", "https://alpha.example/", "school"),
            (2, "Детский сад Ландыш", "https://beta.example/", "kindergarten"),
        ])
        self.conn.execute("INSERT INTO page VALUES (1,?,?,?,?)",
                          ("https://alpha.example/about", 1, 1735689600, "<main>Школа</main>"))

    def assess(self, check_id, outcome, stamp=1735689600):
        req = {"checks": [{"id": check_id}], "materials": [{"id":"doc1", "kind":"pdf", "pdf_page":2}]}
        result = {"results": [{"evidence":"Наблюдение в архиве", "material_ids":["doc1"]}]}
        self.conn.execute("""INSERT INTO model_assessment
            (page_id,check_id,created_at,provider,endpoint,model,outcome,request,response,elapsed_seconds)
            VALUES(1,?,?,'local','localhost','Qwen',?,?,?,1)""",
            (check_id, stamp, outcome, json.dumps(req), json.dumps(result)))

    def test_unknowns_have_concrete_material_and_do_not_score(self):
        result = institution(self.conn, 2)
        self.assertIsNone(result["institution"]["score"])
        self.assertEqual(result["institution"]["quality"], "unrated")
        self.assertEqual(result["institution"]["coverage"], 0)
        by_key = {r["criterion"]:r for r in result["findings"]}
        self.assertIn("Недостаточно данных", by_key["phone"]["description"])
        self.assertIn("контактов", by_key["phone"]["recommendation"])
        self.assertNotIn("schedule", by_key)

    def test_polarity_evidence_latest_and_filter_invariance(self):
        self.assess("feedback_banner", "present")
        self.assess("feedback_banner_overlap", "present")
        self.assess("school_history_facts", "absent")
        self.assess("school_history_facts", "uncertain", 1735689700)
        self.assess("advisory_flag", "absent")
        self.conn.execute("INSERT INTO institution_contact_page VALUES (1,'home','html',?,'[]')",
                          (json.dumps([{"field":"phone","value":"+7 000 000-00-00"}]),))
        report = institution(self.conn, 1)
        self.assertEqual(report["institution"]["score"], 50)
        self.assertEqual(report["institution"]["verified_criteria"], 2)
        result = dashboard(self.conn, {"category":"layout","status":"issue","q":"ОЛИМП"})
        self.assertEqual(result["institutions"][0]["score"], 50)
        self.assertEqual(result["summary"]["total"],1)
        self.assertEqual(len(result["findings"]),1)
        row = result["findings"][0]
        self.assertEqual((row["status"], row["page_id"], row["materials"][0]["page"]), ("issue",1,2))
        self.assertTrue(row["url"].startswith("https://alpha"))
        history=[r for r in report["findings"] if r["criterion"]=="history"]
        self.assertEqual([r["status"] for r in history], ["review"])
        self.assertEqual(dashboard(self.conn, {"date_from":"2025-01-02"})["summary"]["counts"]["issue"], 0)

    def test_invalid_filters(self):
        for filters in ({"status":"undefined"}, {"date_from":"2026-12-01","date_to":"2026-11-01"},
                        {"organization_id":["1","x"]}):
            with self.assertRaises(ValueError):
                dashboard(self.conn,filters)

    def test_original_size_uses_the_downloaded_main_image(self):
        self.conn.execute("INSERT INTO home_main_photo VALUES(1,'found','https://alpha.example/main.jpg','','{}')")
        digest = "a"*64
        self.conn.execute("INSERT INTO home_image_asset VALUES (?,'image/jpeg',800,600,?)", (digest,b"x"))
        self.conn.execute("""INSERT INTO home_image_download VALUES
                (1,'https://alpha.example/main.jpg',1735689600,'https://alpha.example/main.jpg',200,'saved',NULL,?)""",(digest,))
        only = dashboard(self.conn, {"group":"photos", "status":"issue"})
        row, = only["findings"]
        self.assertEqual(row["criterion"], "main_photo_size")
        self.assertEqual(row["materials"][0]["id"],digest)
        self.assertIn("800 × 600",row["description"])
        self.assertEqual(only["institutions"][0]["verified_criteria"],1)

