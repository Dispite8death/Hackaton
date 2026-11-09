import io
import json
import os
import sqlite3
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from PIL import Image

import model_gateway as gateway
import model_settings as settings
from config import SCHEMA_SQL
from model_settings import Profile


def reply(outcome="present", evidence="Указан телефон учреждения", ref="p23-html"):
    return {"choices":[{"message":{"content":json.dumps(
        {"outcome":outcome,"material_ids":[ref],"evidence":evidence}, ensure_ascii=False)}}],
        "usage":{"total_tokens":87}}


class AdminAndGatewayTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        checks=[
            {"id":"garden_phone","instruction":"Опубликован ли контактный телефон?","material_kinds":["html","dom"]},
            {"id":"screen_layout","instruction":"Перекрывается ли текст?","material_kinds":["viewport","full"]},
        ]
        self.db.execute("INSERT INTO organization VALUES(9,'Детский сад','https://example.test','kindergarten')")
        self.db.execute("INSERT INTO crawl_run(id,id_organization) VALUES(17,9)")
        self.db.execute("INSERT INTO page VALUES(23,'https://example.test/',9,500,'<main>Телефон 123456</main>')")
        self.db.execute("""INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,page_id)
                           VALUES(17,'https://example.test/','home',0,500,23)""")
        self.db.execute("INSERT INTO page_passport VALUES(23,'home','Сад','[]','Главная',?)",
                        (json.dumps(checks),))
        self.db.commit()
        self.ollama=Profile("ollama","http://127.0.0.1:11434/v1","garden-local",77,"json_object")
        self.external=Profile("compatible","https://published-api.example.test/v1","team-hosted",45,"prompt")

    def mock_keyring(self):
        passwords={}
        fake=SimpleNamespace(get_password=lambda service,name:passwords.get((service,name)),
                             set_password=lambda service,name,v:passwords.update({(service,name):v}),
                             delete_password=lambda service,name:passwords.pop((service,name)))
        self.enterContext(patch.dict(sys.modules,{"keyring":fake}))
        return passwords

    def test_ollama_returns_null_data_for_an_empty_local_model_library(self):
        with patch.object(gateway,"_api",return_value={"object":"list","data":None}):
            self.assertEqual(gateway.list_models(self.ollama), [])
        with patch.object(gateway,"_api",return_value={"object":"list"}):
            with self.assertRaises(ValueError):
                gateway.list_models(self.ollama)

    def test_ollama_text_model_never_receives_an_image(self):
        with patch.object(gateway, "_api", return_value={"capabilities":["completion"]}) as api:
            result, _ = gateway._predict(self.ollama, "", {"id":"schedule_visible_lessons"},
                                         [], [{"type":"image_url"}])
        self.assertEqual(result["outcome"], "uncertain")
        self.assertIn("не поддерживает изображения", result["evidence"])
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[1], "/api/show")

    def test_active_profile_drives_actual_http_request_and_secret_is_scoped(self):
        passwords=self.mock_keyring()
        old_secret="test-only-value-for-first-address"
        with self.db:
            settings.save_secret(self.db,self.external,old_secret)
            settings.save_profile(self.db,self.external)
        targets=[]
        def opened(req,timeout):
            payload=json.loads(req.data)
            targets.append((req.full_url,payload["model"],req.get_header("Authorization"),timeout,payload))
            return io.BytesIO(json.dumps(reply()).encode())
        opener=Mock()
        opener.open.side_effect=opened
        with patch.object(gateway,"build_opener",return_value=opener):
            first=gateway.assess(self.db,23,"garden_phone")
            with self.db:
                settings.save_profile(self.db,self.ollama)
            second=gateway.assess(self.db,23,"garden_phone")
            new_host=replace(self.external,endpoint="https://new-api.example.test",model="new-model")
            with self.db:
                settings.save_profile(self.db,new_host)
            third=gateway.assess(self.db,23,"garden_phone")
        self.assertEqual([r["outcome"] for r in (first,second,third)], ["present"] * 3)
        self.assertEqual([r[:4] for r in targets],[
            (self.external.endpoint+"/chat/completions","team-hosted","Bearer "+old_secret,45),
            (self.ollama.endpoint+"/chat/completions","garden-local",None,77),
            ("https://new-api.example.test/v1/chat/completions","new-model",None,45)])
        self.assertNotIn("response_format",targets[0][4])
        self.assertEqual(targets[1][4]["response_format"],{"type":"json_object"})
        self.assertEqual(settings.get_profile(self.db,"ollama"),self.ollama)
        self.assertEqual(len(passwords),1)
        self.assertNotIn(old_secret,"".join(self.db.iterdump()))
        rows=self.db.execute("SELECT provider,model,outcome,request,response,usage_json FROM model_assessment ORDER BY id").fetchall()
        self.assertEqual([row[:3] for row in rows],[
            ("compatible","team-hosted","present"),("ollama","garden-local","present"),("compatible","new-model","present")])
        for _,_,_,req,res,usage in rows:
            self.assertEqual(json.loads(req)["identity"],{"organization_id":9,"run_id":17,"page_id":23})
            self.assertEqual(json.loads(req)["identity"],json.loads(res)["identity"])
            self.assertEqual(json.loads(usage)["total_tokens"],87)
        with self.db:
            settings.save_secret(self.db,self.external,"")
        self.assertFalse(passwords)
        self.assertEqual(settings.active_secret(self.db,self.external),"")

    def test_reject_plaintext_external_endpoint_but_allow_local(self):
        for endpoint in ("http://localhost:1234","http://127.0.0.1:11434/v1/",
                         "http://192.168.1.20:1234","http://[::1]:1234"):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(settings.checked(replace(self.ollama,endpoint=endpoint)).endpoint.endswith("/v1"))
        for profile in (replace(self.external,endpoint="http://public-api.example.test/v1"),
                        replace(self.external,endpoint="https://user:password@public-api.example.test/v1"),
                        replace(self.ollama,endpoint="https://public-api.example.test/v1")):
            with self.subTest(endpoint=profile.endpoint):
                with self.assertRaises(ValueError):
                    settings.checked(profile)

    def test_models_list_uses_the_unsaved_endpoint_and_auth_without_activating_it(self):
        payload={"data":[{"id":"local-vision"},{"id":"local-vision"},{"id":"text-embedding"},{"id":"local-text"}]}
        opener=Mock()
        opener.open.return_value=io.BytesIO(json.dumps(payload).encode())
        with patch.object(gateway,"build_opener",return_value=opener):
            items=gateway.list_models(replace(self.external,model=""),key="only-this-request")
        self.assertEqual(items,["local-text","local-vision"])
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,self.external.endpoint+"/models")
        self.assertEqual(request.get_method(),"GET")
        self.assertEqual(request.get_header("Authorization"),"Bearer only-this-request")
        self.assertEqual(settings.get_profile(self.db),settings.DEFAULTS["lmstudio"])
        self.assertNotIn("only-this-request","".join(self.db.iterdump()))

    def test_server_failure_and_unsupported_material_are_persisted(self):
        key="test-only-key-redacted"
        opener=Mock()
        opener.open.side_effect=HTTPError("https://published-api.example.test/v1",403,"Denied",{},
                                          io.BytesIO(("invalid key "+key).encode()))
        with patch.object(gateway,"build_opener",return_value=opener):
            failure=gateway.assess(self.db,23,"garden_phone",profile=self.external,key=key)
        with patch.object(gateway,"_api",return_value=reply(ref="p999-different")):
            wrong=gateway.assess(self.db,23,"garden_phone",profile=self.ollama)
        with patch.object(gateway,"_api") as api:
            missing=gateway.assess(self.db,23,"screen_layout",profile=self.ollama)
            api.assert_not_called()
        self.assertEqual([failure["outcome"],wrong["outcome"],missing["outcome"]],["error","error","uncertain"])
        errors=self.db.execute("SELECT error,response FROM model_assessment ORDER BY id").fetchall()
        self.assertIn("403",errors[0][0])
        self.assertNotIn(key,errors[0][0])
        self.assertIn("материал",errors[1][0])
        self.assertIsNone(errors[1][1])
        self.assertEqual(json.loads(errors[2][1])["results"][0]["material_ids"],[])

    def test_truncation_cannot_establish_absence(self):
        self.db.execute("UPDATE page SET text_page=?",("<main>"+("описание "*1300)+"999-1122</main>",))
        with patch.object(gateway,"_api",return_value=reply("absent","В видимом фрагменте номер не найден")) as api:
            result=gateway.assess(self.db,23,"garden_phone",profile=self.ollama)
        self.assertEqual(result["outcome"],"uncertain")
        parts=api.call_args.args[-1]["messages"][-1]["content"]
        self.assertLess(sum(len(p["text"]) for p in parts if p["type"]=="text"),11000)
        raw=self.db.execute("SELECT request FROM model_assessment WHERE id=?",(result["assessment_id"],)).fetchone()[0]
        self.assertTrue(json.loads(raw)["materials"][0]["partial"])

    def test_duplicate_outcomes_from_model_are_rejected(self):
        result=reply()
        result["choices"][0]["message"]["content"]=(
            '{"outcome":"present","outcome":"absent","material_ids":["p23-html"],"evidence":"разные выводы"}'
        )
        with patch.object(gateway,"_api",return_value=result):
            failure=gateway.assess(self.db,23,"garden_phone",profile=self.ollama)
        self.assertEqual(failure["outcome"],"error")
        self.assertIsNone(self.db.execute("SELECT response FROM model_assessment WHERE id=?",
                                         (failure["assessment_id"],)).fetchone()[0])

    def test_two_preview_dates_require_both_quotes_and_preserve_the_original_response(self):
        dates = ["17 сентября 2025", "20 сентября"]
        check_id = "news_date_card_1"
        html = ("<main><article id='fair'><time>17 сентября 2025</time>"
                "<p>Ярмарка состоится 20 сентября.</p></article><p>Фрагмент соседней карточки</p></main>")
        evidence = {"selector": "#fair", "date_evidence": {"occurrences": [{"raw": d} for d in dates]}}
        self.db.execute("UPDATE page SET text_page=? WHERE id=23", (html,))
        self.db.execute("INSERT INTO news_page VALUES(23,'news_list','html','unknown',1,'{}')")
        self.db.execute("""INSERT INTO news_preview(page_id,ordinal,url,title,date_status,evidence)
                           VALUES(23,0,'https://example.test/fair','Ярмарка','multiple_dates',?)""",
                        (json.dumps(evidence),))
        cases = [
            ("present", "В превью одна дата: 17 сентября 2025.", True),
            ("absent", "Вижу две даты, одна из них — 17 сентября 2025.", True),
            ("absent", "В превью «17 сентября 2025» и в анонсе «20 сентября».", False),
        ]
        for model_outcome, explanation, needs_guard in cases:
            with self.subTest(model_outcome=model_outcome, explanation=explanation):
                with patch.object(gateway,"_api",return_value=reply(model_outcome, explanation)) as api:
                    result = gateway.assess(self.db, 23, check_id, profile=self.ollama)
                parts = api.call_args.args[-1]["messages"][-1]["content"]
                payload = " ".join(p["text"] for p in parts if p["type"]=="text")
                for date in dates:
                    self.assertIn(date, payload)
                self.assertNotIn("соседней карточки", payload)
                row = self.db.execute("""SELECT m.outcome,m.response,g.original_response,g.reason
                    FROM model_assessment m LEFT JOIN model_review_guard g ON g.assessment_id=m.id
                    WHERE m.id=?""", (result["assessment_id"],)).fetchone()
                self.assertEqual(result["outcome"], "uncertain" if needs_guard else model_outcome)
                self.assertEqual(row[0], result["outcome"])
                self.assertEqual(json.loads(row[1]), result["response"])
                expected = {"check_id":check_id, "outcome":model_outcome, "material_ids":["p23-html"],
                            "evidence":explanation}
                if needs_guard:
                    self.assertEqual(json.loads(row[2]), {"identity":result["response"]["identity"],
                                                         "results":[expected]})
                    self.assertEqual(row[3], result["response"]["results"][0]["evidence"])
                    for date in dates:
                        self.assertIn(date, row[3])
                else:
                    self.assertEqual(row[2:], (None,None))
                    self.assertEqual(result["response"]["results"], [expected])

    def test_visible_contact_in_header_is_delivered_to_the_model(self):
        self.db.execute("UPDATE page SET text_page=?",
                        ("<header>Телефон учреждения 777-2121</header><main>Наши занятия</main>",))
        with patch.object(gateway,"_api",return_value=reply()) as api:
            gateway.assess(self.db,23,"garden_phone",profile=self.ollama)
        self.assertIn("777-2121",json.dumps(api.call_args.args[-1],ensure_ascii=False))

    def test_unreadable_visual_is_accounted_without_invoking_model(self):
        self.db.execute("""INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
                           details,full_screenshot,screenshot_mime) VALUES(23,123,'https://example.test/',
                           'partial',1280,800,'{}',?,'image/jpeg')""",(b"not-a-valid-image",))
        with patch.object(gateway,"_api") as api:
            result=gateway.assess(self.db,23,"screen_layout",profile=self.ollama)
            api.assert_not_called()
        self.assertIn(result["outcome"],("error","uncertain"))
        self.assertEqual(self.db.execute("SELECT outcome FROM model_assessment WHERE id=?",
                                         (result["assessment_id"],)).fetchone()[0],result["outcome"])

    def test_image_check_sends_only_material_of_this_page(self):
        output=io.BytesIO()
        Image.new("RGB",(20,30),"white").save(output,format="JPEG")
        self.db.execute("""INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
                           details,full_screenshot,screenshot_mime) VALUES(23,500,'https://example.test/','complete',
                           20,30,'{"full_screenshot_coverage":"document_bounds_captured"}',?,'image/jpeg')""",
                        (output.getvalue(),))
        with patch.object(gateway,"_api",return_value=reply("absent","Наложений нет","p23-full")) as api:
            outcome=gateway.assess(self.db,23,"screen_layout",profile=self.external)
        parts=api.call_args.args[-1]["messages"][-1]["content"]
        self.assertEqual(outcome["outcome"],"absent")
        self.assertTrue(any(p.get("image_url",{}).get("url","").startswith("data:image/jpeg;base64,") for p in parts))
        self.assertNotIn("123456",json.dumps(parts,ensure_ascii=False))
        self.assertEqual(outcome["response"]["results"][0]["material_ids"],["p23-full"])

    def test_pdf_review_renders_every_sheet_and_grounds_result_in_scoped_sheet_ids(self):
        check = {"id":"section_content", "instruction":"Содержит ли файл расписание?", "material_kinds":["pdf"]}
        self.db.execute("UPDATE page_passport SET checks=? WHERE page_id=23", (json.dumps([check]),))
        for pages in (1,3):
            with self.subTest(pages=pages):
                output = io.BytesIO()
                images = [Image.new("RGB",(32,48),color) for color in ("red","blue","green")[:pages]]
                images[0].save(output, format="PDF",save_all=True,append_images=images[1:])
                self.db.execute("""INSERT OR REPLACE INTO page_render(page_id,captured_at,url,status,
                                viewport_width,viewport_height,details,pdf)
                                VALUES(23,500,'https://example.test/','partial',32,48,'{}',?)""",
                                (output.getvalue(),))
                answers = [reply("present" if n==3 else "absent",f"Лист {n}",f"p23-pdf-s{n}") for n in range(1,pages+1)]
                def vision_api(profile, route, key, payload):
                    if route == "/api/show":
                        return {"capabilities":["completion","vision"]}
                    return answers.pop(0)
                with patch.object(gateway,"_api",side_effect=vision_api) as api:
                    result = gateway.assess(self.db,23,"section_content",profile=self.ollama)
                parts = api.call_args.args[-1]["messages"][-1]["content"]
                urls = [p["image_url"]["url"] for p in parts if p["type"]=="image_url"]
                self.assertEqual(sum(call.args[1]=="/chat/completions" for call in api.call_args_list),pages)
                self.assertEqual(len(urls),1)
                self.assertTrue(all(url.startswith("data:image/jpeg;base64,") for url in urls))
                raw = self.db.execute("SELECT request FROM model_assessment WHERE id=?",
                                      (result["assessment_id"],)).fetchone()[0]
                materials = json.loads(raw)["materials"]
                self.assertEqual([m["pdf_page"] for m in materials],list(range(1,pages+1)))
                self.assertTrue(all(m["path"]=="pages/23/page.pdf" and m["pdf_pages"]==pages for m in materials))
                self.assertEqual(result["outcome"], "present" if pages==3 else "absent")

    @unittest.skipUnless(os.environ.get("HACKATON_TEST_ADMIN")=="1","explicit local Tk smoke")
    def test_admin_switches_profile_and_clears_typed_password(self):
        import tkinter as tk
        from admin import Admin
        from home_terms import initialize_terms
        initialize_terms(self.db)
        root=tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        panel=Admin(root,self.db)
        panel._show_profile("compatible")
        panel.endpoint.set(self.external.endpoint)
        panel.model.set(self.external.model)
        panel._save()
        self.assertEqual(settings.get_profile(self.db).provider,"compatible")
        panel.key.set("unsaved-test-value")
        panel._show_profile("ollama")
        self.assertEqual(panel.key.get(),"")
        self.assertEqual(panel.endpoint.get(),settings.DEFAULTS["ollama"].endpoint)
        self.assertIn("Источники",panel.tab)
        from factcheck_sources import get_allowed_sources
        original = get_allowed_sources(self.db)
        panel.publisher_url.set("https://example.org/regional-news")
        panel._edit_source(True)
        expected = "https://example.org/regional-news/"
        self.assertIn(expected,get_allowed_sources(self.db))
        urls = panel.allowed_list.get(0,"end")
        panel.allowed_list.selection_set(urls.index(expected))
        panel._edit_source(False)
        self.assertEqual(get_allowed_sources(self.db),original)


if __name__=="__main__":
    unittest.main()
