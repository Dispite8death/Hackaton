import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from PIL import Image

import model_gateway as gateway
from config import SCHEMA_SQL
from page_evidence import Coverage, Piece, pieces, print_witness, text_witness
from review_contract import allowed_material_ids, make_request


class PageEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.execute("INSERT INTO organization VALUES(1,'Школа','https://example.test','school')")
        self.db.execute("INSERT INTO crawl_run(id,id_organization) VALUES(1,1)")
        self.db.execute("INSERT INTO page VALUES(1,'https://example.test/',1,100,'<main>Программа начальной школы</main>')")
        self.db.execute("""INSERT INTO crawl_fetch(run_id,url,section,depth,fetched_at,page_id)
                         VALUES(1,'https://example.test/','programs',1,100,1)""")
        self.identity = {"organization_id":1,"page_id":1,"run_id":1}

    def check(self, sources, kinds, aggregation="exists"):
        return {"id":"program_content","instruction":"Опубликована ли программа?",
                "material_kinds":kinds,"evidence_policy":{"sources":sources,"aggregation":aggregation}}

    def test_schedule_times_must_be_present_in_the_text_layer(self):
        piece = Piece([], [], "Расписание седьмого класса на понедельник. 09:00 Математика. 10:00 История.")
        for key in ("section_content", "schedule_body_lessons"):
            result = {"outcome":"present", "evidence":"В 9:00 начинается математика."}
            self.assertIsNone(text_witness(piece, result, key))
            result["evidence"] = "Урок истории идёт с 10:00 до 11:00."
            self.assertIn("время", text_witness(piece, result, key))

    def test_entire_middle_of_long_text_and_boundaries_are_kept(self):
        body = "Устав "*900+"Сведения об уникальном событии"+" описание"*1000
        self.db.execute("UPDATE page SET text_page=?",("<main>"+body+"</main>",))
        check = self.check(["page_text"],["html","dom"])
        evidence = list(pieces(self.db,1,check,Coverage(),gateway._text_material))
        self.assertGreater(len(evidence),3)
        self.assertEqual(evidence[0].materials[0]["text_start"],0)
        self.assertEqual(evidence[-1].materials[0]["text_end"],len(body))
        self.assertTrue(any("уникальном событии" in chunk.source_text for chunk in evidence))
        for first,second in zip(evidence,evidence[1:]):
            self.assertLess(second.materials[0]["text_start"],first.materials[0]["text_end"])
        self.assertEqual(len({p.materials[0]["source_sha256"] for p in evidence}),1)
        self.assertTrue(all(p.materials[0]["for_checks"]==[check["id"]] for p in evidence))

    def test_screen_tiling_and_css_coverage_restrict_absence(self):
        with io.BytesIO() as out:
            Image.new("RGB",(1280,2450),"navy").save(out,"JPEG")
            raw = out.getvalue()
        self.db.execute("""INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
          details,full_screenshot,screenshot_mime) VALUES(1,100,'https://example.test/','partial',1280,800,?,?,'image/jpeg')""",
          (json.dumps({"full_screenshot_coverage":"inner_scroll_requires_review"}),raw))
        check = self.check(["page_screen"],["full"])
        cov = Coverage()
        images = list(pieces(self.db,1,check,cov,gateway._text_material))
        self.assertEqual(len(images),3)
        self.assertEqual(images[-1].materials[0]["crop_box"][-1],2450)
        self.assertTrue(all(len(p.parts)==2 and p.parts[1]["type"]=="image_url" for p in images))
        absent = [{"outcome":"absent","material_ids":[p.materials[0]["id"]],"evidence":"Ориентир отсутствует."} for p in images]
        self.assertEqual(gateway._aggregate_parts(absent,cov.gaps,"exists")["outcome"],"uncertain")
        self.assertEqual(gateway._aggregate_parts(absent,[],"exists")["outcome"],"absent")
        witness = dict(absent[1],outcome="present")
        self.assertEqual(gateway._aggregate_parts(absent+[witness],cov.gaps,"exists"),witness)

    def test_html_plus_pdf_retain_each_page_including_scanned_sheets(self):
        images = [Image.new("RGB",(40,65),color) for color in ["red","green","blue","white"]]
        with io.BytesIO() as out:
            images[0].save(out,"PDF",save_all=True,append_images=images[1:])
            self.db.execute("""INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
              details,pdf) VALUES(1,100,'https://example.test/','partial',1280,800,'{}',?)""",(out.getvalue(),))
        check = self.check(["page_text","page_pdf"],["html","pdf"])
        evidence = list(pieces(self.db,1,check,Coverage(),gateway._text_material))
        self.assertEqual([p.materials[0].get("pdf_page") for p in evidence],[None,1,2,3,4])
        self.assertTrue(all(p.parts[-1]["type"]=="image_url" for p in evidence[1:]))
        req = make_request(self.identity,[check],[m for p in evidence for m in p.materials])
        self.assertEqual(allowed_material_ids(check,req["materials"]),[p.materials[0]["id"] for p in evidence])

    def test_print_loss_needs_visible_quote_and_both_materials(self):
        mats = [{"id":"screen"},{"id":"pdf","unmatched_dom_anchors":["Обучение в первую смену"]}]
        answer = {"outcome":"present","material_ids":["screen","pdf"],"evidence":"На экране «Обучение в первую смену»."}
        self.assertTrue(print_witness(mats,answer))
        self.assertFalse(print_witness(mats,dict(answer,evidence="Меню иначе оформлено.")))
        self.assertFalse(print_witness(mats,dict(answer,material_ids=["screen"])))
        self.assertFalse(print_witness([mats[0],dict(mats[1],unmatched_dom_anchors=[])],answer))

    def test_print_comparison_with_scanned_pdf_never_claims_missing_text_layer_matches(self):
        with io.BytesIO() as raster, io.BytesIO() as pdf:
            img=Image.new("RGB",(32,60),"navy")
            img.save(raster,"PNG")
            img.save(pdf,"PDF")
            self.db.execute("""INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
                details,pdf,full_screenshot,screenshot_mime,rendered_html) VALUES
                (1,100,'https://example.test/','complete',32,60,?,?,?,'image/png',?)""",
                (json.dumps({"pdf_media":"screen CSS"}),pdf.getvalue(),raster.getvalue(),
                 "<main><p>Очень важная программа для учебного процесса</p></main>"))
        check=self.check(["print_comparison"],["full","pdf"])
        gaps=Coverage()
        evidence=list(pieces(self.db,1,check,gaps,gateway._text_material))
        self.assertEqual(len(evidence),1)
        self.assertGreaterEqual(len(gaps.gaps),1)
        self.assertEqual([m["kind"] for m in evidence[0].materials],["full","pdf"])
        self.assertEqual(evidence[0].materials[1]["unmatched_dom_anchors"],[])
        self.assertEqual(evidence[0].materials[1]["pdf_media"],"screen CSS")

    def test_text_citations_including_year_and_time_are_grounded(self):
        part=Piece([],[],"Экскурсия 3 сентября 2025 года. Опубликовано 04.09.2025 в 15:00.")
        def verdict(claim,key):
            return text_witness(part,{"outcome":"present","evidence":claim},key)
        self.assertIsNone(verdict("«Экскурсия 3 сентября 2025 года».","event_date_in_text"))
        self.assertIsNotNone(verdict("«Экскурсия 3 сентября»","event_date_in_text"))
        self.assertIsNotNone(verdict("«Экскурсия 4 сентября 2025 года»","event_date_in_text"))
        self.assertIsNotNone(verdict("Дата публикации 04.09.2025","publication_time_evidence"))
        self.assertIsNone(verdict("Время размещения «15:00».","publication_time_evidence"))

    def test_original_only_for_selected_attachment(self):
        q = self.check(["attachment"],["attachment"])
        q["attachment"]={"id":7,"role":"schedule"}
        meta={"id":"p1-attachment-7-test","kind":"attachment","path":"attachments/content.docx",
              "format":"docx","attachment_id":7,"title":"Расписание"}
        with patch("attached_files.attachment_content",return_value=(meta,b"original bytes")), patch(
                "attached_files.office_text",return_value="Понедельник. Математика с 8:30."):
            cov=Coverage()
            evidence=list(pieces(self.db,1,q,cov,gateway._text_material))
        self.assertEqual(len(evidence),1)
        self.assertTrue(cov.gaps)
        material=evidence[0].materials[0]
        self.assertEqual(allowed_material_ids(q,[material,dict(material,id="foreign",attachment_id=8)]),[material["id"]])
        self.assertIn("Расписание",evidence[0].parts[0]["text"])

    def test_professional_courses_and_unverified_scan_transcription(self):
        part=Piece([],[],"Удостоверение о повышении квалификации. Курс занятий 24 часа.")
        answer={"outcome":"present","evidence":"Удостоверение, 24 часа курсов по педагогике."}
        self.assertIsNotNone(text_witness(part,answer,"staff_named_education"))
        self.assertIsNotNone(text_witness(Piece([],[]),dict(answer,evidence="В 1-м классе физика."),
                                         "attachment_schedule_123"))
        part.source_text="Уровень образования — высшее, диплом специалиста."
        answer["evidence"]="Указано высшее образование сотрудника."
        self.assertIsNone(text_witness(part,answer,"staff_named_education"))
