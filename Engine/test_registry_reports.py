import asyncio
import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from Engine import config, model_settings, roster, site_server
from ai_audit_web import db, integration, llm, reports


class SharedRegistryTest(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(config, "DB_PATH", str(folder / "archive.db")))
        self.enterContext(patch.object(db, "DB_PATH", folder / "quick.db"))
        self.enterContext(patch.object(integration, "DATA_DIR", folder))
        site_server.prepare_database()
        integration.initialize()
        self.archive = self.enterContext(closing(config.connect_db()))

    def change(self, **payload):
        return site_server.update_settings(self.archive, "roster", payload)

    def test_clear_restart_import_and_undo_preserve_both_ids(self):
        old = roster.organizations(self.archive)[0]
        quick_id = integration.quick_id(old["site_url"])
        with self.assertRaises(ValueError):
            self.change(operation="clear")
        self.change(operation="clear", confirmed=True)
        site_server.prepare_database()
        integration.initialize()
        self.assertEqual(roster.organizations(self.archive), [])
        self.assertEqual(db.get_sites(), [])
        record = {k:v for k,v in old.items() if k != "id"}
        self.change(operation="import", format="json", content=json.dumps([record,record]))
        self.assertEqual(roster.organizations(self.archive), [old])
        self.change(operation="remove", id=old["id"])
        self.assertIsNone(db.get_site(quick_id))
        self.change(operation="add", record=record)
        self.assertEqual(db.get_site(quick_id)["name"], old["name"])

    def test_import_schemas_and_atomic_failure(self):
        self.change(operation="import", format="csv", content='\ufeffname;kind;site_url\n"Тест; первый";школа;https://test.example/a')
        self.change(operation="import", format="txt", content="Второй & садик & https://test.example/b?q=1&b=2")
        self.assertNotEqual(integration.quick_id("https://test.example/a/"), integration.quick_id("https://test.example/b?q=1&b=2"))
        for fmt, bad in (("txt","OK & school & https://new.example\nНет & школы"),("csv","name,kind\nОдин,school"),
                         ("json","[{}]"),("txt","Один & school & https://new.example\nДва & kindergarten & https://new.example/")):
            with self.subTest(fmt=fmt), self.assertRaises(ValueError):
                self.change(operation="import", format=fmt, content=bad)
            self.assertIsNone(integration.quick_id("https://new.example"))
            self.assertIsNone(integration.archive_id("https://new.example"))
        self.assertEqual(len(db.get_sites()), 82)

    def test_latest_completed_per_site_and_combined_filters(self):
        a,b = db.get_sites()[:2]
        def save(site, level, code, run):
            db.save_site_result(run, site, {"score":70,"findings":[{"severity":level,"agent":"docs","code":code,"message":code}]})
        run = db.start_run(2)
        save(a,"LOW","ONE",run)
        save(b,"HIGH","TWO",run)
        db.finish_run(run,"done",70)
        run = db.start_run(1)
        save(a,"HIGH","THREE",run)
        db.finish_run(run,"done",70)
        save(b,"LOW","UNFINISHED",db.start_run(1))
        rows = reports.latest()
        self.assertEqual({r["findings"][0]["code"] for r in rows}, {"TWO","THREE"})
        subset = reports.select(rows,[a["id"],b["id"]],a["type"],"HIGH","docs","THREE")
        self.assertEqual([r["site"]["id"] for r in subset],[a["id"]])
        self.assertEqual(reports.stats(subset)["total_findings"],1)
        db.save_explanation(subset[0]["result"]["id"], "Текст разбора")
        self.assertEqual(db.get_last_site_result(a["id"])["ai_explanation"],"Текст разбора")
        self.assertEqual(reports.select(rows, since="9999-01-01"), [])
        self.change(operation="remove",id=integration.archive_id(a["url"]))
        self.assertEqual([r["site"]["id"] for r in reports.latest()],[b["id"]])


class ReportProtocolTest(unittest.TestCase):
    def test_provider_and_response_format(self):
        for provider in model_settings.DEFAULTS:
            for mode in ("prompt","json_schema","json_object"):
                with self.subTest(provider=provider, mode=mode):
                    profile = model_settings.Profile(provider, "http://localhost:1234/v1", "test-model", 15, mode, True)
                    settings = {"llm_enabled":"1","llm_profile":profile,"llm_api_key":"test-key"}
                    answer = "Разбор" if mode == "prompt" else json.dumps({"text":"Разбор"})
                    reply = {"choices":[{"finish_reason":"stop","message":{"content":answer}}]}
                    with patch.object(llm, "_api", return_value=reply) as api:
                        self.assertEqual(asyncio.run(llm.explain({"name":"A"}, [], settings)), "Разбор")
                    actual, route, key, body = api.call_args.args
                    self.assertEqual((actual,route,key), (profile,"/chat/completions","test-key"))
                    self.assertEqual(body["model"],profile.model)
                    self.assertEqual(body.get("response_format",{}).get("type"), mode if mode!="prompt" else None)
                    self.assertEqual("chat_template_kwargs" in body, provider=="lmstudio")

    def test_incomplete_or_malformed_responses_are_rejected(self):
        settings = {"llm_enabled":"1","llm_profile":model_settings.DEFAULTS["ollama"]}
        for reply in ({},{"choices":[]},{"choices":[{"message":{"content":"[]"}}]},
                      {"choices":[{"message":{"content":"{}"},"finish_reason":"length"}]},
                      {"choices":[{"message":{"content":json.dumps({"text":False})}}]}):
            with patch.object(llm,"_api",return_value=reply), self.assertRaises(ValueError):
                asyncio.run(llm.summarize({"name":"A"}, [], settings))
