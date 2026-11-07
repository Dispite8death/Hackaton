import os
import unittest
from unittest.mock import Mock

from staff_extract import extract_staff
from staff_render import measure_staff_elements


SITE = "https://school.test/staff"
CARD = """<div class="gw-person-card" id="employee"><div id="name" class="gw-person-card__name">
Иванов Иван Иванович</div><div class="gw-person-card__job">Директор</div></div>"""


class StaffMeasureTest(unittest.TestCase):
    def test_failed_page_and_empty_cards(self):
        page = Mock()
        page.evaluate.side_effect = RuntimeError("page was replaced")
        self.assertEqual(measure_staff_elements(page, [], SITE), [])
        page.evaluate.assert_not_called()
        rows = measure_staff_elements(page, extract_staff(CARD, SITE), SITE)
        self.assertEqual({row["field"] for row in rows}, {"card", "name", "role"})
        self.assertTrue(all(row["status"] == "unknown" and row["details"]["hit_test_uncertain"] for row in rows))
        self.assertEqual(page.evaluate.call_count, 1)


@unittest.skipUnless(os.environ.get("HACKATON_TEST_BROWSER") == "1", "opt-in local Chromium fixture")
class BrowserEvidenceTest(unittest.TestCase):
    def test_css_obstruction_clipping_and_changed_identity(self):
        from browser_capture import BrowserCapture

        cards = extract_staff(CARD, SITE)
        with BrowserCapture(viewport=(600, 400)) as capture:
            page = capture.browser.new_page(viewport={"width": 600, "height": 400})
            base = """html,body{margin:0} #name{width:300px;height:60px}
                #overlay{position:fixed;top:0;left:0;width:300px;height:60px;z-index:8}"""
            fixtures = [("", "visible"), ("#employee{display:none}", "hidden"),
                        ("#employee{opacity:0}", "hidden"), ("#employee{margin-top:500px}", "out_of_view"),
                        ("#overlay{background:red}", "occluded"), ("#overlay{background:red;width:90px}", "partial"),
                        ("#overlay{background:red;pointer-events:none}", "unknown"),
                        ("#overlay{background:red;opacity:0}", "visible"),
                        ("body::after{content:'';position:fixed;inset:0;background:red;pointer-events:none}", "unknown")]
            try:
                for style, expected in fixtures:
                    with self.subTest(style=style):
                        overlay = '<aside id="overlay"></aside>' if style.startswith("#overlay") else ""
                        html = "<!doctype html><meta charset=utf-8><style>" + base + style + "</style>" + CARD + overlay
                        page.route(SITE, lambda route: route.fulfill(body=html, content_type="text/html; charset=utf-8"))
                        page.goto(SITE)
                        rows = measure_staff_elements(page, cards, SITE)
                        name = next(row for row in rows if row["field"] == "name")
                        self.assertEqual(name["status"], expected, name["details"])
                        self.assertEqual(name["details"]["hit_test_uncertain"], expected == "unknown")
                        page.unroute(SITE)
                page.evaluate("document.querySelector('.gw-person-card__job').textContent='Заместитель директора'")
                self.assertTrue(all(r["details"]["reason"] == "card_identity_unconfirmed"
                                    for r in measure_staff_elements(page, cards, SITE)))
                page.evaluate("document.querySelector('.gw-person-card__job').textContent='Директор'")
                page.evaluate("document.querySelector('#name').textContent='Другой Сотрудник Школы'")
                self.assertTrue(all(r["status"] == "unknown" for r in measure_staff_elements(page, cards, SITE)))
                self.assertTrue(all(r["status"] == "unknown" for r in measure_staff_elements(page, cards, SITE + "/other")))
            finally:
                page.close()


if __name__ == "__main__":
    unittest.main()
