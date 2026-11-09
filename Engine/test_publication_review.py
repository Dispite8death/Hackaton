import json
import sqlite3
import unittest
from unittest.mock import patch

import model_gateway as gateway
from config import SCHEMA_SQL
from deferred_checks import review_group
from factcheck_sources import add_allowed_source, allowlist_fingerprint, remove_allowed_source
from publication_review import aggregate_external, guard_source, publication_materials, publication_questions
from review_contract import make_request, validate_response


class PublicationReviewTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.execute("INSERT INTO organization VALUES(1,'Школа № 7','https://school.test','school')")
        self.db.execute("INSERT INTO crawl_run(id,id_organization) VALUES(1,1)")
        self.db.execute("INSERT INTO page VALUES(1,'https://school.test/e',1,100,'<main>Диктант</main>')")
        self.db.execute("INSERT INTO page_passport VALUES(1,'event_detail','Диктант','[]','','[]')")
        self.db.execute("INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,page_id) VALUES(1,'https://school.test/e','events',1,100,1)")
        for host in ("one.ru","two.ru","wire.ru"):
            add_allowed_source(self.db,"https://"+host)
        self.claim={"organization":"Школа № 7","title":"Городской диктант","event_day":"2026-10-14",
                    "allowlist_fingerprint":allowlist_fingerprint(self.db)}
        self.db.execute("INSERT INTO factcheck_run VALUES(5,1,101,'Диктант',2,0,'review',?,'[]')",(json.dumps(self.claim),))
        self.text="В городе прошёл ежегодный экономический диктант."
        for n,(host,group,status) in enumerate((("one.ru",1,"related_unverified"),("two.ru",2,"related_unverified"),
                ("wire.ru",1,"related_unverified"),("unknown.ru",3,"matching_report"),
                ("one.ru",4,"attributed_or_syndicated"))):
            data={"policy_status":"allowed","title":"Диктант","excerpt":self.text,"related_group":group,
                  "signals":{"event_supported":True,"participation_match":False},"sha256":"digest"+str(n)}
            url="https://"+host+"/article"
            self.db.execute("INSERT INTO factcheck_source VALUES(?,?,?,?,?,?,?)",
                            (5,n,url,url,host,status,json.dumps(data,ensure_ascii=False)))
        self.identity={"organization_id":1,"run_id":1,"page_id":1}

    def test_only_current_policy_independent_originals_and_organization_is_not_assumed(self):
        event,org=publication_questions(self.db,1)
        self.assertTrue(all(review_group(q)=="publication" for q in (event,org)))
        items=[m for m,_ in publication_materials(self.db,1,event)]
        self.assertEqual([m["publisher"] for m in items],["one.ru","two.ru","wire.ru"])
        replies=[dict(outcome="present",material_ids=[m["id"]],evidence="«"+self.text+"»") for m in items]
        self.assertIsNone(guard_source(event,items[:1],replies[0]))
        self.assertIsNotNone(guard_source(org,items[:1],replies[0]))
        self.assertIsNotNone(guard_source(event,items[:1],dict(replies[0],evidence="«Школа победила в диктанте»")))
        confirmed=aggregate_external(event,items,replies)
        self.assertEqual(confirmed["outcome"],"present")
        self.assertEqual(len(confirmed["material_ids"]),2)
        for r in ([replies[0]],[replies[0],replies[2]]):
            self.assertEqual(aggregate_external(event,items,r)["outcome"],"uncertain")
        self.assertEqual(aggregate_external(org,items,replies)["outcome"],"uncertain")
        request=make_request(self.identity,[event],items)
        validate_response(request,{"identity":self.identity,"results":[dict(confirmed,check_id=event["id"])]})
        with self.assertRaises(ValueError):
            validate_response(request,{"identity":self.identity,"results":[dict(replies[0],check_id=event["id"])]})
        remove_allowed_source(self.db,"https://two.ru")
        self.assertEqual(publication_questions(self.db,1),[])
        self.assertEqual(publication_materials(self.db,1,event),[])

    def test_gateway_cannot_change_unsupported_participation_into_a_positive_result(self):
        event,org=publication_questions(self.db,1)
        def fake(profile,key,check,materials,parts,*,trace):
            m=materials[0]
            reply={"outcome":"present","material_ids":[m["id"]],"evidence":"«"+self.text+"»"}
            trace["raw_response"]=json.dumps(reply)
            return reply,None
        with patch.object(gateway,"_predict",side_effect=fake):
            a=gateway.assess(self.db,1,event["id"],key="")
            b=gateway.assess(self.db,1,org["id"],key="")
        self.assertEqual([a["outcome"],b["outcome"]],["present","uncertain"])
        self.assertEqual(self.db.execute("SELECT status FROM factcheck_run WHERE id=5").fetchone()[0],"review")
        self.assertEqual(self.db.execute("SELECT count(*) FROM model_evidence_pass").fetchone()[0],6)

    def test_internal_candidate_requires_both_local_records_and_a_complete_news_article(self):
        q={"id":"pair","publication":{"scope":"internal"}}
        e={"id":"e","record_type":"event","planned_start":"2026-10-14","planned_end":"2026-10-14"}
        n={"id":"n","record_type":"preview","full_text_included":True,"event_period_matches":False,
           "source_excerpt":"В городе прошёл ежегодный экономический диктант."}
        r={"outcome":"present","material_ids":["e","n"],"evidence":"«В городе прошёл ежегодный экономический диктант»."}
        self.assertIsNotNone(guard_source(q,[e,n],r))
        n["record_type"]="article"
        self.assertIsNotNone(guard_source(q,[e,n],r))
        n["reported_period"]=["2026-10-14","2026-10-14"]
        self.assertIsNone(guard_source(q,[e,n],r))
        self.assertIsNotNone(guard_source(q,[e,n],dict(r,material_ids=["n"])))

    def test_allowlist_edit_between_calls_stops_remaining_excerpts(self):
        check=publication_questions(self.db,1)[0]
        def edit(profile,key,check,materials,parts,*,trace):
            remove_allowed_source(self.db,"https://two.ru/")
            return {"outcome":"present","material_ids":[materials[0]["id"]],"evidence":"«"+self.text+"»"},None
        with patch.object(gateway,"_predict",side_effect=edit) as api:
            result=gateway.assess(self.db,1,check["id"],key="")
        self.assertEqual(api.call_count,1)
        self.assertEqual(result["outcome"],"uncertain")
        self.assertIn("изменились",result["response"]["results"][0]["evidence"])
