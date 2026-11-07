"""Capture one live browser visit, keeping the original HTTP document and its visual evidence."""

import base64
import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .http_page import MAX_HTML_BYTES, PageResponse, decode_html, same_site
else:
    from http_page import MAX_HTML_BYTES, PageResponse, decode_html, same_site


@dataclass
class RenderEvidence:
    captured_at: int
    url: str
    viewport_width: int
    viewport_height: int
    rendered_html: str | None
    screenshot: bytes | None
    full_screenshot: bytes | None
    pdf: bytes | None
    css_loaded: bool | None
    details: dict
    staff_cards: tuple | None = None


_ENTRY_LINKS = r"""() => {
    const links = [];
    for (const a of document.querySelectorAll('a[href],button,[role=button]')) {
        const label = (a.textContent.trim() || a.getAttribute('aria-label') || '').replace(/\s+/g,' ');
        if (!/^(?:навигатор по сайту|карта сайта|(?:все )?(?:новости(?: и (?:события|мероприятия))?|события|мероприятия))$/i.test(label)) continue;
        const r = a.getBoundingClientRect();
        const visible = a.checkVisibility({opacityProperty:true, visibilityProperty:true});
        const left = Math.max(0,r.left), right = Math.min(innerWidth,r.right);
        const top = Math.max(0,r.top), bottom = Math.min(innerHeight,r.bottom);
        const intersects = right > left && bottom > top;
        const hits = visible && intersects
            ? a.contains(document.elementFromPoint((left+right)/2,(top+bottom)/2)) : null;
        links.push({label, href:a.getAttribute('href'), url:a.href || null, display:getComputedStyle(a).display,
            visible_with_css:visible, intersects_viewport:intersects, center_receives_pointer:hits,
            document_y:Math.round(r.top+scrollY), width:Math.round(r.width), height:Math.round(r.height)});
    }
    return links;
}"""

# Screen and emulated print are measured independently for each document.
# Overflow alone is common even on perfectly printable sites. Compare geometry
# and excess content, and report suspects as observations rather than failures.
_LAYOUT = """() => {
    const root = document.documentElement, body = document.body;
    const selector = el => {
        const parts = [];
        while (el && el.nodeType === 1) {
            if (el.id) { parts.unshift('#' + CSS.escape(el.id)); break; }
            let n = 1;
            for (let s = el.previousElementSibling; s; s = s.previousElementSibling) n++;
            parts.unshift(el.localName + ':nth-child(' + n + ')');
            el = el.parentElement;
        }
        return parts.join('>');
    };
    const scrollable = [], clipped = [], overlays = [];
    let scrollableCount = 0, clippedCount = 0;
    for (const el of document.querySelectorAll('body, body *')) {
        const s = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height || s.visibility === 'hidden') continue;
        const extraY = el.scrollHeight - el.clientHeight, extraX = el.scrollWidth - el.clientWidth;
        const has = pattern => (extraY > 2 && pattern.test(s.overflowY)) ||
                                (extraX > 2 && pattern.test(s.overflowX));
        if (has(/^(auto|scroll)$/) && el !== body) {
            scrollableCount++;
            if (scrollable.length < 24) scrollable.push({selector:selector(el), extraY, extraX});
        }
        if (has(/^(hidden|clip)$/)) {
            clippedCount++;
            if (clipped.length < 32) clipped.push({selector:selector(el), extraY, extraX,
                clientHeight:el.clientHeight, clientWidth:el.clientWidth});
        }
        if (['fixed','sticky'].includes(s.position) && Number(s.opacity) > 0 &&
            r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight &&
            r.width*r.height > innerWidth*innerHeight*0.025 && overlays.length < 16) {
            overlays.push({selector:selector(el), position:s.position, pointerEvents:s.pointerEvents,
                opacity:s.opacity, bounds:[r.x,r.y,r.width,r.height].map(Math.round)});
        }
    }
    return {width:Math.max(root.scrollWidth,body?.scrollWidth||0,innerWidth),
        height:Math.max(root.scrollHeight,body?.scrollHeight||0,innerHeight),
        scrollX, scrollY, rootOverflowY:getComputedStyle(root).overflowY,
        bodyOverflowY:body ? getComputedStyle(body).overflowY : null,
        scrollableCount, scrollable, clippedCount, clipped, overlayCandidates:overlays};
}"""


_RESOURCES = """() => {
    const missing = [], inaccessible = [], checked = new Set(), addresses = new Set();
    function inspect(sheet) {
        if (!sheet || checked.has(sheet)) return;
        checked.add(sheet);
        if (sheet.href) addresses.add(sheet.href);
        try {
            for (const rule of sheet.cssRules) if (rule.type === CSSRule.IMPORT_RULE) {
                if (rule.media.mediaText && !matchMedia(rule.media.mediaText).matches) continue;
                addresses.add(new URL(rule.href, sheet.href || document.baseURI).href);
                if (rule.styleSheet) inspect(rule.styleSheet); else missing.push(rule.href);
            }
        } catch { inaccessible.push(sheet.href || 'inline'); }
    }
    let expected = 0;
    for (const e of document.querySelectorAll('link[rel~="stylesheet"],style')) {
        if (e.disabled || (e.media && !matchMedia(e.media).matches)) continue;
        expected++;
        if (e.href) addresses.add(e.href);
        if (!e.sheet) missing.push(e.href || 'inline'); else inspect(e.sheet);
    }
    for (const sheet of document.adoptedStyleSheets) {
        if (sheet.disabled || (sheet.media.mediaText && !matchMedia(sheet.media.mediaText).matches)) continue;
        expected++;
        inspect(sheet);
    }
    const images = Array.from(document.images);
    return {expectedStyles:expected, stylesheetUrls:Array.from(addresses), missingStyles:missing.slice(0,30),
        unreadableStyles:inaccessible.slice(0,30),
        images:images.length, imagesWithoutPixels:images.filter(x=>!x.naturalWidth).length};
}"""


# A hit test describes the receiver of pointer events only. A transparent
# overlay can intercept all points; an opaque pointer-events:none overlay none.
_POS = """() => {
    const selectors = ['.pos-banner-fluid','#js-show-iframe-wrapper',
        'iframe[src*="pos.gosuslugi.ru"]','[class*="gw-pos-widget"]'];
    const evidence = [], roots = [];
    for (const sel of selectors) for (const [index,el] of document.querySelectorAll(sel).entries()) {
        if (roots.some(x => x.contains(el)||el.contains(x)) || roots.length >= 8) continue;
        roots.push(el);
        const r=el.getBoundingClientRect(), s=getComputedStyle(el);
        const visible=el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
        const x1=Math.max(0,r.left), x2=Math.min(innerWidth,r.right);
        const y1=Math.max(0,r.top), y2=Math.min(innerHeight,r.bottom);
        let points=0, hits=0; const blockers=new Set();
        if (visible && x2>x1 && y2>y1) for (const x of [.15,.5,.85]) for (const y of [.15,.5,.85]) {
            const top=document.elementFromPoint(x1+(x2-x1)*x,y1+(y2-y1)*y);
            points++;
            if (top===el || el.contains(top)) hits++;
            else if (top) blockers.add(top.id ? '#'+top.id : top.localName);
        }
        const state=!visible?'css_hidden':!r.width||!r.height?'empty_geometry':!points?'out_of_view':
            hits===points?'receives_all_sampled_points':hits?'receives_some_sampled_points':'intercepted_at_all_points';
        evidence.push({selector:sel, match_index:index, state, display:s.display, visibility:s.visibility, opacity:s.opacity,
            viewportPoints:points, receives:hits, blockers:Array.from(blockers),
            bounds:[r.x,r.y,r.width,r.height].map(Math.round)});
    }
    return evidence;
}"""


def _full_clip(width, height):
    """Bound Chrome surface and image memory. The exact captured bounds go into evidence."""
    w = max(1, min(int(width), 8192))
    h = max(1, min(int(height), 16000, 24_000_000 // w))
    return {"x": 0, "y": 0, "width": w, "height": h, "scale": 1}


def _print_comparison(screen, printed):
    original = {x["selector"]: x for x in screen["clipped"]}
    extra = [row for row in printed["clipped"]
             if row["extraY"] > original.get(row["selector"], {}).get("extraY", 0) + 5]
    return {"emulation": "print CSS at the recorded viewport; not a PDF page count",
            "additional_vertical_clipping": extra,
            "height_change": printed["height"] - screen["height"],
            "state": "inspect_print_layout" if extra or printed["height"] < screen["height"] * .75
                     else "no_major_change_observed"}


class BrowserCapture:
    def __init__(self, *, pdf=False, viewport=(1280, 800), ready_timeout=45):
        self.include_pdf = pdf
        self.width, self.height = viewport
        if not (1 <= self.width <= 4096 and 1 <= self.height <= 4096):
            raise ValueError("Размер окна должен быть в пределах 1–4096 пикселей")
        if ready_timeout <= 0:
            raise ValueError("Таймаут готовности документа должен быть положительным")
        self.ready_timeout = ready_timeout
        self._stack = ExitStack()

    def __enter__(self):
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError as exc:
            raise RuntimeError("Для браузерного режима установите playwright") from exc
        self._error = Error
        try:
            pw = self._stack.enter_context(sync_playwright())
            engine = pw.chromium
            channels = [None] if Path(engine.executable_path).is_file() else []
            channels += ["chrome", "msedge"]
            for channel in channels:
                try:
                    self.browser = engine.launch(channel=channel, headless=True, chromium_sandbox=True)
                    break
                except Error as exc:
                    if channel == channels[-1]:
                        raise RuntimeError("Не удалось запустить Chromium, Chrome или Edge. "
                                           "Для Playwright установите chromium командой python -m playwright install chromium.") from exc
            self._stack.callback(self.browser.close)
            self.context = self.browser.new_context(
                viewport={"width": self.width, "height": self.height},
                device_scale_factor=1, accept_downloads=False, service_workers="block",
            )
            self._stack.callback(self.context.close)
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, exc_type, exc, tb):
        return self._stack.__exit__(exc_type, exc, tb)

    def fetch(self, url: str, site: str, timeout: float, *, section: str = "home") -> PageResponse:
        if not same_site(url, site):
            return PageResponse(url, None, None, "outside organization")
        page = self.context.new_page()
        try:
            return self._visit(page, url, site, timeout, section)
        except self._error as exc:
            return PageResponse(url, None, None, str(exc).splitlines()[0])
        finally:
            with suppress(self._error):
                page.close()

    def _visit(self, page, url, site, timeout, section):
        page.set_default_timeout(max(1000, timeout*1000))
        failed = []
        css_errors = set()
        pending_styles = {}
        failed_count = 0
        blocked_navigation = []
        finished_documents = set()
        latest_document_request = None

        def remember(resource, address, error):
            nonlocal failed_count
            failed_count += 1
            if len(failed) < 100:
                failed.append({"type":resource, "url":address, "error":error})

        def stylesheet_addresses(req):
            addresses = set()
            while req is not None:
                addresses.add(req.url)
                req = req.redirected_from
            return addresses

        def track_status(response):
            nonlocal latest_document_request
            request = response.request
            if (request.is_navigation_request() and request.frame == page.main_frame and
                    response.status not in (204, 205) and not 300 <= response.status < 400):
                latest_document_request = request
            if response.status >= 400:
                remember(request.resource_type, response.url, f"HTTP {response.status}")
                if request.resource_type == "stylesheet":
                    css_errors.update(stylesheet_addresses(request))

        def requested(req):
            if req.resource_type == "stylesheet":
                pending_styles[req] = stylesheet_addresses(req)

        def finished(req):
            pending_styles.pop(req, None)
            if req.is_navigation_request() and req.frame == page.main_frame:
                finished_documents.add(req)

        def request_failed(req):
            pending_styles.pop(req, None)
            if req.resource_type == "stylesheet":
                css_errors.update(stylesheet_addresses(req))
            remember(req.resource_type, req.url, req.failure or "failed")

        page.on("request", requested)
        page.on("response", track_status)
        page.on("requestfinished", finished)
        page.on("requestfailed", request_failed)
        page.on("popup", lambda popup: popup.close())
        session = self.context.new_cdp_session(page)
        main_frame = session.send("Page.getFrameTree")["frameTree"]["frame"]["id"]

        # CDP pauses *each* new document request, including HTTP redirect hops.
        # Only read-method requests are forwarded by this page's interceptor.
        def guard(event):
            req = event["request"]
            is_main = event.get("frameId") == main_frame and event["resourceType"] == "Document"
            try:
                allow = (req["method"] in {"GET", "HEAD", "OPTIONS"} and
                         (not is_main or same_site(req["url"], site)))
            except ValueError:
                allow = False
            if not allow and is_main:
                blocked_navigation.append(req["url"])
            command = "Fetch.continueRequest" if allow else "Fetch.failRequest"
            args = {"requestId": event["requestId"]}
            if not allow:
                args["errorReason"] = "BlockedByClient"
            try:
                session.send(command, args)
            except self._error:
                pass  # Requests can be cancelled by navigation or page closure.

        session.on("Fetch.requestPaused", guard)
        session.send("Fetch.enable", {"patterns":[
            {"urlPattern":"*", "resourceType":kind, "requestStage":"Request"}
            for kind in ("Document", "XHR", "Fetch", "Ping")
        ]})
        try:
            response = page.goto(url, wait_until="commit", timeout=timeout*1000)
        except self._error as exc:
            return PageResponse(url, None, None, "navigation outside organization" if blocked_navigation
                                else str(exc).splitlines()[0])
        if response is None:
            return PageResponse(url, None, None, "no HTTP navigation response")
        status, final_url = response.status, response.url
        if not 200 <= status < 300:
            return PageResponse(final_url, status, None, f"HTTP {status}")
        headers = response.headers
        content_type = headers.get("content-type", "")
        if content_type.partition(";")[0].strip().lower() not in {"text/html", "application/xhtml+xml"}:
            return PageResponse(final_url, status, None, "not HTML: " + content_type)
        size = headers.get("content-length", "")
        if size.isdecimal() and int(size) > MAX_HTML_BYTES:
            return PageResponse(final_url, status, None, "HTML exceeds 4 MiB")
        try:
            # Main response completion is independent of document parsing and
            # third-party scripts. Never discard raw HTML for a slow stylesheet.
            if response.request not in finished_documents:
                page.wait_for_event("requestfinished", predicate=lambda r: r == response.request,
                                    timeout=timeout * 1000)
            body = response.body()
        except self._error as exc:
            return PageResponse(final_url, status, None, "main HTML incomplete: " + str(exc).splitlines()[0])
        if not body.strip() or len(body) > MAX_HTML_BYTES:
            return PageResponse(final_url, status, None, "empty response" if not body.strip() else "HTML exceeds 4 MiB")
        result = PageResponse(final_url, status, decode_html(body, content_type), loaded_at=int(time.time()))
        details = {"browser_version":self.browser.version, "screenshot_mime":"image/jpeg",
                   "requested_artifacts":["dom", "screenshot", "full_screenshot"] + (["pdf"] if self.include_pdf else []),
                   "artifact_errors":{}}
        render = RenderEvidence(int(time.time()), final_url, self.width, self.height,
                                None, None, None, None, None, details)
        result.render = render
        details.update(ready_timeout_seconds=self.ready_timeout, load_event=False,
                       document_complete_for_extraction=False)
        try:
            # Scripts can block the parser *after* we have the complete main
            # response. Allow additional parsing time without risking that HTML.
            page.wait_for_load_state("domcontentloaded", timeout=self.ready_timeout*1000)
            page.wait_for_load_state("load", timeout=min(timeout*1000, 4500))
            details["load_event"] = True
        except self._error:
            pass
        try:
            render.captured_at = int(time.time())
            details["document_ready_state"] = page.evaluate("document.readyState")
            details["document_complete_for_extraction"] = details["document_ready_state"] in ("interactive", "complete")
            if latest_document_request != response.request or page.url.split("#")[0] != final_url.split("#")[0]:
                raise ValueError("document changed after main navigation")
            self._capture(page, session, render, result.html, section, pending_styles, css_errors)
        except (self._error, ValueError) as exc:
            details["capture_error"] = str(exc).splitlines()[0]
        if latest_document_request != response.request or page.url.split("#")[0] != final_url.split("#")[0]:
            # A reload may replace a document even when its URL stays identical.
            # Nothing measured on the replacement belongs to the raw response.
            details = {"browser_version":details["browser_version"],
                       "requested_artifacts":details["requested_artifacts"],
                       "artifact_errors":{}, "document_complete_for_extraction":False,
                       "capture_error":"main document changed before capture finished"}
            result.render = RenderEvidence(render.captured_at, final_url, self.width, self.height,
                                           None, None, None, None, None, details)
        details["failed_requests"], details["failed_request_count"] = failed, failed_count
        return result

    def _capture(self, page, session, render, raw_html, section, pending_styles, css_errors):
        details = render.details
        errors = details["artifact_errors"]
        def artifact(key, func):
            try:
                return func()
            except self._error as exc:
                errors[key] = str(exc).splitlines()[0]
                return None

        render.rendered_html = artifact("dom", page.content)
        parsed_dom = details["document_complete_for_extraction"] and render.rendered_html is not None
        details["staff_source"] = "rendered_dom" if parsed_dom else "response_html"
        try:
            if __package__:
                from .staff_extract import extract_staff
                from .staff_render import measure_staff_elements
            else:
                from staff_extract import extract_staff
                from staff_render import measure_staff_elements
            source = render.rendered_html if parsed_dom else raw_html
            render.staff_cards = tuple(extract_staff(source, render.url, section))
            if parsed_dom:
                details["staff_measurements"] = measure_staff_elements(page, render.staff_cards, render.url)
            else:
                details["staff_measurements"] = []
                for card in render.staff_cards:
                    container = next((e.selector for e in card.elements if e.field == "card"), None)
                    for el in card.elements:
                        details["staff_measurements"].append({
                            "card_selector":container, "field":el.field, "selector":el.selector,
                            "expected_value":el.text, "status":"unknown",
                            "details":{"reason":"rendered_document_incomplete"},
                        })
        except Exception as exc:
            # Keep the original visit and visual evidence even if one optional analyser fails.
            details["staff_measurement_error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        resources = page.evaluate(_RESOURCES)
        details["resources"] = resources
        applicable = set(resources["stylesheetUrls"])
        resources["failedStyles"] = sorted(applicable & css_errors)
        pending = {url for addresses in pending_styles.values() for url in addresses}
        resources["pendingStyles"] = sorted(applicable & pending)
        if resources["missingStyles"] or resources["failedStyles"]:
            render.css_loaded = False
        elif (details["document_complete_for_extraction"] and resources["expectedStyles"] and
              not resources["pendingStyles"] and not resources["unreadableStyles"]):
            render.css_loaded = True
        if section == "home":
            if parsed_dom and render.css_loaded is True:
                if __package__:
                    from .home_styles import collect_peer_styles
                else:
                    from home_styles import collect_peer_styles
                groups = artifact("peer_styles", lambda: collect_peer_styles(page, render.staff_cards))
                details["peer_styles"] = {"status": "measured" if groups is not None else "unknown",
                                         "groups": groups or []}
            else:
                details["peer_styles"] = {"status": "unknown", "reason": "incomplete_dom_or_unverified_css"}
        if parsed_dom and section in ("home", "site_navigator", "hub", "about", "news", "events"):
            details["entry_links"] = artifact("entry_links", lambda: page.evaluate(_ENTRY_LINKS))
        details["pos_candidates"] = page.evaluate(_POS)
        details["pos_scan"] = ("document_incomplete" if not details["document_complete_for_extraction"]
                               else "no_recognized_candidate" if not details["pos_candidates"]
                               else "viewport_pointer_hit_test_only")
        screen = page.evaluate(_LAYOUT)
        details["screen"] = screen
        def image(clip):
            data = session.send("Page.captureScreenshot", {"format":"jpeg", "quality":78,
                "fromSurface":True, "captureBeyondViewport":True, "clip":clip})["data"]
            return base64.b64decode(data)

        viewport = {"x":screen["scrollX"], "y":screen["scrollY"], "width":self.width, "height":self.height, "scale":1}
        render.screenshot = artifact("screenshot", lambda: image(viewport))
        clip = _full_clip(screen["width"], screen["height"])
        render.full_screenshot = artifact("full_screenshot", lambda: image(clip))
        details["full_screenshot_bounds"] = clip
        capped = clip["width"] < screen["width"] or clip["height"] < screen["height"]
        details["full_screenshot_size_capped"] = capped
        large_clip = any(row["extraY"] > self.height / 2 and row["clientHeight"] >= self.height / 2
                         for row in screen["clipped"])
        details["full_screenshot_coverage"] = (
            "capture_failed" if render.full_screenshot is None
            else "document_incomplete" if not details["document_complete_for_extraction"]
            else "size_capped" if capped
            else "inner_scroll_requires_review" if screen["scrollableCount"]
            else "inner_clip_requires_review" if large_clip
            else "document_bounds_captured"
        )
        if self.include_pdf:
            try:
                page.emulate_media(media="print")
                printed = page.evaluate(_LAYOUT)
                details["print_layout"] = printed
                details["print_comparison"] = _print_comparison(screen, printed)
                if not details["document_complete_for_extraction"]:
                    details["print_comparison"]["state"] = "insufficient_document"
            except self._error as exc:
                details["print_layout_error"] = str(exc).splitlines()[0]
            finally:
                page.emulate_media(media="screen")
            details["pdf_media"] = "screen CSS, A4 paginated, backgrounds included"
            render.pdf = artifact("pdf", lambda: page.pdf(format="A4", print_background=True))
