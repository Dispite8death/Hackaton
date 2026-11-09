"""Homepage sections and labelled photo candidates on preschool sites."""

import re
from collections import Counter
from urllib.parse import parse_qsl, unquote, urldefrag, urlsplit

from bs4 import BeautifulSoup
from soupsieve import match

if __package__:
    from .home_content import _clean, _image_sources, _selector, _text, _url
    from .http_page import same_site
    from .staff_extract import NAME, ROLE, _name
else:
    from home_content import _clean, _image_sources, _selector, _text, _url
    from http_page import same_site
    from staff_extract import NAME, ROLE, _name


LABELS = {
    "team": ("наш коллектив","коллектив","все сотрудники","сотрудники","наши сотрудники","педагоги",
             "наши педагоги","педагогический состав","педагогический коллектив","коллектив детского сада"),
    "news": ("новости","все новости","новости детского сада","новости учреждения","наши новости"),
    "events": ("события","все события","мероприятия","все мероприятия","афиша","календарь мероприятий"),
    "combined": ("новости и события","все новости и события","новости и мероприятия","все новости и мероприятия"),
}
SLUGS = {"team": ("nash-kollektiv","kollektiv","staff","team","employees","sotrudniki","pedagogicheskiy-sostav"),
         "news": ("news","novosti"), "events": ("events","meropriyatiya","sobytiya"),
         "combined": ("novosti-i-sobytiya","novosti-i-meropriyatiya")}
REGIONS = {"team":".gw-person-list,.gw-people-list,.staff-list,.team-list,.team-section,[data-section='team']",
           "news":".gw-news,.news-list,[data-section='news']",
           "events":".tpl-component-gw-events,.events-section,.events-list,[data-section='events']"}
PERSON = ".gw-person-card,.gw-personal-card,.gw-person-info-card,.gw-employee-info,.employee-card,[itemtype$='/Person']"
EVENT = ".event-card,.gw-event-card,.event-item,[itemtype$='/Event'],[data-type='event']"
ARTICLE = ".card,article,.news-card,.news-item,a.object-index,.object,[data-type='news'],figure"
MEDIA_FILE = re.compile(r"\.(?:jpe?g|png|gif|webp|svgz?|bmp|ico|avif|tiff?|mp[34]|webm|mov|avi|"
                       r"pdf|docx?|xlsx?|pptx?|od[stp]|zip|rar|7z|xml|css|js)$",re.I)
BUILDING = re.compile(r"\b(?:здани[еяюи]\w*|фасад\w*|главн\w+\s+(?:вход|корпус)|центральн\w+\s+вход)\b",re.I)
GARDEN = re.compile(r"детск\w*\s+сад\w*|\b(?:[мг]?[абк]?доу|црр[- ]дс|дс\s*№)",re.I)
OTHER = re.compile(r"госуслуг|бан[н]?ер|афиша|бассейн|кабинет|групп\w*|зал\b|памятк\w*|праздник|акци[яи]",re.I)
WARNING = re.compile(r"памятк\w*|заброшенн\w*|аварийн\w*|предупреждаем|объект\w* повышенной опасности",re.I)
EMPTY = re.compile(r"ничего не найдено|(?:событий|мероприятий|новостей|сотрудников) (?:пока )?нет|"
                   r"нет (?:событий|мероприятий|новостей|сотрудников)",re.I)


def _key(value):
    return " ".join(re.findall(r"[а-яa-z0-9]+", _clean(value).casefold().replace("ё","е")))


def _category(value, mapping):
    return next((kind for kind, labels in mapping.items() if value in labels), None)


def _location(el):
    for node in (el,*el.parents):
        if node.name in ("header","footer","nav"):
            return node.name
        markers = " ".join([node.get("id",""), *node.get("class",[])])
        if node.get("role")=="navigation" or re.search(r"(?:^|[\s_-])(?:menu|nav|navbar|offcanvasNavLeft)(?:$|[\s_-])",markers,re.I):
            return "nav"
    return "main"


def _target(base, href):
    target = _url(base, href or "")
    if target:
        try:
            if same_site(target,base):
                return target
        except ValueError:
            pass
    return None


def _slug(url):
    return unquote(urlsplit(url).path).rstrip("/").split("/")[-1].casefold()


def _categories(kind):
    return ("news","events") if kind=="combined" else (kind,)


def _caption(el):
    img = el.select_one("img")
    return (_clean(el.get("data-description")) or _text(el.select_one("figcaption,.card-title,.h5,h2,h3,h4")) or
            (_clean(img.get("title")) or _clean(img.get("alt")) if img else "") or _text(el))[:300]


def _publication_link(el, block, page_url, listing_urls):
    anchors = []
    title = el.select_one(".card-title,.event-title,.news-title,.h5,h2,h3,h4")
    if title:
        link = title.find("a",href=True) or title.find_parent("a",href=True)
        if link is not None:
            anchors.append(link)
    for parent in (el,*el.parents):
        if parent is block:
            break
        if parent.name=="a" and parent.has_attr("href"):
            anchors.append(parent)
            break
    anchors.extend(el.select("a[href]"))
    errors,seen=[],set()
    for anchor in anchors:
        if id(anchor) in seen:
            continue
        seen.add(id(anchor))
        href=(anchor.get("href") or "").strip()
        target=_url(page_url,href)
        if not href or href.startswith("#"):
            reason="fragment_only" if href else "no_detail_link"
        elif not target:
            reason="non_http_link"
        elif not _target(page_url,href):
            reason="external_url"
        else:
            target=urldefrag(target).url
            parts=urlsplit(target)
            path=unquote(parts.path).lower().rstrip("/")
            item_query=any(k.lower() in {"id","newsid","eventid","element_id","itemid","objectid"} and v
                           for k,v in parse_qsl(parts.query))
            if (anchor.has_attr("download") or anchor.has_attr("data-gallery") or
                    MEDIA_FILE.search(path) or "/netcat_files/" in path):
                reason="media_or_file"
            elif target==urldefrag(page_url).url or path==unquote(urlsplit(page_url).path).lower().rstrip("/") and not item_query:
                reason="same_page"
            elif not item_query and (target.split("?")[0].rstrip("/") in listing_urls or _category(_slug(target),SLUGS)
                                     or any(k.casefold() in {"page","paged","curpos","category","sort","offset"}
                                            or k.casefold().startswith("filter[") for k,v in parse_qsl(parts.query))):
                reason="section_listing"
            else:
                return target, []
        errors.append({"href":href,"reason":reason})
    return None, errors or [{"href":None,"reason":"no_detail_link"}]


def _source(el, page_url, ids, kind, publication_url=None):
    anchor = el if el.name=="a" else el.select_one("h2 a[href], h3 a[href], a[href]")
    url = publication_url if kind!="team" else (_target(page_url,anchor.get("href")) if anchor else None)
    if kind=="team" and url and _category(_slug(url), SLUGS):
        return None
    if kind == "team":
        title = _name(_text(el.select_one(NAME+", .gw-person-card__nametext-break, .employee-name, [itemprop='name'], h3, h4")))
        if not title:
            return None
        role = _text(el.select_one(ROLE+", .employee-role, [itemprop='jobTitle']")) or None
    else:
        title = _text(el.select_one(".gw-text-multiline-truncate_rows_2, .name, .card-title, "
                                    ".event-title, .news-title, [itemprop='headline'], h2, h3, h4, .h5"))
        if not title and anchor and len(_text(anchor)) <= 160:
            title = _text(anchor)
        if not title:
            title = _caption(el)
        if not title or _category(_key(title), LABELS):
            return None
        role = None
    image = el.select_one("img")
    chosen = _image_sources(image,page_url)[0] if image else None
    return {"title":title, "url":url, "role":role, "selector":_selector(el,ids),
            "image_url":chosen["url"] if chosen else None, "image_alt":_clean(image.get("alt")) if image else None,
            "date_text":_text(el.select_one("time,.card-subtitle,.object-date,.event-date")) or None}


def _sections(soup, main, page_url, ids):
    result = {k:{"heading_found":False,"headings":[], "navigation_links":[], "listing_links":[], "cards":[],
                 "block_status":"not_found","evidence":{"blocks":[], "source":"html"}} for k in REGIONS}
    regions = {k:[] for k in REGIONS}
    def add_region(kind, el, combined):
        if el is not None and not any(node is el and shared==combined for node,shared in regions[kind]):
            regions[kind].append((el,combined))
    for h in main.select("h1,h2,h3,h4,[role='heading'],.h2"):
        label = _category(_key(_text(h)),LABELS)
        if not label or _location(h)!="main" or h.find_parent(["article","aside","template"]):
            continue
        for kind in _categories(label):
            item = result[kind]
            item["headings"].append({"text":_text(h),"selector":_selector(h,ids),"combined":label=="combined"})
            item["heading_found"] = True
            add_region(kind,h.find_parent("section"),label=="combined")
    for kind, sel in REGIONS.items():
        for region in main.select(sel):
            if _location(region)=="main" and not region.find_parent(["aside","template"]):
                add_region(kind,region,False)
    for a in soup.select("a[href]"):
        target = _target(page_url,a["href"])
        if not target or urlsplit(target).query:
            continue
        label = _category(_key(_text(a) or a.get("aria-label","")),LABELS)
        parts = unquote(urlsplit(target).path).strip("/").casefold().split("/")
        slug_kind = _category(parts[-1],SLUGS)
        if not slug_kind and (not label or any(_category(v,SLUGS) for v in parts[:-1])):
            continue
        if a.get("href","").startswith("#") and not soup.find(id=a["href"][1:]):
            continue
        if a.select_one(".gw-person-card__name, .card-title, .card-body") or a.find_parent(["article","template"]):
            continue
        scope = _location(a)
        bucket = "listing_links" if scope=="main" else "navigation_links"
        group = slug_kind or label
        for kind in _categories(group):
            links = result[kind][bucket]
            previous = next((x for x in links if x["url"]==target),None)
            sel = _selector(a,ids)
            if previous is None:
                previous = {"url":target,"title":_text(a) or a.get("aria-label",""),"selector":sel,
                            "locations":[],"selectors":[],"combined":group=="combined"}
                links.append(previous)
            previous["selectors"].append(sel)
            if scope not in previous["locations"]:
                previous["locations"].append(scope)

    for kind, blocks in regions.items():
        item, seen, empties = result[kind], set(), []
        shared_articles = []
        excluded = {}
        listing_urls = {link["url"].rstrip("/") for group in result.values() for bucket in ("navigation_links","listing_links")
                        for link in group[bucket]}
        def record_excluded(el, errors, scope):
            selector=_selector(el,ids)
            if selector not in excluded:
                img=el.select_one("img")
                chosen=_image_sources(img,page_url)[0] if img else None
                excluded[selector]={"selector":selector,"scope":scope,"title":_caption(el),
                                    "image_url":chosen["url"] if chosen else None,"reasons":errors}
        for block, combined in blocks:
            if kind=="team":
                cards=block.select(PERSON) or block.select("article")
            elif kind=="events" and combined:
                cards=block.select(EVENT)
                for row in block.select(ARTICLE):
                    url, errors = _publication_link(row,block,page_url,listing_urls)
                    if match(EVENT,row) or url and any(part in SLUGS["events"] for part in urlsplit(url).path.strip("/").split("/")[:-1]):
                        cards.append(row)
                    elif url:
                        shared_articles.append(url)
                    else:
                        record_excluded(row,errors,"section")
            else:
                cards=block.select(EVENT) if kind=="events" else []
                cards=cards or block.select(ARTICLE)
            item["evidence"]["blocks"].append({"selector":_selector(block,ids),"combined":combined})
            empties.append(bool(EMPTY.search(_text(block))) or block.get("data-object-count")=="0" or not _text(block))
            card_ids={id(c) for c in cards}
            if kind!="team" and not combined:
                for img in block.select("img"):
                    if any(id(p) in card_ids for p in img.parents):
                        continue
                    if img.find_parent(["aside","nav","footer","header","template"]) or _location(img)!="main":
                        continue
                    tile=img.parent
                    if tile is block or len(tile.select("img"))!=1:
                        continue
                    _,errors=_publication_link(tile,block,page_url,listing_urls)
                    if errors:
                        record_excluded(tile,errors,"section")
            for node in cards:
                if _location(node)!="main" or node.find_parent(["aside","template"]):
                    continue
                if any(id(p) in card_ids for p in node.parents):
                    continue
                target=None
                if kind!="team":
                    target,errors=_publication_link(node,block,page_url,listing_urls)
                    if not target:
                        record_excluded(node,errors,"section")
                        continue
                value=_source(node,page_url,ids,kind,target)
                if value is None:
                    continue
                if kind=="news" and (match(EVENT,node) or value["url"] and
                        any(part in SLUGS["events"] for part in urlsplit(value["url"]).path.strip("/").split("/")[:-1])):
                    continue
                key=(value["url"] or value["selector"],value["title"])
                if key not in seen:
                    seen.add(key)
                    item["cards"].append(value)
        if kind=="events":
            item["evidence"]["unclassified_combined_urls"]=list(dict.fromkeys(shared_articles))
        if kind!="team":
            for tile in main.select(".gw-slider a[data-gallery], .facility-gallery figure"):
                if tile.select_one("img") and _location(tile)=="main":
                    _,errors=_publication_link(tile,main,page_url,listing_urls)
                    if errors:
                        record_excluded(tile,errors,"photo_gallery")
            item["evidence"]["excluded_count"]=len(excluded)
            item["evidence"]["excluded_examples"]=list(excluded.values())[:12]
        item["block_status"]=("cards" if item["cards"] else "empty" if empties and all(empties) else
                              "ambiguous" if blocks or item["heading_found"] else "not_found")
    return result


def _photos(main,page_url,org_name,terms,ids):
    candidates = []
    heading = _text(main.select_one("h1"))
    org = _key(org_name)
    configured = (terms or {}).get("garden_main_photo", ()) or (terms or {}).get("main_photo", ())
    for img in main.select("img"):
        if _location(img)!="main" or img.find_parent(["article","aside","footer","template"]):
            continue
        parents = [img,*img.parents]
        classes = {c for tag in parents for c in tag.get("class",())}
        if classes & {"gw-news","gw-person-card","card-body","gw-widget-edg","pos-banner-fluid","gw-main-banner"}:
            continue
        logo = bool(classes & {"gw-gradient-block__logo","gw-gradient-block__logo-image","institution-logo"})
        slider = bool(classes & {"gw-slider","gw-js-lightbox-slider","facility-gallery"})
        hero = bool(classes & {"hero","intro","hero-image","garden-intro","gw-gradient-block"})
        source, choices = _image_sources(img,page_url)
        if not source:
            continue
        alt, title = _clean(img.get("alt")), _clean(img.get("title"))
        a = img.find_parent("a")
        figure = img.find_parent("figure")
        caption = _clean(a.get("data-description")) if a else ""
        caption = caption or (_text(figure.select_one("figcaption")) if figure else "")
        label = " ".join(v for v in (alt,title,caption) if v)
        name_match = bool(org and (org in _key(label) or (_key(heading) and _key(heading) in _key(label))))
        garden = bool(GARDEN.search(label) or name_match)
        building = bool(BUILDING.search(label) and not WARNING.search(label))
        if not (logo or slider or hero or building and garden) or (not logo and OTHER.search(alt+" "+title)):
            continue
        if not logo and not (garden or building or hero):
            continue
        path = urlsplit(source["url"]).path.lower()
        default = logo and (path.endswith("/kinder/img/cube.svg") or "placeholder" in path)
        source_kind = ("template_logo" if default else "institution_logo" if logo else "slider_image" if slider else "intro_image")
        signals = [source_kind]
        if garden:
            signals.append("institution_label")
        if name_match:
            signals.append("institution_name_matches")
        if building:
            signals.append("building_in_local_caption")
        words = [w for w in configured if len(_key(w))>3 and _key(w) in _key(label)]
        if words:
            signals.append("configured_photo_term")
        value = {"image_url":source["url"],"alt":alt or None,"title":title or None,"selector":_selector(img,ids),
                 "source_kind":source_kind,"signals":signals,"context":caption[:600],"source_attribute":source["attribute"],
                 "source_options":choices,"matched_terms":words}
        # A logo's institution name identifies the logo slot, not the subject depicted.
        building_rank = 0 if logo else 3 if building and (garden or hero or slider) else 1 if garden else 0
        rank = (5 if building_rank==3 else 2 if default else 4 if logo else 3 if hero and garden else
                1 if slider or hero else 0)
        candidates.append((rank,building_rank,value))

    def choose(ranked):
        if not ranked:
            return None,False
        ranked.sort(key=lambda v:-v[0])
        first = ranked[0]
        tied = any(rank==first[0] and val["image_url"]!=first[1]["image_url"] for rank,val in ranked[1:])
        return first,tied
    building_rows = [(b,v) for _,b,v in candidates if b]
    building,btie = choose(building_rows)
    bval = building[1] if building and building[0]>=3 and not btie else {}
    building_result = {"status":"not_identified" if not building else "candidate" if bval else "ambiguous",
                       "image_url":bval.get("image_url"),"selector":bval.get("selector"),"signals":bval.get("signals",[]),
                       "alternatives":[v for _,v in building_rows if v is not bval]}
    ranked = [(rank,v) for rank,_,v in candidates if rank]
    first,tied = choose(ranked)
    best = first[1] if first else {}
    evidence={**best,"selector":best.get("selector"),"signals":best.get("signals",[]),"context":best.get("context",""),
              "source_kind":best.get("source_kind"),"building_verified":False,"building_candidate":building_result,
              "alternatives":[v for _,v in ranked if v is not best]}
    return {"status":"not_found" if not first else "ambiguous" if tied or first[0]<=2 else "found",
            "image_url":None if tied else best.get("image_url"),"alt":None if tied else best.get("alt"),"evidence":evidence}


def extract_garden_home(html: str | BeautifulSoup,page_url: str,org_name: str,terms: dict) -> dict:
    soup = html if isinstance(html,BeautifulSoup) else BeautifulSoup(html,"html.parser")
    main = soup.select_one("main,[role='main']") or soup.body or soup
    ids = Counter(el["id"] for el in soup.select("[id]"))
    return {**_sections(soup,main,page_url,ids),"main_photo":_photos(main,page_url,org_name,terms,ids)}
