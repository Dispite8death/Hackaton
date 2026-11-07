import sqlite3
import unittest
from unittest.mock import patch

from crawl_store import initialize_db
from deferred_checks import deferred_for,review_group
from model_batch import batch_assess
from page_passport import index_page


class BatchMaterialsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        initialize_db(self.db)
        with self.db:
            self.db.execute("INSERT INTO organization VALUES(1,'Школа','https://school.test','school')")
            self.db.execute("INSERT INTO crawl_run(id,id_organization) VALUES(1,1)")
            for pid in (1,2):
                url = f"https://school.test/raspisanie/{pid}"
                self.db.execute("INSERT INTO page VALUES(?,?,1,1732000000,'<main>Расписание</main>')",(pid,url))
                self.db.execute("""INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,page_id)
                    VALUES(?,1,?,'schedule',1,1732000000,?)""",(pid,url,pid))
                index_page(self.db,pid)
            self.db.execute("""INSERT INTO page_attachment(id,page_id,url,role,title,format_hint)
                VALUES(7,1,'https://school.test/timetable.pdf','schedule','Расписание','pdf')""")

    def test_ready_plan_excludes_missing_and_date_scopes(self):
        with patch("model_batch.assess",side_effect=AssertionError("plan invoked the model")):
            result = batch_assess(self.db,page_ids=[1],plan=True)
        self.assertEqual(result["by_group"],{"content":1})
        self.assertEqual(result["ready"],1)
        self.assertGreaterEqual(result["missing"],2)
        groups = {c["id"]:c for c in deferred_for("events_listing","school",1732000000)}
        self.assertIn("1-й квартал 2025",groups["event_plan_next"]["instruction"])
        self.assertEqual(review_group(groups["event_plan_next"]),"publication")

    def test_resume_after_error_and_rotation(self):
        attempts = []
        def model(conn,pid,check,profile):
            attempts.append(pid)
            outcome = "error" if len(attempts)==1 else "present"
            with conn:
                aid = conn.execute("""INSERT INTO model_assessment(page_id,check_id,provider,endpoint,model,
                    outcome,request,elapsed_seconds) VALUES(?,?,?,?,?,?,?,1)""",
                    (pid,check,profile.provider,profile.endpoint,profile.model,outcome,"{}")).lastrowid
            return {"assessment_id":aid,"error":"Не удалось прочесть JSON" if outcome=="error" else None,"outcome":outcome}
        with patch("model_batch.assess",side_effect=model):
            for _ in range(3):
                batch_assess(self.db,limit=1,group="content",rotate=True)
            summary = batch_assess(self.db,group="content",plan=True,rotate=True)
        self.assertEqual(attempts,[1,2,1])
        self.assertEqual((summary["ready"],summary["completed"]),(0,2))

    def test_attachment_retry_after_bytes_become_available(self):
        first = batch_assess(self.db,page_ids=[1],plan=True,group="attachments")
        self.assertEqual((first["ready"],first["missing"]),(0,1))
        with self.db:
            self.db.execute("UPDATE page_attachment SET state='saved',sha256=?,byte_size=42,format='pdf' WHERE id=7",("0"*64,))
        second = batch_assess(self.db,page_ids=[1],plan=True,group="attachments")
        self.assertEqual((second["ready"],second["missing"]),(1,0))

    def test_historical_duplicates_can_be_read_but_the_worker_uses_latest_per_site(self):
        with self.db:
            self.db.execute("UPDATE page SET url=(SELECT url FROM page WHERE id=1) WHERE id=2")
        archive = batch_assess(self.db,group="content",plan=True)
        fresh = batch_assess(self.db,group="content",plan=True,latest_only=True)
        self.assertEqual((archive["pages"],fresh["pages"]),(2,1))

    def test_history_and_programs_are_assigned_to_relevant_sections(self):
        def ids(kind,org,title,path,headings=()):
            return {c["id"] for c in deferred_for(kind,org,1,title=title,url=path,
                          headings=[{"text":h} for h in headings])}
        self.assertNotIn("school_history_facts",ids("about","school","Библиотека","/library/"))
        self.assertIn("school_history_facts",ids("about","school","Наша школа","/custom/"))
        self.assertIn("school_history_facts",ids("about","school","Школьная жизнь","/x/",["Летопись школы"]))
        self.assertNotIn("garden_program_details",ids("information","kindergarten","Вакантные места","/vacant/"))
        self.assertIn("garden_program_details",ids("information","kindergarten","Реализуемые программы","/x/"))
