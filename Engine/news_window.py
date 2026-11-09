"""Scope dated news using published metadata and the local calendar of Magadan."""

import calendar
import json
from datetime import date, datetime

if __package__:
    from .news_order import _location
    from .timeline_audit import _filtered, _pinned
    from .timeline_dates import MAGADAN, _resolved, article_sources, dated_source
else:
    from news_order import _location
    from timeline_audit import _filtered, _pinned
    from timeline_dates import MAGADAN, _resolved, article_sources, dated_source


WINDOW_SQL = """
CREATE INDEX IF NOT EXISTS news_preview_url ON news_preview(url,page_id);
CREATE TABLE IF NOT EXISTS news_window_page (
    page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE, organization_id INTEGER NOT NULL,
    months INTEGER NOT NULL, day TEXT NOT NULL, start_day TEXT NOT NULL, section_url TEXT NOT NULL,
    series TEXT, component TEXT, mode TEXT, position INTEGER, newest TEXT, boundary_url TEXT,
    boundary_date TEXT, reason TEXT NOT NULL, PRIMARY KEY(page_id,months,day)
);
CREATE INDEX IF NOT EXISTS news_window_series ON news_window_page(organization_id,months,day,series);
CREATE TABLE IF NOT EXISTS news_window_date (
    organization_id INTEGER NOT NULL, url TEXT NOT NULL, page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, date_iso TEXT, reason TEXT NOT NULL,
    PRIMARY KEY(organization_id,url,page_id,ordinal)
);
CREATE INDEX IF NOT EXISTS news_window_date_page ON news_window_date(page_id);
CREATE TABLE IF NOT EXISTS news_window_skip (
    organization_id INTEGER NOT NULL,url TEXT NOT NULL,months INTEGER NOT NULL,day TEXT NOT NULL,
    start_day TEXT NOT NULL,page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,boundary_url TEXT,date_iso TEXT,
    PRIMARY KEY(organization_id,url,months,day)
);
"""


def bounds(months, as_of=None):
    if type(months) is not int or not 1<=months<=1200:
        raise ValueError("Глубина архива новостей должна быть от 1 до 1200 календарных месяцев")
    end=as_of if as_of is not None else datetime.now(MAGADAN).date()
    if type(end) is not date:
        raise ValueError("День проверки должен быть календарной датой Магадана")
    year,m0=divmod(end.year*12+end.month-1-months,12)
    start=date(year,m0+1,min(end.day,calendar.monthrange(year,m0+1)[1]))
    return start.isoformat(),end.isoformat()


def _series(url):
    path,scope,component,mode,pos=_location(url)
    return json.dumps([path,scope],ensure_ascii=False),component,mode,pos


def _preview_date(status, iso, raw):
    if status!="valid" or not iso:
        return None,"preview_"+status
    sources=json.loads(raw).get("date_evidence",{}).get("sources",[])
    fields=[dated_source(f["text"],"published_field") for f in sources if f.get("role")=="date_field" and f.get("text")]
    interval,reason=_resolved(fields)
    if interval and interval[0].date().isoformat()==interval[1].date().isoformat()==iso:
        return iso,"preview_published_field"
    return None,"preview_date_unconfirmed_"+reason


def _index(conn, org, pid, months, day, start):
    if conn.execute("SELECT 1 FROM news_window_page WHERE page_id=? AND months=? AND day=?",(pid,months,day)).fetchone():
        return
    row=conn.execute("""SELECT n.page_kind,n.order_status,p.url,n.source FROM news_page n JOIN page p ON p.id=n.page_id
        WHERE n.page_id=? AND p.id_organization=?""",(pid,org)).fetchone()
    if not row:
        return
    kind,order,url,source=row
    if kind not in {"home","news_listing","news_detail"}:
        return
    cards=[]
    for ordinal,target,status,iso,raw in conn.execute("""SELECT ordinal,url,date_status,date_iso,evidence
        FROM news_preview WHERE page_id=? ORDER BY ordinal""",(pid,)):
        when,why=_preview_date(status,iso,raw)
        cards.append((ordinal,target,when,why))
    ordered=(kind=="news_listing" and len(cards)>=2 and order=="newest_first" and not _filtered(url)
             and len({c[1] for c in cards})==len(cards) and all(c[2] for c in cards)
             and all(a[2]>=b[2] for a,b in zip(cards,cards[1:])))
    older=next((c for c in cards if c[2] and c[2]<start),None)
    previous=conn.execute("SELECT 1 FROM news_window_date WHERE page_id=? AND ordinal=-1 LIMIT 1",(pid,)).fetchone()
    if ordered and older or kind=="news_detail" and not previous:
        html=conn.execute("""SELECT CASE WHEN ?='rendered_dom' THEN coalesce(r.rendered_html,p.text_page)
            ELSE p.text_page END FROM page p LEFT JOIN page_render r ON r.page_id=p.id WHERE p.id=?""",(source,pid)).fetchone()[0]
        if ordered and older:
            ordered=not _pinned(html)
        if kind=="news_detail" and not previous:
            interval,why=_resolved(article_sources(html,pid)["published"])
            when=interval[0].date().isoformat() if interval and interval[0].date()==interval[1].date() else None
            cards.append((-1,url,when,"article_"+why))
    series,component,mode,pos=_series(url)
    cutoff=older if ordered else None
    # A URL hosting multiple independent paginated components cannot establish a common boundary.
    if cutoff:
        components={(c,m) for target, in conn.execute("""SELECT l.url FROM crawl_link l JOIN crawl_fetch f ON f.id=l.fetch_id
            WHERE f.page_id=? AND l.section='news' AND l.relation='pagination' AND l.follow=1""",(pid,))
            for s,c,m,p in [_series(target)] if s==series and p is not None}
        if len(components)==1:
            linked_component,linked_mode=components.pop()
            if component and linked_component and component!=linked_component or mode and linked_mode and mode!=linked_mode:
                cutoff=None
            else:
                component,mode=component or linked_component,mode or linked_mode
        else:
            cutoff=None
    with conn:
        conn.executemany("""INSERT OR REPLACE INTO news_window_date(organization_id,url,page_id,ordinal,date_iso,reason)
            VALUES(?,?,?,?,?,?)""",((org,target,pid,ordinal,when,why) for ordinal,target,when,why in cards))
        conn.execute("""INSERT INTO news_window_page VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pid,org,months,day,start,url,series,component,mode,pos,max((c[2] for c in cards if c[2]),default=None),
             cutoff[1] if cutoff else None,cutoff[2] if cutoff else None,
             "dated_unpinned_descending_list_crosses_start" if cutoff else "no_verified_pagination_boundary"))


def _reason(conn, org, url, months, day, start):
    evidence=conn.execute("""SELECT page_id,ordinal,date_iso FROM news_window_date WHERE organization_id=? AND url=?""",
                          (org,url)).fetchall()
    if evidence:
        exact={r[2] for r in evidence if r[2]}
        article=any(r[1]==-1 and r[2] for r in evidence)
        if len(exact)==1 and (article or all(r[2] for r in evidence)):
            when=next(iter(exact))
            source=next(r[0] for r in evidence if r[2]==when)
            if when<start or when>day:
                return source,"before_window" if when<start else "after_window",url,when
        return None
    series,component,mode,pos=_series(url)
    if pos is None or mode not in {"offset","page"}:
        return None
    baseline=0 if mode=="offset" else 1
    markers=conn.execute("""SELECT page_id,boundary_url,boundary_date,coalesce(position,?)
        FROM news_window_page WHERE organization_id=? AND months=? AND day=? AND series=? AND component IS ?
        AND mode=? AND boundary_url IS NOT NULL AND coalesce(position,?)<? ORDER BY coalesce(position,?)""",
        (baseline,org,months,day,series,component,mode,baseline,pos,baseline)).fetchall()
    for pid,boundary,when,at in markers:
        # A known conflict about the boundary item, or a later leaf with in-window news, defeats extrapolation.
        conflict=conn.execute("""SELECT 1 FROM news_window_date WHERE organization_id=? AND url=?
            AND (date_iso IS NULL OR date_iso<>?) LIMIT 1""",(org,boundary,when)).fetchone()
        newer=conn.execute("""SELECT 1 FROM news_window_page WHERE organization_id=? AND months=? AND day=?
            AND series=? AND (component IS ? OR component IS NULL) AND (mode=? OR mode IS NULL)
            AND position>? AND newest>=? LIMIT 1""",(org,months,day,series,component,mode,at,start)).fetchone()
        if not conflict and not newer:
            return pid,"after_verified_listing_boundary",boundary,when
    return None


def skip_queued_url(conn, organization_id, url, section, *, months=12, as_of=None):
    if section!="news":
        return False
    start,day=bounds(months,as_of)
    result=_reason(conn,organization_id,url,months,day,start)
    if result:
        pid,why,boundary,when=result
        running_transaction=conn.in_transaction
        conn.execute("""INSERT INTO news_window_skip VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(organization_id,url,months,day)
            DO UPDATE SET page_id=excluded.page_id,reason=excluded.reason,boundary_url=excluded.boundary_url,date_iso=excluded.date_iso
            WHERE (news_window_skip.page_id,news_window_skip.reason,news_window_skip.date_iso)
                  IS NOT (excluded.page_id,excluded.reason,excluded.date_iso)""",
                     (organization_id,url,months,day,start,pid,why,boundary,when))
        if not running_transaction:
            conn.commit()
        return True
    return False


def filter_publication_links(conn, *, source_page_id, links, organization_id, months=12, as_of=None):
    start,day=bounds(months,as_of)
    _index(conn,organization_id,source_page_id,months,day,start)
    with conn:
        return [link for link in links if not skip_queued_url(conn,organization_id,link.url,link.section,months=months,as_of=as_of)]


def apply_to_frontier(conn, org, *, months=12, as_of=None):
    start,day=bounds(months,as_of)
    pages=conn.execute("""SELECT n.page_id FROM news_page n JOIN page p ON p.id=n.page_id
        WHERE p.id_organization=? AND NOT EXISTS(SELECT 1 FROM news_window_page w WHERE w.page_id=n.page_id
        AND w.months=? AND w.day=?) ORDER BY n.page_id""",(org,months,day)).fetchall()
    for pid, in pages:
        _index(conn,org,pid,months,day,start)
    rows=conn.execute("""SELECT url,state FROM material_backfill WHERE organization_id=? AND section='news'
        AND state IN ('pending','outside_news_window','awaiting_news_policy')""",(org,)).fetchall()
    with conn:
        for url,state in rows:
            blocked=skip_queued_url(conn,org,url,"news",months=months,as_of=as_of)
            next_state="outside_news_window" if blocked else "pending"
            if next_state!=state:
                conn.execute("UPDATE material_backfill SET state=? WHERE organization_id=? AND url=? AND state=?",
                             (next_state,org,url,state))
