"""Page- and employee-scoped photographs from the archive. Never fetch an image here."""

import hashlib
import io
import json
import math
import re
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from PIL import Image
from soupsieve import SelectorSyntaxError

if __package__:
    from .home_image_files import _raster
    from .navigation import resolve_url
    from .staff_extract import CARDS
else:
    from home_image_files import _raster
    from navigation import resolve_url
    from staff_extract import CARDS


_DECOR = re.compile(r"(?:^|[\s/_.-])(?:background|banner|баннер\w*|logo|логотип\w*|icon|icons|sprite|social)(?:$|[\s/_.-])", re.I)
_EXT = {"image/jpeg":"jpg","image/png":"png","image/webp":"webp","image/gif":"gif",
        "image/avif":"avif","image/bmp":"bmp"}


def _json(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result,dict) else {}
    except (TypeError, ValueError):
        return {}


def _unique(soup, selector):
    try:
        matches = soup.select(selector, limit=2)
    except (SelectorSyntaxError, TypeError):
        return None
    return matches[0] if len(matches)==1 else None


def _bound_element(html, url, card_selector, selector, photo_url, name):
    """Confirm the card identity and, for a portrait, the exact descendant img and URL."""
    if not html or not name:
        return None
    soup = BeautifulSoup(html, "html.parser")
    card = _unique(soup,card_selector)
    if not card or " ".join(name.casefold().split()) not in " ".join(card.get_text(" ",strip=True).casefold().split()):
        return None
    if not photo_url:
        if (selector!=card_selector or card.select_one(CARDS)
                or not any(node is card for node in soup.select(CARDS))):
            return None
        return soup, card
    photo = _unique(soup,selector)
    if not photo or photo.name!="img" or not any(parent is card for parent in photo.parents):
        return None
    for nested in card.select(CARDS):
        if any(parent is nested for parent in photo.parents):
            return None
    hints = " ".join((unquote(urlsplit(photo_url).path), photo.get("alt",""), " ".join(photo.get("class",[]))))
    if _DECOR.search(hints):
        return None
    for parent in photo.parents:
        if parent is card:
            break
        if _DECOR.search(" ".join((parent.get("id",""), *parent.get("class",())))):
            return None
    base = soup.find("base",href=True)
    base_url = (resolve_url(url,base["href"]) if base else None) or url
    linked = {resolve_url(base_url,photo[key]) for key in ("src","data-src") if photo.get(key)}
    if resolve_url(url,photo_url) not in linked:
        return None
    return soup, photo


def _numbers(value, size):
    return isinstance(value,(tuple,list)) and len(value)==size and all(
        type(n) in (int,float) and math.isfinite(n) for n in value)


def _usable_size(width, height):
    # An icon, a tracking pixel or a very wide page banner cannot support a portrait check.
    return min(width,height)>=24 and max(width,height)<=4*min(width,height)


def _overlaps(a, b):
    return a[0]<b[0]+b[2] and b[0]<a[0]+a[2] and a[1]<b[1]+b[3] and b[1]<a[1]+a[3]


def staff_crop_box(status, facts, render_details, viewport, raster_size, source, *, clipped_ancestor=False):
    """Return a PIL pixel box for the entire bound img, or None if geometry is ambiguous."""
    if source not in {"viewport_crop","document_crop"} or status not in {"visible","out_of_view"}:
        return None
    if not _numbers(viewport,2) or min(viewport)<=0 or not isinstance(facts,dict) or not isinstance(render_details,dict):
        return None
    if (facts.get("expected_match") is not True or facts.get("hit_test_uncertain") is not False
            or facts.get("matches")!=1 or facts.get("display") in {None,"none"}
            or facts.get("visibility")!="visible" or facts.get("effective_opacity")!=1
            or facts.get("viewport")!=list(viewport) or facts.get("covered")!=0 or facts.get("uncertain")!=0):
        return None
    rect, scroll, screen = facts.get("rect"), facts.get("scroll"), render_details.get("screen") or {}
    if not _numbers(rect,4) or not _usable_size(rect[2],rect[3]) or not _numbers(scroll,2):
        return None
    if not isinstance(screen,dict) or [screen.get("scrollX"),screen.get("scrollY")]!=scroll:
        return None
    if status=="visible":
        fraction = facts.get("in_view_fraction")
        if (facts.get("reason")!="geometry_clear_at_sampled_points" or not facts.get("samples")
                or facts.get("samples")!=facts.get("clear") or not _numbers([fraction],1) or fraction<.995):
            return None
        visible = facts.get("viewport_rect")
        if not _numbers(visible,4) or max(abs(x-y) for x,y in zip(rect,visible))>1:
            return None
    elif facts.get("reason")!="outside_viewport" or _overlaps(rect,(0,0,*viewport)):
        return None
    overlays = screen.get("overlayCandidates")
    if not isinstance(overlays,list):
        return None
    for layer in overlays:
        bounds = layer.get("bounds") if isinstance(layer,dict) else None
        if not _numbers(bounds,4) or _overlaps(rect,bounds):
            return None

    if source=="viewport_crop":
        if status!="visible":
            return None
        frame = [0,0,*viewport]
        target = rect
    else:
        if clipped_ancestor or render_details.get("full_screenshot_coverage") not in {"document_bounds_captured","size_capped"}:
            return None
        clip = render_details.get("full_screenshot_bounds") or {}
        frame = [clip.get(k) for k in ("x","y","width","height")]
        if not _numbers(frame,4) or clip.get("scale")!=1:
            return None
        target = [rect[0]+scroll[0],rect[1]+scroll[1],*rect[2:]]
    if tuple(frame[2:])!=tuple(raster_size):
        return None  # archived screenshot scale or dimensions do not match this measurement
    x, y = target[0]-frame[0], target[1]-frame[1]
    box = math.ceil(x),math.ceil(y),math.floor(x+target[2]),math.floor(y+target[3])
    if x<0 or y<0 or x+target[2]>frame[2] or y+target[3]>frame[3] or not _usable_size(box[2]-box[0],box[3]-box[1]):
        return None
    return box


def _clipped(soup, photo, screen):
    # DOM selectors in the layout inventory use nth-child; the staff parser uses nth-of-type.
    # Resolve them to actual nodes before comparing parentage.
    ancestors = {id(node) for node in photo.parents}
    if not isinstance(screen,dict):
        return True
    for key in ("clipped","scrollable"):
        items = screen.get(key)
        if not isinstance(items,list) or screen.get(key+"Count")!=len(items):
            return True
        for item in items:
            node = _unique(soup,item.get("selector","")) if isinstance(item,dict) else None
            if node is None or id(node) in ancestors:
                return True
    return False


def _material(page_id, observation_id, url, source, data, mime, width, height, proof):
    digest = hashlib.sha256(data).hexdigest()
    stem = f"staff-{observation_id}-{source}-{digest[:16]}"
    return {"page_id":page_id,"observation_id":observation_id,"photo_url":url,"kind":"image",
            "source":source,"data":data,"mime":mime,"sha256":digest,"width":width,"height":height,
            "material_id":f"p{page_id}-{stem}", "path":f"pages/{page_id}/staff/{stem}.{_EXT[mime]}",
            "captured_at":proof.get("captured_at",proof.get("fetched_at")),
            "partial":proof["subject"]=="card", "provenance":proof}


def staff_visual_material(conn, page_id, observation_id, *, displayed=False):
    """Select one archived original or crop for this observation. displayed=True requires a crop."""
    row = conn.execute("""SELECT s.card_selector,s.photo_url,s.full_name,s.role,p.url,p.text_page,
                          r.rendered_html,r.details,r.url,r.captured_at,r.css_loaded,r.viewport_width,r.viewport_height
        FROM staff_observation s JOIN page p ON p.id=s.page_id LEFT JOIN page_render r ON r.page_id=p.id
        WHERE s.page_id=? AND s.id=? AND s.photo_state IN ('linked','placeholder','absent')""",
        (page_id,observation_id)).fetchone()
    if not row:
        return None
    card, url, name, role, page_url, html, dom, raw, render_url, capture, css, vw, vh = row
    field = "photo" if url else "card"
    if displayed and not url:
        return None
    elements = conn.execute("""SELECT selector,expected_value,visual_status,visual_details,checked_at,render_url,
        render_source,css_loaded,viewport_width,viewport_height FROM staff_element
        WHERE observation_id=? AND field=?""",(observation_id,field)).fetchall()
    if len(elements)!=1 or elements[0][1]!=(url or name or role):
        return None
    selector, _, status, measurement, checked, measured_url, method, measured_css, mw, mh = elements[0]
    details = _json(raw)
    rendered = bool(dom and details.get("staff_source")=="rendered_dom" and
                    details.get("document_complete_for_extraction") is True)
    can_crop = (rendered and method=="live" and capture==checked and render_url==measured_url
                and css==1 and measured_css==1 and (vw,vh)==(mw,mh) and status in {"visible","out_of_view"})
    asset = None
    if not displayed and url:
        asset = conn.execute("""SELECT a.data,a.sha256,a.mime,a.width,a.height,d.final_url,d.fetched_at
            FROM available_home_image_download d JOIN home_image_asset a USING(sha256)
            WHERE d.page_id=? AND d.image_url=? AND d.status='saved'""",(page_id,url)).fetchone()
    if not asset and not can_crop:
        return None
    bound = _bound_element(dom if rendered else html, render_url if rendered else page_url,
                           card, selector, url, name or role)
    if not bound:
        return None
    proof = {"card_selector":card, "subject":field, "selector":selector}
    if asset:
        blob, digest, mime, width, height, final_url, fetched = asset
        if (_usable_size(width,height) and mime in _EXT and hashlib.sha256(blob).hexdigest()==digest
                and not _DECOR.search(unquote(urlsplit(final_url).path))):
            try:
                valid = _raster(blob)==(mime,width,height)
            except (ValueError, OSError):
                valid = False
            if valid:
                return _material(page_id,observation_id,url,"original",blob,mime,width,height,
                                 {**proof,"final_url":final_url,"fetched_at":fetched})

    if not can_crop:
        return None
    facts = _json(measurement)
    measurements = details.get("staff_measurements")
    if not isinstance(measurements,list) or not any(
               isinstance(m,dict) and m.get("field")==field and m.get("selector")==selector
               and m.get("card_selector")==card and m.get("expected_value")==elements[0][1] and m.get("status")==status
               and m.get("details")==facts for m in measurements):
        return None
    soup, photo = bound
    for source, column in (("viewport_crop","screenshot"),("document_crop","full_screenshot")):
        if source=="viewport_crop" and status!="visible":
            continue
        clipped = source=="document_crop" and _clipped(soup, photo, details.get("screen") or {})
        if clipped:
            continue
        blob = conn.execute(f"SELECT {column} FROM page_render WHERE page_id=?",(page_id,)).fetchone()[0]
        if not blob:
            continue
        try:
            with Image.open(io.BytesIO(blob), formats=("JPEG","PNG","WEBP")) as image:
                if image.width*image.height>24_000_000:
                    continue
                box = staff_crop_box(status,facts,details,(vw,vh),image.size,source)
                if not box:
                    continue
                crop = image.crop(box).convert("RGB")
                output = io.BytesIO()
                crop.save(output,format="PNG")
                return _material(page_id,observation_id,url,source,output.getvalue(),"image/png",*crop.size,
                                 {**proof,"captured_at":capture,"pixel_box":list(box),"element_rect_css":facts["rect"],
                                  "scroll_css":facts["scroll"],"visual_status":status,
                                  "hit_test_available":status=="visible"})
        except (Image.DecompressionBombError, OSError, ValueError):
            continue
    return None
