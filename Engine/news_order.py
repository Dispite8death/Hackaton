"""Compare only linked adjacent preview pages from one institution and crawl run."""

import re
from collections import Counter, defaultdict
from datetime import date
from urllib.parse import parse_qsl, urlsplit

if __package__:
    from .http_page import same_site
else:
    from http_page import same_site


def _location(url):
    """Separate a feed's identity (including filters) from its page position."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    query = parse_qsl(parts.query, keep_blank_values=True)
    step, pos, component, scope = [], None, None, []
    for key, value in query:
        key = key.casefold()
        if key in {"page","paged","curpos","offset"} or key.startswith("pagen_"):
            step.append(("offset" if key in {"curpos","offset"} else "page", value))
        elif key == "cur_cc":
            component = value
        elif key != "isnaked":
            scope.append((key,value))
    match = re.search(r"/(?:page/|page-|p)(\d+)$",path,re.I)
    if match:
        step.append(("page",match[1]))
        path = path[:match.start()] or "/"
    if any(k.casefold()=="isnaked" and v=="1" for k,v in query) and "/" in path:
        parent, _, leaf = path.rpartition("/")
        if leaf.lower() == parent.rsplit("/",1)[-1].lower()+".html":
            path=parent or "/"
    mode=None
    if step:
        if len(step)==1 and re.fullmatch(r"\d{1,12}", step[0][1]):
            mode,raw = step[0]
            pos=int(raw)
        else:
            mode="unknown"
    return path,sorted(scope),component,mode,pos


def _neighbor(source_url, target_url, site, size, label):
    try:
        if not same_site(source_url,site) or not same_site(target_url,site):
            return "ignore","different_site"
        path,scope,component,mode,pos = _location(source_url)
        next_path,next_scope,next_component,next_mode,next_pos = _location(target_url)
    except ValueError:
        return "unknown","invalid_pagination_url"
    if (path,scope)!=(next_path,next_scope) or component and next_component and component!=next_component:
        return "ignore","different_feed_or_filter"
    if not next_mode and mode and pos is not None:
        return "ignore","backward_or_self"
    if next_pos is None or mode is not None and mode!=next_mode:
        return "unknown","unverified_page_position"
    baseline = pos if pos is not None else (0 if next_mode=="offset" else 1)
    gap = next_pos-baseline
    if gap<=0:
        return "ignore","backward_or_self"
    if next_mode=="page":
        return ("next","page_increment") if gap==1 else ("ignore","nonadjacent_position")
    if not size:
        return "unknown","unknown_offset_step"
    number=int(label) if label and re.fullmatch(r"\d{1,8}",label) else None
    numbered_next=(number is not None and number>=2 and next_pos%(number-1)==0 and
                   baseline==(number-2)*(next_pos//(number-1)))
    if gap==size:
        if number is not None and not numbered_next:
            return "unknown","offset_and_link_label_disagree"
        return "next","offset_matches_card_count"
    # If the page itself labels this the next leaf, a truncated source may
    # explain a mismatch. Its last preview cannot establish that boundary.
    if numbered_next or re.search(r"следующ|показать е[щш][её]|далее|\bnext\b",label or "",re.I):
        return "unknown","offset_disagrees_with_card_count"
    return "ignore","nonadjacent_position"


def _boundary(conn,page_id,ordinal):
    row=conn.execute("""
        SELECT ordinal,url,title,date_status,date_iso FROM news_preview WHERE page_id=? AND ordinal=?
    """,(page_id,ordinal)).fetchone()
    if not row:
        return None
    return dict(zip(("ordinal","url","title","date_status","date_iso"),row),page_id=page_id)


def _valid_date(item):
    if item["date_status"]!="valid" or not isinstance(item["date_iso"],str):
        return False
    try:
        return date.fromisoformat(item["date_iso"]).isoformat()==item["date_iso"]
    except ValueError:
        return False


def compare_news_pages(conn,run_id):
    run=conn.execute("""SELECT r.id_organization,o.site_url FROM crawl_run r JOIN organization o
                        ON o.id=r.id_organization WHERE r.id=?""",(run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Не найден обход {run_id}")
    org,site=run
    rows=conn.execute("""
        SELECT f.id,f.page_id,f.section,f.url,f.final_url,p.url,n.page_kind,n.card_count,f.http_status
        FROM crawl_fetch f LEFT JOIN page p ON p.id=f.page_id AND p.id_organization=?
        LEFT JOIN news_page n ON n.page_id=p.id WHERE f.run_id=? ORDER BY f.id
    """,(org,run_id)).fetchall()
    targets=defaultdict(list)
    for row in rows:
        for url in {u for u in (row[3],row[4],row[5]) if u}:
            targets[url.split("#")[0]].append(row)

    pairs, ignored, seen = [],[],set()
    for fetch_id,pid,section,_,_,source_url,kind,size,_ in rows:
        if kind!="news_listing" or not source_url:
            continue
        for target,label,attached_section,relation,follow in conn.execute("""
            SELECT url,label,section,relation,follow FROM crawl_link WHERE fetch_id=?
                AND relation IN ('pagination','pagination_unresolved') ORDER BY rowid
        """,(fetch_id,)):
            key=(pid,target,attached_section,relation)
            if key in seen:
                continue
            seen.add(key)
            pair={"source_page_id":pid,"source_url":source_url,"section":section,"target_url":target,
                  "target_page_id":None,"label":label,"previous":None,"current":None}
            if attached_section!=section:
                ignored.append({**pair,"reason":"different_section"})
                continue
            if relation=="pagination_unresolved" or not follow:
                pairs.append({**pair,"status":"uncertain","reason":"unresolved_control"})
                continue
            mode,why=_neighbor(source_url,target,site,size,label)
            if mode=="ignore":
                ignored.append({**pair,"reason":why})
                continue
            candidates=targets.get(target.split("#")[0],[])
            scoped=[r for r in candidates if r[2]==section]
            if candidates and not scoped:
                ignored.append({**pair,"reason":"different_section"})
                continue
            destinations={r[1]:r for r in scoped if r[1] is not None and r[6]=="news_listing"}
            if mode=="unknown":
                pairs.append({**pair,"status":"uncertain","reason":why})
                continue
            pair.update(adjacency=why,previous=_boundary(conn,pid,size-1))
            if len(destinations)!=1:
                reason=("ambiguous_target_snapshot" if destinations else "target_unvisited" if not scoped else
                        "target_unavailable" if not any(r[1] is not None for r in scoped) else "target_not_indexed_as_listing")
                pairs.append({**pair,"status":"uncertain","reason":reason,
                              "target_fetch_ids":[r[0] for r in scoped]})
                continue
            target_pid,destination=next(iter(destinations.items()))
            # A redirect to the first page, or to another list, does not represent the advertised next leaf.
            final_mode,final_reason=_neighbor(source_url,destination[5],site,size,label)
            if final_mode!="next":
                pairs.append({**pair,"target_page_id":target_pid,"status":"uncertain",
                              "reason":"target_redirect_position_unknown","redirect_reason":final_reason})
                continue
            pair.update(target_page_id=target_pid,current=_boundary(conn,target_pid,0))
            a,b=pair["previous"],pair["current"]
            if a is None or b is None:
                status,reason="uncertain","boundary_card_missing"
            elif not _valid_date(a) or not _valid_date(b):
                status,reason="uncertain","boundary_date_unresolved"
            elif a["date_iso"] < b["date_iso"]:
                status,reason="out_of_order","later_date_on_next_page"
            else:
                status,reason="newest_first","boundary_dates_nonincreasing"
            pairs.append({**pair,"status":status,"reason":reason})
    counts=Counter(p["status"] for p in pairs)
    status=("out_of_order" if counts["out_of_order"] else "uncertain" if counts["uncertain"] else
            "newest_first" if counts["newest_first"] else "no_linked_pages")
    return {"run_id":run_id,"organization_id":org,"status":status,"pairs":pairs,"ignored_links":ignored,
            "summary":{"listings":sum(r[6]=="news_listing" for r in rows),"newest_first":counts["newest_first"],
                       "out_of_order":counts["out_of_order"],"uncertain":counts["uncertain"],"ignored":len(ignored)}}
