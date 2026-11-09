"""Read-only, evidence-based summaries for the web interface and its PDF exports."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import re
from hashlib import sha256
from urllib.parse import urlsplit


ZONE=timezone(timedelta(hours=11))
KINDS={"school":"Школы","kindergarten":"Детские сады"}
CATEGORIES={"home":"Главная страница","news":"Новости","events":"Мероприятия","documents":"Документы",
            "staff":"Сотрудники","contacts":"Контакты","schedule":"Расписание","programs":"Образование",
            "layout":"Оформление","factcheck":"Внешние источники","structure":"Об учреждении"}
GROUPS={"content":"Содержание","publication":"Публикации","layout":"Оформление","photos":"Фотографии",
        "attachments":"Вложения","navigation":"Навигация"}
STATUS={"issue":"Требует исправления","pass":"Подтверждено","review":"Нужна сверка","unverified":"Недостаточно данных","info":"Справочно"}
SEVERITY={"high":"Высокая","medium":"Средняя","low":"Низкая"}
QUALITY={"excellent":"Отлично","good":"Хорошо","fair":"Удовлетворительно","poor":"Требует внимания","unrated":"Недостаточно данных"}
CONFIDENCE={"high":"Высокая","medium":"Средняя","low":"Низкая","unknown":"Не установлена"}
LABELS={"kind":KINDS,"category":CATEGORIES,"group":GROUPS,"severity":SEVERITY,"quality":QUALITY,
        "detector":{"ai":"ИИ","algorithm":"Алгоритм","none":"Ещё не проверено"},"confidence":CONFIDENCE,"status":STATUS}
GRADING={
    "scale":"0–100","method":"Каждый применимый критерий имеет один голос. Если на проверенной странице выявлена проблема, "
    "критерий требует исправления. Балл равен доле подтверждённых критериев среди критериев с определённым результатом.",
    "coverage":"Охват — доля критериев с подтверждённым результатом или замечанием среди применимых. "
    "Вопросы для сверки и непроверенные критерии в балл не входят.",
    "classification":"При охвате менее 50% или менее трёх оценённых критериев класс качества не назначается. "
    "Шкала проекта: от 90 — отлично, от 75 — хорошо, от 55 — удовлетворительно. Ниже 55 — требует внимания.",
    "scope":"Оценка относится к сохранённым материалам. Фильтры замечаний не пересчитывают балл учреждения. "
    "Место определяется среди всех учреждений за выбранный период. Даты задаются по времени проверки в Магадане."
}

# Each row is a single voting criterion regardless of the number of pages or model calls.
# The last column selects the institution type; '*' denotes both.
SPECS={
 "address":("contacts","content","Адрес учреждения","high","Опубликуйте почтовый адрес в контактах учреждения.","*"),
 "phone":("contacts","content","Контактный телефон","high","Уточните действующий телефон и укажите его в контактах.","*"),
 "email":("contacts","content","Электронная почта","medium","Опубликуйте действующий адрес электронной почты.","*"),
 "hours":("contacts","content","Режим работы","medium","Укажите дни и часы работы учреждения.","*"),
 "reception":("contacts","content","Часы приёма","medium","Уточните дни и часы личного приёма.","school"),
 "news_gap":("news","publication","Интервал между новостями","high","Планируйте публикации с интервалом до 14 дней.","*"),
 "news_delay":("news","publication","Публикация в течение 24 часов","high","Сверьте дату события и публикуйте новость в течение суток.","*"),
 "document_links":("documents","attachments","Каталог документов","medium","Проверьте названия документов и ссылки на оригиналы.","*"),
 "document_content":("documents","attachments","Содержание документов","medium","Сверьте оригинал документа с его названием в каталоге.","*"),
 "staff_identity":("staff","content","ФИО и должность сотрудников","medium","Укажите ФИО и должность вместе в персональной карточке.","*"),
 "staff_education":("staff","content","Образование сотрудников","medium","Дополните профиль сведениями об основном профессиональном образовании.","*"),
 "staff_contacts":("staff","content","Контакты сотрудников","medium","Укажите служебный контакт в профиле сотрудника.","*"),
 "staff_photo":("staff","photos","Фотографии сотрудников","medium","Сверьте карточку и разместите подходящую фотографию сотрудника.","*"),
 "building":("home","photos","Главное изображение","medium","Проверьте изображение учреждения и подпись к нему.","*"),
 "main_photo_size":("home","photos","Размер оригинала главной фотографии","low","Подготовьте основной файл главной фотографии размером 1200 × 900 пикселей.","school"),
 "description":("home","content","Описание школы на главной","low","Добавьте краткое содержательное описание этой школы.","school"),
 "history":("structure","content","История школы","medium","Укажите проверяемые исторические факты в разделе о школе.","school"),
 "management":("structure","content","Структура управления","medium","Укажите органы управления и их функции или контакты.","school"),
 "schedule":("schedule","attachments","Расписание учебных занятий","high","Разместите доступное расписание с классом, занятием и днём или временем.","school"),
 "quarter_current":("events","publication","План на текущий квартал","medium","Укажите названия и полные даты запланированных мероприятий.","school"),
 "quarter_next":("events","publication","План на следующий квартал","medium","Опубликуйте планы следующего квартала с полными датами.","school"),
 "canteen":("home","photos","Фотография школьной столовой","low","Добавьте различимый снимок помещения столовой в соответствующий блок.","school"),
 "programs":("programs","content","Образовательные программы","medium","Укажите название и содержание реализуемой программы.","kindergarten"),
 "staff_resources":("programs","content","Материалы для педагогов","medium","Дополните раздел практическими материалами для педагогов.","kindergarten"),
 "banner":("layout","layout","Доступность навигации на экране","medium","Проверьте, перекрывает ли баннер текст или элементы меню.","*"),
}
EXTRA={
 "garden_team":("staff","content","Карточки коллектива на главной","low","Откройте главную и сверьте персональные карточки с разделом «Все сотрудники»."),
 "navigation":("structure","navigation","Обязательные разделы","medium","Откройте разделы, для которых ещё нет сохранённой страницы."),
 "external":("factcheck","publication","Подтверждение события внешними источниками","medium","Сверьте публикации независимых издателей."),
 "match":("events","publication","Связь мероприятия с новостью","low","Сверьте даты, участников и обстоятельства в обеих публикациях."),
 "dates":("news","publication","Даты в публикациях","medium","Сверьте даты события и размещения в конкретной публикации."),
 "template":("news","publication","Шаблонное содержимое публикации","medium","Замените подтверждённый шаблон материалом конкретной публикации."),
 "print":("layout","layout","Читаемость сохранённого PDF","low","Сравните содержимое PDF с экранной версией и проверьте стили печати."),
 "style":("layout","layout","Единообразие оформления","low","Сверьте оформление однотипных элементов на странице."),
 "advisory":("home","photos","Справочное наблюдение по фотографии","low","При необходимости сверьте исходное изображение вручную."),
 "staff_match":("staff","content","Совпадение ФИО в учреждениях","low","Уточните кадровые сведения. Совпадение ФИО само по себе не устанавливает личность."),
}

def _acquisition(key):
    if key in {"address","phone","email","hours","reception"}:
        return "Сохраните шапку главной и страницу контактов. Сверьте поле «"+SPECS[key][2]+"» в тексте."
    actions={
        "news_gap":"Сохраните последовательные страницы новостной ленты с датами и переходами между страницами.",
        "news_delay":"Сохраните полную новость с датой события и карточку или метаданные с датой публикации.",
        "document_links":"Сохраните каталог документов со ссылками на файлы, включая следующие страницы списка.",
        "document_content":"Загрузите оригинал документа по ссылке из каталога вместе с его названием и описанием.",
        "staff_identity":"Откройте каталог и персональные профили сотрудников с ФИО и должностью.",
        "staff_contacts":"Откройте персональные профили и сверьте ФИО со служебным телефоном или почтой.",
        "staff_education":"Сохраните профиль с ФИО и сведениями об основном образовании или дипломе.",
        "staff_photo":"Загрузите оригинал фотографии и снимок соответствующей карточки сотрудника.",
        "building":"Сохраните главный экран с подписями и оригинал предполагаемой фотографии здания.",
        "main_photo_size":"Скачайте исходный файл главной фотографии для измерения его ширины и высоты.",
        "description":"Сохраните текст блока описания школы на главной и проверьте его границы.",
        "history":"Догрузите разделы «О школе» и «История» с полным текстом исторической справки.",
        "management":"Сохраните раздел об органах управления, их полномочиях и опубликованных контактах.",
        "schedule":"Откройте раздел расписания, выберите класс и сохраните строки занятий или оригинал файла.",
        "quarter_current":"Догрузите список мероприятий текущего квартала и карточки с полными датами.",
        "quarter_next":"Догрузите план следующего квартала и отдельные карточки с полными датами.",
        "canteen":"Сохраните блок питания, исходное фото столовой и подпись к нему.",
        "programs":"Откройте раздел образования и загрузите программу с её названием и содержанием.",
        "staff_resources":"Сохраните раздел для педагогов и доступные по ссылкам материалы.",
        "banner":"Сохраните снимок страницы с загруженными стилями и расположением баннера относительно меню.",
        "dates":"Сохраните карточку и полный текст публикации с датами размещения и самого события.",
        "template":"Сохраните полный текст, подпись и исходные изображения спорной публикации.",
        "external":"Сохраните статьи разных разрешённых издателей с датой события и явно названными участниками.",
        "match":"Сохраните полную карточку мероприятия и новость с датой и участниками.",
        "print":"Сохраните полный снимок экрана и многостраничный PDF одной версии страницы.",
        "style":"Сохраните экранную версию страницы с загруженными стилями и однотипными элементами.",
    }
    if key in actions:
        return actions[key]
    return EXTRA[key][4]


def _json(value,default=None):
    return json.loads(value) if value else default


def _iso(timestamp):
    return datetime.fromtimestamp(timestamp,ZONE).isoformat(timespec="seconds") if timestamp else None


def _counts(rows):
    c=Counter(r["status"] for r in rows)
    return {k:c[k] for k in STATUS}


def _url(value):
    try:
        return value if value and urlsplit(value).scheme in {"http","https"} and urlsplit(value).hostname else None
    except ValueError:
        return None


def _filters(raw):
    raw=raw or {}
    if not isinstance(raw,dict):
        raise ValueError("Фильтры должны быть именованными параметрами.")
    filters={}
    alias={"query":"q","search":"q","org":"organization_id","org_id":"organization_id","organization_ids":"organization_id",
           "type":"kind","view":"group","dateFrom":"date_from","dateTo":"date_to"}
    for key,value in raw.items():
        key=alias.get(key,key)
        if value is None or value=="" or key not in {*LABELS,"organization_id","q","date_from","date_to"}:
            continue
        values=value if isinstance(value,(tuple,list,set)) else [value]
        if key in {"q","date_from","date_to"}:
            text=str(next(iter(values),"")).strip()
            if not text:
                continue
            if key.startswith("date_"):
                try:
                    text=date.fromisoformat(text).isoformat()
                except ValueError as exc:
                    raise ValueError("Для фильтра даты требуется ГГГГ-ММ-ДД.") from exc
            filters[key]=text[:250]
        else:
            parts=[s.strip() for v in values for s in str(v).split(",") if s.strip() and s.strip() not in {"all","*"}]
            if key=="organization_id":
                if any(not s.isascii() or not s.isdigit() or not 0<int(s)<2**63 for s in parts):
                    raise ValueError("Выберите числовой ID учреждения.")
                parts=[int(s) for s in parts]
            elif any(s not in LABELS[key] for s in parts):
                raise ValueError("Неизвестное значение фильтра "+key+".")
            if parts:
                filters[key]=list(dict.fromkeys(parts))
    if filters.get("date_from","")>filters.get("date_to","9999"):
        raise ValueError("Начальная дата позже конечной.")
    return filters


def _quality(score,coverage,decided):
    if score is None or coverage<50 or decided<3:
        return "unrated"
    return "excellent" if score>=90 else "good" if score>=75 else "fair" if score>=55 else "poor"


def _grade(rows,criteria):
    decisions=defaultdict(set)
    for r in rows:
        if r["scored"] and r["criterion"] in criteria:
            decisions[r["criterion"]].add(r["status"])
    passed=sum("issue" not in outcomes and "pass" in outcomes for outcomes in decisions.values())
    decided=len(decisions)
    return {"score":round(100*passed/decided,1) if decided else None,
            "coverage":round(100*decided/len(criteria),1) if criteria else 0,
            "verified_criteria":decided,"total_criteria":len(criteria),"passed_criteria":passed}


def _model_key(key,check):
    # Negative-polarity questions describe defects. Absence of a classification or an external article is no site defect.
    diagnostic=(key.startswith("advisory_") or check.get("visual",{}).get("rule_scope")=="advisory")
    if diagnostic:
        return "advisory","informational"
    for prefix,criterion,polarity in (
        ("publication_external_","external","informational"),("publication_internal_match_","match","informational"),
        ("publication_filler","template","negative"),("publication_stock_image","template","negative"),
        ("publication_time_evidence","dates","positive"),("event_date_in_text","dates","positive"),
        ("news_date_card_","dates","positive"),("pdf_missing_screen_content","print","negative"),
        ("layout_peer_difference","style","negative"),("home_building_caption_conflict","building","negative"),
        ("feedback_banner_overlap","banner","negative"),("screen_layout","banner","negative"),
        ("feedback_banner","banner","informational"),("staff_placeholder_","staff_photo","negative"),
        ("staff_portrait","staff_photo","positive"),("staff_head_in_frame_","staff_photo","positive"),
        ("staff_sharp_","staff_photo","positive"),("staff_series_","staff_photo","positive"),
        ("staff_named_education","staff_education","positive"),("staff_named_contact","staff_contacts","positive"),
        ("staff_resources_substantive","staff_resources","positive"),("garden_program_details","programs","positive"),
        ("school_history_facts","history","positive"),("management_structure_details","management","positive"),
        ("school_description","description","positive"),("description_length","description","positive"),
        ("schedule_body_lessons","schedule","positive"),("schedule_visible_lessons","schedule","positive"),
        ("main_photo_original_size","main_photo_size","positive"),("garden_team_cards","garden_team","informational"),
        ("garden_staff_list_link","garden_team","informational"),("staff_identity","staff_identity","positive"),
        ("staff_education","staff_education","positive"),("contact_institution_hours","hours","positive"),
        ("event_template_title","template","negative"),("events_quarter_current","quarter_current","positive"),
        ("events_quarter_next","quarter_next","positive"),
        ("attachment_schedule_","schedule","positive"),("attachment_","document_content","positive"),
        ("event_plan_current","quarter_current","positive"),("event_plan_next","quarter_next","positive"),
        ("canteen_","canteen","positive"),("garden_building_photo","building","positive"),
        ("school_main_","building","positive"),("contact_reception_hours","reception","positive"),
        ("garden_hours","hours","positive"),("garden_address","address","positive"),
        ("garden_phone","phone","positive"),("garden_email","email","positive")):
        if key.startswith(prefix):
            if check.get("visual",{}).get("include_in_score") is False and polarity=="positive":
                polarity="informational"
            return criterion,polarity
    return "navigation","informational"


def _collect(conn,filters):
    def in_period(ts):
        if not (filters.get("date_from") or filters.get("date_to")):
            return True
        stamp=(_iso(ts) or "")[:10]
        return bool(stamp and filters.get("date_from","")<=stamp<=filters.get("date_to","9999"))
    orgs={r[0]:dict(id=r[0],name=r[1],kind=r[2],site_url=r[3]) for r in
          conn.execute("SELECT id,name,kind,site_url FROM organization ORDER BY id")}
    pages={p:(oid,url,ts) for p,oid,url,ts in conn.execute("SELECT id,id_organization,url,date_loaded FROM page")}
    runs={r[0]:dict(run_id=r[0],organization_id=r[1],date=_iso(r[2]),finished=_iso(r[3]),state=r[4],purpose=r[5],
                   stop_reason=r[6],pages=r[7],started_at=r[2]) for r in conn.execute("""
          SELECT r.id,r.id_organization,r.started_at,r.finished_at,r.state,r.purpose,r.stop_reason,count(f.page_id)
          FROM crawl_run r LEFT JOIN crawl_fetch f ON f.run_id=r.id GROUP BY r.id ORDER BY r.started_at DESC,r.id DESC""")}
    findings={}
    def put(oid,criterion,status,description,*,pid=None,url=None,stamp=None,evidence="",source="Проверка архива",
            confidence=None,title=None,identity=None,scored=True,source_id=None,materials=None,detector="algorithm"):
        if oid not in orgs:
            return
        org=orgs[oid]
        meta=SPECS.get(criterion) or EXTRA[criterion]
        category,group,name,severity,remedy=meta[:5]
        page=pages.get(pid)
        stamp=stamp if stamp is not None else (page[2] if page else None)
        if not in_period(stamp):
            return
        key=(oid,criterion,str(identity if identity is not None else "summary"))
        previous=findings.get(key)
        if previous and (previous["_ts"],previous["page_id"] or 0)>(stamp or 0,pid or 0):
            return
        grade=scored and criterion in SPECS and SPECS[criterion][-1] in ("*",org["kind"]) and status in {"pass","issue"}
        token=key[2] if re.fullmatch(r"[a-zA-Z0-9_-]{1,50}",key[2]) else sha256(key[2].encode()).hexdigest()[:16]
        findings[key]={"id":f"{oid}-{criterion}-{token}","criterion":criterion,"organization_id":oid,
            "organization_name":org["name"],"kind":org["kind"],"category":category,"group":group,
            "title":title or name,"description":description,"evidence":evidence or description,
            "status":status,"severity":severity,"confidence":confidence or {"pass":"high","issue":"high","review":"low",
            "info":"medium","unverified":"unknown"}[status],"priority":{"high":1,"medium":2,"low":3}[severity],
            "recommendation":remedy if status=="issue" else _acquisition(criterion) if status in {"review","unverified"} else "",
            "page_id":pid,"url":_url(url or (page[1] if page else org["site_url"])),"checked_at":_iso(stamp),
            "detector":detector,"source":source,"source_id":source_id,"materials":materials or [],"scored":bool(grade),"_ts":stamp or 0}

    def page_evidence(pid,criterion,status,description,**kwargs):
        if pid in pages:
            put(pages[pid][0],criterion,status,description,pid=pid,**kwargs)

    latest_audit={}
    for r in runs.values():
        if r["purpose"]=="audit" and r["finished"] and in_period(r["started_at"]):
            latest_audit.setdefault(r["organization_id"],r["run_id"])
    for run,pid,raw in conn.execute("SELECT run_id,home_page_id,evidence FROM institution_audit"):
        audit=_json(raw,{})
        oid=audit.get("organization_id")
        if run!=latest_audit.get(oid):
            continue
        cover=audit.get("coverage",{})
        have=set(cover.get("visited_sections",[]))
        want=set(cover.get("expected_sections",[]))
        if want:
            put(oid,"navigation","info",f"В основном обходе сохранено разделов: {len(have & want)} из {len(want)}.",
                pid=pid,scored=False,source="Основной обход",source_id=run,
                evidence=f"Обход № {run}. Дополнительные загрузки отражены в материалах соответствующих разделов.")

    # Prefer the latest positive, identified institutional contact from the contact section or home.
    for pid,sec,raw in conn.execute("SELECT page_id,section,observations FROM institution_contact_page"):
        if sec not in {"contacts","home","information"}:
            continue
        for obs in _json(raw,[]):
            field=obs.get("field")
            if field in {"address","phone","email","hours","reception"} and obs.get("value"):
                page_evidence(pid,field,"pass","Опубликовано в контактах учреждения.",evidence=str(obs["value"])[:450],
                              source="Текст и разметка страницы")
    for pid,field,status,raw in conn.execute("SELECT page_id,field,status,evidence FROM garden_home_contact"):
        if field not in {"address","phone","email","hours"} or status!="found":
            continue
        values=[str(x.get("value","")) for x in _json(raw,{}).get("values",[]) if x.get("value")]
        if values:
            page_evidence(pid,field,"pass","Контакт найден на главной странице детского сада.",evidence=" · ".join(values)[:450])

    for pid,status,example,length in conn.execute("SELECT page_id,status,text,char_count FROM home_school_description"):
        if status=="found" and example and isinstance(length,int):
            ok=300<=length<=400
            page_evidence(pid,"description","pass" if ok else "issue",
                f"В выделенном описании школы {length} символов. Диапазон памятки — 300–400.",
                evidence=example[:320],source="Подсчёт символов в блоке описания")

    for pid,url,stamp,sha,width,height in conn.execute("""SELECT p.page_id,d.final_url,d.fetched_at,d.sha256,a.width,a.height
            FROM home_main_photo p JOIN available_home_image_download d USING(page_id,image_url)
            JOIN home_image_asset a ON a.sha256=d.sha256 WHERE p.status='found' AND d.status='saved'"""):
        page_evidence(pid,"main_photo_size","pass" if (width,height)==(1200,900) else "issue",
            f"Размер оригинала {width} × {height} пикселей. В кейсе указано 1200 × 900.",
            url=url,stamp=stamp,source="Измерение исходного изображения",
            materials=[{"kind":"image","id":sha,"url":_url(url)}])

    for pid,count in conn.execute("""SELECT page_id,card_count FROM garden_home_section
            WHERE section='team' AND block_status='cards' AND card_count>0"""):
        page_evidence(pid,"garden_team","info",f"На главной странице обнаружены персональные карточки ({count}).",
                      scored=False,source="Разметка блока коллектива")

    for pid,status,raw in conn.execute("SELECT page_id,status,evidence FROM school_schedule_page"):
        data=_json(raw,{})
        if status=="lesson_rows":
            page_evidence(pid,"schedule","pass","На странице обнаружены строки занятий со временем.",
                          evidence=str((data.get("lesson_examples") or ["Строки расписания"])[0])[:400])
        elif status=="explicit_empty":
            page_evidence(pid,"schedule","issue","На странице расписания опубликовано сообщение об отсутствии данных.",
                          evidence=data.get("empty_notice") or "Раздел показывает пустое расписание.",
                          identity=pages.get(pid,(None,""))[1])
        elif status in {"material_candidate","external_diary","class_selection_required"}:
            page_evidence(pid,"schedule","review","Найдена ссылка или форма выбора. Содержание занятий требует сверки.",
                          source="Раздел расписания",identity="candidate")

    for pid,raw in conn.execute("SELECT page_id,evidence FROM institution_document_page WHERE section='documents'"):
        docs=_json(raw,{}).get("documents",[])
        if docs:
            page_evidence(pid,"document_links","pass",f"На странице каталога найдено ссылок на документы: {len(docs)}.",
                          evidence=" · ".join(str(x.get("title") or x.get("url","")) for x in docs[:3])[:400])

    for run,pid,nxt,days,status,raw in conn.execute("SELECT run_id,page_id,next_page_id,days,status,evidence FROM timeline_gap"):
        if status not in {"over_14d","within_14d"} or days is None:
            continue
        data=_json(raw,{})
        a,b=data.get("from",{}),data.get("to",{})
        sample=status=="within_14d"
        page_evidence(pid,"news_gap","pass" if sample else "issue",
              f"Между двумя последовательными публикациями прошло {days} дн. По кейсу допустимо до 14.",
              evidence=" · ".join(" ".join(str(x.get(k) or "") for k in ("date_iso","title")) for x in (a,b)),
              identity="within" if sample else (str(a.get("url"))+"|"+str(b.get("url"))),
              source="Даты соседних публикаций",source_id=run)
    for run,url,pid,title,status,raw in conn.execute("""SELECT run_id,url,page_id,title,status,evidence FROM timeline_publication
              WHERE status IN ('over_24h','within_24h','published_before_event')"""):
        data=_json(raw,{})
        sources=data.get("published_sources",[])+data.get("event_sources",[])
        if not pid:
            pid=next((s.get("page_id") for s in sources if s.get("page_id") in pages),None)
        if not pid:
            continue
        outcome={"over_24h":"issue","within_24h":"pass","published_before_event":"review"}[status]
        message={"over_24h":"Публикация запоздала более чем на 24 часа.",
                 "within_24h":"Публикация уложилась в 24 часа.","published_before_event":"Указанная дата размещения предшествует событию."}[status]
        excerpts=list(dict.fromkeys(str(s.get("raw","")) for s in sources if s.get("raw")))
        if data.get("reason")=="over_24h_for_every_possible_past_year":
            message+=" Даже при самом позднем допустимом годе интервал превышает сутки."
        page_evidence(pid,"news_delay",outcome,message,evidence=" · ".join([title,*excerpts])[:450],url=url,
                      identity=url,source="Сопоставление дат события и размещения",source_id=run)

    for run,period,start,end,status,n,raw in conn.execute("SELECT * FROM school_event_quarter WHERE status='dated_candidates'"):
        quarter=_json(raw,{})
        pid=next(iter(quarter.get("listing_page_ids",[])),None)
        if pid in pages and period in {"current","next"}:
            page_evidence(pid,"quarter_"+period,"review",
                f"Найдены датированные кандидаты ({n}) за период {start} — {end}. Содержание плана требует сверки.",
                evidence=" · ".join(str(x.get("title","")) for x in quarter.get("dated_candidates",[])[:3])[:420],
                source="Календарные поля карточек",source_id=run)

    # A contact or professional qualification is attached to a named card. Courses alone cannot establish a degree.
    personnel=defaultdict(dict)
    for pid,name,role,profile,email,phone,education in conn.execute("""SELECT page_id,full_name,role,profile_url,email,phone,education
             FROM staff_observation WHERE full_name IS NOT NULL ORDER BY page_id"""):
        if pid not in pages or not in_period(pages[pid][2]) or not (name and name.strip()):
            continue
        person=personnel[pages[pid][0]].setdefault((name.casefold().strip(),(role or "").casefold().strip()),{})
        person["pid"]=pid
        for key,value in (("staff_identity",role),("staff_contacts",email or phone),
                          ("staff_education",education and re.search(r"\b(?:высшее|бакалавр\w*|магистр\w*|среднее профессиональное)\b",education,re.I))):
            if value:
                person[key]=pid
    for oid,persons in personnel.items():
        for key in ("staff_identity","staff_contacts","staff_education"):
            hits=[p[key] for p in persons.values() if key in p]
            if not persons:
                continue
            pid=max(hits or [p["pid"] for p in persons.values()])
            state="pass" if len(hits)==len(persons) else "review"
            put(oid,key,state,f"Признак найден у {len(hits)} из {len(persons)} групп карточек с указанным ФИО.",
                pid=pid,source="Сохранённые карточки сотрудников",
                evidence="Повторы с одинаковыми ФИО и должностью объединены для подсчёта. "
                         "Незаполненное поле в списке может быть опубликовано в индивидуальном профиле.")

    for name,oid,pid,n in conn.execute("""SELECT s.full_name,m.organization_id,s.page_id,m.organization_count
            FROM staff_cross_org_match m JOIN staff_observation s ON s.id=m.representative_observation_id"""):
        put(oid,"staff_match","review",f"ФИО «{name}» найдено у {n} учреждений.",pid=pid,
            identity=name,scored=False,source="Сравнение имён в карточках")

    _model_findings(conn,put,pages)
    return orgs,findings,runs,latest_audit


def _model_findings(conn,put,pages):
    try:
        from .factcheck_sources import allowlist_fingerprint
    except ImportError:
        from factcheck_sources import allowlist_fingerprint
    policy=allowlist_fingerprint(conn)
    for aid,pid,key,ts,outcome,model,request,response,error in conn.execute("""SELECT id,page_id,check_id,created_at,outcome,
              model,request,response,error FROM model_assessment ORDER BY created_at,id"""):
        if pid not in pages:
            continue
        parsed=_json(request,{})
        check=next((c for c in parsed.get("checks",[]) if c["id"]==key),{})
        criterion,polarity=_model_key(key,check)
        reply=(_json(response,{}) or {}).get("results",[])
        evidence=reply[0].get("evidence","") if reply else ""
        if outcome=="error":
            state="unverified"
        elif outcome=="uncertain":
            state="review"
        elif polarity=="informational" or outcome=="not_applicable":
            state="info"
        else:
            state="pass" if (outcome=="present")== (polarity=="positive") else "issue"
        pub=check.get("publication",{})
        if pub.get("scope") in {"participation","event"} and pub.get("allowlist_fingerprint")!=policy:
            state="review"
            evidence="Список допустимых источников изменился после проверки. "+evidence
        meta=SPECS.get(criterion) or EXTRA[criterion]
        title=check.get("visual",{}).get("label") or meta[2]
        if key.startswith("attachment_"):
            title="Расписание во вложении" if criterion=="schedule" else "Содержание вложения"
        if pub.get("scope")=="participation":
            title="Подтверждение участия учреждения"
        if key.startswith("feedback_banner_overlap"):
            title="Баннер перекрывает меню"
        elif key=="feedback_banner":
            title="Присутствие баннера обратной связи"
        if state=="unverified":
            description="Недостаточно данных. Проверку модели не удалось завершить."
            evidence="Проверка № "+str(aid)+". Повторите её при доступном сервере модели."
        elif outcome=="not_applicable":
            description="Условие проверки не применимо к этому снимку."
        elif state=="review":
            description="Недостаточно данных для вывода. По сохранённым материалам требуется сверка."
        else:
            description=evidence or title
        used=set(reply[0].get("material_ids",[])) if reply else set()
        refs=[]
        for m in parsed.get("materials",[]):
            if m.get("id") in used:
                refs.append({"id":m["id"],"kind":m.get("kind"),"page":m.get("pdf_page"),"url":_url(m.get("source_url") or m.get("url")),
                             "publisher":m.get("publisher"),"observation_id":m.get("observation_id")})
        # Optional observations about flags, clothing and image classification preserve their original scope.
        allow=polarity!="informational" and check.get("visual",{}).get("include_in_score",True)
        ident="model:"+pages[pid][1]+":"+key
        put(pages[pid][0],criterion,state,description,pid=pid,stamp=ts,title=title,evidence=evidence[:800],identity=ident,
            detector="ai",source="ИИ · "+model,source_id=aid,materials=refs,scored=allow,confidence="medium" if state in {"issue","pass","info"} else (
            "unknown" if state=="unverified" else "low"))


def _key(text):
    return re.sub(r"[^а-яa-z0-9]+"," ",(text or "").casefold().replace("ё","е")).strip()


def _matches(org,text):
    text=_key(text)
    return all(term in text for term in org.split())


def _build(conn,filters,detail_id=None):
    filters=_filters(filters)
    orgs,mapping,runs,latest=_collect(conn,filters)
    if detail_id is not None and detail_id not in orgs:
        raise KeyError(detail_id)
    byorg=defaultdict(list)
    for row in mapping.values():
        byorg[row["organization_id"]].append(row)
    run_byorg=defaultdict(list)
    for r in runs.values():
        run_byorg[r["organization_id"]].append(r)
    options={key:[{"value":v,"label":label} for v,label in labels.items()] for key,labels in LABELS.items()}
    options["organization_id"]=[{"value":oid,"label":o["name"]} for oid,o in orgs.items()]
    included=[]
    rank_candidates=[]
    visible=[]
    all_per_org={}
    query=_key(filters.get("q"))
    choices={k:set(v) for k,v in filters.items() if isinstance(v,list)}
    issue_filter=any(k in choices for k in ("status","confidence","severity","group","category","detector"))
    def selected(key,value):
        return key not in choices or value in choices[key]
    for oid,org in orgs.items():
        applicable={k for k,s in SPECS.items() if s[-1] in ("*",org["kind"])}
        rows=byorg[oid]
        seen={r["criterion"] for r in rows if r["status"]!="info"}
        for key in sorted(applicable-seen):
            cat,group,title,severity,remedy,_=SPECS[key]
            rows.append({"id":f"{oid}-{key}-unverified","criterion":key,"organization_id":oid,"organization_name":org["name"],
                "kind":org["kind"],"category":cat,"group":group,"title":title,"description":"Недостаточно данных. Для этого критерия нет проверенного результата в выбранном периоде.",
                "evidence":"Материалы для вывода не подтверждены.","status":"unverified","severity":severity,"confidence":"unknown",
                "priority":{"high":1,"medium":2,"low":3}[severity],"recommendation":_acquisition(key),
                "url":_url(org["site_url"]),"page_id":None,"checked_at":None,"detector":"none","source":"План проверок","source_id":None,
                "materials":[],"scored":False,"_ts":0})
        grade=_grade(rows,applicable)
        quality=_quality(grade["score"],grade["coverage"],grade["verified_criteria"])
        if quality!="unrated":
            rank_candidates.append((grade["score"],grade["coverage"],oid))
        if detail_id is not None and oid!=detail_id or not selected("organization_id",oid) or not selected("kind",org["kind"]):
            continue
        if not selected("quality",quality):
            continue
        org_hit=not query or query==str(oid) or _matches(query,org["name"]+" "+org["site_url"])
        selection=[r for r in rows if all(selected(k,r[k]) for k in ("status","confidence","severity","group","category","detector"))
                   and (org_hit or _matches(query," ".join((r["title"],r["description"],r["evidence"],r["recommendation"],
                                                         CATEGORIES[r["category"]]))))]
        if (issue_filter or not org_hit) and not selection and detail_id is None:
            continue
        all_per_org[oid]=rows
        counts=_counts(rows)
        last=max((r["_ts"] for r in rows),default=0)
        selected_runs=[r for r in run_byorg[oid] if r["date"] and
                       filters.get("date_from","")<=r["date"][:10]<=filters.get("date_to","9999")]
        if selected_runs:
            last=max(last,selected_runs[0]["started_at"])
        included.append(dict(org,**grade,quality=quality,counts=counts,last_checked=_iso(last),rank=None,
                        matching_findings=len(selection),runs=len(selected_runs),last_run_id=selected_runs[0]["run_id"] if selected_runs else None))
        visible.extend(selection)
    included.sort(key=lambda r:(r["quality"]=="unrated",-(r["score"] if r["score"] is not None else -1),-r["coverage"],r["name"].casefold(),r["id"]))
    ranks={}
    for n,(score,coverage,oid) in enumerate(sorted(rank_candidates,reverse=True),1):
        ranks.setdefault((score,coverage),n)
    for org in included:
        if org["quality"]!="unrated":
            org["rank"]=ranks[(org["score"],org["coverage"])]
    status_order={"issue":0,"review":1,"unverified":2,"pass":3,"info":4}
    visible.sort(key=lambda r:(status_order[r["status"]],r["priority"],-r["_ts"],r["organization_id"],r["title"]))
    scored=[r["score"] for r in included if r["score"] is not None]
    total_criteria=sum(r["total_criteria"] for r in included)
    summary={"total":len(included),"schools":sum(o["kind"]=="school" for o in included),
             "kindergartens":sum(o["kind"]=="kindergarten" for o in included),"findings":len(visible),
             "counts":_counts(visible),"average_score":round(sum(scored)/len(scored),1) if scored else None,
             "coverage":round(100*sum(r["verified_criteria"] for r in included)/total_criteria,1) if total_criteria else 0,
             "quality_counts":{q:sum(1 for o in included if o["quality"]==q) for q in QUALITY}}
    result={"as_of":datetime.now(ZONE).isoformat(timespec="seconds"),"filters":filters,"options":options,"grading":dict(GRADING),
            "summary":summary,"institutions":included,"findings":[{k:v for k,v in r.items() if k!="_ts"} for r in visible]}
    if detail_id is not None:
        rows=all_per_org.get(detail_id,[])
        result["institution"]=included[0] if included else None
        result["categories"]=[]
        for cat,label in CATEGORIES.items():
            subset=[r for r in visible if r["category"]==cat]
            if not subset:
                continue
            applicable={k for k,s in SPECS.items() if s[0]==cat and s[-1] in ("*",orgs[detail_id]["kind"])}
            group=[r for r in rows if r["category"]==cat]
            result["categories"].append(dict(id=cat,label=label,counts=_counts(subset),**_grade(group,applicable)))
        result["history"]=[{k:v for k,v in r.items() if k!="started_at"} for r in run_byorg[detail_id]
                           if r["date"] and filters.get("date_from","")<=r["date"][:10]<=filters.get("date_to","9999")]
    return result


def dashboard(conn, filters=None):
    return _build(conn,filters)


def institution(conn, organization_id, filters=None):
    try:
        oid=int(organization_id)
    except (TypeError,ValueError) as exc:
        raise ValueError("Некорректный ID учреждения.") from exc
    return _build(conn,filters,detail_id=oid)
