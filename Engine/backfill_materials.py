"""Resume observed case-relevant links across archived institutions with full browser evidence."""

import argparse
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing, nullcontext
from urllib.parse import parse_qsl, unquote, urlsplit

if __package__:
    from .backfill_pagers import repeating_pager
    from .news_window import apply_to_frontier, skip_queued_url
    from .browser_capture import BrowserCapture
    from .config import connect_db
    from .crawl_store import finish_run, initialize_db, record_fetch
    from .http_page import same_site
    from .navigation import (ASSET_EXTENSIONS, DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, Link, discover_links,
                            find_section_headings, resolve_url, with_navigator_links)
    from .render_store import incomplete_document
    from .scan_staff import save_snapshot_staff
    from .site_navigator import discover_site_navigator
    from .staff_photo_assets import archive_staff_photos
else:
    from backfill_pagers import repeating_pager
    from news_window import apply_to_frontier, skip_queued_url
    from browser_capture import BrowserCapture
    from config import connect_db
    from crawl_store import finish_run, initialize_db, record_fetch
    from http_page import same_site
    from navigation import (ASSET_EXTENSIONS, DOCUMENT_EXTENSIONS, IMAGE_EXTENSIONS, Link, discover_links,
                            find_section_headings, resolve_url, with_navigator_links)
    from render_store import incomplete_document
    from scan_staff import save_snapshot_staff
    from site_navigator import discover_site_navigator
    from staff_photo_assets import archive_staff_photos


SECTIONS = {"information","about","history","programs","management","staff_resources","schedule",
            "contacts","team","staff","documents","news","events"}
DIRECTORIES = {"team","staff","management"}
FEEDS = {"news","events"}


def target_priority(link, site, origin, profiles, wrappers, kind):
    """Keep explicit school/garden materials; linked binary files have their own queue."""
    if not link.follow or link.section not in SECTIONS or not same_site(link.url,site):
        return None
    parts = urlsplit(link.url)
    path = unquote(parts.path).lower()
    if parts.fragment or any(k.lower().startswith(("filter","sort","order","utm_")) for k,_ in parse_qsl(parts.query)):
        return None
    if any(path.endswith(ext) for ext in DOCUMENT_EXTENSIONS | ASSET_EXTENSIONS | IMAGE_EXTENSIONS):
        return None
    if link.relation=="section":
        return 0
    if link.relation=="pagination":
        return 1 if link.section not in FEEDS else 4
    if link.relation!="detail":
        return None
    if link.section in DIRECTORIES:
        return 3 if link.url in profiles or "/persony/" in path else None
    if link.url in wrappers:
        return None
    if link.section in FEEDS:
        return 5 if origin in FEEDS else None
    if link.section=="information":
        if any(base in path for base in ("/svedeniya-ob-obrazovatelnoy-organizatsii/","/sveden/")):
            return 2
    if link.section in {"history","schedule","contacts"}:
        return 1
    if link.section=="documents" or kind=="kindergarten" and link.section in {"programs","staff_resources"}:
        return 3
    return None


def known_urls(conn, org, *, since=None):
    latest = {}
    for url,final,when in conn.execute("""SELECT f.url,f.final_url,f.fetched_at FROM crawl_fetch f JOIN crawl_run r ON r.id=f.run_id
                               WHERE r.id_organization=? AND f.page_id IS NOT NULL""",(org,)):
        for u in (url,final):
            if u:
                latest[u]=max(latest.get(u,0),when)
    refresh=dict(conn.execute("SELECT url,cutoff FROM material_backfill_refresh WHERE organization_id=?",(org,)))
    return {u for u,when in latest.items() if when>=max(since or 0,refresh.get(u,0))}


def group_sources(conn, org, *, since=None):
    profiles = {r[0] for r in conn.execute("""SELECT DISTINCT s.profile_url FROM staff_observation s
        JOIN page p ON p.id=s.page_id WHERE p.id_organization=? AND p.date_loaded>=?
        AND s.profile_url IS NOT NULL""",(org,since or 0))}
    wrappers = {r[0] for r in conn.execute("""SELECT DISTINCT d.detail_url FROM institution_document_link d
        JOIN page p ON p.id=d.page_id WHERE p.id_organization=? AND p.date_loaded>=?
        AND d.detail_url IS NOT NULL AND length(d.url)>0""",(org,since or 0))}
    return profiles, wrappers


def queue_edges(conn, org, site, kind, edges, known, profiles, wrappers, *, since=None):
    choices = {}
    stopped={p[0] for p in conn.execute("""SELECT s.page_id FROM material_pager_stop s JOIN page p ON p.id=s.page_id
        WHERE p.id_organization=?""",(org,))}
    for pid, depth, origin, link in edges:
        if link.url in known or link.relation=="pagination" and pid in stopped:
            continue
        priority = target_priority(link,site,origin,profiles,wrappers,kind)
        if priority is None:
            continue
        row = (priority,depth+1,pid,link.section,link.relation)
        if link.url not in choices or row<choices[link.url]:
            choices[link.url] = row
    conn.executemany("""INSERT INTO material_backfill(organization_id,url,priority,depth,source_page_id,section,relation)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(organization_id,url) DO UPDATE SET priority=excluded.priority,
        section=excluded.section, relation=excluded.relation, depth=excluded.depth, source_page_id=excluded.source_page_id,state='pending'
        WHERE material_backfill.state='pager_review' OR material_backfill.state='pending'
        AND (excluded.priority,excluded.depth)<(material_backfill.priority,material_backfill.depth)""",
        ((org,url,*row) for url,row in choices.items()))
    if since is not None:
        # Reopen a finished route only when a current source links it and its previous capture is stale.
        conn.executemany("""UPDATE material_backfill SET state='pending',run_id=NULL,fetch_id=NULL,
            error=NULL,lease_until=NULL,priority=?,depth=?,source_page_id=?,section=?,relation=?
            WHERE organization_id=? AND url=? AND state NOT IN ('pending','working')
            AND coalesce((SELECT fetched_at FROM crawl_fetch WHERE id=material_backfill.fetch_id),0)<?""",
            ((*row,org,url,since) for url,row in choices.items()))
        conn.executemany("""INSERT INTO material_backfill_refresh VALUES(?,?,?) ON CONFLICT(organization_id,url)
            DO UPDATE SET cutoff=max(cutoff,excluded.cutoff)""",((org,url,since) for url in choices))


def prioritize_timeline(conn, oid):
    """Fetch the two actual publications behind a still-ambiguous event/news match first."""
    conn.execute("""WITH targets(url) AS (
        SELECT t.event_key FROM timeline_event_news t JOIN crawl_run r ON r.id=t.run_id
        WHERE r.id_organization=? AND t.status='candidate_needs_review'
        UNION SELECT json_extract(n.value,'$.url') FROM timeline_event_news t JOIN crawl_run r ON r.id=t.run_id
        JOIN json_each(t.evidence,'$.news_candidates') n
        WHERE r.id_organization=? AND t.status='candidate_needs_review')
        UPDATE material_backfill SET priority=-1 WHERE organization_id=? AND state='pending'
        AND priority>=0 AND section IN ('news','events') AND url IN targets""",(oid,oid,oid))


def queue_failed_document_cards(conn, oid=None):
    """Look for another published download only when the original linked file failed."""
    rows=conn.execute("""WITH failed AS MATERIALIZED (
        SELECT a.page_id,a.url FROM page_attachment a JOIN page p ON p.id=a.page_id
        WHERE a.state='error' AND (? IS NULL OR p.id_organization=?)
        AND NOT EXISTS (SELECT 1 FROM page_attachment ok WHERE ok.url=a.url AND ok.state='saved'
                        AND ok.fetched_at>=p.date_loaded))
        SELECT p.id_organization,o.site_url,p.id,p.url,f.depth,d.detail_url,d.section,d.catalog
        FROM failed a JOIN institution_document_link d ON d.page_id=a.page_id AND d.url=a.url
        JOIN page p ON p.id=a.page_id JOIN organization o ON o.id=p.id_organization JOIN crawl_fetch f ON f.page_id=p.id
        WHERE length(d.detail_url)>0 ORDER BY p.date_loaded DESC,p.id DESC""",(oid,oid)).fetchall()
    observed={}
    eligible={}
    for org,site,pid,page,depth,raw,section,catalog in rows:
        url=resolve_url(page,raw)
        if not url or url==page or not same_site(url,site):
            continue
        path=unquote(urlsplit(url).path).lower()
        if any(path.endswith(ext) for ext in DOCUMENT_EXTENSIONS|ASSET_EXTENSIONS|IMAGE_EXTENSIONS):
            continue
        if org not in observed:
            observed[org]=known_urls(conn,org)
        if url not in observed[org]:
            eligible.setdefault((org,url),(pid,"documents" if catalog else section,depth+1))
    before=conn.total_changes
    with conn:
        conn.executemany("""INSERT INTO material_backfill(organization_id,url,source_page_id,section,depth,
            relation,priority) VALUES(?,?,?,?,?,'document_fallback',-2)
            ON CONFLICT(organization_id,url) DO UPDATE SET state='pending',source_page_id=excluded.source_page_id,
            section=excluded.section,depth=excluded.depth,relation='document_fallback',priority=-2,lease_until=NULL
            WHERE material_backfill.state IN ('covered_by_attachment','pending','pager_review')
            AND material_backfill.relation!='document_fallback'""",
            ((*key,*value) for key,value in eligible.items()))
    return conn.total_changes-before


def queue_publication_previews(conn, oid):
    rows=conn.execute("""SELECT f.page_id,f.depth,'news',n.url,n.title FROM news_preview n
        JOIN news_page list ON list.page_id=n.page_id JOIN crawl_fetch f ON f.page_id=n.page_id
        JOIN page p ON p.id=n.page_id WHERE p.id_organization=? AND list.page_kind IN ('news_listing','home')
        UNION ALL SELECT f.page_id,f.depth,'events',e.url,e.title FROM home_event_link e
        JOIN crawl_fetch f ON f.page_id=e.page_id JOIN page p ON p.id=e.page_id
        WHERE p.id_organization=? AND e.kind='event' AND length(e.url)>0""",(oid,oid))
    return [(pid,depth,section,Link(url,title or "",section,"detail",True)) for pid,depth,section,url,title in rows]


def seed(conn, orgs, *, since=None, news_months=12, as_of=None, include_feeds=True):
    """Old snapshots supply edges too. A recorded successful redirect covers both URLs."""
    for org in orgs:
        oid,site,kind = org
        abandoned=[row[0] for row in conn.execute("""SELECT DISTINCT r.id FROM crawl_run r
            JOIN material_backfill b ON b.run_id=r.id WHERE r.id_organization=? AND r.state='running'
            AND max(r.started_at,coalesce((SELECT max(f.fetched_at) FROM crawl_fetch f WHERE f.run_id=r.id),0))
                <unixepoch()-900 AND NOT EXISTS (SELECT 1 FROM material_backfill w WHERE w.run_id=r.id
                AND w.state='working' AND w.lease_until>=unixepoch())""",(oid,))]
        known = known_urls(conn,oid,since=since)
        profiles,wrappers = group_sources(conn,oid,since=since)
        for pid, in conn.execute("""SELECT f.page_id FROM material_backfill b JOIN crawl_fetch f ON f.id=b.fetch_id
            WHERE b.organization_id=? AND b.relation='pagination' AND f.page_id IS NOT NULL""",(oid,)).fetchall():
            repeating_pager(conn,pid)
        rows = conn.execute("""SELECT f.page_id,f.depth,f.section,l.url,l.label,l.section,l.relation,l.follow
            FROM crawl_run r JOIN crawl_fetch f ON f.run_id=r.id JOIN crawl_link l ON l.fetch_id=f.id
            WHERE r.id_organization=? AND f.page_id IS NOT NULL AND l.follow=1
            AND f.fetched_at>=? AND l.relation IN ('section','detail','pagination')""",(oid,since or 0))
        edges = [(r[0],r[1],r[2],Link(*r[3:])) for r in rows]
        preview_edges=queue_publication_previews(conn,oid)
        if since is not None:
            current={r[0] for r in conn.execute("SELECT id FROM page WHERE id_organization=? AND date_loaded>=?",(oid,since))}
            preview_edges=[e for e in preview_edges if e[0] in current]
        edges.extend(preview_edges)
        for r in conn.execute("""SELECT f.page_id,f.depth,f.section,s.profile_url,s.full_name
            FROM crawl_run r JOIN crawl_fetch f ON f.run_id=r.id JOIN staff_observation s ON s.page_id=f.page_id
            WHERE r.id_organization=? AND f.fetched_at>=? AND s.profile_url IS NOT NULL""",(oid,since or 0)):
            url=resolve_url(site,r[3])
            if url:
                edges.append((r[0],r[1],r[2],Link(url,r[4] or "",r[2] if r[2] in DIRECTORIES else "staff","detail",True)))
        with conn:
            conn.execute("""UPDATE material_backfill SET state='pager_review'
                WHERE organization_id=? AND state='pending' AND relation='pagination'
                AND source_page_id IN (SELECT page_id FROM material_pager_stop)""",(oid,))
            conn.execute("""INSERT OR IGNORE INTO material_backfill_visit
                SELECT fetch_id,source_page_id,relation FROM material_backfill
                WHERE organization_id=? AND fetch_id IS NOT NULL""",(oid,))
            queue_edges(conn,oid,site,kind,edges,known,profiles,wrappers,since=since)
            prioritize_timeline(conn,oid)
            # Reconcile a capture committed just before an interrupted worker marked its frontier row.
            conn.execute("""UPDATE material_backfill SET fetch_id=(SELECT min(f.id) FROM crawl_fetch f
                WHERE f.run_id=material_backfill.run_id AND f.url=material_backfill.url)
                WHERE organization_id=? AND state='working' AND lease_until<unixepoch() AND fetch_id IS NULL""",(oid,))
            conn.execute("""UPDATE material_backfill SET state=CASE WHEN
                (SELECT page_id FROM crawl_fetch WHERE id=fetch_id) IS NULL THEN 'failed' ELSE 'saved' END,
                error=(SELECT error FROM crawl_fetch WHERE id=fetch_id),lease_until=NULL
                WHERE organization_id=? AND state='working' AND lease_until<unixepoch() AND fetch_id IS NOT NULL""",(oid,))
            conn.execute("""UPDATE material_backfill SET state='pending',run_id=NULL,lease_until=NULL
                WHERE organization_id=? AND state='working' AND lease_until<unixepoch()""",(oid,))
            conn.executemany("""UPDATE material_backfill SET state='already_saved' WHERE organization_id=?
                AND url=? AND state='pending'""",((oid,url) for url in known))
        for run in abandoned:
            finish_run(conn,run,"interrupted",reason="worker_lease_expired")
        queue_failed_document_cards(conn,oid)
        with conn:
            if include_feeds:
                conn.execute("""UPDATE material_backfill SET state='pending' WHERE organization_id=?
                    AND state='deferred_by_user' AND section IN ('news','events')""",(oid,))
            else:
                conn.execute("""UPDATE material_backfill SET state='deferred_by_user' WHERE organization_id=?
                    AND state IN ('pending','awaiting_news_policy') AND section IN ('news','events')""",(oid,))
        if include_feeds:
            apply_to_frontier(conn,oid,months=news_months,as_of=as_of)


def capture(conn, browser, run, org, item, known, profiles, wrappers, timeout, *, news_months=12, as_of=None, include_feeds=True):
    oid,site,kind = org
    url,section,depth = item
    response=browser.fetch(url,site,timeout,section=section)
    html=response.html
    links,headings=[],[]
    if html is not None:
        render=response.render
        doc=(render.rendered_html if render and not incomplete_document(render.details) else None) or html
        links=discover_links(doc,response.final_url,site,kind,section)
        navigator=discover_site_navigator(doc,response.final_url,site,kind,is_map_page=False)
        links=with_navigator_links(links,navigator)
        headings=find_section_headings(doc,kind,section)
    fid=record_fetch(conn,run,url,section,depth,final_url=response.final_url,status=response.status,
                     error=response.error,html=html,loaded_at=response.loaded_at,links=links,
                     headings=headings,render=response.render)
    pid=conn.execute("SELECT page_id FROM crawl_fetch WHERE id=?",(fid,)).fetchone()[0]
    if pid is not None:
        known.update((url,response.final_url))
        if section in DIRECTORIES and getattr(response.render,"staff_cards",None) is None:
            save_snapshot_staff(conn,pid,response.final_url,section)
        # Read wrappers directly from the new listing before its run-level document index exists.
        if section in {"documents","information","programs","staff_resources"}:
            from bs4 import BeautifulSoup
            if __package__:
                from .document_links import extract_documents
            else:
                from document_links import extract_documents
            for entry in extract_documents(BeautifulSoup(doc,"html.parser"),response.final_url)["documents"]:
                if entry.get("url") and entry.get("detail_url"):
                    wrappers.add(entry["detail_url"])
        for profile, name in conn.execute("SELECT profile_url,full_name FROM staff_observation WHERE page_id=? AND profile_url IS NOT NULL",(pid,)):
            actual=resolve_url(response.final_url,profile)
            if actual:
                profiles.add(actual)
                links.append(Link(actual,name or "",section if section in DIRECTORIES else "staff","detail",True))
        with conn:
            if repeating_pager(conn,pid):
                links=[link for link in links if link.relation!="pagination"]
            queue_edges(conn,oid,site,kind,((pid,depth,section,link) for link in links),known,profiles,wrappers)
            conn.executemany("""UPDATE material_backfill SET state='covered_by_attachment' WHERE organization_id=?
                AND url=? AND relation='detail' AND section IN ('documents','information','programs','staff_resources')
                AND state='pending'""",((oid,w) for w in wrappers))
            photos=conn.execute("SELECT count(DISTINCT photo_url) FROM staff_observation WHERE page_id=?",(pid,)).fetchone()[0]
            conn.execute("""UPDATE material_backfill SET lease_until=unixepoch()+?
                WHERE organization_id=? AND url=? AND run_id=? AND state='working'""",
                (max(900,int(photos*(timeout+1))+60),oid,url,run))
        archive_staff_photos(conn,pid,timeout=timeout)
        if not include_feeds:
            with conn:
                conn.execute("""UPDATE material_backfill SET state='deferred_by_user' WHERE organization_id=?
                                AND state='pending' AND section IN ('news','events')""",(oid,))
        elif section=="news" or any(link.section=="news" for link in links):
            apply_to_frontier(conn,oid,months=news_months,as_of=as_of)
    with conn:
        conn.execute("""UPDATE material_backfill SET state=?,fetch_id=?,error=?,lease_until=NULL
                        WHERE organization_id=? AND url=? AND run_id=?""",
                     ("saved" if pid else "failed",fid,response.error,oid,url,run))
        conn.execute("""INSERT OR IGNORE INTO material_backfill_visit SELECT fetch_id,source_page_id,relation
                        FROM material_backfill WHERE organization_id=? AND url=? AND fetch_id IS NOT NULL""",(oid,url))
    return response.status, pid


def process_org(org, batch_size, render_wait, timeout, *, db=None, runs=None, news_months=12, as_of=None,
                include_feeds=True):
    oid,site,kind=org
    with (closing(connect_db()) if db is None else nullcontext(db)) as conn:
        conn.execute("PRAGMA busy_timeout=120000")
        known=known_urls(conn,oid)
        profiles,wrappers=group_sources(conn,oid)
        run=None
        processed=errors=0
        try:
            with BrowserCapture(pdf=True,ready_timeout=render_wait) as browser:
                for _ in range(batch_size):
                    with conn:
                        conn.execute("BEGIN IMMEDIATE")
                        row=conn.execute("""SELECT url,section,depth FROM material_backfill WHERE organization_id=?
                            AND state='pending' AND coalesce(lease_until,0)<=unixepoch()
                            AND (? OR section NOT IN ('news','events'))
                            ORDER BY priority,depth,source_page_id,url LIMIT 1""",(oid,int(include_feeds))).fetchone()
                        if row is None or conn.execute("""SELECT 1 FROM material_backfill WHERE organization_id=?
                                AND state='working' LIMIT 1""",(oid,)).fetchone():
                            break
                        if row[0] in known:
                            conn.execute("UPDATE material_backfill SET state='already_saved' WHERE organization_id=? AND url=?",(oid,row[0]))
                            continue
                        if skip_queued_url(conn,oid,row[0],row[1],months=news_months,as_of=as_of):
                            conn.execute("""UPDATE material_backfill SET state='outside_news_window'
                                WHERE organization_id=? AND url=?""",(oid,row[0]))
                            continue
                        if run is None:
                            run=conn.execute("""INSERT INTO crawl_run(id_organization,purpose,max_pages)
                                VALUES(?,'sample',?)""",(oid,batch_size)).lastrowid
                        conn.execute("""UPDATE material_backfill SET state='working',run_id=?,lease_until=unixepoch()+900
                            WHERE organization_id=? AND url=?""",(run,oid,row[0]))
                    status,pid=capture(conn,browser,run,org,row,known,profiles,wrappers,timeout,
                                       news_months=news_months,as_of=as_of,include_feeds=include_feeds)
                    processed+=1
                    if pid is None:
                        errors+=1
                    if status==429:
                        with conn:
                            conn.execute("""UPDATE material_backfill SET lease_until=unixepoch()+300
                                WHERE organization_id=? AND state='pending'""",(oid,))
                        break
        except BaseException as exc:
            if run is not None:
                with conn:
                    conn.execute("""UPDATE material_backfill SET lease_until=0,error=?
                        WHERE run_id=? AND state='working'""",(str(exc)[:500],run))
                finish_run(conn,run,"interrupted",reason=type(exc).__name__)
            raise
        if run is not None:
            finish_run(conn,run,"partial" if errors else "completed",reason="selected_fetch_errors" if errors else "selected_materials")
            if runs is not None:
                runs.append({"run_id":run,"organization_id":oid,"processed":processed,"errors":errors})
        return processed


def stats(conn, ids):
    where=",".join("?" for _ in ids)
    return dict(conn.execute(f"""SELECT state,count(*) FROM material_backfill WHERE organization_id IN ({where})
                                  GROUP BY state ORDER BY state""",ids))


def main(argv=None):
    parser=argparse.ArgumentParser(description="Догрузить известные страницы кейса с HTML, PDF и снимками браузера.")
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all",action="store_true")
    group.add_argument("--org",type=int,nargs="+")
    parser.add_argument("--workers",type=int,default=4)
    parser.add_argument("--batch",type=int,default=4,help="число страниц одного учреждения за заход")
    parser.add_argument("--limit",type=int,help="максимум попыток за запуск, по умолчанию до исчерпания очереди")
    parser.add_argument("--render-wait",type=float,default=25)
    parser.add_argument("--timeout",type=float,default=15)
    parser.add_argument("--news-months",type=int,default=12,help="глубина новостей в календарных месяцах по времени Магадана")
    parser.add_argument("--defer-feeds",action="store_true",help="на этом запуске отложить новости и мероприятия")
    parser.add_argument("--plan",action="store_true",help="обновить очередь и вывести объём без запросов к сайтам")
    parser.add_argument("--retry-failed",action="store_true")
    args=parser.parse_args(argv)
    if not 1<=args.news_months<=1200:
        parser.error("Глубина архива новостей должна быть от 1 до 1200 календарных месяцев")
    if not 1<=args.workers<=12 or args.batch<1 or args.limit is not None and args.limit<1 or min(args.timeout,args.render_wait)<=0:
        parser.error("Укажите положительные пределы; число браузеров от 1 до 12")
    with closing(connect_db()) as conn:
        initialize_db(conn)
        orgs=[(i,resolve_url(site,""),kind) for i,site,kind in conn.execute("SELECT id,site_url,kind FROM organization ORDER BY id")
              if args.all or i in args.org]
        by_id={o[0]:o for o in orgs}
        ids=list(by_id)
        if not ids or args.org and set(args.org)-set(ids):
            parser.error("Неизвестный ID учреждения")
        if args.retry_failed:
            with conn:
                conn.executemany("""UPDATE material_backfill SET state='pending',fetch_id=NULL,run_id=NULL,error=NULL
                    WHERE organization_id=? AND state='failed'""",((i,) for i in ids))
        seed(conn,orgs,news_months=args.news_months,include_feeds=not args.defer_feeds)
        print("Очередь",stats(conn,ids),flush=True)
        if args.plan:
            return
        processed=0
        marks=",".join("?" for _ in ids)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            active={}
            while active or args.limit is None or processed<args.limit:
                busy={org for org,_ in active.values()}
                capacity=args.workers-len(active)
                remaining=args.limit-processed-sum(n for _,n in active.values()) if args.limit else capacity*args.batch
                candidates=conn.execute(f"""SELECT organization_id,min(priority) FROM material_backfill
                    WHERE state='pending' AND coalesce(lease_until,0)<=unixepoch() AND (? OR section NOT IN ('news','events'))
                    AND organization_id IN ({marks})
                    AND organization_id NOT IN (SELECT organization_id FROM material_backfill WHERE state='working')
                    GROUP BY organization_id ORDER BY min(priority),(SELECT count(*) FROM material_backfill b
                        WHERE b.organization_id=material_backfill.organization_id AND b.state IN ('failed','saved')),organization_id
                    LIMIT ?""",(int(not args.defer_feeds),*ids,args.workers)).fetchall() if capacity and remaining>0 else []
                for oid,_ in candidates:
                    if oid in busy:
                        continue
                    size=min(remaining,args.batch)
                    if capacity<=0 or size<=0:
                        break
                    active[pool.submit(process_org,by_id[oid],size,args.render_wait,args.timeout,
                                       news_months=args.news_months,include_feeds=not args.defer_feeds)]=(oid,size)
                    remaining-=size
                    capacity-=1
                if not active:
                    pending=conn.execute(f"""SELECT min(lease_until) FROM material_backfill WHERE state='pending'
                        AND lease_until>unixepoch() AND organization_id IN ({marks})""",ids).fetchone()[0]
                    if pending:
                        time.sleep(min(30,max(1,pending-time.time())))
                        continue
                    break
                done,_=wait(active,return_when=FIRST_COMPLETED)
                for f in done:
                    active.pop(f)
                    processed+=f.result()
                print("Попыток",processed,stats(conn,ids),flush=True)


if __name__=="__main__":
    main()
