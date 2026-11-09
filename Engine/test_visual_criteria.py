import sqlite3
import unittest
import json
from unittest.mock import patch

from config import SCHEMA_SQL
from visual_criteria import SERIES_LIMIT, visual_questions


class VisualCriteriaTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.execute("INSERT INTO organization VALUES(1,'Школа','https://school.example','school')")
        self.db.execute("INSERT INTO organization VALUES(2,'Детский сад','https://garden.example','kindergarten')")
        for pid,org,kind in [(1,1,"home"),(2,2,"home"),(3,1,"staff_directory"),(4,1,"history")]:
            self.db.execute("INSERT INTO page VALUES(?, 'https://example.test/', ?,1,'<main></main>')",(pid,org))
            self.db.execute("INSERT INTO page_passport VALUES(?,?, '','[]','','[]')",(pid,kind))

    def person(self,oid,*,pid=3,photo=None,name=None,profile=None,head=False,missing=False):
        self.db.execute("""INSERT INTO staff_observation(id,page_id,card_selector,full_name,role,is_head,photo_url,
                          photo_state,profile_url,method) VALUES(?,?,?,?,?,?,?,?,?,'template')""",
                        (oid,pid,f"#p{oid}",name or f"Сотрудник {oid}", "Директор" if head else "Педагог",
                         int(head),None if missing else (photo or f"/img/{oid}.jpg"),"absent" if missing else "linked",
                         profile or f"/staff/{oid}"))

    def questions(self,pid):
        questions = visual_questions(self.db,pid)
        indexed = {question["id"]:question for question in questions}
        self.assertEqual(len(questions),len(indexed))
        self.assertTrue(all(q["material_kinds"]==["image"] for q in questions))
        return indexed

    def test_home_types_and_conditional_properties_are_school_specific(self):
        checks = self.questions(1)
        self.assertIn("групповой портрет",checks["school_main_photo"]["instruction"])
        for key in ["school_main_building","school_main_logo","school_main_group"]:
            self.assertFalse(checks[key]["visual"]["include_in_score"])
        for key in ["school_main_horizon","school_main_building_visible","school_main_group_light",
                    "school_main_group_attire","canteen_level","canteen_framing"]:
            self.assertTrue(checks[key]["visual"]["conditional"])
            self.assertIn("not_applicable",checks[key]["instruction"])
        self.assertEqual(checks["canteen_photo"]["visual"]["reference_pages"],[16])
        self.assertFalse(self.questions(2))
        self.assertFalse(self.questions(4))
        self.assertEqual(visual_questions(self.db,999),[])

    def test_head_reference_is_optional_and_gardens_only_require_a_person(self):
        self.person(10,pid=2,head=True,missing=True)
        self.person(11,pid=2)
        checks = self.questions(2)
        self.assertEqual(set(checks),{"staff_portrait_10","staff_portrait_11","advisory_head_clothing_10",
                                      "advisory_flag_10","advisory_flag_stripes_10"})
        self.assertEqual(checks["staff_portrait_10"]["visual"]["rule_scope"],"case")
        self.assertEqual(checks["staff_portrait_10"]["visual"]["reference_pages"],[])
        for key,q in checks.items():
            if key.startswith("advisory_"):
                self.assertTrue(q["visual"]["manual_only"])
                self.assertFalse(q["visual"]["include_in_score"])
                self.assertIn("ОМСУ",q["instruction"])
        self.assertTrue(checks["advisory_flag_stripes_10"]["visual"]["conditional"])

    def test_series_uses_single_proven_container_and_distinct_people_and_images(self):
        self.db.execute("""UPDATE page SET text_page='<main>
            <section><ul><li id="p1">A</li></ul></section>
            <section><ul><li id="p2">B</li><li id="p3">B</li><li id="p4">B</li>
                <li id="p5">B</li><li id="p6">C</li><li id="p7">D</li><li id="p8">E</li></ul></section>
            </main>' WHERE id=3""")
        self.person(1)
        self.person(2,photo="/same.jpg",name="Иванов Антон Иванович",profile="/profiles/b")
        self.person(3,photo="/same.jpg")
        self.person(4,name="Антон Иванович Иванов")
        self.person(5,profile="/profiles/b")
        self.person(6)
        self.person(7)
        self.person(8)
        checks = self.questions(3)
        selection = checks["staff_series_scale_2"]["visual"]
        self.assertEqual(selection["observation_ids"],[2,6,7])
        self.assertEqual(len(selection["observation_ids"]),SERIES_LIMIT)
        self.assertEqual(selection["selection_mode"],"unverified")
        self.assertIn("Выберите uncertain",checks["staff_series_scale_2"]["instruction"])
        self.assertTrue(selection["displayed"])
        self.assertTrue(checks["staff_series_crowns_2"]["visual"]["displayed"])
        self.assertTrue(checks["staff_head_in_frame_6"]["visual"]["displayed"])
        self.assertFalse(checks["staff_sharp_6"]["visual"]["displayed"])
        self.assertIn("выборку", checks["staff_series_background_2"]["instruction"])
        self.assertEqual(checks["staff_portrait_8"]["visual"]["observation_ids"],[8])

    def test_standalone_cards_and_separate_lists_are_never_compared(self):
        self.db.execute("""UPDATE page SET text_page='<main><section><ul><li id="p1">A</li></ul></section>
                           <ul><li id="p2">B</li></ul><article id="p3">C</article></main>' WHERE id=3""")
        for oid in (1,2,3):
            self.person(oid)
        self.assertFalse(any(q["visual"]["target"]=="staff_series" for q in self.questions(3).values()))

    def test_displayed_series_skips_partial_unbound_and_repeated_photos(self):
        # IDs 1–3 have saved original files, yet their displayed frames are partially obscured.
        # ID 4 is in a different CSS row. ID 5 has plausible SQL measurements but no valid image.
        html = "<main><ul>"+"".join(f'<li id="p{i}">Сотрудник {i}</li>' for i in range(1,10))+"</ul></main>"
        self.db.execute("UPDATE page SET text_page=? WHERE id=3",(html,))
        for oid in range(1,10):
            self.person(oid)
            top = 50 if oid<=3 else 180 if oid==4 else 360
            rect = {"rect":[(oid%5)*40,top,32,48]}
            self.db.execute("""INSERT INTO staff_element(observation_id,field,selector,expected_value,
                               visual_status,visual_details,render_source,css_loaded)
                               VALUES(?,'photo',?,?,?,?,'live',1)""",
                            (oid,f"#img{oid}",f"/img/{oid}.jpg","partial" if oid<=3 else "out_of_view",json.dumps(rect)))
            if oid<=3:
                self.db.execute("INSERT INTO home_image_download VALUES(3,?,1,?,200,'saved',NULL,?)",
                                (f"/img/{oid}.jpg",f"/img/{oid}.jpg",str(oid)*64))
        def image(conn,page_id,oid,*,displayed):
            self.assertEqual(page_id,3)
            if displayed and oid==5:
                return None
            if displayed:
                self.assertGreater(oid,3)
            # Distinct URLs can serve identical images. IDs 6 and 7 are duplicates.
            return {"source":"document_crop" if displayed else "original","sha256":str(6 if oid==7 else oid)*64}
        with patch("visual_criteria.staff_visual_material",side_effect=image):
            checks=self.questions(3)
        for key in ("scale","crowns"):
            picked=checks[f"staff_series_{key}_6"]["visual"]
            self.assertEqual(picked["observation_ids"],[6,8,9])
            self.assertEqual(picked["selection_mode"],"same_css_row")
        background=checks["staff_series_background_1"]["visual"]
        self.assertEqual(background["observation_ids"],[1,2,3])
        self.assertEqual(background["selection_mode"],"originals")

    def test_displayed_does_not_join_adjacent_lists_even_when_css_positions_match(self):
        html = '<main><ul><li id="p1"></li><li id="p2"></li></ul><ul><li id="p3"></li><li id="p4"></li></ul></main>'
        self.db.execute("UPDATE page SET text_page=? WHERE id=3",(html,))
        for oid in range(1,5):
            self.person(oid)
            self.db.execute("""INSERT INTO staff_element(observation_id,field,selector,expected_value,visual_status,
                               visual_details,render_source,css_loaded) VALUES(?,'photo',?,?,'visible',?,'live',1)""",
                            (oid,f"#img{oid}",f"/img/{oid}.jpg",json.dumps({"rect":[oid*25,10,30,40]})))
        def image(conn,pid,oid,*,displayed):
            return {"source":"viewport_crop","sha256":str(oid)*64} if oid in (2,3,4) else None
        with patch("visual_criteria.staff_visual_material",side_effect=image):
            checks=self.questions(3)
        self.assertEqual(checks["staff_series_scale_3"]["visual"]["observation_ids"],[3,4])


if __name__=="__main__":
    unittest.main()
