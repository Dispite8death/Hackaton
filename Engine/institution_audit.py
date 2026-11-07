"""Header contacts, school schedule evidence and separate school/preschool reference profiles."""

import argparse
import json
import re
from collections import Counter, defaultdict
from contextlib import closing
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

if __package__:
    from .attached_files import index_attachments
    from .document_inventory import DOCUMENT_SCHEMA_SQL, document_inventory, index_documents, reference_documents
    from .institution_contacts import CONTACT_FIELDS, extract_contacts, section_links
    from .schedule_evidence import extract_schedule
    from .navigation import REQUIRED_SECTIONS
    from .render_store import incomplete_document
else:
    from attached_files import index_attachments
    from document_inventory import DOCUMENT_SCHEMA_SQL, document_inventory, index_documents, reference_documents
    from institution_contacts import CONTACT_FIELDS, extract_contacts, section_links
    from schedule_evidence import extract_schedule
    from navigation import REQUIRED_SECTIONS
    from render_store import incomplete_document


INSTITUTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS institution_contact_page (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    section TEXT NOT NULL, source TEXT NOT NULL, observations TEXT NOT NULL, section_links TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS school_schedule_page (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    source TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS institution_audit (
    run_id INTEGER PRIMARY KEY REFERENCES crawl_run(id) ON DELETE CASCADE,
    home_page_id INTEGER REFERENCES page(id), evidence TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS institution_contact_observation AS
    SELECT p.page_id,p.section,p.source,json_extract(j.value,'$.field') AS field,
        json_extract(j.value,'$.location') AS location,json_extract(j.value,'$.value') AS value,
        json_extract(j.value,'$.block_text') AS block_text,json_extract(j.value,'$.selector') AS selector
    FROM institution_contact_page p,json_each(p.observations) j;
""" + DOCUMENT_SCHEMA_SQL

_LABELS={"address":"адрес","phone":"телефон","email":"почта","hours":"режим работы","reception":"часы приёма",
         "contacts_link":"ссылка на контакты"}
_TOP={"header","under_header"}
_LATEST="""SELECT o.id,o.name,o.kind,r.id,a.evidence FROM organization o
    LEFT JOIN crawl_run r ON r.id=(SELECT max(q.id) FROM crawl_run q WHERE q.id_organization=o.id
                                   AND q.purpose='audit' AND q.finished_at IS NOT NULL)
    LEFT JOIN institution_audit a ON a.run_id=r.id"""


def _dump(data):
    return json.dumps(data,ensure_ascii=False,separators=(",",":"),allow_nan=False)


def _document(conn, pid):
    html, dom, raw_details = conn.execute("""
        SELECT p.text_page,r.rendered_html,r.details FROM page p LEFT JOIN page_render r ON r.page_id=p.id WHERE p.id=?
    """,(pid,)).fetchone()
    rendered=bool(dom) and not incomplete_document(json.loads(raw_details))
    return BeautifulSoup(dom if rendered else html,"html.parser"), "rendered_dom" if rendered else "response_html"


def _fetches(conn, run_id, org):
    result=[]
    for fid,url,final,sec,depth,status,error,pid in conn.execute("""
        SELECT f.id,f.url,f.final_url,f.section,f.depth,f.http_status,f.error,p.id FROM crawl_fetch f
        LEFT JOIN page p ON p.id=f.page_id AND p.id_organization=? WHERE run_id=? ORDER BY f.id
    """,(org,run_id)):
        result.append({"fetch_id":fid,"url":url,"final_url":final or url,"section":sec,"depth":depth,
                       "http_status":status,"error":error,"page_id":pid})
    return result


def _specialized_url(url):
    return bool(re.search(r"otdyh|otdykh|lager|o-lagere|sektsi|kruzhk",
                          unquote(urlsplit(url).path).casefold()))


def _entrypoints(links, section, fetches, home):
    """Retain different published routes, HTTP failures and redirects to the home page separately."""
    groups={}
    for link in (v for v in links if v["section"]==section):
        url=link["url"]
        item=groups.setdefault(url,{"url":url,"locations":[],"selectors":[],"visits":[],"status":"unvisited",
                                    "scope":"subdivision" if _specialized_url(url) else "institution"})
        if link["location"] not in item["locations"]:
            item["locations"].append(link["location"])
        item["selectors"].append(link["selector"])
        if link.get("anchor_exists") is True:
            item["status"]="in_page_anchor"
        elif not link["internal"]:
            item["status"]="external"
    for item in groups.values():
        if item["status"] in ("external","in_page_anchor"):
            continue
        for f in fetches:
            if f["depth"]==0 or item["url"] not in (f["url"],f["final_url"]):
                continue
            state="http_error" if f["page_id"] is None else (
                "redirected_home" if home and f["final_url"] in {home["url"],home["final_url"]} else "page_saved")
            item["visits"].append({"fetch_id":f["fetch_id"],"page_id":f["page_id"],"http_status":f["http_status"],
                                   "error":f["error"],"final_url":f["final_url"],"status":state})
            if item["status"]!="page_saved":
                item["status"]=state
    return list(groups.values())


def _eligible_fetches(section, fetches, routes):
    confirmed={v["page_id"] for r in routes for v in r["visits"] if v["status"]=="page_saved"}
    # Children's holiday camps and optional clubs frequently have an identically named contact/schedule page.
    return [f for f in fetches if f["depth"]>0 and f["page_id"] and
            not _specialized_url(f["final_url"]) and (f["page_id"] in confirmed or f["section"]==section)]


def _coverage(conn,run_id,kind,fetches):
    expected=set(REQUIRED_SECTIONS[kind])
    if kind=="kindergarten":
        expected.discard("events")  # The case requires kindergarten news. A standalone events section is optional.
    visited={f["section"] for f in fetches if f["page_id"]}
    urls={u for f in fetches if f["page_id"] for u in (f["url"],f["final_url"])}
    for section,url in conn.execute("""SELECT DISTINCT l.section,l.url FROM crawl_link l
        JOIN crawl_fetch f ON l.fetch_id=f.id WHERE f.run_id=? AND l.relation='section' AND l.follow=1""",(run_id,)):
        if url in urls:
            visited.add(section)
    return {"visited_sections":sorted(visited & expected),"expected_sections":sorted(expected),
            "observed":len(visited & expected),"total":len(expected)}


def _save_contacts(conn, page, section, source, soup, links):
    obs=extract_contacts(soup,page["final_url"],section=section)
    conn.execute("""INSERT INTO institution_contact_page VALUES(?,?,?,?,?)
        ON CONFLICT(page_id) DO UPDATE SET section=excluded.section,source=excluded.source,
          observations=excluded.observations,section_links=excluded.section_links""",
                 (page["page_id"],section,source,_dump(obs),_dump(links)))
    return obs


def _staff_evidence(conn,run):
    """Count each linked profile once even when its card appears both on the home page and in a directory."""
    profiles={}
    for profile,name,role,photo,education,email,phone in conn.execute("""
        SELECT s.profile_url,s.full_name,s.role,s.photo_state,s.education,s.email,s.phone
        FROM staff_observation s JOIN crawl_fetch f ON f.page_id=s.page_id WHERE f.run_id=?
    """,(run,)):
        if not name or not name.strip():
            continue
        key=profile or (name.casefold().strip(),(role or "").casefold().strip())
        evidence=profiles.setdefault(key,set())
        if photo=="linked":
            evidence.add("photo_link")
        if education and education.strip():
            evidence.add("education")
        if (email and email.strip()) or (phone and phone.strip()):
            evidence.add("contact")
    total=len(profiles)
    complete=sum(len(v)==3 for v in profiles.values())
    return {"grouped_cards":total,"with_photo_education_contact":complete,
            "filled_fraction":complete/total if total else None,
            "scope":"Сохранённые карточки; фото учитывается по ссылке, личность на изображении отдельно проверяется."}


def audit_run(conn,run_id):
    """Update the observations for exactly this crawl in the caller's transaction."""
    row=conn.execute("""SELECT o.id,o.name,o.kind,o.site_url,r.state FROM organization o JOIN crawl_run r
                        ON r.id_organization=o.id WHERE r.id=?""",(run_id,)).fetchone()
    if row is None:
        raise ValueError(f"Не найден обход {run_id}")
    org,name,kind,site,state=row
    fetches=_fetches(conn,run_id,org)
    index_documents(conn,run_id,fetches,_document)
    # Old derived rows can become ineligible after reclassification. All writes stay in the caller's transaction.
    for table in ("institution_contact_page","school_schedule_page"):
        conn.execute(f"DELETE FROM {table} WHERE page_id IN (SELECT page_id FROM crawl_fetch WHERE run_id=?)",(run_id,))
    home=next((f for f in fetches if f["page_id"] and f["depth"]==0),None)
    links, observed, page_fields = [], defaultdict(set), []
    if home:
        soup,source=_document(conn,home["page_id"])
        links=section_links(soup,home["final_url"],site)
        for link in links:
            if link["href"].startswith("#"):
                link["anchor_exists"]=soup.find(id=unquote(link["href"][1:])) is not None
        for obs in _save_contacts(conn,home,"home",source,soup,links):
            observed[obs["location"]].add(obs["field"])
    contacts=_entrypoints(links,"contacts",fetches,home)
    home_urls={home["url"],home["final_url"]} if home else set()
    for f in _eligible_fetches("contacts",fetches,contacts):
        if f["final_url"] in home_urls:
            continue
        soup,source=_document(conn,f["page_id"])
        values=_save_contacts(conn,f,"contacts",source,soup,[])
        # Keep repeated page header/footer in its own evidence; it must not count as a filled contacts section.
        fields={v["field"] for v in values if v["location"]=="contacts_page"}
        observed["contacts_page"].update(fields)
        page_fields.append({"page_id":f["page_id"],"url":f["final_url"],"fields":sorted(fields)})
    header_fields=set().union(*(observed[loc] for loc in _TOP))
    if any(r["scope"]=="institution" and _TOP.intersection(r["locations"]) for r in contacts):
        header_fields.add("contacts_link")
    contact_summary={"fields_by_location":{k:sorted(v) for k,v in observed.items()},
                     "header_profile":sorted(header_fields),"section_pages":page_fields,"routes":contacts}
    schedule=None
    if kind=="school":
        routes=_entrypoints(links,"schedule",fetches,home)
        pages=[]
        for f in _eligible_fetches("schedule",fetches,routes):
            if f["final_url"] in home_urls:
                continue
            soup,source=_document(conn,f["page_id"])
            data=extract_schedule(soup,f["final_url"])
            conn.execute("""INSERT INTO school_schedule_page VALUES(?,?,?,?)
                ON CONFLICT(page_id) DO UPDATE SET source=excluded.source,status=excluded.status,evidence=excluded.evidence""",
                         (f["page_id"],source,data["status"],_dump(data)))
            pages.append({"page_id":f["page_id"],"url":f["final_url"],"status":data["status"],
                          "material_count":len(data["materials"]),"empty_notice":data["empty_notice"]})
        states={page["status"] for page in pages}
        priority=("lesson_rows","material_candidate","external_diary","class_selection_required",
                  "explicit_empty","no_material_detected")
        if states:
            strongest=next(status for status in priority if status in states)
        elif any(route["status"]=="http_error" for route in routes):
            strongest="http_error"
        elif routes:
            strongest="route_without_saved_schedule"
        else:
            strongest="no_saved_schedule"
        schedule={"routes":routes,"from_header":any(r["scope"]=="institution" and _TOP.intersection(r["locations"]) for r in routes),
                  "status":strongest,"pages":pages}
    result={"organization_id":org,"organization":name,"kind":kind,"run_id":run_id,"state":state,
            "home_page_id":home["page_id"] if home else None,"contacts":contact_summary,"schedule":schedule,
            "coverage":_coverage(conn,run_id,kind,fetches),"staff_evidence":_staff_evidence(conn,run_id)}
    conn.execute("""INSERT INTO institution_audit VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET
        home_page_id=excluded.home_page_id,evidence=excluded.evidence""",(run_id,result["home_page_id"],_dump(result)))
    index_attachments(conn,run_id=run_id)
    return result


def institution_report(conn,run_id):
    row=conn.execute("SELECT evidence FROM institution_audit WHERE run_id=?",(run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def page_observations(conn,page_id):
    result={}
    row=conn.execute("SELECT source,observations,section_links FROM institution_contact_page WHERE page_id=?",(page_id,)).fetchone()
    if row:
        result["contact_observations"]={"source":row[0],"values":json.loads(row[1]),"section_links":json.loads(row[2])}
    row=conn.execute("SELECT source,evidence FROM school_schedule_page WHERE page_id=?",(page_id,)).fetchone()
    if row:
        result["schedule"]={"source":row[0],**json.loads(row[1])}
    row=conn.execute("SELECT source,section,catalog,evidence FROM institution_document_page WHERE page_id=?",
                     (page_id,)).fetchone()
    if row:
        result["documents"]={"source":row[0],"section":row[1],"catalog":bool(row[2]),**json.loads(row[3])}
    return result


def reference_report(conn,kind,minimum_prevalence=None):
    """Observed same-kind benchmark. Missing or unreviewed runs are visible and never enter the denominator."""
    if minimum_prevalence is None:
        if __package__:
            from .model_settings import setting
        else:
            from model_settings import setting
        minimum_prevalence=float(setting(conn,"reference_minimum",0.5))
    if kind not in ("school","kindergarten") or not 0<minimum_prevalence<=1:
        raise ValueError("Нужны тип учреждения и порог от 0 (исключая) до 1")
    rows=conn.execute(_LATEST+" WHERE o.kind=? ORDER BY o.id",(kind,)).fetchall()
    available=[]
    omitted=[]
    for org,_,_,run,raw in rows:
        record=json.loads(raw) if raw else None
        if record and record["home_page_id"] is not None:
            available.append(record)
        else:
            omitted.append({"organization_id":org,"run_id":run,"reason":"no_audited_home"})
    counts=Counter(f for record in available for f in record["contacts"]["header_profile"])
    total=len(available)
    profile={f:{"count":counts[f],"assessed":total,"fraction":counts[f]/total if total else None}
             for f in (*CONTACT_FIELDS,"contacts_link")}
    common=[f for f,v in profile.items() if total and v["fraction"]>=minimum_prevalence]
    assessed_sections=[r["contacts"] for r in available if r["contacts"]["section_pages"]]
    section_total=len(assessed_sections)
    section_counts=Counter(field for contact in assessed_sections
                           for field in contact["fields_by_location"].get("contacts_page",[]))
    section_profile={f:{"count":section_counts[f],"assessed":section_total,
                        "fraction":section_counts[f]/section_total if section_total else None} for f in CONTACT_FIELDS}
    section_common=[f for f,v in section_profile.items() if section_total and v["fraction"]>=minimum_prevalence]
    required=set(CONTACT_FIELDS if kind=="school" else CONTACT_FIELDS[:-1])
    candidates=[]
    for r in available:
        fields=set(r["contacts"]["header_profile"])
        body_fields=r["contacts"]["fields_by_location"]
        published=set().union(*map(set,body_fields.values())) if body_fields else set()
        observed=r["coverage"]["observed"]
        # A rendered lesson with its time is stronger evidence than an attachment awaiting review.
        schedule_grade=max(({"lesson_rows":2,"material_candidate":1}.get(s["status"],0)
                            for s in (r.get("schedule") or {}).get("pages",[])),default=0)
        staff=r.get("staff_evidence") or _staff_evidence(conn,r["run_id"])
        rank=(observed,len(required & published),schedule_grade,staff["filled_fraction"] or 0,
              len(fields.intersection(common)),r["state"]=="completed")
        candidates.append({"organization_id":r["organization_id"],"name":r["organization"],"run_id":r["run_id"],
                           "home_page_id":r["home_page_id"],"rank":list(rank),"staff_evidence":staff,
                           "schedule_evidence_grade":schedule_grade if kind=="school" else None,
                           "declared_contact_fields":sorted(required & published),"header_present":sorted(fields),
                           "header_common_unseen":sorted(set(common)-fields),"coverage":r["coverage"],"state":r["state"]})
    candidates.sort(key=lambda row:tuple(-x for x in row["rank"])+(row["organization_id"],))
    best=candidates[0] if candidates else None
    tied=[r["organization_id"] for r in candidates if r["rank"]==best["rank"]] if best else []
    documents=reference_documents(conn,best,candidates)
    return {"kind":kind,"registered":len(rows),"assessed":total,"unassessed":omitted,"fields":profile,
            "contacts_section_assessed":section_total,"contact_section_fields":section_profile,
            "common_contact_section_fields":section_common,
            "threshold":minimum_prevalence,"common_header_fields":common,"reference":best,"equal_candidates":tied,
            "documents":documents,
            "ranking_rule":["число посещённых разделов своего чек-листа","число требуемых опубликованных контактных полей",
                            "для школ — наблюдаемые уроки со временем (2), кандидат-файл (1), прочее (0)",
                            "доля изученных групп карточек с фото-ссылкой, образованием и контактом",
                            "соответствие типичной шапке своей группы","завершённость обхода",
                            "меньший ID при равенстве"],
            "scope":"Наблюдаемый эталон охвата и контактных полей. Доступность перехода не подтверждает содержание файлов.",
            "candidates":candidates}


def main():
    if __package__:
        from .config import connect_db
        from .crawl_store import initialize_db
    else:
        from config import connect_db
        from crawl_store import initialize_db
    parser=argparse.ArgumentParser(description="Контакты, расписание, опубликованные документы и сравнение с эталоном")
    scope=parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all",action="store_true",help="пересчёт последних основных обходов без посещения сайтов")
    scope.add_argument("--run",type=int,help="пересчёт и вывод по указанному обходу")
    scope.add_argument("--org",type=int,help="вывод по учреждению")
    scope.add_argument("--references",action="store_true",help="эмпирические эталоны каждой группы")
    parser.add_argument("--documents",action="store_true",
                        help="для --all пересчитать только документы, для --run/--org показать весь перечень ссылок")
    parser.add_argument("--threshold",type=float,help="доля совпадений для типичного поля, от 0 (исключая) до 1")
    parser.add_argument("--json",action="store_true")
    args=parser.parse_args()
    with closing(connect_db()) as conn:
        initialize_db(conn)
        if args.all:
            runs=[(r[3],r[0]) for r in conn.execute(_LATEST+" ORDER BY o.id") if r[3] is not None]
            for run,org in runs:
                with conn:
                    if args.documents:
                        index_documents(conn,run,_fetches(conn,run,org),_document)
                    else:
                        audit_run(conn,run)
            print(f"Обработано основных обходов: {len(runs)}")
            return
        if args.references:
            result=[reference_report(conn,kind,args.threshold) for kind in ("school","kindergarten")]
            if not args.json:
                for item in result:
                    print(f"{item['kind']} · главные {item['assessed']}/{item['registered']}")
                    for k,v in item["fields"].items():
                        print(f"  {_LABELS[k]} в шапке или под шапкой: {v['count']}/{v['assessed']}")
                    for k,v in item["contact_section_fields"].items():
                        print(f"  {_LABELS[k]} в разделе «Контакты»: {v['count']}/{v['assessed']}")
                    print("Эталон по измеренным признакам:",item["reference"]["name"] if item["reference"] else "недостаточно данных")
                    docs=item["documents"]
                    print(f"  Каталоги документов обследованы у {docs['institutions_with_catalog']} учреждений. "
                          f"Обнаружено ссылок на файлы в каталогах: {docs['catalog_files']}. "
                          f"В перечне эталона {len(docs['reference_items'])} файлов и {len(docs['reference_groups'])} тематических групп.")
                return
        elif args.run is not None:
            with conn:
                result=audit_run(conn,args.run)
            if args.documents:
                result=document_inventory(conn,args.run)
        else:
            row=conn.execute(_LATEST+" WHERE o.id=?",(args.org,)).fetchone()
            result=json.loads(row[4]) if row and row[4] else None
            if result is None:
                parser.exit(1,"Нет выполненной проверки для этого учреждения\n")
            if args.documents:
                result=document_inventory(conn,result["run_id"])
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
