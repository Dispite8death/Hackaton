"""Stop expanding an observed forward pager that repeatedly serves the same list."""

import json
import re
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup


def _cursor(url):
    parts=urlsplit(url)
    keys=[]
    positions=[]
    for key,value in parse_qsl(parts.query,keep_blank_values=True):
        if re.fullmatch(r"page|paged|offset|curpos|pagen_\d+",key,re.I) and value.isdecimal():
            positions.append((key.lower(),int(value)))
        else:
            keys.append((key,value))
    path=parts.path
    if not positions:
        match=re.search(r"/(?:page[-/]|p)(\d+)/?$",path,re.I)
        if match:
            positions=[("page",int(match[1]))]
            path=path[:match.start(1)]
    if len(positions)!=1:
        return None
    return (parts.scheme,parts.netloc,path,tuple(sorted(keys)),positions[0][0]),positions[0][1]


def _marks(conn, pid, section):
    if section=="news":
        listing=conn.execute("SELECT page_kind FROM news_page WHERE page_id=?",(pid,)).fetchone()
        if not listing or listing[0]!="news_listing":
            return None
        return set(conn.execute("SELECT url,title FROM news_preview WHERE page_id=?",(pid,)).fetchall())
    if section=="events":
        return set(conn.execute("SELECT url,title FROM home_event_link WHERE page_id=? AND kind='event'",(pid,)).fetchall())
    if section in {"team","staff","management"}:
        return set(conn.execute("SELECT profile_url,full_name,role FROM staff_observation WHERE page_id=?",(pid,)).fetchall())
    if section in {"documents","programs","information","staff_resources"}:
        evidence=conn.execute("SELECT evidence FROM institution_document_page WHERE page_id=?",(pid,)).fetchone()
        if evidence:
            return {(d.get("url"),d.get("title")) for d in json.loads(evidence[0]).get("documents",[])}
        # Indexing normally runs when the group of visits finishes.
        return {tuple(row) for row in conn.execute("""SELECT l.url,l.label FROM crawl_link l JOIN crawl_fetch f
            ON f.id=l.fetch_id WHERE f.page_id=? AND l.relation='document'""",(pid,))}
    return None


def repeating_pager(conn, page_id):
    """Three direct, increasing offsets in one component. Published alternatives remain recorded."""
    chain=[]
    pid=page_id
    for _ in range(3):
        row=conn.execute("""SELECT f.url,f.section,b.source_page_id,r.status,p.id_organization
            FROM crawl_fetch f JOIN page p ON p.id=f.page_id JOIN page_render r ON r.page_id=p.id
            JOIN material_backfill b ON b.run_id=f.run_id AND b.url=f.url AND b.organization_id=p.id_organization
            WHERE f.page_id=? AND b.relation='pagination'""",(pid,)).fetchone()
        if not row or row[3]!="complete":
            return False
        cursor=_cursor(row[0])
        if not cursor or chain and (cursor[0]!=chain[-1][1][0] or cursor[1]>=chain[-1][1][1]
                                    or row[1]!=chain[-1][2] or row[4]!=chain[-1][3]):
            return False
        chain.append((pid,cursor,row[1],row[4]))
        pid=row[2]
    marks=_marks(conn,page_id,chain[0][2])
    if marks is None or any(_marks(conn,p,sec)!=marks for p,_,sec,_ in chain[1:]):
        return False
    if not marks:
        def visible_content(p):
            html=conn.execute("SELECT rendered_html FROM page_render WHERE page_id=?",(p,)).fetchone()[0]
            dom=BeautifulSoup(html or "","html.parser")
            main=dom.select_one("main,[role=main]") or dom.body or dom
            for node in main.select("nav,header,footer,aside,script,style,[role=navigation],[class*=paginat],.pager"):
                node.decompose()
            anchors=sorted(a["href"] for a in main.select("a[href]") if not _cursor(a["href"]))
            images=sorted(a["src"] for a in main.select("img[src]"))
            return " ".join(main.stripped_strings),anchors,images
        if len({repr(visible_content(p)) for p,_,_,_ in chain})!=1:
            return False
    with conn:
        conn.execute("INSERT OR IGNORE INTO material_pager_stop VALUES(?,?,?,?)",
                     (page_id,chain[1][0],chain[2][0],"same_entries_on_three_offsets" if marks else "three_empty_offsets"))
    return True
