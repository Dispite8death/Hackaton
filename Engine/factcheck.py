"""Find external reporting for one saved publication and retain the evidence for each decision."""

import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from contextlib import closing
from datetime import date, timedelta
from functools import lru_cache, partial
from itertools import zip_longest
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup
import idna

if __package__:
    from .archive_access import uses_archive
    from .factcheck_sources import SOURCE_SCHEMA_SQL, get_allowed_sources, snapshot_fingerprint, match_publisher, checked_redirect, SOCIAL_DOMAINS
    from .factcheck_sources import school_hosts as _school_hosts, is_school_host as _school
    from .http_page import decode_html
    from .model_settings import setting
    from .timeline_dates import article_sources, dated_source, HAPPENED, EXCLUSIONS
else:
    from archive_access import uses_archive
    from factcheck_sources import SOURCE_SCHEMA_SQL, get_allowed_sources, snapshot_fingerprint, match_publisher, checked_redirect, SOCIAL_DOMAINS
    from factcheck_sources import school_hosts as _school_hosts, is_school_host as _school
    from http_page import decode_html
    from model_settings import setting
    from timeline_dates import article_sources, dated_source, HAPPENED, EXCLUSIONS


FACTCHECK_SCHEMA_SQL = SOURCE_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS factcheck_run (
    id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES page(id),
    checked_at INTEGER NOT NULL, query TEXT NOT NULL, minimum_sources INTEGER NOT NULL,
    source_count INTEGER NOT NULL, status TEXT NOT NULL, claim TEXT NOT NULL, searches TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS factcheck_page ON factcheck_run(page_id,id);
CREATE TABLE IF NOT EXISTS factcheck_source (
    check_id INTEGER NOT NULL REFERENCES factcheck_run(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL, url TEXT NOT NULL, final_url TEXT, publisher TEXT, status TEXT NOT NULL,
    evidence TEXT NOT NULL, PRIMARY KEY(check_id,ordinal)
);
"""
DEFAULT_SEARCH_SITES = ("https://www.iro-49.ru",)
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
SOCIAL = SOCIAL_DOMAINS
STOP = set("на при для этот это наш нашей нашем школа школы школьный детский детского детсада "
           "прошел прошёл состоялся состоялось прошли прошло года год города региональный регионального "
           "магадан магадане магаданской муниципальный бюджетное учреждение участие приняли принял".split())
LOCALITIES = ("Магадан","Омсукчан","Сусуман","Ягодное","Дебин","Усть-Омчуг","Эвенск","Сеймчан","Сокол",
              "Палатка","Стекольный","Хасын","Ола","Армань","Уптар","Клёпка","Талон","Талая","Тауйск",
              "Тахтоямск","Балаганное","Гадля","Синегорье","Мяунджа","Дукат")


def _json(obj):
    return json.dumps(obj,ensure_ascii=False,separators=(",",":"))


def _norm(text):
    return " ".join(re.findall(r"[а-яa-z0-9]+",text.casefold().replace("ё","е")))


@lru_cache(maxsize=1)
def _suffix_list():
    import tldextract
    return tldextract.TLDExtract(suffix_list_urls=(),cache_dir=None)


def _publisher(host):
    return _suffix_list()(host).top_domain_under_public_suffix or host


def _host(url):
    p = urlsplit(url)
    if (p.scheme not in ("http","https") or not p.hostname or p.username is not None or
            p.password is not None or p.port not in (None,80,443) or re.search(r"[\\\x00-\x20]",url)):
        raise ValueError("Нужен публичный HTTP(S) URL")
    return idna.encode(p.hostname.removesuffix("."),uts46=True,std3_rules=True).decode("ascii").lower()


def _social_account(url):
    parts=[s.casefold() for s in urlsplit(url).path.strip("/").split("/") if s]
    if not parts:
        return None
    domain=_publisher(_host(url))
    if domain=="t.me" and parts[0]=="s" and len(parts)>1:
        return domain,parts[1]
    if domain in ("vk.com","vk.ru"):
        number=re.fullmatch(r"(?:public|club|wall-)(\d+)(?:_\d+)?",parts[0])
        return "vk.com",number[1] if number else parts[0]
    return domain,parts[0]


def _linked_social(conn,url):
    account=_social_account(url)
    if not account:
        return False
    prefix=account[0] if account[0]!="vk.com" else "vk."
    for (linked,) in conn.execute("""SELECT DISTINCT l.url FROM crawl_link l JOIN crawl_fetch f
                      ON f.id=l.fetch_id WHERE f.depth=0 AND l.url LIKE ?""",("%"+prefix+"%",)):
        try:
            if _social_account(linked)==account:
                return True
        except ValueError:
            continue
    return False


class SourceNotAllowed(ValueError):
    def __init__(self, url, reason="URL отсутствует в списке разрешённых издателей"):
        self.url = url
        super().__init__(reason)


def _fetch(url, blocked, timeout, accept, *, validate_url=None):
    """Connect to a validated public IP; perform TLS verification for the original hostname on every hop."""
    for _ in range(6):
        host = _host(url)
        if _school(host,blocked):
            raise ValueError("school_resource")
        if validate_url:
            validate_url(url)
        p=urlsplit(url)
        port=p.port or (443 if p.scheme=="https" else 80)
        addresses=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("nonpublic_address")
        last=None
        for family,kind,protocol,_,addr in addresses:
            sock=socket.socket(family,kind,protocol)
            try:
                sock.settimeout(timeout)
                sock.connect(addr)
                if p.scheme=="https":
                    sock=ssl.create_default_context().wrap_socket(sock,server_hostname=host)
                break
            except OSError as err:
                last=err
                sock.close()
        else:
            raise last or OSError("Не удалось соединиться с издателем")
        with closing(http.client.HTTPConnection(host,port,timeout=timeout)) as conn:
            conn.sock=sock
            path=quote(p.path or "/",safe="/%:@!$&'()*+,;=-._~")
            query=quote(p.query,safe="%&=+:,;/?@-._~")
            conn.request("GET",urlunsplit(("","",path,query,"")),headers={
                "User-Agent":"Hackaton-EduSiteAudit/1.0","Accept":accept,"Accept-Encoding":"identity"})
            with conn.getresponse() as resp:
                if resp.status in (301,302,303,307,308) and resp.getheader("Location"):
                    location=resp.getheader("Location")
                    if validate_url:
                        try:
                            checked_redirect(location)
                        except ValueError as error:
                            raise SourceNotAllowed(urljoin(url, location), str(error)) from error
                    url=urljoin(url,location)
                    continue
                if not 200<=resp.status<300:
                    raise ValueError(f"HTTP {resp.status}")
                body=resp.read(2*1024*1024+1)
                if len(body)>2*1024*1024:
                    raise ValueError("Документ превышает 2 МиБ")
                return url,resp.getheader("Content-Type",""),body
    raise ValueError("Слишком много перенаправлений")


def _stem_words(text):
    return {w[:5] for w in _norm(text).split() if len(w)>3 and w not in STOP}


def _claim(conn,page_id):
    row=conn.execute("""SELECT p.url,p.text_page,o.name,o.kind,b.title,b.page_kind
        FROM page p JOIN organization o ON o.id=p.id_organization JOIN page_passport b ON b.page_id=p.id
        WHERE p.id=?""",(page_id,)).fetchone()
    if not row or row[5] not in ("news_detail","event_detail"):
        raise ValueError("Для фактчекинга нужен page_id конкретной новости или мероприятия")
    url,html,org,org_kind,title,role=row
    refs=article_sources(html,page_id)
    event_dates={ref["bounds"][0][:10] for ref in refs["event"] if ref.get("bounds")}
    if any(not ref.get("bounds") for ref in refs["event"]):
        event_dates.clear()
    if role=="news_detail" and conn.execute("SELECT 1 FROM sqlite_master WHERE name='timeline_publication'").fetchone():
        stored=conn.execute("SELECT event_start,event_end FROM timeline_publication WHERE page_id=?",(page_id,)).fetchall()
        if stored:
            event_dates={v[:10] for lo,hi in stored for v in (lo,hi) if v}
    if role=="event_detail":
        event_dates.update(v for (v,) in conn.execute("""SELECT DISTINCT date_start FROM home_event_link
                           WHERE page_id=? AND kind='event' AND date_start IS NOT NULL""",(page_id,)))
    location=next((place for place in LOCALITIES[1:] if _norm(place) in _norm(org)),None)
    if not location and "магадан" in _norm(org+" "+url):
        location="Магадан"
    if not location and "magadan" in urlsplit(url).netloc:
        location="Магадан"
    num=re.search(r"№\s*(\d+)\b",org)
    institution_type=next((word for word in ("гимназия","лицей","школа","детский сад")
                           if word in org.casefold()),"детский сад" if org_kind=="kindergarten" else "школа")
    name_aliases=[s.strip() for s in re.findall(r"[«\"]([^«»\"]{4,110})[»\"]",org) if len(_norm(s))>=6]
    main=BeautifulSoup(html,"html.parser")
    area=main.select_one(".object-full,#description,[itemprop=articleBody],article,main") or main
    body=" ".join(area.stripped_strings)
    nationwide = bool(re.search(r"\b(?:все|обще)российск", title, re.I) and
                      re.search(r"по всей (?:россии|стране)|во всех регионах|общероссийск", body[:2500], re.I))
    return {"page_id":page_id,"url":url,"title":title,"organization":org,"number":num[1] if num else None,
            "organization_type":institution_type,"aliases":name_aliases,"location":location,
            "event_day":next(iter(event_dates)) if len(event_dates)==1 else None,
            "event_date_sources":sorted(event_dates),"scope":"событие и участие именно этого учреждения",
            "event_scope":"nationwide" if nationwide else "local","original_text":body}


def _search(query, claim, sites, limit, timeout, fetch, blocked):
    providers=[]
    for site in sites:
        host=_host(site)
        target=site.rstrip("/")+"/wp-json/wp/v2/search?"+urlencode({"search":query,"subtype":"post","per_page":limit})
        providers.append((host,target,"wordpress"))
    keywords={"марафон":"marathon","фестив":"festival","бокс":"boxing","турнир":"tournament","открыт":"opening",
              "олимпиад":"olympiad","диктант":"dictation","конкурс":"competition","эстафет":"relay"}
    terms=[english for word,english in keywords.items() if word in _norm(query+" "+claim["title"])]
    params={"query":("Magadan "+" ".join(terms[:1])).strip(),
            "mode":"ArtList","format":"json","maxrecords":limit}
    if claim["event_day"]:
        day=date.fromisoformat(claim["event_day"])
        params.update(startdatetime=(day-timedelta(days=3)).strftime("%Y%m%d000000"),
                      enddatetime=(day+timedelta(days=14)).strftime("%Y%m%d235959"))
    rules=claim["allowed_sources"]
    if len(rules)>12:
        offset=(claim["page_id"]*12)%len(rules)
        rules=(rules[offset:]+rules[:offset])[:12]
    phrase = query if claim["event_scope"]=="nationwide" else f"{claim['location'] or 'Магадан'} {query}"
    # At most two URL-scoped index requests per page; the exact URL policy is enforced on every hit.
    for start in range(0,len(rules),6):
        filters=["site:"+base.split("://",1)[1].rstrip("/") for base in rules[start:start+6]]
        scoped_query=f"{phrase} ({' OR '.join(filters)})"
        providers.append(("Bing News RSS","https://www.bing.com/news/search?"+urlencode({
            "q":scoped_query,"format":"rss","setlang":"ru-ru"}),"rss"))
    providers.append(("GDELT DOC",GDELT+"?"+urlencode(params),"gdelt"))
    hits,diagnostics=[],[]
    for name,target,kind in providers:
        info={"provider":name,"request_url":target,"status":"ok","count":0}
        urls=[]
        try:
            def same_search_service(new_url):
                start, destination = urlsplit(target), urlsplit(new_url)
                if (_host(new_url)!=_host(target) or destination.scheme!=start.scheme or
                        (destination.port or (443 if destination.scheme=="https" else 80)) !=
                        (start.port or (443 if start.scheme=="https" else 80)) or
                        destination.path.rstrip("/")!=start.path.rstrip("/")):
                    raise ValueError("Поисковый сервис перенаправил запрос за пределы API")
            read_index = fetch or partial(_fetch,validate_url=same_search_service)
            final,mime,data=read_index(target,blocked,timeout,"application/rss+xml, application/xml" if kind=="rss" else "application/json")
            same_search_service(final)
            if kind=="rss":
                if "xml" not in mime.lower() or b"<!DOCTYPE" in data.upper():
                    raise ValueError("Ожидалась публичная RSS-лента")
                root=ElementTree.fromstring(data)
                if root.tag!="rss" or root.find("channel") is None:
                    raise ValueError("Некорректная RSS-лента")
                for node in root.iterfind("channel/item/link"):
                    link=node.text or ""
                    if link and _publisher(_host(link))=="bing.com":
                        link=next(iter(parse_qs(urlsplit(link).query).get("url",[])),"")
                    if link:
                        urls.append(link)
            else:
                if "json" not in mime.lower():
                    raise ValueError("Поисковый API не вернул JSON")
                items=json.loads(data)
                if kind=="gdelt":
                    items=items.get("articles",[]) if isinstance(items,dict) else None
                if not isinstance(items,list):
                    raise ValueError("Неизвестный формат ответа поискового API")
                urls=[item["url"] for item in items if isinstance(item,dict) and isinstance(item.get("url"),str)]
            urls=urls[:limit]
            info.update(count=len(urls),status="ok" if urls else "no_results",urls=urls)
        except (OSError,ValueError,http.client.HTTPException,ElementTree.ParseError) as err:
            info.update(status="error",error=str(err)[:400])
        hits.append(urls)
        diagnostics.append(info)
    return [u for group in zip_longest(*hits) for u in group if u],diagnostics


def _body(html):
    soup=BeautifulSoup(html,"html.parser")
    title=next((text for heading in (soup.find("h1"),soup.title) if heading
                and (text:=" ".join(heading.stripped_strings))),"")
    if not title and (og_title:=soup.select_one('meta[property="og:title"]')):
        title=og_title.get("content","")
    name=soup.select_one('meta[property="og:site_name"]')
    canonical=soup.select_one('link[rel~="canonical"]')
    og_name=name.get("content","") if name else ""
    main=soup.select_one('[itemprop="articleBody"],.entry-content,.article-body,.article__body,article,main') or soup.body or soup
    for tag in main.select("nav,header,footer,aside,script,style,.breadcrumbs,.related-posts,.comments"):
        tag.decompose()
    blocks=[" ".join(tag.stripped_strings) for tag in main.select("p,blockquote") if tag.find("p") is None]
    if not blocks:
        blocks=[" ".join(main.stripped_strings)]
    return title,og_name,(canonical.get("href","") if canonical else ""),blocks


def _org_evidence(claim,text):
    flat=_norm(text)
    if any(_norm(a) in flat for a in claim["aliases"] if len(_norm(a))>=6 and
           (_norm(claim["location"] or "__") in _norm(a) or claim["number"] and claim["number"] in _norm(a).split())):
        return True
    name=claim["organization_type"]
    kind={"школа":r"(?:школ\w*|сош|мбоу)","гимназия":r"гимнази\w*","лицей":r"лице\w*",
          "детский сад":r"(?:детск\w*\s+сад\w*|детсад\w*|доу|м[абк]доу|центр\w*\s+развития\s+реб[её]нка)"}[name]
    n=claim["number"]
    if n and re.search(r"\b"+kind+r"(?:\s+комбинированного\s+вида)?\s*(?:№|номер|n)?\s*"+re.escape(n)+r"\b",text,re.I):
        return True
    # In unnumbered village organisations the direct place-name qualifier is part of the school identity.
    place=_norm(claim["location"] or "").replace(" ","-")
    return bool(not n and place and place!="магадан" and re.search(kind+r"\s+(?:(?:п|с|поселка|посёлка|села)\.?\s+)?"+
                re.escape(place),text,re.I))


def _participation(claim, text):
    """Require the institution and an affirmative action in the same short clause."""
    involvement = r"\b(?:участвовал[аи]?|принял[аи]?\s+участие|пров[её]л|провел[аи]|организовал[аи]?|выступил[аи]?|посетил[аи]?|стал[аи]?\s+площадкой)\b"
    sentences = re.split(r"(?<!\bп)(?<!\bс)(?<!\bг)[.!?;]+\s+", text, flags=re.I)
    for sentence in sentences:
        if EXCLUSIONS.search(sentence) or re.search(r"\b(?:без\s+участия|отказ\w*|пропустил\w*|не\s+\w+)", sentence, re.I):
            continue
        for action in re.finditer(involvement, sentence, re.I):
            nearby = sentence[max(0, action.start()-110):action.end()+110]
            if _org_evidence(claim, nearby):
                return True
    return False


def _assess(claim,title,blocks):
    needles=_stem_words(claim["title"])
    named=next((terms for phrase in re.findall(r"[«\"]([^»\"]+)[»\"]",claim["title"])
                if len(terms:=_stem_words(phrase))>=2),set())
    place=_norm(claim["location"] or "").replace(" ","-")
    place_pattern=r"\bмагадан(?:е|а|ом)?\b" if place=="магадан" else re.escape(place[:max(4,len(place)-2)])
    best=None
    for i,block in enumerate(blocks):
        if not block.strip():
            continue
        window=" ".join(blocks[max(i-1,0):i+2])[:1400]
        normalized=_norm(title+" "+window)
        observed=_stem_words(title+" "+window)
        hits=needles & observed
        named_match=not named or len(named & observed)/len(named)>=0.65
        event_match=len(hits)>=2 and len(hits)/max(1,len(needles))>=0.45 and named_match
        location=bool(place and re.search(place_pattern,normalized))
        org=_org_evidence(claim,title+" "+window)
        participation=org and _participation(claim,title+". "+window)
        happened=bool(HAPPENED.search(block) or re.search(r"\b(?:начался|открылся|завершился|закончилось)\b",block,re.I))
        if EXCLUSIONS.search(block) or EXCLUSIONS.search(title) or re.search(r"\b(?:опроверг|опровержение|отмена)", title, re.I):
            happened = False
        announced=bool(re.search(r"\b(?:состоится|пройд[её]т|планируется|приглаша\w*)\b",title+" "+block,re.I))
        date_ref=dated_source(block,"external_report")
        matched=bool(date_ref["bounds"] and date_ref["bounds"][0][:10]==claim["event_day"])
        national = claim["event_scope"]=="nationwide" and bool(re.search(r"\b(?:все|обще)российск", normalized))
        event_supported = bool(event_match and (location or national) and happened and matched)
        marks={"title_match":event_match,"named_event_match":named_match,"place_match":location,"organization_match":org,"participation_match":participation,"occurred":happened,
               "announcement_wording":announced,"event_supported":event_supported,
               "event_date_match":matched,"matched_title_terms":sorted(hits),"source_date":date_ref["bounds"]}
        grade=(event_supported and location and participation,event_supported,event_match and location,
               org+happened+matched,len(hits),announced)
        if best is None or grade>best[0]:
            best=(grade,marks,block[:550])
    if not best:
        return "empty",{},None
    grade,marks,excerpt=best
    status=("matching_report" if grade[0] else "related_unverified" if marks["title_match"] and
            (marks["place_match"] or marks["organization_match"] or marks["event_supported"]) else "unrelated")
    if status=="related_unverified" and marks["announcement_wording"] and not marks["occurred"]:
        status="announcement"
    return status,marks,excerpt


def _shingles(text):
    words=_norm(text[:130_000]).split()
    return {tuple(words[i:i+5]) for i in range(len(words)-4)}


def _copy(a,b):
    return len(a)>=8 and len(b)>=8 and len(a&b)/min(len(a),len(b))>=0.65


def _edition(title):
    match=re.search(r"\b([ivx]+|\d{1,2})(?:\s*[-‑]?\s*[йя])?\s+(?:региональн|всероссийск|международн|городск|областн)",title,re.I)
    if not match:
        return None
    value=match[1].lower()
    return int(value) if value.isdigit() else {"i":1,"ii":2,"iii":3,"iv":4,"v":5,"vi":6,"vii":7,"viii":8,"ix":9,"x":10}.get(value)


def _group_sources(rows,fingerprints):
    # An outlet may publish two versions; a mirror of either belongs to the same group.
    ids=list(fingerprints)
    groups={i:i for i in ids}
    for pos,i in enumerate(ids):
        for j in ids[:pos]:
            a,b=rows[i],rows[j]
            same_owner=(a["publisher"]==b["publisher"] or bool(a["publisher_name"]) and
                        _norm(a["publisher_name"])==_norm(b["publisher_name"]))
            if same_owner or _copy(fingerprints[i],fingerprints[j]):
                old,new=max(groups[i],groups[j]),min(groups[i],groups[j])
                if old!=new:
                    groups={k:(new if v==old else v) for k,v in groups.items()}
    supported={}
    event_groups=set()
    labels={}
    for i in ids:
        row=rows[i]
        group=groups[i]
        row["related_group"]=labels.setdefault(group,len(labels)+1)
        row["counted_for_event"]=row["signals"]["event_supported"] and group not in event_groups
        if row["counted_for_event"]:
            event_groups.add(group)
        if row["status"]!="matching_report":
            continue
        if group not in supported:
            supported[group]=i
            continue
        other=rows[supported[group]]
        row["status"]=("same_publisher" if row["publisher"]==other["publisher"] or
                       row["publisher_name"] and _norm(row["publisher_name"])==_norm(other["publisher_name"])
                       else "duplicate_text")
    return len(supported),len(labels),len(event_groups)


@uses_archive
def run_factcheck(conn,page_id,*,query=None,candidate_urls=(),search_sites=None,minimum_sources=None,limit=None,
                  timeout=8,fetch=None,search=True):
    """Search public indexes, read the original reports, then commit the entire attempt atomically."""
    config=setting(conn,"factcheck",{})
    minimum_sources=config.get("minimum_sources",2) if minimum_sources is None else minimum_sources
    limit=config.get("limit",6) if limit is None else limit
    if type(minimum_sources)!=int or type(limit)!=int or not 2<=minimum_sources<=limit<=30 or not 0<timeout<=60:
        raise ValueError("Нужны минимум два источника, лимит 2–30 и таймаут до 60 секунд")
    claim=_claim(conn,page_id)
    blocked=_school_hosts(conn)
    rules=get_allowed_sources(conn)
    claim.update(allowed_sources=rules,allowlist_fingerprint=snapshot_fingerprint(rules),
                 allowlist_status="active" if rules else "empty")

    def check_source(url):
        try:
            rule=match_publisher(url,rules)
        except ValueError as error:
            raise SourceNotAllowed(url,str(error)) from error
        if rule is None:
            raise SourceNotAllowed(url)
        return rule

    read_article = fetch or partial(_fetch,validate_url=check_source)
    sites=config.get("search_sites",DEFAULT_SEARCH_SITES) if search_sites is None else search_sites
    if not isinstance(sites,(tuple,list)) or len(sites)>16:
        raise ValueError("Укажите не более 16 поисковых площадок")
    quoted=re.findall(r"[«\"]([^»\"]{5,120})[»\"]",claim["title"])
    query=query or (quoted[0] if quoted else " ".join(claim["title"].split()[:7]))
    if not isinstance(query,str) or not query.strip() or len(query)>300:
        raise ValueError("Поисковый запрос должен содержать от 1 до 300 символов")
    found,searches=_search(query,claim,sites,limit,timeout,fetch,blocked) if search and rules else ([],[])
    rows=[]
    original=_shingles(claim.pop("original_text"))
    fingerprints={}
    seen_urls=set()
    article_requests=0
    for url in dict.fromkeys([*candidate_urls,*found]):
        row={"url":url,"final_url":None,"publisher":None,"status":"unreadable",
             "policy_status":"blocked","discovery":"provided" if url in candidate_urls else "search"}
        try:
            try:
                host=_host(url)
            except ValueError as error:
                raise SourceNotAllowed(url,str(error)) from error
            if _school(host,blocked):
                raise ValueError("school_resource")
            if _publisher(host) in SOCIAL:
                # Shared accounts need owner verification before an originating publisher can be assigned.
                row["status"]="school_linked_social" if _linked_social(conn,url) else "social_owner_unverified"
                rows.append(row)
                continue
            row["matched_base_url"]=check_source(url)
            row["policy_status"]="allowed"
            if article_requests>=limit:
                row["status"]="read_limit_reached"
                rows.append(row)
                continue
            article_requests+=1
            final,mime,data=read_article(url,blocked,timeout,"text/html,application/xhtml+xml")
            final_host=_host(final)
            if _school(final_host,blocked):
                raise ValueError("school_resource")
            publisher=_publisher(final_host)
            row.update(final_url=final,publisher=publisher,retrieved_at=int(time.time()))
            if publisher in SOCIAL:
                row["policy_status"]="blocked"
                row["status"]="school_linked_social" if _linked_social(conn,final) else "social_owner_unverified"
                rows.append(row)
                continue
            row["final_matched_base_url"]=check_source(final)
            if final in seen_urls:
                row["status"]="duplicate_url"
            elif "html" not in mime.lower():
                row["status"]="unsupported_content"
            else:
                title,name,canonical,blocks=_body(decode_html(data,mime))
                if canonical:
                    try:
                        checked_redirect(canonical)
                    except ValueError as error:
                        raise SourceNotAllowed(canonical,str(error)) from error
                    check_source(urljoin(final,canonical))
                status,marks,excerpt=_assess(claim,title,blocks)
                text=" ".join(blocks)
                shingles=_shingles(text)
                cross_canonical=canonical and _publisher(_host(urljoin(final,canonical)))!=publisher
                # A source credit makes publisher independence ambiguous even if the copied text was edited.
                credit=next((b[:220] for b in blocks if re.search(r"(?:^|\b)(?:источник\s*:|по материалам\b|"
                    r"по информации пресс-службы|со ссылкой на|сообщает пресс-служба|перепечатано)",b,re.I)),None)
                row.update(title=title[:400],publisher_name=name[:200],canonical=canonical,excerpt=excerpt,
                           signals=marks,attribution=credit,sha256=hashlib.sha256(data).hexdigest())
                local_edition,other_edition=_edition(claim["title"]),_edition(title)
                if local_edition and other_edition and local_edition!=other_edition:
                    status="different_edition"
                elif cross_canonical or credit:
                    status="attributed_or_syndicated"
                elif _copy(shingles,original):
                    status="original_reprint"
                if status in ("matching_report","related_unverified","announcement"):
                    fingerprints[len(rows)]=shingles
                row["status"]=status
            seen_urls.add(final)
        except SourceNotAllowed as err:
            row.update(status="source_not_allowed",policy_status="blocked",blocked_url=err.url,error=str(err))
        except (ValueError,OSError,http.client.HTTPException) as err:
            row.update(status="school_resource" if str(err)=="school_resource" else "unreadable",error=str(err)[:400])
            if row["status"]=="school_resource":
                row["policy_status"]="blocked"
        rows.append(row)
    total,related_groups,event_count=_group_sources(rows,fingerprints)
    related=any(r.get("signals",{}).get("title_match") for r in rows)
    status="corroborated" if total>=minimum_sources else "review" if related else "insufficient"
    event_status="corroborated" if event_count>=minimum_sources else "insufficient"
    if rows:
        attempted=[row for row in rows if row["policy_status"]=="allowed" and row["status"]!="read_limit_reached"]
        retryable = bool(attempted) and all(row["status"]=="unreadable" for row in attempted)
    else:
        retryable = bool(searches) and all(provider["status"] == "error" for provider in searches)
    if retryable:
        status = event_status = "retryable"
    claim.update(event_source_count=event_count,event_status=event_status)
    with conn:
        cur=conn.execute("""INSERT INTO factcheck_run(page_id,checked_at,query,minimum_sources,source_count,status,claim,searches)
                            VALUES(?,unixepoch(),?,?,?,?,?,?)""",
                         (page_id,query,minimum_sources,total,status,_json(claim),_json(searches)))
        check_id=cur.lastrowid
        conn.executemany("INSERT INTO factcheck_source VALUES(?,?,?,?,?,?,?)",(
            (check_id,n,r["url"],r["final_url"],r["publisher"],r["status"],_json(r)) for n,r in enumerate(rows)))
    return {"check_id":check_id,"status":status,"retryable":retryable,"source_count":total,"related_groups":related_groups,
            "event_source_count":event_count,"event_status":event_status,
            "claim":claim,"sources":rows,"searches":searches}


def main():
    if __package__:
        from .config import connect_db
        from .crawl_store import initialize_db
    else:
        from config import connect_db
        from crawl_store import initialize_db
    parser=argparse.ArgumentParser(description="Сопоставить сохранённую новость или мероприятие с внешними публикациями")
    parser.add_argument("--page",type=int,required=True)
    parser.add_argument("--query",help="поисковая фраза для региональных издателей")
    parser.add_argument("--source",action="append",default=[],help="дополнительный URL, можно несколько")
    parser.add_argument("--minimum",type=int)
    parser.add_argument("--limit",type=int)
    parser.add_argument("--no-search",action="store_true",help="сверить только перечисленные публикации")
    args=parser.parse_args()
    with closing(connect_db()) as conn:
        initialize_db(conn)
        report=run_factcheck(conn,args.page,query=args.query,candidate_urls=args.source,minimum_sources=args.minimum,
                             limit=args.limit,search=not args.no_search)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
