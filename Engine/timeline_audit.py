"""Audit publication intervals, reporting delays, and school event follow-up in one archived crawl."""

import argparse
import json
import re
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime
from urllib.parse import parse_qsl, urldefrag, urljoin, urlsplit

if __package__:
    from .news_order import compare_news_pages
    from .timeline_dates import MAGADAN, article_sources, dated_source, latency, _resolved
else:
    from news_order import compare_news_pages
    from timeline_dates import MAGADAN, article_sources, dated_source, latency, _resolved


TIMELINE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS timeline_run (
    run_id INTEGER PRIMARY KEY REFERENCES crawl_run(id) ON DELETE CASCADE, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline_gap (
    run_id INTEGER NOT NULL REFERENCES crawl_run(id) ON DELETE CASCADE,
    page_id INTEGER NOT NULL REFERENCES page(id), ordinal INTEGER NOT NULL,
    next_page_id INTEGER NOT NULL REFERENCES page(id), next_ordinal INTEGER NOT NULL,
    days INTEGER, status TEXT NOT NULL, evidence TEXT NOT NULL,
    PRIMARY KEY(run_id,page_id,ordinal,next_page_id,next_ordinal)
);
CREATE TABLE IF NOT EXISTS timeline_publication (
    run_id INTEGER NOT NULL REFERENCES crawl_run(id) ON DELETE CASCADE, url TEXT NOT NULL,
    page_id INTEGER REFERENCES page(id), title TEXT NOT NULL, published_start TEXT, published_end TEXT,
    event_start TEXT, event_end TEXT, status TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(run_id,url)
);
CREATE INDEX IF NOT EXISTS timeline_publication_page ON timeline_publication(page_id);
CREATE TABLE IF NOT EXISTS timeline_event_news (
    run_id INTEGER NOT NULL REFERENCES crawl_run(id) ON DELETE CASCADE, event_key TEXT NOT NULL,
    event_title TEXT, date_start TEXT, date_end TEXT, status TEXT NOT NULL, evidence TEXT NOT NULL,
    PRIMARY KEY(run_id,event_key)
);
"""


def _json(value):
    return json.dumps(value,ensure_ascii=False,separators=(",",":"))


def _window(refs):
    bounds,_ = _resolved(refs)
    return tuple(v.isoformat() for v in bounds) if bounds else (None,None)


def _row(card):
    return {k:card[k] for k in ("page_id","ordinal","url","title","date_status","date_iso")}


def _pinned(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html,"html.parser")
    result=set()
    nodes=list(soup.select("[data-pinned],.pinned,.is-pinned,.sticky-post,.pinned-post,[title*='акреплен'],[aria-label*='акреплен'],[title*='акреплён']"))
    pinned_label=re.compile(r"^(?:закрепл[её]н[ао]?|закрепл[её]нная (?:запись|новость|публикация))$",re.I)
    nodes.extend(label.parent for label in soup.find_all(string=pinned_label))
    for node in nodes:
        if str(node.get("data-pinned","")).lower() in {"false","0","no"}:
            continue
        card = node.find_parent(class_=re.compile(r"^(?:card|gw-news-item|news-item|news-card)$")) or node.find_parent("article") or node
        anchor = card if card.name=="a" else card.find("a",href=True) or node.find_parent("a",href=True)
        if anchor is not None and anchor.get("href"):
            result.add(anchor["href"])
    return result


def _filtered(url):
    return any(value and (key.lower().startswith("filter") or key.lower() in {
        "category","category[]","q","tag","topic","search","date","year","sort","order"})
        for key,value in parse_qsl(urlsplit(url).query))


def _data(conn,run_id,org):
    listings = {}
    detail_rows = []
    aliases = {}
    for pid,requested,url,kind,html in conn.execute("""
        SELECT p.id,f.url,p.url,n.page_kind,CASE WHEN n.source='rendered_dom'
           THEN coalesce(r.rendered_html,p.text_page) ELSE p.text_page END
        FROM news_page n JOIN page p ON n.page_id=p.id JOIN crawl_fetch f ON f.page_id=p.id
        LEFT JOIN page_render r ON r.page_id=p.id
        WHERE f.run_id=? AND p.id_organization=? AND n.page_kind IN ('news_listing','news_detail')
        ORDER BY p.id
    """,(run_id,org)):
        if kind=="news_listing":
            listings[pid] = {"url":url,"pinned":{urljoin(url,href) for href in _pinned(html)},
                             "filtered":_filtered(url),"cards":[]}
        else:
            canonical = urldefrag(url).url
            aliases[urldefrag(requested).url] = canonical
            detail_rows.append((pid,canonical,html))
    news = {}

    def article(url,title):
        return news.setdefault(url,{"title":title,"page_id":None,"published":[],"event":[],"reports":[],
                                    "links":set(),"previews":[]})

    for pid,ordinal,url,title,stat,iso,evidence in conn.execute("""
        SELECT n.page_id,n.ordinal,n.url,n.title,n.date_status,n.date_iso,n.evidence
        FROM news_preview n JOIN page p ON p.id=n.page_id JOIN crawl_fetch f ON f.page_id=p.id
        WHERE f.run_id=? AND p.id_organization=? ORDER BY n.page_id,n.ordinal
    """,(run_id,org)):
        url = aliases.get(urldefrag(url).url,urldefrag(url).url)
        card = dict(page_id=pid,ordinal=ordinal,url=url,title=title,date_status=stat,date_iso=iso)
        if pid in listings:
            listings[pid]["cards"].append(card)
        item=article(url,title)
        item["previews"].append(_row(card))
        fields = json.loads(evidence).get("date_evidence",{}).get("sources",[])
        for field in fields:
            if field.get("role")=="date_field" and field.get("text"):
                item["published"].append(dated_source(field["text"],"preview_published_field",page_id=pid,
                                                      ordinal=ordinal,selector=field.get("selector")))
    for pid,url,html in detail_rows:
        facts=article_sources(html,pid)
        item=article(url,facts["title"])
        item["page_id"]=pid
        item["title"] = facts["title"] or item["title"]
        for key in ("published","event","reports"):
            item[key].extend(facts[key])
        item["links"].update(urldefrag(urljoin(url,href)).url for href in facts["links"])
    for listing in listings.values():
        for card in listing["cards"]:
            bounds,_reason=_resolved(news[card["url"]]["published"])
            card["publication_supported"] = bool(bounds and bounds[0].date().isoformat()==card["date_iso"])
    return listings,news


def _gap(a,b,listings,route):
    evidence={"from":_row(a),"to":_row(b),"relation":route,"scope":"adjacent_displayed_previews"}
    delta=None
    try:
        if a["date_status"]=="valid" and b["date_status"]=="valid":
            delta=(date.fromisoformat(a["date_iso"])-date.fromisoformat(b["date_iso"])).days
    except (TypeError,ValueError):
        pass
    parts=[listings[a["page_id"]],listings[b["page_id"]]]
    if a["url"]==b["url"]:
        status,reason="uncertain","same_publication_repeated"
    elif any(l["filtered"] for l in parts):
        status,reason="uncertain","filtered_news_listing"
    elif any(c["url"] in l["pinned"] for l,c in zip(parts,(a,b))):
        status,reason="uncertain","pinned_preview"
    elif delta is None:
        status,reason="uncertain","preview_date_unresolved"
    elif not a["publication_supported"] or not b["publication_supported"]:
        status,reason="uncertain","publication_date_not_established"
    elif delta<0:
        status,reason="uncertain","inverted_publication_order"
    elif delta>14 and any(b["date_iso"]<d<a["date_iso"] for d in parts[0]["feed_dates"]):
        status,reason="uncertain","other_dated_publication_between_pair"
    elif delta>14:
        status,reason="over_14d","observed_gap_on_listing"
    else:
        status,reason="within_14d","observed_gap_on_listing"
    evidence["reason"]=reason
    return a["page_id"],a["ordinal"],b["page_id"],b["ordinal"],delta,status,_json(evidence)


def _events(conn,run_id,org,day,news):
    grouped=defaultdict(list)
    alias_targets=defaultdict(set)
    for requested,final in conn.execute("""
        SELECT f.url,p.url FROM crawl_fetch f JOIN page p ON p.id=f.page_id
        WHERE f.run_id=? AND p.id_organization=? AND f.section='events'
    """,(run_id,org)):
        alias_targets[urldefrag(requested).url].add(urldefrag(final).url)
    aliases={url:next(iter(targets)) for url,targets in alias_targets.items() if len(targets)==1}
    for page,ordinal,url,title,start,end,raw in conn.execute("""
        SELECT e.page_id,e.ordinal,e.url,e.title,e.date_start,e.date_end,e.evidence
        FROM home_event_link e JOIN page p ON p.id=e.page_id JOIN crawl_fetch f ON f.page_id=p.id
        WHERE e.kind='event' AND f.run_id=? AND p.id_organization=? ORDER BY e.page_id,e.ordinal
    """,(run_id,org)):
        key=urldefrag(url).url if url else f"page:{page}:card:{ordinal}"
        key=aliases.get(key,key)
        data=json.loads(raw)
        grouped[key].append({"page_id":page,"ordinal":ordinal,"title":title,"start":start,"end":end or start,
                             "date_reason":data.get("date_evidence",{}).get("reason"),
                             "signals":data.get("evidence",{}).get("heuristics",[])})
    noise={"для","при","под","или","день","дня","дню","акция","мероприятие","мероприятия","школы","школе",
           "школа","участие","конкурс","конкурса","праздник","событие","заголовок","прошел","состоялся"}
    def tokens(value):
        return {w for w in re.findall(r"[а-яёa-z0-9]+",(value or "").casefold().replace("ё","е")) if len(w)>=3}-noise
    for key,rows in grouped.items():
        title=rows[0]["title"] or ""
        unique_dates={(r["start"],r["end"]) for r in rows if r["start"]}
        lo,hi=next(iter(unique_dates)) if len(unique_dates)==1 else (None,None)
        try:
            if not lo or date.fromisoformat(lo)>date.fromisoformat(hi):
                lo,hi=None,None
        except ValueError:
            lo,hi=None,None
        if any(r["date_reason"] in {"possible_date_error","conflicting_dates"} for r in rows):
            lo,hi=None,None
        candidates=[]
        if any({"template_title","template_description"} & set(r["signals"]) for r in rows):
            status="template_needs_review"
        elif lo and lo>day:
            status="upcoming"
        else:
            title_words=tokens(title)
            for url,n in news.items():
                direct=any(aliases.get(link,link)==key for link in n["links"])
                overlap=title_words & tokens(n["title"])
                same_title=len(title_words)>=2 and title_words<=tokens(n["title"])
                if not direct and (len(overlap)<2 or len(overlap)/len(title_words)<0.6):
                    continue
                start,end=_window(n["event"])
                actual_date_matches=bool(lo and start and lo<=start[:10]<=hi and lo<=end[:10]<=hi)
                candidates.append({"url":url,"page_id":n["page_id"],"title":n["title"],"direct_link":direct,
                                   "matching_words":sorted(overlap),"occurrence_in_planned_period":actual_date_matches,
                                   "reported_in_article":bool(n["reports"]),
                                   "report_matches":bool(n["page_id"] and n["reports"] and actual_date_matches and (direct or same_title))})
            status=("reported_on_site" if any(c["report_matches"] for c in candidates) else "candidate_needs_review" if candidates
                    else "not_matched_in_capture" if lo else "date_unresolved")
        yield key,title,lo,hi,status,_json({"sources":rows,"news_candidates":candidates,
                                           "scope":"same_institution_same_run","evaluated_on":day})


def refresh_timeline(conn,run_id):
    """Replace derived findings for this run inside the caller's transaction; no network operations."""
    run=conn.execute("SELECT id_organization,started_at,state,stop_reason FROM crawl_run WHERE id=?",(run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Не найден обход {run_id}")
    listings,news=_data(conn,run_id,run[0])
    feed_dates=defaultdict(set)
    for listing in listings.values():
        address=urlsplit(listing["url"])
        listing["feed_dates"]=feed_dates[(address.hostname,address.path.rstrip("/"))]
        if not listing["filtered"]:
            listing["feed_dates"].update(c["date_iso"] for c in listing["cards"]
                                         if c["date_status"]=="valid" and c["publication_supported"])
    pairs={}
    for listing in listings.values():
        cards=listing["cards"]
        for a,b in zip(cards,cards[1:]):
            if b["ordinal"]==a["ordinal"]+1:
                gap=_gap(a,b,listings,"same_listing_page")
                pairs[gap[:4]]=gap
    boundary=compare_news_pages(conn,run_id)
    card_by_id={(card["page_id"],card["ordinal"]):card for listing in listings.values() for card in listing["cards"]}
    for pair in boundary["pairs"]:
        a,b=pair["previous"],pair["current"]
        if pair.get("adjacency") and a and b and a["page_id"] in listings and b["page_id"] in listings:
            a,b=card_by_id[(a["page_id"],a["ordinal"])],card_by_id[(b["page_id"],b["ordinal"])]
            gap=_gap(a,b,listings,"verified_pagination_link")
            pairs[gap[:4]]=gap
    articles=[]
    for url,item in news.items():
        status,proof=latency(item["published"],item["event"])
        if not item["page_id"]:
            proof["reason"]="detail_not_captured"
        proof.update(published_sources=item["published"],event_sources=item["event"],previews=item["previews"])
        articles.append((url,item["page_id"],item["title"],*_window(item["published"]),*_window(item["event"]),
                         status,_json(proof)))
    day=datetime.fromtimestamp(run[1],MAGADAN).date().isoformat()
    events=list(_events(conn,run_id,run[0],day,news))
    summary={"scope":"observed_cards_and_news_of_one_run","run_state":run[2],"stop_reason":run[3],
             "listing_pages":len(listings),"pagination":boundary["summary"],
             "unverified_pagination":[{"source_page_id":r["source_page_id"],"target_url":r["target_url"],"reason":r["reason"]}
                                      for r in boundary["pairs"] if not r.get("current")],
             "gaps":dict(Counter(g[5] for g in pairs.values())),
             "publication_24h":dict(Counter(a[7] for a in articles)),
             "event_news":dict(Counter(e[4] for e in events))}
    conn.execute("SAVEPOINT timeline")
    try:
        for table,rows,width in (("timeline_gap",pairs.values(),8),("timeline_publication",articles,10),
                                 ("timeline_event_news",events,7)):
            conn.execute(f"DELETE FROM {table} WHERE run_id=?",(run_id,))
            placeholders=",".join("?" for _ in range(width))
            conn.executemany(f"INSERT INTO {table} VALUES({placeholders})",((run_id,*row) for row in rows))
        conn.execute("""INSERT INTO timeline_run VALUES(?,?) ON CONFLICT(run_id)
                     DO UPDATE SET summary=excluded.summary""",(run_id,_json(summary)))
    except BaseException:
        conn.execute("ROLLBACK TO timeline")
        raise
    finally:
        conn.execute("RELEASE timeline")
    return summary


def timeline_issues(conn,run_id):
    """Yield observed over-limit pairs, definite clock anomalies and event/news review candidates."""
    for days,page,other,raw in conn.execute("""
        SELECT days,page_id,next_page_id,evidence FROM timeline_gap WHERE run_id=? AND status='over_14d'
        ORDER BY days DESC,page_id,ordinal
    """,(run_id,)):
        data=json.loads(raw)
        a,b=data["from"],data["to"]
        yield (f"{days} дней между «{a['title']}» ({a['date_iso']}) и «{b['title']}» ({b['date_iso']}). "
               f"Снимки {page}, {other}. {a['url']} · {b['url']}")
    for page,title,url,status,raw in conn.execute("""
        SELECT page_id,title,url,status,evidence FROM timeline_publication WHERE run_id=? AND status
        IN ('over_24h','published_before_event') ORDER BY page_id
    """,(run_id,)):
        data=json.loads(raw)
        label="Позднее 24 часов" if status=="over_24h" else "Публикация датирована раньше описанного события"
        publication=next((x["raw"] for x in data["published_sources"] if x["field"]=="article_published"),None)
        event=next((x["raw"] for x in data["event_sources"]),None)
        yield f"{label}. «{title}». Событие «{event}», публикация «{publication}», снимок {page}. {url}"
    for title,status,raw in conn.execute("""
        SELECT event_title,status,evidence FROM timeline_event_news WHERE run_id=? AND status
        IN ('candidate_needs_review','reported_on_site') ORDER BY event_key
    """,(run_id,)):
        evidence=json.loads(raw)
        label="В новости сообщается о проведении" if status=="reported_on_site" else "Требуется сверка с новостью"
        yield f"{label}. «{title}». " + ", ".join(item["url"] for item in evidence["news_candidates"])


def main(argv=None):
    if __package__:
        from .config import connect_db
        from .crawl_store import initialize_db
    else:
        from config import connect_db
        from crawl_store import initialize_db
    parser=argparse.ArgumentParser(description="Проверить даты новостей и связь с мероприятиями в сохранённом обходе")
    scope=parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--run",type=int,nargs="+")
    scope.add_argument("--org",type=int,nargs="+",help="последний основной обход каждого учреждения")
    scope.add_argument("--all",action="store_true",help="все завершённые обходы, включая выборочные")
    parser.add_argument("--issues",action="store_true",help="показать превышения с датами, ссылками и ID снимков")
    args=parser.parse_args(argv)
    with closing(connect_db()) as conn:
        initialize_db(conn)
        if args.run:
            ids=list(dict.fromkeys(args.run))
        elif args.org:
            ids=[]
            for org in dict.fromkeys(args.org):
                row=conn.execute("""SELECT max(id) FROM crawl_run WHERE id_organization=? AND purpose='audit'
                                 AND finished_at IS NOT NULL""",(org,)).fetchone()
                if row[0] is None:
                    parser.error(f"Нет завершённого основного обхода для учреждения {org}")
                ids.append(row[0])
        else:
            ids=[r[0] for r in conn.execute("SELECT id FROM crawl_run WHERE finished_at IS NOT NULL ORDER BY id")]
        for run_id in ids:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                result=refresh_timeline(conn,run_id)
            print(f"Обход {run_id} · интервалы {result['gaps']} · 24 часа {result['publication_24h']} · мероприятия {result['event_news']}")
            if args.issues:
                for issue in timeline_issues(conn,run_id):
                    print("  "+issue)


if __name__=="__main__":
    main()
