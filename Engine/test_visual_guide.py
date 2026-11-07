import hashlib
import io
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pypdfium2
from PIL import Image, ImageChops, ImageStat

import visual_guide
from visual_guide import guide_illustration


def question(check_id,target,pages):
    return {"id":check_id,"visual":{"target":target,"reference_pages":pages}}


class VisualGuideTest(unittest.TestCase):
    def test_individual_figures_are_bounded_jpeg_with_stable_source_and_check_identity(self):
        cases = [
            ("advisory_flag_3","staff",1,1),("advisory_head_clothing_3","staff",2,2),
            ("advisory_flag_stripes_3","staff",3,3),("school_main_logo","school_main",10,18),
            ("school_main_photo","school_main",11,19),("school_main_building","school_main",11,20),
            ("school_main_horizon","school_main",12,21),("staff_head_in_frame_8","staff",13,22),
            ("staff_series_crowns_3","staff_series",13,23),("staff_placeholder_8","staff",14,24),
            ("canteen_framing","canteen",16,28),
        ]
        paths = set()
        for key,target,page,figure in cases:
            with self.subTest(figure=figure):
                meta,data,caption = guide_illustration(question(key,target,[page]))
                self.assertEqual((meta["kind"],meta["page_number"],meta["figure"],meta["for_checks"]),
                                 ("guide",page,figure,[key]))
                self.assertEqual(meta["document_sha256"],visual_guide.GUIDE_SHA256)
                self.assertEqual(meta["sha256"],hashlib.sha256(data).hexdigest())
                self.assertIn(f"рис. {figure}",caption)
                self.assertNotIn(meta["path"],paths)
                paths.add(meta["path"])
                with Image.open(io.BytesIO(data)) as img:
                    self.assertEqual(img.format,"JPEG")
                    self.assertEqual(img.size,(meta["width"],meta["height"]))
                    self.assertLessEqual(img.width,750)
                    self.assertLessEqual(img.height,450)
                    self.assertEqual(getattr(img,"n_frames",1),1)

    def test_two_independent_regions_on_same_page_are_not_swapped(self):
        # Independently crop the full upright page with top-left pixel coordinates.
        with pypdfium2.PdfDocument(visual_guide.GUIDE) as doc, closing(doc[10]) as page:
            with closing(page.render(scale=1)) as bitmap:
                reference = bitmap.to_pil().convert("RGB")
        for key,rect in [("school_main_photo",(149,58,524,207)),("school_main_building",(159,399,515,557))]:
            _,data,_ = guide_illustration(question(key,"school_main",[11]))
            actual = Image.open(io.BytesIO(data)).convert("RGB").resize((40,24),Image.Resampling.BOX)
            expected = reference.crop(rect).resize((40,24),Image.Resampling.BOX)
            self.assertLess(max(ImageStat.Stat(ImageChops.difference(actual,expected)).mean),7)

    def test_missing_or_different_edition_and_unillustrated_subjects_return_no_artifact(self):
        for key,target,pages in [("school_main_group_light","school_main",[11]),("school_main_group","school_main",[11]),
                                  ("staff_portrait_42","staff",[]),("staff_placeholder_2","canteen",[14]),
                                  ("canteen_level","canteen",[13])]:
            self.assertIsNone(guide_illustration(question(key,target,pages)))
        path = Mock()
        path.stat.side_effect = [FileNotFoundError(),SimpleNamespace(st_mtime_ns=1,st_size=17)]
        path.read_bytes.return_value = b"different edition"
        with patch.object(visual_guide,"GUIDE",path):
            q = question("canteen_level","canteen",[16])
            self.assertIsNone(guide_illustration(q))
            self.assertIsNone(guide_illustration(q))


if __name__=="__main__":
    unittest.main()
