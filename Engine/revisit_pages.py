"""Revisit specific archived pages in a new sample run for each institution."""

import argparse
import re
from collections import defaultdict
from contextlib import closing
from urllib.parse import parse_qsl, urlsplit

from browser_capture import BrowserCapture
from attached_files import download_pending
from config import connect_db
from crawl_store import finish_run, initialize_db, record_fetch, start_run
from http_page import same_site
from navigation import STAFF_DIRECTORIES, discover_links, find_section_headings, resolve_url, words
from render_store import incomplete_document
from scan_staff import save_snapshot_staff
from staff_photo_assets import archive_staff_photos


def _position(url):
    parts = urlsplit(url)
    for key, value in parse_qsl(parts.query):
        key=key.casefold()
        if value.isdecimal() and (key in {"page","paged","offset","curpos"} or key.startswith("pagen_")):
            return ("offset" if key in {"offset","curpos"} else "page", int(value))
    match = re.search(r"/(?:page[-/]|p)(\d+)/?$",parts.path,re.I)
    return ("page", int(match[1])) if match else None


def _next_url(links, current, site, reserved):
    """Select only an observed forward control; an opaque JS button has no target to visit."""
    choices = []
    here = _position(current)
    for link in links:
        if (link.relation!="pagination" or not link.follow or link.url in reserved or
                link.url==current or not same_site(link.url,site)):
            continue
        label, position = words(link.label), _position(link.url)
        pos = position[1] if position else None
        if re.search(r"предыдущ|назад|prev|back",label):
            continue
        if position:
            baseline = here[1] if here and here[0]==position[0] else (0 if position[0]=="offset" else 1)
            if pos <= baseline:
                continue
        explicit = label in {"далее","следующая","следующая страница","показать еще","загрузить еще","next"}
        number = int(label) if label.isdecimal() else None
        if explicit:
            choices.append((0,pos if pos is not None else 0,link.url))
        elif pos is not None or number is not None and number>(here[1] if here and here[0]=="page" else 1):
            choices.append((1,pos if pos is not None else number,link.url))
    return min(choices)[2] if choices else None


def _capture_one(conn, browser, run, source_id, url, site, kind, section, depth, relation, timeout, archive_staff):
    response = browser.fetch(url,site,timeout,section=section)
    links, headings = [], []
    if response.html is not None:
        render = response.render
        document = (render.rendered_html if render and not incomplete_document(render.details) else None) or response.html
        links = discover_links(document,response.final_url,site,kind,section)
        headings = find_section_headings(document,kind,section)
    fetch_id=record_fetch(conn,run,url,section,depth,final_url=response.final_url,
                          status=response.status,error=response.error,html=response.html,loaded_at=response.loaded_at,
                          links=links,headings=headings,render=response.render,
                          original_page_id=source_id,revisit_relation=relation)
    page_id=conn.execute("SELECT page_id FROM crawl_fetch WHERE id=?",(fetch_id,)).fetchone()[0]
    if page_id is not None:
        if (section in STAFF_DIRECTORIES or section=="home") and getattr(response.render,"staff_cards",None) is None:
            save_snapshot_staff(conn,page_id,response.final_url,section)
        if archive_staff:
            archive_staff(conn,page_id,timeout=timeout)
    report={"original_page_id":source_id,"run_id":run,"fetch_id":fetch_id,"page_id":page_id,
            "url":response.final_url,"relation":relation,"http_status":response.status,"error":response.error}
    return report, links


def revisit_pages(conn, page_ids, *, staff_next=False, render_wait=45, timeout=12, capture_factory=BrowserCapture,
                  archive_staff=None,archive_files=None):
    """Capture selected page IDs and optionally one linked next directory page per selection."""
    ids=list(dict.fromkeys(page_ids))
    if capture_factory is BrowserCapture and archive_staff is None:
        archive_staff=archive_staff_photos
    if capture_factory is BrowserCapture and archive_files is None:
        archive_files=download_pending
    if not ids or any(type(i) is not int or i<=0 for i in ids) or render_wait<=0 or timeout<=0:
        raise ValueError("Нужны положительные ID страниц и положительные тайм-ауты")
    groups=defaultdict(list)
    for pid in ids:
        row=conn.execute("""
            SELECT p.id_organization,p.url,o.site_url,o.kind,f.section,f.depth
            FROM page p JOIN organization o ON o.id=p.id_organization
            JOIN crawl_fetch f ON f.page_id=p.id JOIN crawl_run r ON r.id=f.run_id AND r.id_organization=o.id
            WHERE p.id=?
        """,(pid,)).fetchone()
        if row is None:
            raise ValueError(f"Нет сохранённой страницы и сведений об обходе для page_id={pid}")
        org,url,site,kind,section,depth=row
        url,site=resolve_url(url,""),resolve_url(site,"")
        if not url or not site or not same_site(url,site):
            raise ValueError(f"URL снимка page_id={pid} выходит за пределы сайта учреждения")
        groups[org].append((pid,url,site,kind,section,depth))

    reports=[]
    for org,selection in groups.items():
        run=start_run(conn,org,purpose="sample")
        reserved={url for _,url,*_ in selection}
        batch=[]
        try:
            with capture_factory(pdf=True,ready_timeout=render_wait) as browser:
                for pid,url,site,kind,section,depth in selection:
                    report,links=_capture_one(conn,browser,run,pid,url,site,kind,section,depth,"revisit",timeout,archive_staff)
                    batch.append(report)
                    reserved.add(report["url"])
                    if staff_next and section in STAFF_DIRECTORIES:
                        target=_next_url(links,report["url"],site,reserved)
                        if target:
                            reserved.add(target)
                            additional,_=_capture_one(conn,browser,run,pid,target,site,kind,section,depth+1,"staff_next",timeout,archive_staff)
                            batch.append(additional)
                            reserved.add(additional["url"])
        except BaseException as exc:
            finish_run(conn,run,"interrupted" if isinstance(exc,KeyboardInterrupt) else "failed",
                       reason=str(exc)[:400] or type(exc).__name__)
            raise
        successful=sum(r["page_id"] is not None for r in batch)
        state="completed" if successful==len(batch) else "partial" if successful else "failed"
        finish_run(conn,run,state,reason=None if state=="completed" else "selected_page_unavailable")
        if archive_files:
            archive_files(conn,run_id=run)
        reports.extend(batch)
    return reports


def main(argv=None):
    parser=argparse.ArgumentParser(description="Повторно сохранить выбранные страницы в новом выборочном обходе.")
    parser.add_argument("--page",required=True,nargs="+",type=int,metavar="ID")
    parser.add_argument("--staff-next",action="store_true",help="посетить до одной следующей страницы каталога сотрудников")
    parser.add_argument("--render-wait",type=float,default=45,metavar="SEC",help="ожидание отрисовки документа в секундах")
    args=parser.parse_args(argv)
    with closing(connect_db()) as conn:
        initialize_db(conn)
        try:
            result=revisit_pages(conn,args.page,staff_next=args.staff_next,render_wait=args.render_wait)
        except ValueError as error:
            parser.error(str(error))
    for row in result:
        print(f"original_page_id={row['original_page_id']} run_id={row['run_id']} fetch_id={row['fetch_id']} "
              f"page_id={row['page_id']} {row['relation']} HTTP {row['http_status']} {row['url']} {row['error'] or ''}")
    return int(any(row["page_id"] is None for row in result))


if __name__=="__main__":
    raise SystemExit(main())
