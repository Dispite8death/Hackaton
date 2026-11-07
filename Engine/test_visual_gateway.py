"""Exercise the same selected images through model input, persisted answers and offline export."""

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import model_gateway as gateway
import test_staff_visual as staff_fixture
import visual_materials
from page_passport import index_page, questions_for
from page_review import _export_page
from review_contract import allowed_material_ids, validate_response


class VisualGatewayTest(unittest.TestCase):
    def setUp(self):
        # Reuse the archived DOM, sources and real browser-measurement layout from the crop tests.
        self.fixture = staff_fixture.StaffVisualTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db = self.fixture.db
        self.db.execute("INSERT INTO crawl_run(id,id_organization) VALUES(8,1)")
        self.db.execute("""INSERT INTO crawl_fetch(id,run_id,url,section,depth,fetched_at,http_status,page_id)
                           VALUES(11,8,?,'staff',1,80,200,1)""", (staff_fixture.SITE,))
        index_page(self.db,1)
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with io.BytesIO() as out:
            Image.new("RGB", (48,32), "green").save(out,format="JPEG")
            self.guide_bytes=out.getvalue()
        self.guide_render=self.enterContext(patch.object(visual_materials,"guide_illustration",side_effect=self.illustration))
        self.api=self.enterContext(patch.object(gateway,"_api",side_effect=AssertionError("Unexpected model request")))

    def illustration(self, check):
        if not check["id"].startswith(("staff_portrait_","staff_sharp_")):
            return None
        mid="guide-fixture-p14-fig24"
        meta={"id":mid,"kind":"guide","path":f"guide/{mid}.jpg","mime":"image/jpeg","source_document":"case.pdf",
              "page_number":14,"figure":24,"sha256":hashlib.sha256(self.guide_bytes).hexdigest(),
              "width":48,"height":32,"for_checks":[check["id"]]}
        return meta,self.guide_bytes,"Памятка, рис. 24 (с. 14)."

    def add_second_card(self):
        other=staff_fixture.SITE+"ports/two.png"
        self.db.execute("""INSERT INTO staff_observation(id,page_id,card_selector,full_name,role,is_head,
            photo_url,photo_state,method) VALUES(4,1,'#other','Пётр Петров','Учитель',0,?,'linked','template')""",(other,))
        self.db.execute("""INSERT INTO staff_element(id,observation_id,field,selector,expected_value)
                           VALUES(2,4,'photo','#unrelated',?)""",(other,))
        self.fixture.store_original(url=other)

    def check(self, key):
        return next(c for c in questions_for(self.db,1) if c["id"]==key)

    def model_answer(self, key, outcome, refs):
        answer = {"outcome":outcome,"material_ids":refs,"evidence":"Наблюдение в указанной области."}
        self.api.side_effect=None
        self.api.return_value={"choices":[{"message":{"content":json.dumps(answer)}}]}
        return gateway.assess(self.db,1,key,key="")

    def test_separate_target_and_guides_in_model_payload_and_saved_answer(self):
        self.fixture.store_original()
        self.add_second_card()
        check=self.check("staff_portrait_3")
        bundle=visual_materials.visual_bundle(self.db,1,check)
        self.guide_render.assert_called_once_with(check)
        self.guide_render.reset_mock()
        target,guide=[m for m,_,_ in bundle]
        self.assertEqual((target["kind"],target["observation_id"],target["source"]),("image",3,"original"))
        self.assertEqual((guide["kind"],guide["page_number"],guide["figure"],guide["for_checks"]),
                         ("guide",14,24,[check["id"]]))
        materials,parts=gateway._inputs(self.db,1,check)
        self.guide_render.assert_called_once_with(check)
        self.assertEqual([(m["id"],m["kind"],m["path"]) for m in materials],
                         [(m["id"],m["kind"],m["path"]) for m,_,_ in bundle])
        self.assertEqual([p["type"] for p in parts],["text","image_url"]*2)
        self.assertEqual(allowed_material_ids(check,materials),[target["id"]])
        result=self.model_answer(check["id"],"present",[target["id"]])
        self.assertEqual(result["outcome"],"present",result["error"])
        payload=self.api.call_args_list[0].args[3]
        schema=payload["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["material_ids"]["items"]["enum"],[target["id"]])
        self.assertEqual(payload["messages"][-1]["content"][1:],parts)
        raw,reply=self.db.execute("SELECT request,response FROM model_assessment WHERE id=?",
                                  (result["assessment_id"],)).fetchone()
        validate_response(json.loads(raw),json.loads(reply))
        self.guide_render.reset_mock()
        other, _=gateway._inputs(self.db,1,self.check("staff_portrait_4"),include_guide=False)
        self.guide_render.assert_not_called()
        for forbidden in (guide["id"],other[0]["id"]):
            with self.subTest(material=forbidden):
                bad=self.model_answer(check["id"],"present",[forbidden])
                self.assertEqual(bad["outcome"],"error")

    def verification(self, result):
        row=self.db.execute("""SELECT status,guided_pass,target_pass FROM model_visual_verification
                               WHERE assessment_id=?""", (result["assessment_id"],)).fetchone()
        return row[0],json.loads(row[1]),json.loads(row[2]) if row[2] else None

    def responses(self, first, second):
        def reply(value, usage):
            return {"choices":[{"message":{"content":value}}],"usage":usage}
        self.api.reset_mock()
        self.api.side_effect=[reply(json.dumps(first,ensure_ascii=False),{"prompt_tokens":15,"completion_tokens":5,"total_tokens":20}),
                              second if isinstance(second,Exception) else reply(second,{"prompt_tokens":8,"completion_tokens":3,"total_tokens":11})]

    def test_independent_confirmation_cannot_inherit_school_name_from_example(self):
        self.fixture.store_original()
        check=self.check("staff_portrait_3")
        materials,_=gateway._inputs(self.db,1,check)
        target,guide=materials
        first={"outcome":"present","material_ids":[target["id"]],"evidence":"Школа из иллюстрации №999"}
        second={**first,"evidence":"На фотографии учреждения видна фигура на однотонном фоне."}
        self.responses(first,json.dumps(second,ensure_ascii=False))
        result=gateway.assess(self.db,1,check["id"],key="")
        self.assertEqual(result["outcome"],"present",result["error"])
        self.assertEqual(result["response"]["results"][0]["evidence"],second["evidence"])
        self.assertEqual(self.api.call_count,2)
        one,two=[call.args[3] for call in self.api.call_args_list]
        self.assertEqual(one["model"],two["model"])
        self.assertEqual(two["messages"][-1]["content"],one["messages"][-1]["content"][:3])
        self.assertNotIn(guide["id"],json.dumps(two,ensure_ascii=False))
        self.assertNotIn(first["evidence"],json.dumps(two,ensure_ascii=False))
        status,guided,blind=self.verification(result)
        self.assertEqual(status,"agreed")
        self.assertEqual((guided["material_ids"],blind["material_ids"]),([target["id"],guide["id"]],[target["id"]]))
        self.assertEqual((json.loads(guided["raw_response"]),json.loads(blind["raw_response"])),(first,second))
        self.assertEqual((guided["usage"]["total_tokens"],blind["usage"]["total_tokens"]),(20,11))
        saved=self.db.execute("SELECT usage_json FROM model_assessment WHERE id=?",(result["assessment_id"],)).fetchone()[0]
        self.assertEqual(json.loads(saved),{"prompt_tokens":23,"completion_tokens":8,"total_tokens":31})

    def test_disagreement_or_invalid_blind_response_preserves_targets_and_records_failure(self):
        self.fixture.store_original()
        check=self.check("staff_portrait_3")
        materials,_=gateway._inputs(self.db,1,check)
        target,guide=materials
        first={"outcome":"absent","material_ids":[target["id"]],"evidence":"Силуэт на фотографии"}
        for second,status in ((json.dumps({**first,"outcome":"present"}),"disagreed"),
                              (json.dumps({**first,"material_ids":[guide["id"]]}),"target_error"),
                              ("{incomplete","target_error"),(OSError("API unavailable"),"target_error")):
            with self.subTest(second=second):
                self.responses(first,second)
                result=gateway.assess(self.db,1,check["id"],key="")
                self.assertEqual(result["outcome"],"uncertain",result["error"])
                self.assertEqual(result["response"]["results"][0]["material_ids"],[target["id"]])
                recorded,_,blind=self.verification(result)
                self.assertEqual(recorded,status)
                self.assertEqual(blind["raw_response"],second if isinstance(second,str) else None)
                self.assertEqual(bool(blind["error"]),status=="target_error")
                self.assertEqual(self.api.call_count,2)

    def test_blind_answer_quoting_an_unverified_sign_needs_review(self):
        self.fixture.store_original()
        check = self.check("staff_portrait_3")
        target = gateway._inputs(self.db,1,check)[0][0]["id"]
        first = {"outcome":"present","material_ids":[target],"evidence":"Виден портрет на светлом фоне."}
        for evidence, status in (
            ("Видна вывеска «Название учреждения».", "target_guarded"),
            ("Выполнено условие «виден портрет».", "agreed"),
        ):
            with self.subTest(evidence=evidence):
                second = dict(first,evidence=evidence)
                self.responses(first,json.dumps(second,ensure_ascii=False))
                result = gateway.assess(self.db,1,check["id"],key="")
                self.assertEqual(self.verification(result)[0],status)
                self.assertEqual(result["outcome"],"uncertain" if status == "target_guarded" else "present")

    def test_first_uncertain_and_portrait_guard_skip_second_request(self):
        self.fixture.store_original()
        key="staff_portrait_3"
        target=gateway._inputs(self.db,1,self.check(key),include_guide=False)[0][0]["id"]
        result=self.model_answer(key,"uncertain",[target])
        self.assertEqual(self.verification(result)[::2],("guided_uncertain",None))
        self.assertEqual(self.api.call_count,1)
        self.api.reset_mock()
        guarded=self.model_answer("staff_sharp_3","present",[target])
        self.assertEqual(guarded["outcome"],"uncertain",guarded["error"])
        self.assertEqual(self.verification(guarded)[::2],("guard_skipped",None))
        self.assertEqual(self.api.call_count,1)

    def test_not_applicable_is_also_checked_and_invalid_first_pass_is_recorded(self):
        self.fixture.store_original()
        key="staff_sharp_3"
        target,guide=gateway._inputs(self.db,1,self.check(key))[0]
        out=self.model_answer(key,"not_applicable",[target["id"]])
        self.assertEqual((out["outcome"],self.api.call_count),("not_applicable",2))
        self.assertEqual(self.verification(out)[0],"agreed")
        self.api.reset_mock()
        bad=self.model_answer(key,"present",[guide["id"]])
        status,first,second=self.verification(bad)
        self.assertEqual((bad["outcome"],status,second,self.api.call_count),("error","guided_error",None,1))
        self.assertTrue(first["error"])
        self.assertEqual(json.loads(first["raw_response"])["material_ids"],[guide["id"]])

    def test_conditional_answer_requires_target_and_original_cannot_replace_displayed_crop(self):
        self.fixture.store_original()
        crop_check=self.check("staff_head_in_frame_3")
        out=gateway.assess(self.db,1,crop_check["id"],key="")
        self.assertEqual(out["outcome"],"uncertain",out["error"])
        self.api.assert_not_called()
        self.guide_render.assert_not_called()
        self.assertEqual(out["response"]["results"][0]["material_ids"],[])
        self.fixture.render()
        cropped, _=gateway._inputs(self.db,1,crop_check,include_guide=False)
        full, _=gateway._inputs(self.db,1,self.check("staff_portrait_3"),include_guide=False)
        self.assertEqual((full[0]["source"],cropped[0]["source"]),("original","viewport_crop"))
        self.assertNotEqual(full[0]["sha256"],cropped[0]["sha256"])
        ref=cropped[0]["id"]
        self.assertEqual(self.model_answer(crop_check["id"],"not_applicable",[ref])["outcome"],"not_applicable")
        for key,refs in ((crop_check["id"],[]),(crop_check["id"],[full[0]["id"]]),("staff_portrait_3",[full[0]["id"]])):
            with self.subTest(check=key,refs=refs):
                self.assertEqual(self.model_answer(key,"not_applicable",refs)["outcome"],"error")

    def test_no_bound_target_neither_model_nor_guide_is_requested(self):
        self.add_second_card()
        check=self.check("staff_portrait_3")
        self.assertEqual(gateway._inputs(self.db,1,check),([],[]))
        out=gateway.assess(self.db,1,check["id"],key="")
        self.assertEqual(out["outcome"],"uncertain",out["error"])
        self.api.assert_not_called()
        self.guide_render.assert_not_called()

    def test_export_binds_each_file_to_its_question_and_validates_roundtrip(self):
        self.fixture.store_original()
        self.fixture.render()
        self.add_second_card()
        page=_export_page(self.db,1,self.folder,{})
        request=json.loads((self.folder/page["request_file"]).read_text(encoding="utf-8"))
        self.assertTrue(json.loads((self.folder/page["schema_file"]).read_text(encoding="utf-8")))
        self.assertEqual(request["identity"],{"organization_id":1,"run_id":8,"page_id":1})
        self.assertEqual(json.loads(json.dumps(page,ensure_ascii=False))["page_id"],1)
        body,materials=request["checks"],request["materials"]
        guides=[m for m in materials if m["kind"]=="guide"]
        self.assertTrue(guides)
        for m in (m for m in materials if m["kind"] in ("image","guide") and "for_checks" in m):
            data=(self.folder/m["path"]).read_bytes()
            if m["kind"]=="guide":
                self.assertEqual(data,self.guide_bytes)
            else:
                self.assertEqual(hashlib.sha256(data).hexdigest(),m["sha256"])
        answer={"identity":request["identity"],"results":[dict(check_id=c["id"],outcome="uncertain",
               material_ids=[],evidence="Недостаточно сведений") for c in body]}
        subject=next(r for r in answer["results"] if r["check_id"]=="staff_portrait_3")
        def refs(key):
            return allowed_material_ids(next(c for c in body if c["id"]==key),materials)
        subject.update(outcome="present",material_ids=refs("staff_portrait_3"))
        self.assertEqual(len(subject["material_ids"]),1)
        validate_response(request,answer)
        for wrong in (refs("staff_portrait_4"),refs("staff_head_in_frame_3"),[guides[0]["id"]]):
            subject["material_ids"]=wrong
            with self.subTest(refs=wrong), self.assertRaises(ValueError):
                validate_response(request,answer)
