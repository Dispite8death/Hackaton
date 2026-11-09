import os
import threading
import time
import unittest
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from browser_capture import BrowserCapture, _full_clip, _print_comparison


def fixture(style=""):
    return ("""<!doctype html><meta charset="utf-8"><style>
        html,body{margin:0} .spacer{height:1900px}
        #marker{background:#00a836;height:64px}
        """ + style + """</style><main id="window"><p>First line</p>
        <div class="spacer"></div><div id="marker">END-OF-PAGE-614</div></main>
        <script>document.documentElement.dataset.rendered='yes'</script>""").encode()


@contextmanager
def server(routes):
    calls = Counter()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls[self.path] += 1
            route = routes.get(self.path, (404, {}, b"not found"))
            status, headers, body = route() if callable(route) else route
            try:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as instance:
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:" + str(instance.server_port), calls
        finally:
            instance.shutdown()
            thread.join()


class GeometryTest(unittest.TestCase):
    def test_surface_cap_preserves_normal_long_pages(self):
        self.assertEqual(_full_clip(1280, 2250)["height"], 2250)
        clip = _full_clip(5000, 60000)
        self.assertLessEqual(clip["width"] * clip["height"], 24_000_000)

    def test_existing_clip_is_not_an_automatic_print_problem(self):
        normal = {"height":1000, "clipped":[{"selector":"#hero", "extraY":200}]}
        self.assertEqual(_print_comparison(normal, normal)["state"], "no_major_change_observed")


@unittest.skipUnless(os.environ.get("HACKATON_TEST_BROWSER") == "1", "opt-in local Chromium fixtures")
class BrowserCaptureTest(unittest.TestCase):
    def test_news_directory_link_visibility_and_fold(self):
        body = """<meta charset="utf-8"><style>.mobile{display:none}.lower{margin-top:1800px}</style>
            <a class="mobile" href="/map/">Навигатор по сайту</a>
            <a href="/map/">Навигатор по сайту</a>
            <div class="lower"><a href="/news/">Все новости и события</a></div>""".encode()
        with server({"/":(200,{"Content-Type":"text/html"},body)}) as (site,_):
            with BrowserCapture() as capture:
                response = capture.fetch(site+"/",site,5,section="home")
        links = response.render.details["entry_links"]
        self.assertEqual([x["visible_with_css"] for x in links], [False,True,True])
        self.assertEqual([x["intersects_viewport"] for x in links], [False,True,False])

    def test_pos_hit_results_are_distinct_from_visual_opacity(self):
        doc = b"""<style>
          .pos-banner-fluid,aside{position:absolute;width:80px;height:80px;top:0}
          aside{z-index:3} #clear{left:0;background:transparent}
          #solid{left:110px;background:black;pointer-events:none}
          </style><div class="pos-banner-fluid">one</div><aside id="clear"></aside>
          <div class="pos-banner-fluid" style="left:110px">two</div><aside id="solid"></aside>
          <div class="pos-banner-fluid" style="top:1800px">three</div>
          <div class="pos-banner-fluid" style="display:none">four</div>"""
        with server({"/":(200,{"Content-Type":"text/html"},doc)}) as (site,_):
            with BrowserCapture() as capture:
                response = capture.fetch(site+"/",site,5)
        d = response.render.details
        self.assertEqual(d["pos_scan"], "viewport_pointer_hit_test_only")
        self.assertEqual([(r["match_index"],r["state"]) for r in d["pos_candidates"]], [
            (0,"intercepted_at_all_points"), (1,"receives_all_sampled_points"),
            (2,"out_of_view"), (3,"css_hidden")])

    def test_delayed_image_does_not_invalidate_readable_screen_styles(self):
        def slow_image():
            time.sleep(4)
            return 404, {}, b""
        doc = b'<link rel="stylesheet" href="/main.css"><img src="/slow.png"><p>Content</p>'
        css = {"Content-Type":"text/css"}
        with server({"/":(200,{"Content-Type":"text/html"},doc),
                     "/main.css":(200,css,b'@import url("/import.css");'),
                     "/import.css":(200,css,b'p{color:green}'),
                     "/slow.png":slow_image}) as (site,_):
            with BrowserCapture() as capture:
                response = capture.fetch(site+"/",site,1)
        d = response.render.details
        self.assertFalse(d["load_event"])
        self.assertTrue(d["document_complete_for_extraction"])
        self.assertGreater(d["resources"]["imagesWithoutPixels"], 0)
        self.assertIn(site+"/import.css", d["resources"]["stylesheetUrls"])
        self.assertIs(response.render.css_loaded, True)

    def test_same_url_reload_keeps_only_the_original_response(self):
        visits = 0
        def document():
            nonlocal visits
            visits += 1
            body = (b'<p>First document</p><img src="/slow.png"><script>setTimeout(()=>location.reload(),400)</script>'
                    if visits == 1 else b'<p>Replacement document</p>')
            return 200, {"Content-Type":"text/html", "Cache-Control":"no-store"}, body
        def slow_image():
            time.sleep(3)
            return 404, {}, b""
        with server({"/":document, "/slow.png":slow_image}) as (site,_):
            with BrowserCapture() as capture:
                response = capture.fetch(site+"/",site,3)
        self.assertGreaterEqual(visits, 2)
        self.assertIn("First document",response.html)
        self.assertIsNone(response.render.rendered_html)
        self.assertIsNone(response.render.staff_cards)
        self.assertNotIn("screen",response.render.details)
        self.assertIn("document changed", response.render.details["capture_error"])

    def test_slow_script_does_not_discard_main_response(self):
        def slow_script():
            time.sleep(3)
            return 200, {"Content-Type":"text/javascript"}, b""
        staff = '<a class="gw-person-card"><span class="gw-person-card__job">Заведующий</span><span class="gw-person-card__name">Иванова Анна Ивановна</span></a>'
        page = ('<meta charset="utf-8"><main>Raw response survives</main><script src="/slow.js"></script><p>later</p>' + staff).encode()
        with server({"/":(200,{"Content-Type":"text/html"},page), "/slow.js":slow_script}) as (site,_):
            with BrowserCapture(pdf=True, ready_timeout=.2) as capture:
                response = capture.fetch(site+"/", site, 1)
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.error)
        # A system content filter can inject additional scripts even on loopback.
        self.assertIn("<main>Raw response survives</main>", response.html)
        self.assertIn('<script src="/slow.js"></script><p>later</p>', response.html)
        self.assertFalse(response.render.details["load_event"])
        self.assertEqual(response.render.details["document_ready_state"], "loading")
        self.assertFalse(response.render.details["document_complete_for_extraction"])
        self.assertEqual(response.render.details["full_screenshot_coverage"], "document_incomplete")
        self.assertEqual(response.render.details["print_comparison"]["state"], "insufficient_document")
        self.assertEqual(response.render.staff_cards[0].name, "Иванова Анна Ивановна")
        self.assertTrue(all(m["status"]=="unknown" for m in response.render.details["staff_measurements"]))

    def test_js_inserted_staff_comes_from_the_saved_rendered_dom(self):
        script = """<meta charset="utf-8"><main></main><script>
            document.querySelector('main').innerHTML = '<a class="gw-person-card"><span class="gw-person-card__job">Заведующий</span><span class="gw-person-card__name">Иванова Анна Ивановна</span></a>'
            </script>"""
        with server({"/":(200,{"Content-Type":"text/html; charset=utf-8"},script.encode())}) as (site,_):
            with BrowserCapture() as capture:
                response = capture.fetch(site+"/",site,3,section="team")
        self.assertEqual(response.render.details["staff_source"], "rendered_dom")
        self.assertEqual(response.render.staff_cards[0].name, "Иванова Анна Ивановна")
        self.assertTrue(response.render.details["staff_measurements"])

    def test_full_screen_and_print_only_clipping(self):
        normal = fixture()
        clipped = fixture("@media print { #window { height:100vh; overflow:hidden; } }")
        scrolling = fixture("#window { height:100vh; overflow:auto; }")
        resources = fixture().replace(b"<main", b'<link rel="stylesheet" href="/missing.css"><main')
        pos = fixture().replace(b"<main", b'<div class="pos-banner-fluid" style="position:absolute;left:0;top:0;width:180px;height:40px">Hello</div><aside style="position:fixed;inset:0;background:red;z-index:2"></aside><main')
        ok = {"Content-Type":"text/html; charset=utf-8"}
        with server({"/normal":(200,ok,normal), "/clipped":(200,ok,clipped), "/scroll":(200,ok,scrolling),
                     "/missing":(200,ok,resources), "/pos":(200,ok,pos)}) as (site, calls):
            with BrowserCapture(pdf=True, viewport=(800,500)) as capture:
                for path, expected in (("/normal","no_major_change_observed"),("/clipped","inspect_print_layout")):
                    response = capture.fetch(site+path,site,5)
                    self.assertEqual(response.status, 200, response.error)
                    self.assertEqual(calls[path], 1)
                    r = response.render
                    self.assertNotIn('data-rendered="yes"', response.html)
                    self.assertIn('data-rendered="yes"', r.rendered_html)
                    self.assertTrue(r.screenshot.startswith(b"\xff\xd8"))
                    self.assertTrue(r.full_screenshot.startswith(b"\xff\xd8"))
                    self.assertTrue(r.pdf.startswith(b"%PDF"))
                    self.assertGreater(r.details["full_screenshot_bounds"]["height"], 1700)
                    self.assertEqual(r.details["print_comparison"]["state"], expected)
                    self.assertEqual(r.details["full_screenshot_coverage"], "document_bounds_captured")
                missing = capture.fetch(site+"/missing",site,5)
                self.assertEqual(missing.status, 200)
                self.assertFalse(missing.render.css_loaded)
                overlay = capture.fetch(site+"/pos",site,5)
                self.assertEqual(overlay.render.details["pos_candidates"][0]["state"], "intercepted_at_all_points")
                scrolling = capture.fetch(site+"/scroll",site,5).render.details
                self.assertEqual(scrolling["full_screenshot_coverage"], "inner_scroll_requires_review")
                self.assertEqual(scrolling["print_comparison"]["state"], "no_major_change_observed")

    def test_out_of_organization_redirect_stops_before_target(self):
        with server({}) as (outside, calls):
            with server({"/start":(302,{"Location":"/next"},b""),
                         "/next":(302,{"Location":outside+"/escaped"},b"")}) as (site, _):
                with BrowserCapture() as capture:
                    response = capture.fetch(site+"/start",site,5)
                self.assertIsNone(response.html)
                self.assertIn("outside", response.error)
                self.assertEqual(calls["/escaped"], 0)
