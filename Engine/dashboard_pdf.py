"""Generate a printable report from the same filtered, local data as the dashboard."""

from collections import defaultdict
from html import escape
from io import BytesIO
import os
from pathlib import Path
import threading
from urllib.parse import unquote, urlsplit

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import CondPageBreak, KeepTogether, LongTable, Paragraph, SimpleDocTemplate, TableStyle

if __package__:
    from .dashboard_data import CATEGORIES, CONFIDENCE, KINDS, LABELS, QUALITY, SEVERITY, STATUS, _filters, dashboard
else:
    from dashboard_data import CATEGORIES, CONFIDENCE, KINDS, LABELS, QUALITY, SEVERITY, STATUS, _filters, dashboard


_FONT_LOCK=threading.Lock()
_REGULAR="AuditBody"
_BOLD="AuditHeading"
INK="#122c36"
TEAL="#146758"
MUTED="#4a656c"
STATUS_COLORS={"issue":"#9b302a","review":"#815008","pass":"#226351","unverified":"#455661","info":"#425c81"}


def _fonts():
    with _FONT_LOCK:
        if _REGULAR in pdfmetrics.getRegisteredFontNames() and _BOLD in pdfmetrics.getRegisteredFontNames():
            return
        windows=Path(os.environ.get("WINDIR","C:/Windows"))/"Fonts"
        candidates=[(windows/"arial.ttf",windows/"arialbd.ttf"),
            (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
            (Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf")),
            (Path("/System/Library/Fonts/Supplemental/Arial.ttf"),Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"))]
        for regular,bold in candidates:
            if regular.is_file() and bold.is_file():
                pdfmetrics.registerFont(TTFont(_REGULAR,str(regular)))
                pdfmetrics.registerFont(TTFont(_BOLD,str(bold)))
                pdfmetrics.registerFontFamily(_REGULAR,normal=_REGULAR,bold=_BOLD)
                return
        raise RuntimeError("Для PDF нужен системный шрифт Arial, DejaVu Sans или Liberation Sans с кириллицей.")


def _text(value):
    # ReportLab parses a small XML subset; school text is never treated as report markup.
    return escape("".join(c for c in str(value or "") if c in "\t\n" or ord(c)>=32))


def _p(value,style):
    return Paragraph(_text(value).replace("\n","<br/>"),style)


def _styles():
    def style(name,size,leading,color=INK,*,bold=False,before=0,after=0):
        return ParagraphStyle(name,fontName=_BOLD if bold else _REGULAR,fontSize=size,leading=leading,
            textColor=colors.HexColor(color),spaceBefore=before,spaceAfter=after,splitLongWords=True)
    return {"hero":style("hero",23,28,bold=True,after=7),"h1":style("h1",16,20,bold=True,before=7,after=6),
            "h2":style("h2",11,14,bold=True,before=8,after=3),"lead":style("lead",10,14,after=4),
            "body":style("body",9,12,after=3),"small":style("small",8,11,MUTED,after=3),
            "tiny":style("tiny",7.5,10,MUTED),"table":style("table",8.2,10.8),
            "th":style("th",8.2,11,"#ffffff",bold=True),"meta":style("meta",8.7,12,TEAL,bold=True,after=4)}


def _number(value):
    if value is None:
        return "—"
    return f"{value:g}".replace(".",",")


def _date(text):
    return ".".join(reversed(text[:10].split("-"))) if text else "дата не зафиксирована"


def _source(row,style):
    source=[_text(row["source"])]
    if row.get("source_id"):
        source.append("№ "+str(row["source_id"]))
    if row.get("page_id"):
        source.append("снимок № "+str(row["page_id"]))
    if row.get("checked_at"):
        source.append(_date(row["checked_at"]))
    url=row.get("url")
    if url and urlsplit(url).scheme in {"https","http"}:
        parsed=urlsplit(url)
        visible=parsed.netloc+unquote(parsed.path)
        if len(visible)>98:
            visible=parsed.netloc+"/…/"+unquote(parsed.path).strip("/").split("/")[-1][:58]
        source.append(f'<link href="{_text(url)}" color="{TEAL}">{_text(visible)}</link>')
    for ref in row.get("materials",[]):
        if ref.get("page"):
            source.append("лист PDF "+str(ref["page"]))
        if ref.get("publisher") and ref.get("url") and urlsplit(ref["url"]).scheme in {"https","http"}:
            source.append(f'<link href="{_text(ref["url"])}" color="{TEAL}">{_text(ref["publisher"])}</link>')
    return Paragraph(" · ".join(dict.fromkeys(source)),style)


def _criterion(row,s):
    state=row["status"]
    label=STATUS[state]
    if state=="issue":
        label+=f" · приоритет {row['priority']} · критичность {SEVERITY[row['severity']].lower()}"
    heading=Paragraph(f'<font color="{STATUS_COLORS[state]}">{_text(label)}</font>  {_text(row["title"])}',s["h2"])
    parts=[heading,_p(row["description"],s["body"])]
    if row["evidence"] and row["evidence"]!=row["description"]:
        parts.append(_p(row["evidence"],s["small"]))
    if row.get("recommendation"):
        parts.append(_p("Действие. "+row["recommendation"],s["body"]))
    parts.append(_source(row,s["tiny"]))
    return KeepTogether(parts)


def _ranking(orgs,s):
    rows=[[_p(v,s["th"]) for v in ("Место","Учреждение","Балл","Охват","Класс качества")]]
    for org in orgs:
        rows.append([_p(str(org["rank"] or "—"),s["table"]),_p(org["name"],s["table"]),
                     _p(_number(org["score"]),s["table"]),_p(_number(org["coverage"])+"%",s["table"]),
                     _p(QUALITY[org["quality"]],s["table"])])
    table=LongTable(rows,colWidths=[35,257,43,47,106],repeatRows=1,hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor(TEAL)),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#f0f5f4")]),
        ("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),5),
        ("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
    return table


def _describe_filters(filters):
    titles={"kind":"Тип","organization_id":"Номера учреждений","category":"Раздел","group":"Группа",
            "criterion":"Тип замечания","detector":"Кто проверил","severity":"Критичность","quality":"Качество","confidence":"Уверенность","status":"Состояние",
            "date_from":"С даты","date_to":"По дату","q":"Поиск"}
    parts=[]
    for k,v in filters.items():
        values=v if isinstance(v,list) else [v]
        labels=LABELS.get(k,{})
        parts.append(titles.get(k,k)+" — "+", ".join(str(labels.get(x,x)) for x in values))
    return " · ".join(parts) or "Все учреждения и категории"


def build_pdf(conn, filters=None, organization_ids=None, *, include_quality=False):
    """Return PDF bytes without accessing any school, browser or external font service."""
    filters=_filters(filters)
    if organization_ids is not None:
        ids=_filters({"organization_id":organization_ids}).get("organization_id",[])
        if "organization_id" in filters:
            ids=[n for n in ids if n in filters["organization_id"]]
        if not ids:
            raise ValueError("В выбранной группе учреждений нет ID для экспорта.")
        filters["organization_id"]=ids
    data=dashboard(conn,filters)
    _fonts()
    s=_styles()
    target=BytesIO()
    doc=SimpleDocTemplate(target,pagesize=A4,leftMargin=48,rightMargin=48,topMargin=53,bottomMargin=49,
                          title="Аудит сайтов образовательных учреждений",author="Мониторинг учреждений")
    orgs=data["institutions"]
    summary=data["summary"]
    story=[_p("Аудит сайтов образовательных учреждений",s["hero"]),
        _p("Магаданская область · "+_date(data["as_of"])+" · местное время",s["meta"]),
        _p(_describe_filters(filters),s["small"])]
    if not orgs:
        story.append(_p("По выбранным фильтрам учреждения не найдены.",s["lead"]))
    else:
        story.extend([_p(f"Учреждений в выборке — {summary['total']}. Замечаний — {summary['counts']['issue']}. "
                         f"Требуют сверки — {summary['counts']['review']}. Охват критериев — {_number(summary['coverage'])}%.",s["lead"]),
                      _p("Как читать оценку",s["h2"]),_p(data["grading"]["method"]+" "+data["grading"]["coverage"],s["small"]),
                      _p(data["grading"]["classification"]+" "+data["grading"]["scope"],s["small"])])
        if include_quality:
            leaders = [org for org in orgs if org["quality"] in ("good", "excellent")]
            story.extend([_p("Хорошо и отлично в выбранной группе",s["h1"]),
                          _p("В таблицу вошли учреждения, соответствующие фильтрам отчёта. "
                             "Место и балл отражают общую оценку за выбранный период.",s["small"])])
            story.append(_ranking(leaders,s) if leaders else _p("В этой выборке таких учреждений нет.",s["body"]))
    byorg=defaultdict(list)
    for row in data["findings"]:
        byorg[row["organization_id"]].append(row)
    for org in orgs:
        story.extend([CondPageBreak(155),_p(org["name"],s["h1"]),
            _p(f"{KINDS[org['kind']]} · учреждение № {org['id']} · балл {_number(org['score'])} из 100 · "
               f"охват {_number(org['coverage'])}% ({org['verified_criteria']} из {org['total_criteria']})",s["meta"])])
        subset=byorg[org["id"]]
        for row in subset:
            if row["status"]!="unverified":
                story.append(_criterion(row,s))
        pending=[r for r in subset if r["status"]=="unverified"]
        if pending:
            story.extend([CondPageBreak(75),_p("Недостаточно данных",s["h2"]),
                          _p("Эти пункты исключены из расчёта балла. Для проверки потребуются следующие материалы.",s["small"])])
            for row in pending:
                story.append(Paragraph("<b>"+_text(row["title"])+"</b> · "+_text(row["recommendation"]),s["small"]))
    def frame(canvas,doc):
        w,h=A4
        canvas.saveState()
        canvas.setFont(_BOLD,8)
        canvas.setFillColor(colors.HexColor(TEAL))
        canvas.drawString(48,h-31,"МОНИТОРИНГ ОБРАЗОВАТЕЛЬНЫХ УЧРЕЖДЕНИЙ")
        canvas.setStrokeColor(colors.HexColor("#b8c9c6"))
        canvas.line(48,35,w-48,35)
        canvas.setFont(_REGULAR,8)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(48,23,"Аудит сайтов · Магаданская область")
        canvas.drawRightString(w-48,23,str(doc.page))
        canvas.restoreState()
    doc.build(story,onFirstPage=frame,onLaterPages=frame)
    return target.getvalue()
