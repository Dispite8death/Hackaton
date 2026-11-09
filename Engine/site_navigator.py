"""Locate a named site navigator and preserve links from its own panel or map."""

import argparse
import json
import re
from collections import Counter
from contextlib import closing
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Tag

if __package__:
    from .home_content import _selector
    from .http_page import same_site
    from .navigation import GARDEN_COMBINED, classify, resolve_url, words
else:
    from home_content import _selector
    from http_page import same_site
    from navigation import GARDEN_COMBINED, classify, resolve_url, words


_NAMES = {"навигатор по сайту","карта сайта","навигация по сайту","sitemap","site map"}
_MAP_LIST = ".gw-site-map-list, .sitemap-list, .site-map-list, [itemtype$='/SiteNavigationElement'][aria-label]"


def _named(label):
    return re.sub(r"^(?:открыть|показать|перейти в) ", "", words(label or "")) in _NAMES


def _lineage(node):
    return (t for t in (node, *node.parents) if isinstance(t,Tag))


def _crumb_or_template(node):
    return any(t.name in {"template","noscript"} or any("breadcrumb" in c.lower() for c in t.get("class", ()))
               or t.get("itemtype","").endswith("/BreadcrumbList")
               or words(t.get("aria-label","")) in {"breadcrumb","breadcrumbs","хлебные крошки"}
               for t in _lineage(node))


def _location(node):
    for t in _lineage(node):
        mark = " ".join([t.get("id",""), *t.get("class", ())]).lower()
        if "offcanvas" in mark or "mobile-menu" in mark:
            return "mobile_menu"
        if t.name == "footer" or "gw-footer" in mark:
            return "footer"
        if t.name == "header" or "gw-header" in mark or "gw-top-nav" in mark:
            return "header"
        if t.name in {"aside","main"}:
            return "sidebar" if t.name=="aside" else "main"
    return "document"


def _labels(node, unique_ids):
    labels = [node.get("aria-label"), node.get_text(" ",strip=True), node.get("title")]
    refs = (node.get("aria-labelledby") or "").split()
    if refs:
        labels.append(" ".join(unique_ids[key].get_text(" ",strip=True) for key in refs if key in unique_ids))
    return [label for label in labels if label]


def discover_site_navigator(html, page_url, site_url, org_kind, *, is_map_page=False):
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")
    elements = soup.find_all(id=True)
    ids = Counter(t["id"] for t in elements)
    unique_ids = {t["id"]:t for t in elements if ids[t["id"]]==1}
    base_tag = soup.find("base", href=True)
    base = resolve_url(page_url, base_tag["href"]) if base_tag else page_url
    base = base or page_url
    entries, panels, links = [], {}, []
    seen = set()

    def panel(node, label, kind):
        key = id(node)
        if key not in panels:
            panels[key] = (node, {"label":label,"kind":kind,"selector":_selector(node,ids),"location":_location(node)})
        return panels[key][1]["selector"]

    for trigger in soup.select("a, button, [role=button], [role=link], summary"):
        label = next((text for text in _labels(trigger, unique_ids) if _named(text)), None)
        if label is None or _crumb_or_template(trigger):
            continue
        href = trigger.get("href")
        item = {"label":label,"href":href,"url":None,"selector":_selector(trigger,ids),"location":_location(trigger),
                "panel_selector":None,"kind":"unresolved_control"}
        refs = (trigger.get("aria-controls") or "").split()
        if trigger.get("popovertarget"):
            refs.append(trigger["popovertarget"])
        for value in (trigger.get("data-bs-target"),trigger.get("data-target"),href):
            if value and value.startswith("#") and len(value)>1:
                refs.append(unquote(value[1:]))
        if href and "#" in href and not href.startswith("#") and resolve_url(base,href)==resolve_url(page_url,""):
            refs.append(unquote(href.partition("#")[2]))
        target = next((unique_ids[k] for k in refs if k in unique_ids), None)
        if target is None and trigger.name == "summary" and trigger.parent.name == "details":
            target = trigger.parent
        if target is not None and target.name not in {"body","html"} and not _crumb_or_template(target):
            item.update(kind="panel_control",panel_selector=panel(target,label,"panel"))
        elif href and not href.startswith("#"):
            real = resolve_url(base, href)
            if real and same_site(real,site_url):
                item.update(kind="map_link",url=real)
            elif real:
                item["kind"] = "external_reference"
        entries.append(item)

    # A labelled navigation region can exist without any control to open it.
    for node in soup.select("nav[aria-label],aside[aria-label],section[aria-label],[role=navigation][aria-label],"
                            "nav[aria-labelledby],aside[aria-labelledby],section[aria-labelledby],[role=navigation][aria-labelledby]"):
        label = next((s for s in _labels(node,unique_ids) if _named(s)), None)
        if label and not _crumb_or_template(node):
            panel(node,label,"panel")
    for title in soup.select("h2,h3,h4"):
        if not _named(title.get_text(" ",strip=True)) or _crumb_or_template(title):
            continue
        owner = title.find_parent(["nav","aside","section","dialog"])
        if owner and owner.find("a",href=True):
            panel(owner,title.get_text(" ",strip=True),"panel")

    main = soup.find("main") or soup.find(attrs={"role":"main"}) or soup.body or soup
    h1 = next((h for h in main.select("h1") if not _crumb_or_template(h)), None)
    title = h1.get_text(" ",strip=True) if h1 is not None else ""
    page_title = soup.title.get_text(" ",strip=True) if soup.title else ""
    map_lists = [t for t in main.select(_MAP_LIST) if not _crumb_or_template(t) and
                 _location(t) not in {"header","footer","mobile_menu"} and
                 ("SiteNavigationElement" not in t.get("itemtype","") or _named(t.get("aria-label","")))]
    containers = {id(t) for t in map_lists}
    map_lists = [t for t in map_lists if not any(id(parent) in containers for parent in t.parents)]
    named_page = _named(title) or (not title and _named(page_title))
    # The saved response may be a fragment without a title or main. An explicit
    # map-list component and a map URL together identify that case.
    map_path = re.fullmatch(r"(?:map|site[-_]?map|karta-sa[yj]ta)(?:\.html?)?",
                            urlsplit(page_url).path.rstrip("/").rsplit("/",1)[-1], re.I)
    actual_map = named_page or bool(map_lists and (is_map_page or map_path))
    if actual_map:
        for container in map_lists or [main]:
            panel(container,title or page_title or "Карта сайта","map_page")

    page = urlsplit(page_url)
    for node, scope in panels.values():
        for a in node.select("a[href]"):
            if _crumb_or_template(a) or (scope["kind"]=="map_page" and _location(a) in {"header","footer","mobile_menu"}):
                continue
            href = a["href"].strip()
            if not href or href.startswith("#"):
                continue
            url = resolve_url(base, href)
            if not url or not same_site(url,site_url):
                continue
            label = next(iter(_labels(a,unique_ids)), "")
            link = classify(url,label,a.attrs,False,False,False,site_url,page,org_kind,"hub",None)
            section, relation, follow = (link.section,link.relation,link.follow) if link else ("unknown","navigation",False)
            roles = [section]
            if org_kind=="kindergarten" and section=="news" and (words(label) in GARDEN_COMBINED or
                    words(unquote(urlsplit(url).path.rstrip("/").split("/")[-1])) in GARDEN_COMBINED):
                roles.append("events")
            selector = _selector(a,ids)
            for role in roles:
                if (selector,role) in seen:
                    continue
                seen.add((selector,role))
                links.append({"url":url,"href":href,"label":label,"section":role,"relation":relation,
                              "follow":bool(follow and url!=page_url),"source":"navigator","container":scope["kind"],
                              "selector":selector,"panel_selector":scope["selector"]})
    return {"is_navigator_page":actual_map,"map_page_requested":bool(is_map_page),"entries":entries,
            "panels":[s for _,s in panels.values()],"links":links}


_UPSERT = """
INSERT INTO site_navigator_page(page_id,is_navigator_page,evidence) VALUES(?,?,?)
ON CONFLICT(page_id) DO UPDATE SET is_navigator_page=excluded.is_navigator_page,evidence=excluded.evidence
"""


def _values(conn, page_id):
    row = conn.execute("""
        SELECT p.url,p.text_page,o.site_url,o.kind,f.section,f.depth,r.rendered_html,
               json_extract(r.details,'$.document_complete_for_extraction'), json_extract(r.details,'$.document_ready_state')
        FROM page p JOIN organization o ON o.id=p.id_organization LEFT JOIN crawl_fetch f ON f.page_id=p.id
        LEFT JOIN page_render r ON r.page_id=p.id WHERE p.id=?
    """, (page_id,)).fetchone()
    if row is None:
        return None
    url,raw,site,kind,section,depth,dom,complete,ready = row
    rendered = bool(dom) and complete != 0 and ready != "loading"
    report = discover_site_navigator(dom if rendered else raw,url,site,kind,is_map_page=section=="site_navigator")
    if (section not in {"about","home","hub","site_navigator"} and depth!=0 and
            not report["entries"] and not report["panels"]):
        return None
    report["source"] = "rendered_dom" if rendered else "response_html"
    return page_id,int(report["is_navigator_page"]),json.dumps(report,ensure_ascii=False,separators=(",",":"))


def index_navigator(conn, page_id):
    values = _values(conn,page_id)
    if values is None:
        conn.execute("DELETE FROM site_navigator_page WHERE page_id=?", (page_id,))
        return False
    conn.execute(_UPSERT, values)
    return True


def main():
    if __package__:
        from .config import connect_db
        from .crawl_store import initialize_db
    else:
        from config import connect_db
        from crawl_store import initialize_db
    parser = argparse.ArgumentParser(description="Проиндексировать именованные навигаторы из сохранённых снимков")
    parser.add_argument("--index-all", required=True, action="store_true")
    parser.parse_args()
    with closing(connect_db()) as conn:
        initialize_db(conn)
        ids = [r[0] for r in conn.execute("SELECT id FROM page ORDER BY id")]
        count = 0
        for i in range(0,len(ids),25):
            rows = [(pid,_values(conn,pid)) for pid in ids[i:i+25]]
            with conn:
                conn.executemany(_UPSERT, [v for _,v in rows if v])
                conn.executemany("DELETE FROM site_navigator_page WHERE page_id=?", [(pid,) for pid,v in rows if not v])
            count += sum(v is not None for _,v in rows)
        print(f"Проверено снимков: {len(ids)}, сохранено результатов: {count}")


if __name__ == "__main__":
    main()
