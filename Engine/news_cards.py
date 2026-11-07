"""Visible date evidence and on-page ordering of linked publication previews."""

import re
from collections import Counter
from datetime import date
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment

if __package__:
    from .garden_home import _caption, _category, _key, _location, _publication_link, _target, LABELS
    from .home_content import _image_sources, _selector, _text
else:
    from garden_home import _caption, _category, _key, _location, _publication_link, _target, LABELS
    from home_content import _image_sources, _selector, _text


MONTHS = {}
for number, pair in enumerate(zip(
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split(),
    "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь".split()),1):
    MONTHS.update({m:number for m in pair})
MONTH = "|".join(sorted(MONTHS,key=len,reverse=True))
DATES = re.compile(r"(?<![\w.])(?:(?P<iso>\d{4}-\d{2}-\d{2})|"
                   r"(?P<num>\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2}))|"
                   rf"(?P<d>\d{{1,2}})(?:\s*[-–—]\s*(?P<d2>\d{{1,2}}))?\s+(?P<month>{MONTH})"
                   r"(?:\s+(?P<y>\d{4})(?:\s*г(?:ода|\.)?)?)?)(?!\w)", re.I)
FIELDS = ("time,.gw-news-item__date,.card-subtitle,.object-date,.object-index-date-info,.event-date,"
          ".news-date,.publication-date,.date,[itemprop='datePublished']")
BLOCKS = ".gw-news,.news-list,.news-section,.tpl-component-gw-news,[data-section='news'],[data-news]"
CARDS = ".gw-news-item,.card,.news-item,.news-card,article,a.object-index,.object,[itemtype$='/NewsArticle'],figure"
TITLE = ".gw-news-item__title-link,.gw-news-item__title,.card-title,.news-title,.name,.gw-text-multiline-truncate_rows_2,.h5,h2,h3,h4"


def _hidden(node):
    return (node.name in {"script","style","template","noscript","svg","nav","footer","button","select"} or
            node.has_attr("hidden") or node.get("aria-hidden")=="true" or
            bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)\b",node.get("style",""),re.I)))


def _parts(m):
    if m["iso"]:
        y, month, day = map(int,m["iso"].split("-"))
        return [(y,month,day)]
    if m["num"]:
        d, month, y=m["num"].split(".")
        return [(int(y) if len(y)==4 else None,int(month),int(d))]
    return [(int(m["y"]) if m["y"] else None, MONTHS[m["month"].lower()],int(day))
            for day in (m["d"],m["d2"]) if day]


def _occurrences(text, field, selector):
    scan = re.sub(r"(?:https?://|www\.)\S+",lambda m:" "*len(m[0]),text,flags=re.I)
    matches=[(m.start(),m.end(),m[0],point) for m in DATES.finditer(scan) for point in _parts(m)]
    if field:
        for short in re.finditer(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?![\d.])",scan):
            if not any(start<=short.start()<end for start,end,_,_ in matches):
                matches.append((short.start(),short.end(),short[0],(None,int(short[2]),int(short[1]))))
        if not matches and re.fullmatch(rf"(?:{MONTH})(?:\s+\d{{4}})?|\d{{4}}|(?:0?[1-9]|[12]\d|3[01])",scan,re.I):
            matches.append((0,len(text),text,(None,None,None)))
    found=[]
    for start,end,raw,(y,m,d) in sorted(matches,key=lambda entry:entry[0]):
        status,value="incomplete_date",None
        if m is not None:
            try:
                point=date(y if y is not None else 2000,m,d)
                if y is not None:
                    status,value="valid",point.isoformat()
            except ValueError:
                status="invalid_date"
        found.append({"selector":selector,"start":start,"end":end,"raw":raw,"date_status":status,"date_iso":value})
    return found


def _dates(card,ids):
    fields=card.select(FIELDS)
    marked={id(n) for n in fields}
    groups={}
    for string in card.find_all(string=True):
        if isinstance(string,Comment) or not string.strip():
            continue
        parents=[]
        for node in string.parents:
            parents.append(node)
            if node is card:
                break
        if any(_hidden(n) for n in parents):
            continue
        # A nested time and its wrapping subtitle describe one displayed field.
        root=next((n for n in reversed(parents) if id(n) in marked),None)
        field=root is not None
        if root is None:
            root=next((n for n in parents if n.name in {"p","div","li","h1","h2","h3","h4","h5","h6"}),card)
        entry=groups.setdefault(id(root),(root,field,[]))
        entry[2].append(str(string))
    sources, occurrences=[],[]
    for root,field,strings in groups.values():
        text=" ".join(" ".join(strings).split())
        selector=_selector(root,ids)
        hits=_occurrences(text,field,selector)
        if not hits and not field:
            continue
        source={"selector":selector,"role":"date_field" if field else "preview_text","text":text}
        if field:
            source["technical_datetimes"]=[n["datetime"] for n in (root,*root.select("[datetime]")) if n.has_attr("datetime")]
        sources.append(source)
        for hit in hits:
            hit.update(source_index=len(sources)-1, position=len(occurrences))
            occurrences.append(hit)
    status=("missing_date" if not occurrences else "multiple_dates" if len(occurrences)>1
            else occurrences[0]["date_status"])
    return {"date_status":status,"date_iso":occurrences[0]["date_iso"] if status=="valid" else None,
            "date_text":" | ".join(hit["raw"] for hit in occurrences) or None,
            "date_evidence":{"method":"visible_preview_text","sources":sources,"occurrences":occurrences,
                             "position_basis":"zero-based character offsets in the normalized source text"}}


def extract_news_page(html, page_url, *, section="news", page_kind="news_listing"):
    """One document's preview cards in DOM order. detail pages have no preview-order criterion."""
    result={"page_kind":page_kind,"section":section,"cards":[],"excluded":[],"order_status":"not_applicable",
            "inversions":[],"order_evidence":{"dated_count":0,"unresolved_ordinals":[]}}
    if page_kind in {"news_detail","detail","event_detail","events_detail"}:
        return result
    soup=html if isinstance(html,BeautifulSoup) else BeautifulSoup(html,"html.parser")
    ids=Counter(n["id"] for n in soup.select("[id]"))
    main=soup.select_one("main,[role='main']") or soup.body or soup
    components=BLOCKS + (",.tpl-component-gw-events,.events-list" if section=="events" else "")
    blocks=list(main.select(components))
    for h in main.select("h1,h2,h3,[role='heading']"):
        if _category(_key(_text(h)),LABELS) in {"news","combined"}:
            parent=h.find_parent("section")
            if parent is not None:
                blocks.append(parent)
    if not blocks and page_kind in {"news_listing","listing"}:
        blocks=[main]
    regions={id(b):b for b in blocks}
    selectors=CARDS + ",.news-list li,[data-news] li" + (",.event-card,.event-item,[itemtype$='/Event']" if section=="events" else "")
    rows=main.select(selectors)
    row_nodes={id(n) for n in rows}
    listing_urls={page_url.split("?")[0].rstrip("/")} if page_kind!="home" else set()
    for a in main.select("a[href]"):
        if _category(_key(_text(a)),LABELS) in {"news","combined","events"}:
            listing_url=_target(page_url,a["href"])
            if listing_url:
                listing_urls.add(listing_url.split("?")[0].rstrip("/"))
    for el in rows:
        block=next((p for p in el.parents if id(p) in regions),None)
        if block is None or _location(el)!="main":
            continue
        ancestors=list(el.parents)
        if (any(id(p) in row_nodes for p in ancestors[:ancestors.index(block)]) or
                any(_hidden(p) or p.name=="aside" for p in [el,*ancestors])):
            continue
        if any(set(p.get("class",())) & {"gw-person-card","gw-group-card","gw-widget-edg","gw-main-banner",
                                          "groups-list","staff-list","facility-gallery","gw-slider"} for p in [el,*ancestors]):
            continue
        url,errors=_publication_link(el,block,page_url,listing_urls)
        title_field=el.select_one(TITLE)
        title=_text(title_field) or _caption(el)
        selector=_selector(el,ids)
        if url and title_field is None and el.select_one("img") and not el.select_one(FIELDS):
            path_parts=set(urlsplit(url).path.lower().strip("/").split("/"))
            if el.name=="figure" or path_parts & {"gallery","photo","photos","album","foto","fotoalbom","fotogalereya"}:
                errors=[{"href":url,"reason":"photo_gallery_without_publication_preview"}]
                url=None
        if not url or not title or _category(_key(title),LABELS):
            result["excluded"].append({"selector":selector,"title":title,
                                       "reasons":errors or [{"reason":"no_publication_title"}]})
            continue
        image=el.select_one("img")
        source=_image_sources(image,page_url)[0] if image else None
        result["cards"].append({"ordinal":len(result["cards"]),"url":url,"title":title,"selector":selector,
                                "image_url":source["url"] if source else None,
                                "image_alt":image.get("alt") if image else None,**_dates(el,ids)})
    known=[c for c in result["cards"] if c["date_status"]=="valid"]
    gaps=[c["ordinal"] for c in result["cards"] if c["date_status"]!="valid"]
    for prev,cur in zip(known,known[1:]):
        if cur["date_iso"]>prev["date_iso"]:
            result["inversions"].append({"previous_ordinal":prev["ordinal"],"current_ordinal":cur["ordinal"],
                                         "older_ordinal":prev["ordinal"],"newer_ordinal":cur["ordinal"],
                                         "previous_date":prev["date_iso"],"current_date":cur["date_iso"]})
    result["order_status"]=("insufficient_cards" if len(result["cards"])<2 else "partial" if gaps else
                            "out_of_order" if result["inversions"] else "newest_first")
    result["order_evidence"].update(dated_count=len(known), unresolved_ordinals=gaps)
    return result
