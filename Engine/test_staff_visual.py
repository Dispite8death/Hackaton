import hashlib
import io
import json
import sqlite3
import unittest
from copy import deepcopy

from PIL import Image, ImageDraw

from config import SCHEMA_SQL
from staff_visual import staff_crop_box, staff_visual_material


SITE = "https://school.example/"
PHOTO = SITE+"ports/one.png"
HTML = ('<main><div class="gw-person-card" id="card"><span>Анна Иванова</span>'
        '<img id="portrait" src="/ports/one.png"></div>'
        '<div class="gw-person-card" id="other">Пётр Петров<img id="unrelated" src="/ports/two.png"></div></main>')


def png(width, height, rect=None):
    image = Image.new("RGB",(width,height),"blue")
    if rect:
        ImageDraw.Draw(image).rectangle(rect,fill="red")
    output = io.BytesIO()
    image.save(output,format="PNG")
    return output.getvalue()


class StaffVisualTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.execute("INSERT INTO organization VALUES(1,'Школа',?,'school')",(SITE,))
        self.db.execute("INSERT INTO page VALUES(1,?,1,80,?)",(SITE,HTML))
        self.db.execute("INSERT INTO page VALUES(2,?,1,70,?)",(SITE,HTML))
        self.db.execute("""INSERT INTO staff_observation(id,page_id,card_selector,full_name,role,is_head,
            photo_url,photo_state,method) VALUES(3,1,'#card','Анна Иванова','Учитель',0,?,'linked','template')""",(PHOTO,))
        self.db.execute("""INSERT INTO staff_element(id,observation_id,field,selector,expected_value)
            VALUES(1,3,'photo','#portrait',?)""",(PHOTO,))

    def store_original(self, page=1, url=PHOTO):
        data = png(40,50)
        digest = hashlib.sha256(data).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO home_image_asset VALUES(?,'image/png',40,50,?)",(digest,data))
        self.db.execute("INSERT INTO home_image_download VALUES(?,?,200,?,200,'saved',NULL,?)",(page,url,url,digest))

    def render(self, *, offscreen=False, subject="photo"):
        rect = [20,150 if offscreen else 25,40,50]
        status = "out_of_view" if offscreen else "visible"
        facts = {"expected_match":True,"hit_test_uncertain":False,"reason":"outside_viewport" if offscreen
                 else "geometry_clear_at_sampled_points","matches":1,"display":"block","visibility":"visible",
                 "effective_opacity":1,"viewport":[100,100],"scroll":[3,10],"rect":rect,"samples":0 if offscreen else 9,
                 "clear":0 if offscreen else 9,"covered":0,"uncertain":0}
        if not offscreen:
            facts.update(viewport_rect=rect,in_view_fraction=1)
        selector, value = ("#portrait",PHOTO) if subject=="photo" else ("#card","Анна Иванова")
        measurement = {"field":subject,"selector":selector,"expected_value":value,"card_selector":"#card",
                       "status":status,"details":facts}
        details = {"staff_source":"rendered_dom","document_complete_for_extraction":True,
                   "screen":{"scrollX":3,"scrollY":10,"overlayCandidates":[],"clippedCount":0,"clipped":[],
                             "scrollableCount":0,"scrollable":[]},
                   "staff_measurements":[measurement],"full_screenshot_coverage":"document_bounds_captured",
                   "full_screenshot_bounds":{"x":5,"y":7,"width":120,"height":250,"scale":1}}
        if subject=="card":
            self.db.execute("UPDATE page SET text_page=replace(text_page,?,'') WHERE id=1",
                            ('<img id="portrait" src="/ports/one.png">',))
            self.db.execute("UPDATE staff_observation SET photo_url=NULL,photo_state='absent' WHERE id=3")
        self.db.execute("""UPDATE staff_element SET field=?,selector=?,expected_value=?,visual_status=?,visual_details=?,
                        checked_at=100,render_url=?,render_source='live',css_loaded=1,viewport_width=100,viewport_height=100
                        WHERE id=1""",(subject,selector,value,status,json.dumps(facts),SITE))
        self.db.execute("""INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
                        css_loaded,details,rendered_html,screenshot_mime,screenshot,full_screenshot)
                        SELECT 1,100,?,'complete',100,100,1,?,p.text_page,'image/png',?,?
                        FROM page p WHERE p.id=1""",(SITE,json.dumps(details),png(100,100,(20,25,59,74)),
                        png(120,250,(18,153,57,202))))
        return facts, details

    def test_original_requires_same_snapshot_url_and_employee_dom(self):
        self.store_original(page=2)
        self.store_original(url=SITE+"ports/two.png")
        self.assertIsNone(staff_visual_material(self.db,1,3))
        self.assertIsNone(staff_visual_material(self.db,2,3))
        self.store_original()
        original = staff_visual_material(self.db,1,3)
        self.assertEqual((original["source"],original["captured_at"],original["photo_url"]),("original",200,PHOTO))
        self.assertIn("p1-staff-3-original",original["material_id"])
        self.assertEqual(hashlib.sha256(original["data"]).hexdigest(),original["sha256"])
        self.assertIsNone(staff_visual_material(self.db,1,3,displayed=True))
        for html in (HTML.replace('id="portrait"','id="other-img"'),
                     HTML.replace('id="portrait"','class="banner" id="portrait"'),
                     HTML.replace("Анна Иванова","Пётр Петров"),
                     HTML.replace("/ports/one.png","/ports/two.png")):
            self.db.execute("UPDATE page SET text_page=? WHERE id=1",(html,))
            self.assertIsNone(staff_visual_material(self.db,1,3))

    def test_displayed_requires_crop_even_if_original_exists(self):
        self.render()
        self.store_original()
        self.assertEqual(staff_visual_material(self.db,1,3)["source"],"original")
        crop = staff_visual_material(self.db,1,3,displayed=True)
        self.assertEqual((crop["source"],crop["width"],crop["height"],crop["captured_at"]),
                         ("viewport_crop",40,50,100))
        with Image.open(io.BytesIO(crop["data"])) as img:
            self.assertEqual(img.getcolors(),[(2000,(255,0,0))])
        self.assertTrue(crop["provenance"]["hit_test_available"])
        self.db.execute("UPDATE staff_element SET checked_at=99")
        self.assertIsNone(staff_visual_material(self.db,1,3,displayed=True))

    def test_document_coordinates_account_for_scroll_and_clip_origin(self):
        _, details = self.render(offscreen=True)
        crop = staff_visual_material(self.db,1,3,displayed=True)
        self.assertEqual(crop["source"],"document_crop")
        self.assertEqual(crop["provenance"]["pixel_box"],[18,153,58,203])
        self.assertFalse(crop["provenance"]["hit_test_available"])
        with Image.open(io.BytesIO(crop["data"])) as img:
            self.assertEqual(img.getcolors(),[(2000,(255,0,0))])
        for change in ("clipped_parent","different_scroll","different_element","unverified_css","missing_geometry"):
            with self.subTest(change=change):
                evidence = deepcopy(details)
                if change=="clipped_parent":
                    evidence["screen"].update(clippedCount=1,clipped=[{"selector":"#card"}])
                if change=="different_scroll":
                    evidence["screen"]["scrollY"] += 1
                if change=="different_element":
                    evidence["staff_measurements"][0]["selector"] = "#unrelated"
                if change=="missing_geometry":
                    del evidence["full_screenshot_bounds"]
                self.db.execute("UPDATE page_render SET details=?,css_loaded=?",
                                (json.dumps(evidence),int(change!="unverified_css")))
                self.assertIsNone(staff_visual_material(self.db,1,3,displayed=True))

    def test_reject_overlay_other_viewport_and_clipped_image(self):
        facts, details = self.render()
        for status, width in (("occluded",100),("partial",100),("unknown",100),("visible",200)):
            self.assertIsNone(staff_crop_box(status,facts,details,(100,100),(width,100),"viewport_crop"))
        details["screen"]["overlayCandidates"] = [{"position":"fixed","bounds":[0,65,100,22]}]
        self.assertIsNone(staff_crop_box("visible",facts,details,(100,100),(100,100),"viewport_crop"))

    def test_card_only_fallback_does_not_claim_an_individual_portrait_frame(self):
        self.render(subject="card")
        material = staff_visual_material(self.db,1,3)
        self.assertEqual((material["provenance"]["subject"],material["partial"],material["photo_url"]),
                         ("card",True,None))
        self.assertIsNone(staff_visual_material(self.db,1,3,displayed=True))
        self.db.execute("UPDATE page_render SET rendered_html=replace(rendered_html,'gw-person-card','directory')")
        self.assertIsNone(staff_visual_material(self.db,1,3))


if __name__=="__main__":
    unittest.main()
