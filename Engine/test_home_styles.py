import os
import unittest

from browser_capture import BrowserCapture
from test_browser_capture import server


@unittest.skipUnless(os.environ.get("HACKATON_TEST_BROWSER") == "1", "opt-in local Chromium fixtures")
class HomeStylesTest(unittest.TestCase):
    def test_only_card_titles_are_compared_and_missing_css_is_unknown(self):
        html = """<!doctype html><meta charset="utf-8"><style>
          main {font-family:Arial;color:#232323} .slider-tpl {color:inherit}
          h2 {font:20px Arial} h3 {color:blue}
          .important span {color:#ba0000}
        </style><main><div id="calendar" class="tpl-component-gw-events tpl-template-slider">
          <h3>Декоративный заголовок раздела</h3>
          <a href="/one" class="slider-tpl object-index"><div><h2 class="name">Первое событие</h2></div></a>
          <a href="/two" class="slider-tpl object-index"><div><h2 class="name">Второе событие</h2></div></a>
          <a href="/three" class="slider-tpl object-index"><div><h2 class="name important">Третье <span>событие</span></h2></div></a>
        </div><section class="events-section" id="other"><h2>Другой блок</h2>
          <article><h3>Открытый урок</h3></article><article><h3>Классный час</h3></article>
        </section></main>"""
        ok = {"Content-Type":"text/html; charset=utf-8"}
        missing = html.replace("<main>", '<link rel="stylesheet" href="/missing.css"><main>')
        with server({"/":(200,ok,html.encode()), "/missing":(200,ok,missing.encode())}) as (site,_):
            with BrowserCapture() as capture:
                live = capture.fetch(site+"/",site,5).render
                partial = capture.fetch(site+"/missing",site,5).render
        styles = live.details["peer_styles"]
        self.assertEqual(styles["status"], "measured")
        groups = {item["container"]: item for item in styles["groups"]}
        self.assertEqual(groups["#other"]["result"], "uniform")
        events = groups["#calendar"]
        self.assertEqual(events["result"], "different")
        self.assertEqual(events["role"], "event_title")
        self.assertEqual([e["text"] for e in events["elements"]],
                         ["Первое событие", "Второе событие", "Третье событие"])
        first, second, third = [e["styles"] for e in events["elements"]]
        self.assertEqual(first, second)
        self.assertEqual(set(style[2] for style in third), {"rgb(35, 35, 35)", "rgb(186, 0, 0)"})
        self.assertIs(partial.css_loaded, False)
        self.assertEqual(partial.details["peer_styles"],
                         {"status":"unknown","reason":"incomplete_dom_or_unverified_css"})


if __name__ == "__main__":
    unittest.main()
