"""Collect evidence from a saved schedule page. File or image content requires a separate review."""

import re
from collections import Counter
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

if __package__:
    from .home_content import _selector
    from .institution_contacts import clean
    from .navigation import DOCUMENT_EXTENSIONS, resolve_url
else:
    from home_content import _selector
    from institution_contacts import clean
    from navigation import DOCUMENT_EXTENSIONS, resolve_url

TERM = re.compile(r"расписани|raspisan|timetable|schedule",re.I)
EXTRA = re.compile(r"внеуроч|факультатив|кружк|секци|звонк|экзамен|график консультаци|дополнительн",re.I)
DECORATION = re.compile(r"banner|баннер|logo|иконк|эмблем|/icon/|/icons/|/netcat_template/|\.svg(?:$|\?)",re.I)
NOT_TIMETABLE = re.compile(r"памятк|реклам|фотографи[ия]\s+директор|согласи[ея]|политик[аи]\s+конфиденц|горяч\w*\s+лини",re.I)
EMPTY = re.compile(r"расписание[^.!?\n]{0,55}?не заполнено|расписание отсутствует|расписание пока не опубликовано",re.I)
TIME = re.compile(r"\b(?:[01]?\d|2[0-3])[:.]\d{2}\b")
SUBJECT = re.compile(r"(?:математик|язык|чтение|геометр|литератур|истори|географ|физик|хими|биолог|физкультур|обществознан|информатик|разговоры о важном|изо\b|окружающий мир|технологи)",re.I)
IMAGE_SUFFIXES = {".jpg",".jpeg",".png",".webp",".tiff",".tif",".avif",".bmp",".gif"}
DOCUMENT_SUFFIXES = DOCUMENT_EXTENSIONS | {".odt",".ods",".rtf"}


def _excluded(node):
    return any(p.name in {"header","footer","nav","aside","script","style","template","noscript"}
               or p.has_attr("hidden") or p.get("aria-hidden")=="true"
               or re.search(r"breadcrumb|banner|pos-widget|gw-footer|social|share-button|gw-calendar|datepicker",
                            " ".join([p.get("id",""),*p.get("class",())]),re.I) for p in (node,*node.parents))


def _caption(node):
    caption=node.find_parent(class_="document-object-item") or node.find_parent("figure")
    if caption:
        title=caption.select_one(".document-caption,figcaption")
        if title:
            return clean(title)
    for p in (node,*node.parents):
        if p.name in {"main","body","html","[document]"}:
            break
        sib=p.find_previous_sibling()
        if sib:
            text=clean(sib)
            if text and len(text)<=230:
                return text
    return ""


def extract_schedule(soup, url):
    if not isinstance(soup,BeautifulSoup):
        soup=BeautifulSoup(soup,"html.parser")
    ids=Counter(e["id"] for e in soup.find_all(id=True))
    main=soup.select_one("main,[role=main]") or soup.body or soup
    h1=clean(main.select_one("h1"))
    text=clean(main)
    resources={}
    for node in main.select("a[href],img[src],img[data-src],iframe[src],embed[src],object[data]"):
        if _excluded(node):
            continue
        attr=("href" if node.name=="a" else "data" if node.name=="object" else
              "data-src" if node.get("data-src") else "src")
        raw=node.get(attr,"")
        target=resolve_url(url,raw) if raw and not raw.startswith("#") else None
        if not target:
            continue
        path=unquote(urlsplit(target).path)
        suffix=PurePosixPath(path).suffix.lower()
        label=clean(node) or clean(node.get("alt")) or clean(node.get("title"))
        caption=_caption(node)
        context=" ".join((label,caption))
        if DECORATION.search(path+" "+label) or NOT_TIMETABLE.search(path+" "+label):
            continue
        if suffix in DOCUMENT_SUFFIXES or node.get("type","").casefold()=="application/pdf" or node.has_attr("download"):
            kind="file"
        elif suffix in IMAGE_SUFFIXES or node.name=="img":
            if not (TERM.search(path+" "+context) or
                    (TERM.search(h1) and ("/netcat_files/" in path or node.find_parent("figure")))):
                continue
            kind="image"
        elif node.name in {"iframe","embed","object"} and TERM.search(path+" "+context):
            kind="embedded"
        else:
            continue
        if NOT_TIMETABLE.search(caption) and not TERM.search(label+" "+path):
            continue
        if target not in resources:
            resources[target]={"url":target,"kind":kind,"label":label,"caption":caption,
                               "selector":_selector(node,ids),"scope":"supplementary" if EXTRA.search(context+" "+path)
                               else "unspecified","content_status":"requires_review"}
    rows=[]
    for lesson in main.select(".gw-schedule-lesson,.schedule-lesson,[itemtype$='/EducationEvent']"):
        if _excluded(lesson):
            continue
        phrase=clean(lesson)
        if not re.search(r"[а-яёa-z]{3}",phrase,re.I) or not TIME.search(phrase):
            continue
        if len(rows)<20:
            rows.append({"selector":_selector(lesson,ids),"text":phrase[:250]})
    for table in main.select("table"):
        if _excluded(table):
            continue
        phrase=clean(table)
        if ((TIME.search(phrase) or re.search(r"понедельник|вторник|среда|четверг|пятница",phrase,re.I))
                and SUBJECT.search(phrase) and len(rows)<20):
            rows.append({"selector":_selector(table,ids),"text":phrase[:400]})
    empty=EMPTY.search(text)
    controls=main.select_one("select[gw-schedule-select],.gw-schedule select,select[name='class']")
    diary=re.search(r"[^.!?]{0,50}расписание[^.!?]{0,120}электронн\w+ дневник\w*",text,re.I)
    if rows:
        status="lesson_rows"
    elif resources:
        status="material_candidate"
    elif diary:
        status="external_diary"
    elif empty:
        status="explicit_empty"
    elif controls:
        status="class_selection_required"
    else:
        status="no_material_detected"
    return {"status":status,"heading":h1,"materials":list(resources.values()),"lesson_examples":rows,
            "empty_notice":empty[0] if empty else None,"diary_notice":diary[0] if diary else None,
            "has_class_selector":bool(controls),
            "model_question":"Содержит ли материал расписание уроков этого учреждения? Укажите классы, период и основание.",
            "content_verdict":"pending_review" if resources or rows else "unverified"}
