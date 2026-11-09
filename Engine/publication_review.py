"""Model evidence from archived external excerpts and same-institution event/news candidates."""

from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

if __package__:
    from .factcheck_sources import get_allowed_sources, snapshot_fingerprint, match_publisher, school_hosts, is_school_host
    from .model_settings import setting
    from .timeline_dates import BODY, article_sources, _resolved
else:
    from factcheck_sources import get_allowed_sources, snapshot_fingerprint, match_publisher, school_hosts, is_school_host
    from model_settings import setting
    from timeline_dates import BODY, article_sources, _resolved


def _hash(s):
    return sha256(s.encode()).hexdigest()


def policy_is_current(conn,check):
    info=check.get("publication",{})
    if info.get("scope") not in {"event","participation"}:
        return True
    policy=snapshot_fingerprint(get_allowed_sources(conn))
    required=max(2,setting(conn,"factcheck",{}).get("minimum_sources",2))
    return policy==info["allowlist_fingerprint"] and required<=info["minimum_sources"]


def _external(conn,pid):
    row=conn.execute("""SELECT id,checked_at,minimum_sources,claim FROM factcheck_run
                     WHERE page_id=? ORDER BY id DESC LIMIT 1""",(pid,)).fetchone()
    if not row:
        return None
    rid,stamp,minimum,raw=row
    claim=json.loads(raw)
    rules=get_allowed_sources(conn)
    fingerprint=snapshot_fingerprint(rules)
    if not rules or claim.get("allowlist_fingerprint")!=fingerprint:
        return None
    blocked=school_hosts(conn)
    rows=[]
    for n,url,final,publisher,status,evidence in conn.execute("""SELECT ordinal,url,final_url,publisher,status,evidence
          FROM factcheck_source WHERE check_id=? AND status IN ('matching_report','related_unverified','announcement')
          ORDER BY ordinal""",(rid,)):
        data=json.loads(evidence)
        if (data.get("policy_status")!="allowed" or not final or not publisher or not data.get("related_group")
            or not data.get("excerpt") or data.get("attribution")):
            continue
        try:
            if (not all(match_publisher(link,rules) for link in (url,final)) or any(
                is_school_host(urlsplit(link).hostname or "",blocked) for link in (url,final))):
                continue
        except ValueError:
            continue
        rows.append((n,url,final,publisher,data))
    if not rows:
        return None
    threshold=max(2,minimum,setting(conn,"factcheck",{}).get("minimum_sources",2))
    return dict(rid=rid,stamp=stamp,claim=claim,threshold=threshold,fingerprint=fingerprint,rows=rows)


def _records(conn,pid):
    owner=conn.execute("""SELECT f.run_id,p.id_organization,p.url,b.page_kind FROM page p JOIN crawl_fetch f ON f.page_id=p.id
                       JOIN page_passport b ON b.page_id=p.id
                       WHERE p.id=?""",(pid,)).fetchone()
    if not owner:
        return
    base_run,org,page_url,page_kind=owner
    where="t.run_id=?"
    params=(base_run,)
    if page_kind=="event_detail":
        where="t.event_key=? AND r.id_organization=?"
        params=(page_url,org)
    seen=set()
    for run,key,title,lo,hi,raw in conn.execute(f"""SELECT t.run_id,event_key,event_title,date_start,date_end,evidence
              FROM timeline_event_news t JOIN crawl_run r ON r.id=t.run_id
              WHERE {where} AND status='candidate_needs_review'
              ORDER BY (t.run_id=?) DESC,t.run_id DESC,t.event_key""",(*params,base_run)):
        evidence=json.loads(raw)
        sources=evidence.get("sources",[])
        primary=min((s["page_id"] for s in sources if s.get("page_id")),default=None)
        if pid!=primary and key!=page_url:
            continue
        for news in evidence.get("news_candidates",[]):
            url=news.get("url")
            if not url or (key,url) in seen:
                continue
            # A subsequent targeted revisit can supply the original article. Retain both crawl IDs/times.
            def detail(address,kind):
                return conn.execute("""SELECT p.id,p.date_loaded,f.run_id FROM page p JOIN crawl_fetch f ON f.page_id=p.id
                    JOIN page_passport b ON b.page_id=p.id
                    WHERE p.id_organization=? AND p.url=? AND b.page_kind=?
                    ORDER BY (f.run_id=?) DESC,p.date_loaded DESC,p.id DESC LIMIT 1""",(org,address,kind,run)).fetchone()
            full=detail(url,"news_detail")
            full_event=detail(key,"event_detail")
            preview=conn.execute("""SELECT n.page_id,n.ordinal,n.title,n.date_text,n.evidence FROM news_preview n
                JOIN crawl_fetch f ON f.page_id=n.page_id JOIN page p ON p.id=n.page_id
                WHERE f.run_id=? AND p.id_organization=? AND n.url=? ORDER BY n.page_id,n.ordinal LIMIT 1""",
                (run,org,url)).fetchone()
            if not full and not preview:
                continue
            seen.add((key,url))
            digest=_hash(key+"\n"+url)[:18]
            yield dict(run=run,org=org,key=key,title=title,lo=lo,hi=hi,candidate=news,full=full,
                       preview=preview,digest=digest,sources=sources,full_event=full_event)


def _question(key,instruction,scope,kind,**metadata):
    return {"id":key,"instruction":instruction,"material_kinds":[kind],
            "evidence_policy":{"sources":["publication_sources"],"aggregation":"consensus"},
            "publication":{"scope":scope,**metadata}}


def publication_questions(conn,pid):
    checks=[]
    sources=_external(conn,pid)
    if sources:
        claim=sources["claim"]
        for scope in ("event","participation"):
            subject=("Сообщает ли сам издатель о состоявшемся событии? Анонс или похожая акция другого года "
                     "не подтверждает его проведение." if scope=="event" else
                     "Указывает ли сам издатель, что именно данное учреждение или его сотрудники либо учащиеся "
                     "участвовали в этом событии? Одно название города, чужой вуз, место проведения или перечень "
                     "возможных участников недостаточны.")
            instruction=(f"Вопрос о событии «{claim['title']}», дата из карточки {claim.get('event_day') or 'не установлена'}, "
                         f"учреждение «{claim['organization']}». {subject} "
                         "Процитируй дословно один фрагмент статьи в «ёлочках» с действием. "
                         "Если отрывок не подтверждает утверждение, выбери uncertain.")
            checks.append(_question(f"publication_external_{scope}_{sources['rid']}",instruction,scope,"external_excerpt",
                   factcheck_id=sources["rid"],minimum_sources=sources["threshold"],allowlist_fingerprint=sources["fingerprint"]))
    for rec in _records(conn,pid):
        key=f"publication_internal_match_{rec['run']}_{rec['digest']}"
        checks.append(_question(key,"Описывает ли новость на сайте того же учреждения именно указанное мероприятие? "
                "Сопоставь название, даты самого действия и конкретные обстоятельства. Приведи дословную фразу из новости в «ёлочках». "
                "Совпадение названий ежегодной акции само по себе недостаточно. Дата публикации может отличаться "
                "от даты события. Для определённого ответа укажи оба материала. В поле evidence достаточно одной "
                "причины и короткой цитаты, до 300 символов. Это проверка связи двух записей.",
                "internal","timeline_record",run=rec["run"],digest=rec["digest"],news_page_id=rec["full"][0] if rec["full"] else None,
                event_page_id=rec["full_event"][0] if rec["full_event"] else None))
    return checks


def _mat(pid,check,mid,kind,text,**metadata):
    return ({"id":mid,"kind":kind,"path":f"pages/{pid}/publication/{mid}.txt","partial":True,
             "page_id":pid,"for_checks":[check["id"]],"sha256":_hash(text),**metadata},text)


def publication_materials(conn,pid,check):
    info=check["publication"]
    if info["scope"] in {"event","participation"}:
        src=_external(conn,pid)
        if not src or (src["rid"],src["fingerprint"])!=(info["factcheck_id"],info["allowlist_fingerprint"]):
            return []
        rows=[]
        for ordinal,url,final,publisher,data in src["rows"]:
            mid=f"p{pid}-fc{src['rid']}-a{ordinal}"
            text="Заголовок издателя: "+data.get("title","")+"\nСохранённый отрывок статьи:\n"+data["excerpt"]
            rows.append(_mat(pid,check,mid,"external_excerpt",text,source_url=url,final_url=final,
                publisher=publisher,publisher_name=data.get("publisher_name",""),independent_group=data["related_group"],
                captured_at=data.get("retrieved_at") or src["stamp"],factcheck_id=src["rid"],ordinal=ordinal,
                matched_base_url=data.get("final_matched_base_url"),article_sha256=data.get("sha256"),
                allowlist_fingerprint=src["fingerprint"],
                support_event=bool(data.get("signals",{}).get("event_supported")),
                support_participation=bool(data.get("signals",{}).get("participation_match")),
                source_excerpt=data["excerpt"],source_title=data.get("title","")))
        return rows
    rec=next((r for r in _records(conn,pid) if r["digest"]==info["digest"] and r["run"]==info["run"]),None)
    if not rec:
        return []
    prefix=f"p{pid}-pair{rec['run']}-{rec['digest']}"
    event_text=(f"Карточка мероприятия: {rec['title']}\nНачало из календарного поля: {rec['lo'] or 'не установлено'}"
                f"\nКонец: {rec['hi'] or 'не установлен'}")
    ids=[r["page_id"] for r in rec["sources"]]
    full_event=rec["full_event"]
    if full_event:
        snippet=conn.execute("SELECT text_page FROM page WHERE id=?",(full_event[0],)).fetchone()[0]
        html=BeautifulSoup(snippet,"html.parser")
        local=html.select_one(".tpl-block-full") or html.select_one("article") or html.select_one("main")
        if local:
            event_text+="\nСодержимое сохранённой страницы мероприятия:\n"+local.get_text(" ",strip=True)[:1800]
            ids.append(full_event[0])
    event=_mat(pid,check,prefix+"-event","timeline_record",event_text,record_type="event",
               source_url=rec["key"],source_run_id=rec["run"],source_page_ids=ids,detail_run_id=full_event[2] if full_event else None,
               detail_captured_at=full_event[1] if full_event else None,
               planned_start=rec["lo"],planned_end=rec["hi"])
    news=rec["candidate"]
    occurrence=None
    if rec["full"]:
        sid,stamp,srun=rec["full"]
        html=conn.execute("SELECT text_page FROM page WHERE id=?",(sid,)).fetchone()[0]
        soup=BeautifulSoup(html,"html.parser")
        main=soup.select_one(BODY) or soup.select_one('main,[role=main]')
        bounds,_why=_resolved(article_sources(html,sid)["event"])
        if bounds:
            occurrence=[d.date().isoformat() for d in bounds]
        if main:
            for n in main.select("header,footer,aside,nav,script,style,.breadcrumb,.related-news"):
                n.decompose()
        body=main.get_text(" ",strip=True) if main else ""
        origin="article" if body else "preview"
    else:
        body,origin="","preview"
    if not body and rec["preview"]:
        sid,ordinal,title,date,raw=rec["preview"]
        srun=rec["run"]
        stamp=conn.execute("SELECT date_loaded FROM page WHERE id=?",(sid,)).fetchone()[0]
        fragments=json.loads(raw).get("date_evidence",{}).get("sources",[])
        body="\n".join(r["role"]+": "+r["text"] for r in fragments if r.get("text"))
        if not body:
            body=(date or "")+" "+title
    if not body:
        return []
    text=f"Новость: {news['title']}\n"+body[:3500]
    record=_mat(pid,check,prefix+"-news","timeline_record",text,record_type=origin,source_url=news["url"],
                source_page_ids=[sid],source_run_id=srun,captured_at=stamp,full_text_included=len(body)<=3500,
                event_period_matches=bool(news.get("occurrence_in_planned_period")),reported_period=occurrence,
                source_excerpt=body[:3500])
    return [event,record]


def _norm(text):
    return " ".join(re.findall(r"\w+",text.casefold().replace("ё","е")))


def _quoted(evidence,text):
    normalized=" "+_norm(text)+" "
    quotes=re.findall(r"«([^»]{5,350})»|\"([^\"\n]{5,350})\"|'([^'\n]{5,350})'",evidence)
    return any(len(_norm(q).split())>=3 and " "+_norm(q)+" " in normalized for group in quotes for q in group if q)


def guard_source(check,materials,result):
    info=check["publication"]
    if info["scope"]=="internal":
        if result["outcome"]=="uncertain":
            return None
        if set(result["material_ids"])!={m["id"] for m in materials}:
            return "Для сравнения требуются обе исходные записи."
        article=next((m for m in materials if m.get("record_type")=="article"),None)
        if not article or not article["full_text_included"]:
            return "Доступно лишь превью или фрагмент новости. Одного совпадения названий недостаточно."
        if not _quoted(result["evidence"],article["source_excerpt"]):
            return "Цитата из новости не подтверждена сохранённым текстом."
        plan=next(m for m in materials if m["record_type"]=="event")
        period=article.get("reported_period")
        matches=bool(period and plan["planned_start"] and plan["planned_end"] and
                     plan["planned_start"]<=period[0]<=period[1]<=plan["planned_end"])
        if result["outcome"]=="present" and not (article["event_period_matches"] or matches):
            return "Не установлена дата описанного события для сравнения с планом."
        return None
    if result["outcome"]!="present":
        return "Данный отрывок не подтверждает утверждение о событии или участии учреждения."
    m=materials[0]
    if not _quoted(result["evidence"],m["source_excerpt"]):
        return "Дословная цитата с утверждением не подтверждена сохранённым отрывком."
    if not m["support_event"]:
        return "По данному отрывку не подтверждено проведение именно этого события."
    if info["scope"]=="participation" and not m["support_participation"]:
        return "В отрывке отсутствует подтверждённая связь события с участием именно данного учреждения."
    return None


def aggregate_external(check,materials,results):
    selected={mid for r in results if r["outcome"]=="present" for mid in r["material_ids"]}
    evidence=independent_reports(check,[m for m in materials if m["id"] in selected])
    refs=[m["id"] for m in evidence]
    publishers=[m["publisher"] for m in evidence]
    goal=check["publication"]["minimum_sources"]
    verified=len(refs)>=goal
    subject="Проведение события" if check["publication"]["scope"]=="event" else "Участие именно этого учреждения"
    msg=f"{subject}. Подтверждающих независимых издателей: {len(refs)} из требуемых {goal}."
    if verified:
        msg+=" "+", ".join(sorted(publishers))+"."
    elif results:
        msg+=" "+next((r["evidence"] for r in results if r["outcome"]=="uncertain"),"")
    return {"outcome":"present" if verified else "uncertain","material_ids":refs if verified else
            list(dict.fromkeys(x for r in results for x in r["material_ids"])),"evidence":msg[:600].strip()}


def independent_reports(check,materials):
    info=check["publication"]
    groups=set()
    publishers=set()
    hashes=set()
    rows=[]
    for m in materials:
        if (m.get("factcheck_id")!=info["factcheck_id"] or m.get("allowlist_fingerprint",info["allowlist_fingerprint"])!=info["allowlist_fingerprint"]
            or not m.get("support_event") or info["scope"]=="participation" and not m.get("support_participation")):
            continue
        key,host,digest=m.get("independent_group"),m.get("publisher"),m.get("article_sha256")
        if not key or not host or key in groups or host in publishers or digest and digest in hashes:
            continue
        groups.add(key)
        publishers.add(host)
        hashes.add(digest)
        rows.append(m)
    return rows
