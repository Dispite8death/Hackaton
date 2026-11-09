"""Published file links and same-kind comparisons of observed document catalogues."""

import json
import re
from collections import Counter, defaultdict
from urllib.parse import unquote, urlsplit


DOCUMENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS institution_document_page (
    page_id INTEGER PRIMARY KEY REFERENCES page(id) ON DELETE CASCADE,
    section TEXT NOT NULL, catalog INTEGER NOT NULL CHECK(catalog IN (0,1)),
    source TEXT NOT NULL, evidence TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS institution_document_link AS
    SELECT p.page_id,p.section,p.catalog,p.source,json_extract(j.value,'$.url') AS url,
           json_extract(j.value,'$.title') AS title,json_extract(j.value,'$.format') AS format,
           json_extract(j.value,'$.detail_url') AS detail_url,json_extract(j.value,'$.group') AS document_group
    FROM institution_document_page p,json_each(p.evidence,'$.documents') j;
"""

# Only specific subjects form comparable groups. The legal form "приказ" or "положение"
# is recorded separately and never suffices for a subject match between institutions.
_RULES = (
    ("charter_amendments", "Изменения в уставе", r"(?:изменени|дополнени).{0,55}устав"),
    ("charter", "Устав", r"^устав(?:\s|$)"),
    ("medical_license", "Лицензия на медицинскую деятельность", r"лицензи.{0,65}медицинск"),
    ("education_license", "Лицензия на образовательную деятельность", r"лицензи.{0,65}образовательн"),
    ("unspecified_license", "Лицензия без уточнения деятельности", r"^лицензия(?:\s*\d+)?$"),
    ("accreditation", "Аккредитация образовательной деятельности",
     r"^(?!.*(?:эксперт|медицинск))(?:свидетельств.{0,60}аккредитаци|(?:государственн\w*\s+)?аккредитаци|"
     r".*аккредитаци\w*\s+образовательн\w*\s+(?:деятельност|программ))"),
    ("self_assessment", "Самообследование учреждения", r"самообследован"),
    ("public_report", "Публичный отчёт или доклад", r"публичн\w*\s+(?:отчет|доклад)"),
    ("financial_plan", "План финансово-хозяйственной деятельности", r"план.{0,22}(?:финансово хозяйствен|фхд\b)"),
    ("employee_rules", "Правила внутреннего трудового распорядка", r"(?:трудов\w*\s+распорядк|внутренн\w*\s+распорядк\w*\s+(?:работник|сотрудник))"),
    ("learner_rules", "Правила внутреннего распорядка обучающихся", r"внутренн\w*\s+распорядк\w*\s+(?:обучающ|учащ|воспитанник)"),
    ("transfer", "Перевод и отчисление", r"(?:перевод.{0,65}отчислен|отчислен.{0,65}перевод)"),
    ("admission", "Порядок приёма", r"(?:поряд|правил|положени).{0,35}прием(?:а|е)\b(?!\s+(?:граждан|обращений|на работу))"),
    ("study_hours", "Режим занятий", r"режим\w*\s+заняти"),
    ("pedagogical_council", "Педагогический совет", r"(?:педагогическ\w*\s+совет|педсовет)"),
    ("parents_council", "Совет родителей", r"совет\w*\s+родител"),
    ("general_staff_assembly", "Общее собрание работников", r"общ\w*\s+собрани\w*\s+(?:сотрудник|работник|трудов)"),
    ("disputes", "Комиссия по урегулированию споров", r"комисси.{0,45}(?:урегулирован|регулирован).{0,24}спор"),
    ("personal_data", "Персональные данные и конфиденциальность", r"(?:персональн\w*\s+данн|политик\w*\s+конфиденциальност)"),
    ("anti_corruption", "Противодействие коррупции", r"(?:антикоррупц|противодействи.{0,18}коррупц)"),
    ("paid_education", "Платные образовательные услуги", r"платн\w*\s+(?:дополнительн\w*\s+)?образовательн\w*\s+услуг"),
    ("parent_fee", "Родительская плата", r"родительск\w*\s+плат"),
    ("education_contract", "Договор об образовательных услугах", r"договор.{0,45}образовательн\w*\s+услуг"),
    ("development_program", "Программа развития учреждения", r"программ\w*\s+развити"),
    ("education_program", "Образовательная программа", r"^(?:основная\s+|адаптированная\s+)?образовательная программа"),
    ("workplace_assessment", "Специальная оценка условий труда", r"специальн\w*\s+оценк\w*\s+условий\s+труда"),
    ("fire_safety", "Пожарная безопасность", r"пожарн\w*\s+безопасност"),
    ("supervision_order", "Предписания органов контроля", r"^предписани"),
)
_GROUPS = {code:label for code,label,_ in _RULES}
_PATTERNS = [(code, re.compile(pattern)) for code,_,pattern in _RULES]
_FORMS = re.compile(r"\b(устав|положение|правила|порядок|лицензия|свидетельство|приказ|постановление|план|"
                    r"акт|предписание|программа|отчет|договор|протокол|выписка|график|перечень|расписание)\b")
_EXTENSION = re.compile(r"\.(?:pdf|docx?|odt|rtf|xlsx?|ods|pptx?|zip|rar|7z|jpe?g|png)\s*$", re.I)
_CATALOG_PATH = re.compile(r"/(?:dokumenty|dokumenti|documents|документы)(?:/|$)", re.I)
_SCOPE = ("Сопоставляются ссылки в каталогах учреждений одного типа. Причины совпадения — один URL, "
          "нормализованное название или общая тема в названиях. Из других разделов приведены файлы, "
          "обнаруженные на сохранённых страницах со ссылками на документы. Содержимое и доступность файлов "
          "по ссылкам не проверялись. Ненайденный при частичном обходе документ может быть на следующем листе каталога.")


def _normalized(title):
    return " ".join(re.findall(r"[a-zа-я0-9]+", _EXTENSION.sub("",title).casefold().replace("ё","е")))


def _classify(document):
    title = _normalized(document["title"])
    group = next((code for code,pattern in _PATTERNS if pattern.search(title)), None)
    form = _FORMS.search(title)
    return {**document, "group":group, "group_label":_GROUPS.get(group), "type":form[0] if form else None}


def index_documents(conn, run_id, fetches, read_document):
    if __package__:
        from .document_links import extract_documents
    else:
        from document_links import extract_documents
    conn.execute("""DELETE FROM institution_document_page WHERE page_id IN (
                    SELECT page_id FROM crawl_fetch WHERE run_id=?)""", (run_id,))
    # The crawler also records file links from other sections. Inspect their actual DOM
    # to retain card titles and exclude links outside the main content.
    candidates = {row[0] for row in conn.execute("""SELECT DISTINCT f.page_id FROM crawl_fetch f JOIN crawl_link l
        ON l.fetch_id=f.id WHERE f.run_id=? AND l.relation='document'""", (run_id,))}
    home_urls = {u for f in fetches if f["depth"]==0 for u in (f["url"],f["final_url"])}
    for f in fetches:
        pid, url = f["page_id"], f["final_url"]
        if pid is None or (f["depth"] and url in home_urls):
            continue
        catalog = f["section"]=="documents" or bool(_CATALOG_PATH.search(unquote(urlsplit(url).path)))
        if not catalog and pid not in candidates:
            continue
        soup, source = read_document(conn,pid)
        evidence = extract_documents(soup,url)
        if not catalog and not evidence["documents"]:
            continue
        evidence["documents"] = [_classify(doc) for doc in evidence["documents"]]
        conn.execute("INSERT INTO institution_document_page VALUES(?,?,?,?,?)",
                     (pid,f["section"],int(catalog),source,json.dumps(evidence,ensure_ascii=False,separators=(",",":"))))


def document_inventory(conn, run_id):
    """Deduplicate file URLs within the run; keep the snapshots disclosing each link."""
    run = conn.execute("SELECT state,stop_reason FROM crawl_run WHERE id=?", (run_id,)).fetchone()
    if not run:
        raise ValueError(f"Не найден обход {run_id}")
    visits = {}
    for url,final,pid,code in conn.execute(
            "SELECT url,final_url,page_id,http_status FROM crawl_fetch WHERE run_id=? ORDER BY id", (run_id,)):
        if not pid:
            visits.setdefault(url, (None,code))
        else:
            visits[url] = visits[final or url] = (pid,code)
    by_url, pages = {}, []
    listing_ids = set()
    pages_with_files = set()
    pagination, details,linked_details = set(),set(),set()
    rows = conn.execute("""SELECT i.page_id,f.final_url,f.url,i.section,i.catalog,i.evidence
        FROM institution_document_page i JOIN crawl_fetch f ON f.page_id=i.page_id
        WHERE f.run_id=? ORDER BY i.catalog DESC,i.page_id""", (run_id,))
    for pid,final,url,section,catalog,raw in rows:
        evidence = json.loads(raw)
        if catalog:
            pages.append({"page_id":pid, "url":final or url})
            normalized_path = unquote(urlsplit(final or url).path).rstrip("/")
            if (evidence["pagination_urls"] or evidence["detail_urls"]
                    or _CATALOG_PATH.fullmatch("/"+normalized_path.rsplit("/",1)[-1])):
                listing_ids.add(pid)
            if evidence["documents"]:
                pages_with_files.add(pid)
            pagination.update(evidence["pagination_urls"])
            details.update(evidence["detail_urls"])
            linked_details.update(d["detail_url"] for d in evidence["documents"] if d.get("detail_url"))
        for d in evidence["documents"]:
            existing = by_url.get(d["url"])
            if existing is None:
                existing = by_url[d["url"]] = {**d,"catalog":bool(catalog),"source_page_ids":[],"source_sections":[]}
            elif existing["title_source"] == "filename" and d["title_source"] != "filename":
                existing.update(d)
            existing["source_page_ids"].append(pid)
            if section not in existing["source_sections"]:
                existing["source_sections"].append(section)
    items = sorted(by_url.values(),key=lambda x:(not x["catalog"],_normalized(x["title"]),x["url"]))
    catalog_page_ids = {p["page_id"] for p in pages}
    def unresolved(urls):
        return sorted(url for url in urls if visits.get(url,(None,None))[0] not in catalog_page_ids)
    entries = [r[0] for r in conn.execute("""SELECT DISTINCT l.url FROM crawl_link l JOIN crawl_fetch f ON f.id=l.fetch_id
               WHERE f.run_id=? AND l.section='documents' AND l.relation='section' ORDER BY l.url""",(run_id,))]
    routes = []
    for entry in entries:
        page_id,status = visits.get(entry,(None,None))
        state = ("catalog_saved" if page_id in catalog_page_ids else "different_target" if page_id else
                 "failed" if entry in visits else "unvisited")
        routes.append({"url":entry,"page_id":page_id,"http_status":status,"state":state})
        if page_id in catalog_page_ids:
            listing_ids.add(page_id)
    pending_pages = unresolved(pagination)
    pending_details = unresolved(details)
    unresolved_details = [url for url in details-linked_details if visits.get(url,(None,None))[0] not in pages_with_files]
    partial = (not listing_ids or pending_pages or pending_details or any(route["state"]!="catalog_saved" for route in routes)
               or run[1] not in (None,"frontier_exhausted"))
    coverage = {"catalog_pages":pages, "observed_catalog_pages":len(pages), "listing_page_ids":sorted(listing_ids),
                "catalog_routes":routes,
                "unvisited_pagination":pending_pages,"unvisited_detail_urls":pending_details,
                "details_without_observed_file":sorted(unresolved_details),
                "run_state":run[0],"stop_reason":run[1],
                "status":"unseen" if not pages else "partial" if partial else "observed_catalog"}
    return {"run_id":run_id,"items":items,"catalog_files":sum(i["catalog"] for i in items),
            "other_files":sum(not i["catalog"] for i in items),"coverage":coverage}


def _example(doc):
    return {k:doc[k] for k in ("title","url","format","source_page_ids")}


def _index(inventory):
    groups, exact = defaultdict(list), defaultdict(list)
    for doc in inventory["items"]:
        if not doc["catalog"]:
            continue
        if doc["group"]:
            groups[doc["group"]].append(doc)
        name = _normalized(doc["title"])
        if doc["title_source"] != "filename" and len(name)>=16 and len(name.split())>=2:
            exact[name].append(doc)
    return groups,exact


def compare_documents(reference, institution):
    left, left_names = _index(reference)
    right, right_names = _index(institution)
    left_docs = {d["url"]:d for d in reference["items"] if d["catalog"]}
    right_docs = {d["url"]:d for d in institution["items"] if d["catalog"]}
    shared_urls = left_docs.keys() & right_docs.keys()
    shared_names = left_names.keys() & right_names.keys()
    def other_titles(documents, index):
        matched = shared_urls | {d["url"] for name in shared_names for d in index[name]}
        return [_example(d) for url,d in documents.items() if url not in matched]
    return {"catalog_files":institution["catalog_files"],"other_files":institution["other_files"],
            "coverage":institution["coverage"],
            "matching_urls":[{"reason":"same_published_file_url","reference":_example(left_docs[url]),
                              "institution":_example(right_docs[url])} for url in sorted(shared_urls)],
            "reference_titles_unseen":other_titles(left_docs,left_names),
            "additional_titles":other_titles(right_docs,right_names),
            "matched_groups":[{"group":g,"label":_GROUPS[g],"reason":"same_subject_in_titles",
                               "reference":_example(left[g][0]),"institution":_example(right[g][0])}
                              for g in sorted(left.keys() & right.keys())],
            "reference_groups_unseen":[{"group":g,"label":_GROUPS[g],"reference":_example(left[g][0])}
                                       for g in sorted(left.keys()-right.keys())],
            "additional_groups":[{"group":g,"label":_GROUPS[g],"institution":_example(right[g][0])}
                                 for g in sorted(right.keys()-left.keys())],
            "matching_titles":[{"title":t,"reason":"same_title_ignoring_case_spaces_and_punctuation",
                               "reference":_example(left_names[t][0]),"institution":_example(right_names[t][0])}
                              for t in sorted(shared_names)]}


def reference_documents(conn,best,candidates):
    if not best:
        return {"reference_organization_id":None,"reference_run_id":None,"reference_items":[],"reference_groups":[],
                "catalog_files":0,"other_files":0,"institutions_with_catalog":0,"scope":_SCOPE}
    baseline = document_inventory(conn,best["run_id"])
    groups,_ = _index(baseline)
    totals = Counter()
    for candidate in candidates:
        items = baseline if candidate["run_id"]==best["run_id"] else document_inventory(conn,candidate["run_id"])
        candidate["documents"] = compare_documents(baseline,items)
        totals.update({"catalog":items["catalog_files"],"other":items["other_files"],
                       "seen":int(bool(items["coverage"]["catalog_pages"]))})
    return {"reference_organization_id":best["organization_id"],"reference_run_id":best["run_id"],
            "reference_items":[d for d in baseline["items"] if d["catalog"]],
            "reference_groups":[{"group":g,"label":_GROUPS[g],"files":len(files)} for g,files in sorted(groups.items())],
            "catalog_files":totals["catalog"],"other_files":totals["other"],"institutions_with_catalog":totals["seen"],
            "scope":_SCOPE}
