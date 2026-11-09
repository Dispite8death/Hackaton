"""Read page-scoped archives in model-sized pieces. Units retain positions in the original."""

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import base64
import io
import json
import re

from PIL import Image, ImageOps

if __package__:
    from .visual_guide import _PDF_LOCK
else:
    from visual_guide import _PDF_LOCK


TEXT_WINDOW = 2800
SCREEN_HEIGHT = 1000


@dataclass
class Piece:
    materials: list
    parts: list
    source_text: str = ""


@dataclass
class Coverage:
    gaps: list = field(default_factory=list)


def policy_for(check):
    if check.get("visual"):
        return None
    if check["id"].startswith("news_date_card_"):
        return None
    if check.get("evidence_policy"):
        return check["evidence_policy"]
    if check.get("attachment"):
        return {"sources":["attachment"],"aggregation":"exists"}
    kinds = check.get("material_kinds", ())
    if "image" in kinds or not ({"pdf","full","viewport"} & set(kinds)):
        return None
    sources = []
    if "pdf" in kinds:
        sources.append("page_pdf")
    if "dom" in kinds or "html" in kinds:
        sources.append("page_text")
    if "full" in kinds or "viewport" in kinds:
        sources.append("page_screen")
    return {"sources":sources,"aggregation":"exists"}


def _windows(size, window, overlap):
    if size <= 0:
        return
    start = 0
    while True:
        end = min(start+window, size)
        yield start,end
        if end == size:
            return
        start = end-overlap


def _image_part(image, max_size=(1080,1200)):
    image.thumbnail(max_size,Image.Resampling.LANCZOS)
    with io.BytesIO() as out:
        image.save(out,format="JPEG",quality=88)
        data = base64.b64encode(out.getvalue()).decode("ascii")
    return {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+data}}


def _piece(meta, caption, *parts):
    if meta["kind"]=="attachment":
        caption = "Название ссылки на сайте: "+json.dumps(meta.get("title") or "",ensure_ascii=False)+". "+caption
    return Piece([meta],[{"type":"text","text":f"Материал {meta['id']}. {caption}"},*parts])


def _text_pieces(text,base,label):
    for start,end in _windows(len(text),TEXT_WINDOW,140):
        meta = dict(base,text_start=start,text_end=end,text_length=len(text))
        if end-start < len(text):
            meta.update(id=f"{base['id']}-t{start}",partial=True)
        fragment = text[start:end]
        piece = _piece(meta,f"{label}. Символы {start+1}–{end} из {len(text)}.\n{fragment}")
        piece.source_text = fragment
        yield piece


def _screen_pieces(raw,base,coverage,*,viewport=False):
    with Image.open(io.BytesIO(raw)) as source:
        with ImageOps.exif_transpose(source) as image:
            width,height = image.size
            for left,right in _windows(width,1440,70):
                for top,bottom in _windows(height,SCREEN_HEIGHT,90):
                    cropped = (right-left,bottom-top)!=(width,height)
                    meta = dict(base,source_size=[width,height],crop_box=[left,top,right,bottom],
                                coverage=coverage,partial=cropped or base["partial"])
                    if cropped:
                        meta["id"] = f"{base['id']}-x{left}y{top}"
                    label = "Окно браузера" if viewport else "Экранная версия страницы"
                    if base["kind"]=="attachment":
                        label = "Изображение во вложении"
                    with image.crop((left,top,right,bottom)).convert("RGB") as region:
                        yield _piece(meta,f"{label}. Область {meta['crop_box']} в {width} × {height}. Охват: {coverage}.",
                                     _image_part(region))


@contextmanager
def _open_pdf(raw):
    import pypdfium2 as pdfium
    with _PDF_LOCK:
        document = pdfium.PdfDocument(raw)
    try:
        yield document
    finally:
        with _PDF_LOCK:
            document.close()


def _pdf_page(document,index,*,image=True):
    # Render one page before yielding to the model. The same document handle serves the next page.
    with _PDF_LOCK, closing(document[index]) as page:
        with closing(page.get_textpage()) as textpage:
            text = textpage.get_text_bounded().strip()
        if not image:
            return text,None
        width,height = page.get_size()
        if min(width,height)<=0:
            raise ValueError("Некорректный размер страницы PDF")
        scale = min(2, 1090/max(width,height))
        with closing(page.render(scale=scale)) as bitmap:
            with bitmap.to_pil().convert("RGB") as raster:
                return text,_image_part(raster)


def _pdf_pieces(raw,base,coverage):
    with _open_pdf(raw) as document:
        with _PDF_LOCK:
            count = len(document)
        yield from _pdf_sheet_pieces(document,count,base,coverage)


def _pdf_sheet_pieces(document,count,base,coverage):
    for number in range(1,count+1):
        try:
            text,image = _pdf_page(document,number-1)
        except (ValueError,OSError,RuntimeError) as exc:
            coverage.gaps.append(f"Страница {number} файла {base['id']} недоступна: {str(exc)[:150]}")
            continue
        meta = dict(base,id=f"{base['id']}-s{number}",pdf_page=number,pdf_pages=count,
                    partial=count>1 or base["partial"])
        label = f"Страница {number} из {count}. " + (
            f"PDF браузера ({base.get('pdf_media','режим CSS неизвестен')})"
            if base["kind"]=="pdf" else "Оригинальное вложение")
        # Split dense text without removing the middle. A scanned page is always sent as pixels.
        if text:
            for piece in _text_pieces(text,meta,label):
                piece.parts.append(image)
                yield piece
        else:
            yield _piece(meta,label+". Текстового слоя нет.",image)


def _base(pid,check,kind,path,raw,*,partial=False):
    return {"id":f"p{pid}-{kind}","kind":kind,"path":f"pages/{pid}/{path}",
            "for_checks":[check["id"]],"page_id":pid,"partial":partial,"source_sha256":sha256(raw).hexdigest()}


def _read_screen(conn,pid,check,coverage):
    kinds = check["material_kinds"]
    first_screen = check["id"].startswith("feedback_banner")
    order = [("viewport","screenshot"),("full","full_screenshot")] if first_screen else [
             ("full","full_screenshot"),("viewport","screenshot")]
    for kind,column in order:
        if kind not in kinds:
            continue
        row = conn.execute(f"SELECT {column},details,screenshot_mime FROM page_render WHERE page_id=?",(pid,)).fetchone()
        if not row or not row[0]:
            continue
        raw,details,mime = row
        details = json.loads(details or "{}")
        extent = details.get("full_screenshot_coverage","unknown") if kind=="full" else "viewport"
        partial = (kind=="full" and extent!="document_bounds_captured" or
                   kind=="viewport" and "full" in kinds and not first_screen)
        if partial:
            coverage.gaps.append(f"Неполный охват экранного снимка: {extent}.")
        suffix = {"image/jpeg":"jpg","image/png":"png","image/webp":"webp"}.get(mime,"bin")
        base = _base(pid,check,kind,f"{kind}.{suffix}",raw,partial=partial)
        yield from _screen_pieces(raw,base,extent,viewport=kind=="viewport")
        return
    coverage.gaps.append("Экранный снимок отсутствует.")


def _read_pdf(conn,pid,check,coverage):
    row = conn.execute("SELECT pdf,details FROM page_render WHERE page_id=?",(pid,)).fetchone()
    if not row or not row[0]:
        coverage.gaps.append("Печатный PDF страницы отсутствует.")
        return
    details = json.loads(row[1] or "{}")
    media = details.get("pdf_media","режим CSS неизвестен")
    suspect = (details.get("document_complete_for_extraction") is False or
               ("screen" not in media and details.get("print_comparison",{}).get("state")
                in {"inspect_print_layout","insufficient_document"}))
    if suspect:
        coverage.gaps.append("Печать могла пропустить содержимое страницы.")
    base = _base(pid,check,"pdf","page.pdf",row[0],partial=suspect)
    base["pdf_media"] = media
    yield from _pdf_pieces(row[0],base,coverage)


def _read_text(conn,pid,check,coverage,extract_text):
    row = conn.execute("""SELECT p.text_page,r.rendered_html,r.details FROM page p
        LEFT JOIN page_render r ON r.page_id=p.id WHERE p.id=?""",(pid,)).fetchone()
    if not row:
        coverage.gaps.append("Нет текста страницы.")
        return
    html,dom,details = row
    state = json.loads(details or "{}")
    kinds = check["material_kinds"]
    good_dom = (dom and "dom" in kinds and state.get("document_complete_for_extraction") is not False
                and state.get("document_ready_state")!="loading")
    kind,source,path = ("dom",dom,"rendered.html") if good_dom else ("html",html,"response.html")
    selector = None
    if check["id"].startswith("news_date_card_"):
        ordinal = int(check["id"].rsplit("_",1)[1])-1
        ref = conn.execute("SELECT json_extract(evidence,'$.selector') FROM news_preview WHERE page_id=? AND ordinal=?",
                           (pid,ordinal)).fetchone()
        if not ref or not ref[0]:
            coverage.gaps.append("Не установлена область новостного превью.")
            return
        selector = ref[0]
    text = extract_text(source,check["id"],selector,limit=False)[0] if source and kind in kinds else ""
    if not text:
        coverage.gaps.append("В нужной области нет доступного текста.")
        return
    base = _base(pid,check,kind,path,source.encode())
    yield from _text_pieces(text,base,"Текст области страницы в порядке чтения")


def _read_attachment(conn,pid,check,coverage):
    if __package__:
        from .attached_files import attachment_content, office_text
    else:
        from attached_files import attachment_content, office_text
    item = attachment_content(conn,pid,check)
    if not item:
        coverage.gaps.append("Оригинальный файл вложения не получен.")
        return
    meta,raw = item
    base = dict(meta,for_checks=[check["id"]],page_id=pid,partial=False,source_sha256=sha256(raw).hexdigest(),
                attachment_id=check["attachment"]["id"])
    fmt = base["format"].lower().lstrip(".")
    if fmt=="pdf":
        yield from _pdf_pieces(raw,base,coverage)
    elif fmt in {"gif","png","jpg","jpeg","webp","bmp","tiff","tif"}:
        with Image.open(io.BytesIO(raw)) as source:
            if getattr(source,"n_frames",1)>1:
                coverage.gaps.append("Показан первый кадр многостраничного изображения.")
                base["partial"] = True
        yield from _screen_pieces(raw,base,"оригинал файла")
    else:
        text = office_text(raw,fmt)
        if not text.strip():
            coverage.gaps.append("Нет читаемого текста вложения.")
            return
        # Embedded images and precise page layout do not survive Office text extraction.
        if fmt in {"csv","txt"}:
            yield from _text_pieces(text,base,"Текст исходного файла")
        else:
            coverage.gaps.append("Для офисного вложения проверен текст. Встроенные изображения и вёрстка не проверены.")
            yield from _text_pieces(text,dict(base,partial=True),"Текст офисного вложения")


def _print_comparison(conn,pid,check,coverage):
    row = conn.execute("""SELECT pdf,full_screenshot,screenshot_mime,details,rendered_html
                       FROM page_render WHERE page_id=?""",(pid,)).fetchone()
    if not row or not row[0] or not row[1]:
        coverage.gaps.append("Для сверки нужны PDF и полный экранный снимок одной загрузки.")
        return
    pdf,screen,mime,details,dom = row
    details = json.loads(details or "{}")
    with _open_pdf(pdf) as document:
        with _PDF_LOCK:
            count = len(document)
        yield from _compare_print_regions(pid,check,coverage,pdf,screen,mime,details,dom,document,count)


def _compare_print_regions(pid,check,coverage,pdf,screen,mime,details,dom,document,count):
    if not count:
        coverage.gaps.append("PDF пуст.")
        return
    # Search the complete PDF text layer before treating a missing line on one page as lost on print.
    text_parts = [_pdf_page(document,n,image=False)[0] for n in range(count)]
    pdf_text = _normalize(" ".join(text_parts))
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(dom or "", "html.parser")
    area = soup.select_one("main,article,[role=main]") or soup
    markers = []
    for node in area.select("h1,h2,h3,p,li"):
        line = " ".join(node.stripped_strings)
        words = _normalize(line).split()
        if len(words)>=5 and " ".join(words[:8]) not in pdf_text:
            markers.append(" ".join(line.split()[:8]))
    all_text = all(len(t.strip())>=25 for t in text_parts)
    hint = details.get("print_comparison",{}).get("state","unknown")
    if not all_text:
        coverage.gaps.append("В части PDF нет достаточного текстового слоя для точного сравнения.")
    coverage.gaps.append("Сопоставление выборочных областей не устанавливает отсутствие потерь во всём документе.")
    suffix = {"image/png":"png","image/jpeg":"jpg","image/webp":"webp"}.get(mime,"bin")
    screen_base = _base(pid,check,"full",f"full.{suffix}",screen,partial=True)
    pdf_base = _base(pid,check,"pdf","page.pdf",pdf,partial=True)
    with Image.open(io.BytesIO(screen)) as source:
        w,h = source.size
        for position,number,top in dict.fromkeys([("start",1,0),("end",count,max(0,h-900))]):
            bottom = min(h,top+900)
            a = dict(screen_base,id=screen_base["id"]+"-"+position,crop_box=[0,top,w,bottom],source_size=[w,h])
            b = dict(pdf_base,id=f"{pdf_base['id']}-cmp-{position}",pdf_page=number,pdf_pages=count,
                     pdf_media=details.get("pdf_media","режим CSS неизвестен"),
                     checked_text_pages=count,unmatched_dom_anchors=markers[:3]+markers[-3:] if all_text else [])
            with source.crop((0,top,w,bottom)).convert("RGB") as view:
                first = _piece(a,"Экранная версия браузера.",_image_part(view))
            _,picture = _pdf_page(document,number-1)
            details_text = (f"Сохранённый PDF ({b['pdf_media']}), лист {number} из {count}. Отдельное измерение CSS print: {hint}. "
                "Различие компоновки и перенос на другой лист сами по себе не означают потерю текста. "
                "Кандидаты из DOM без совпадения в текстовом слое всего PDF: "
                +json.dumps(b["unmatched_dom_anchors"],ensure_ascii=False)+". Для present процитируй один из этих "
                "фрагментов, только если он действительно виден на экране. Сошлись на оба материала.")
            second = _piece(b,details_text,picture)
            yield Piece(first.materials+second.materials,first.parts+second.parts)
            if count==1 and h<=900:
                break


def _normalize(value):
    return " ".join(re.findall(r"[\w]+",value.casefold().replace("ё","е")))


def print_witness(materials,result):
    pdf = next((m for m in materials if "unmatched_dom_anchors" in m),None)
    if pdf is None or result["outcome"]!="present":
        return True
    answer = _normalize(result["evidence"])
    return (set(result["material_ids"])=={m["id"] for m in materials}
            and any(_normalize(a) in answer for a in pdf["unmatched_dom_anchors"]))


def unconfirmed_times(evidence, source):
    def times(text):
        return {f"{int(h)}:{m}" for h,m in re.findall(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", text)}
    return sorted(times(evidence) - times(source))


def text_witness(piece,result,check_id):
    """Check claims against the actual text layer; do not OCR a scanned sheet implicitly."""
    if result["outcome"]!="present":
        return None
    answer = result["evidence"]
    text = " "+_normalize(piece.source_text)+" "
    if check_id=="staff_named_education":
        degree = (r"\b(?:бакалавр\w*|магистр\w*|специалитет\w*|высшее|"
                  r"высш\w+\s+(?:профессиональн\w+\s+|педагогическ\w+\s+)?образован\w+|"
                  r"средн\w*\s+(?:профессиональн\w+|специальн\w+|общ\w+)\s*(?:образован\w*)?|"
                  r"диплом\w*(?:\s+о)?\s+(?:специалист\w*|высш\w*|средн\w*|об\s+окончании))\b")
        if not re.search(degree,answer,re.I) or (piece.source_text and not re.search(degree,text,re.I)):
            return "Уровень образования сотрудника или диплом не подтверждены. Курсы и удостоверения о повышении квалификации учитываются отдельно."
    schedule = check_id.startswith("attachment_schedule_") or check_id in {"schedule_body_lessons","schedule_visible_lessons"}
    if schedule and len(re.findall(r"[а-яa-z]{2,}",text,re.I))<6:
        return ("Конкретная строка с классом, предметом и временем требует сверки по оригиналу: "
                "независимого подтверждения прочитанного текста нет.")
    if piece.source_text and (schedule or check_id=="section_content") and unconfirmed_times(answer, piece.source_text):
        return "Модель указала время занятия, которого нет в текстовом материале. Сверьте строку с оригиналом."
    if not piece.source_text:
        return None
    if check_id=="event_date_in_text" or check_id.startswith("event_plan_"):
        years = set(re.findall(r"\b(?:19|20)\d{2}\b",answer))
        if not any(f" {year} " in text for year in years):
            return "В ответе отсутствует явно указанный и подтверждённый год события."
    if check_id=="publication_time_evidence":
        times = re.findall(r"(?<![\d.])(?:[01]?\d|2[0-3]):[0-5]\d\b",answer)
        times += re.findall(r"\b(?:время|в)\s+((?:[01]?\d|2[0-3])\.[0-5]\d)(?![.\d])",answer,re.I)
        if not any(" "+_normalize(t)+" " in text for t in times):
            return "В ответе отсутствует подтверждаемое источником время размещения."
    quotes = [m[0] or m[1] or m[2] for m in re.findall(r"«([^»\n]{2,300})»|\"([^\"\n]{2,300})\"|'([^'\n]{2,300})'",answer)]
    if any(" "+_normalize(q)+" " not in text for q in quotes if _normalize(q)):
        return "Цитата из обоснования не найдена в переданном текстовом фрагменте."
    return None


def pieces(conn,pid,check,coverage,extract_text):
    for source in policy_for(check)["sources"]:
        try:
            if source=="page_pdf":
                yield from _read_pdf(conn,pid,check,coverage)
            elif source=="page_screen":
                yield from _read_screen(conn,pid,check,coverage)
            elif source=="page_text":
                yield from _read_text(conn,pid,check,coverage,extract_text)
            elif source=="attachment":
                yield from _read_attachment(conn,pid,check,coverage)
            elif source=="print_comparison":
                yield from _print_comparison(conn,pid,check,coverage)
            elif source=="publication_sources":
                if __package__:
                    from .publication_review import publication_materials
                else:
                    from publication_review import publication_materials
                records=publication_materials(conn,pid,check)
                if check["publication"]["scope"]=="internal" and records:
                    yield Piece([m for m,t in records],
                                [{"type":"text","text":m["id"]+"\n"+t} for m,t in records],
                                source_text="\n".join(t for m,t in records))
                else:
                    for m,t in records:
                        item=_piece(m,"Независимый издатель: "+m["publisher"]+".\n"+t)
                        item.source_text=t
                        yield item
        except (OSError, ValueError, RuntimeError, Image.DecompressionBombError) as exc:
            coverage.gaps.append(f"Материал {source} не прочитан: {str(exc)[:160]}")
