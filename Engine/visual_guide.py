"""One case-guide figure per visual question, from the verified local edition."""

import hashlib
import io
import re
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from threading import Lock


GUIDE = Path(__file__).resolve().parents[1] / "Задание" / "Типичные ошибки заполнения .pdf"
GUIDE_SHA256 = "22afed3d10c16320f73404cda7cf0d5fc944f7950a848b1835a925f2f20a8aa3"
_PDF_LOCK = Lock()

# PDF page, zero-based image index from top to bottom, and the issue described in the guide.
_FIGURES = {
    1: (1,0,"В карточке главы муниципального образования вместо портрета показан флаг."),
    2: (2,0,"Пример замечания к одежде главы муниципального образования на фотографии."),
    3: (3,0,"В примере муниципального сайта авторы указывают на перевёрнутый флаг."),
    18: (10,0,"На главной показан слишком крупный логотип школы."),
    19: (11,0,"На главной фотография школы показана слишком мелко."),
    20: (11,1,"Пример фотографии здания школы при пасмурном освещении."),
    21: (12,0,"Здание видно частично, линия горизонта наклонена, на переднем плане сугробы."),
    22: (13,0,"В отображённой рамке портрета обрезана часть головы."),
    23: (13,1,"Фотографии сотрудников различаются фоном и стилем; в памятке отмечена нечёткость."),
    24: (14,0,"В карточке сотрудника оставлено шаблонное изображение."),
    28: (16,0,"В памятке предложено выровнять и приблизить снимок столовой."),
}
_CHECKS = {
    "school_main": {"school_main_photo":19, "school_main_logo":18, "school_main_building":20,
                    "school_main_horizon":21, "school_main_building_visible":21},
    "canteen": {"canteen_photo":28, "canteen_level":28, "canteen_framing":28},
    "staff": {"staff_portrait":24, "staff_placeholder":24, "staff_head_in_frame":22, "staff_sharp":23,
              "advisory_head_clothing":2, "advisory_flag":1, "advisory_flag_stripes":3},
    "staff_series": {"staff_series_background":23, "staff_series_scale":23, "staff_series_crowns":23},
}


@lru_cache(maxsize=1)
def _verified_bytes(path, modified, size):
    data = path.read_bytes()
    return data if hashlib.sha256(data).hexdigest()==GUIDE_SHA256 else None


@lru_cache(maxsize=len(_FIGURES))
def _render(path, modified, size, figure):
    import pypdfium2 as pdfium

    data = _verified_bytes(path,modified,size)
    if data is None:
        return None
    number, index, _ = _FIGURES[figure]
    with _PDF_LOCK, pdfium.PdfDocument(data) as document:
        with closing(document[number-1]) as page:
            bounds = sorted((obj.get_bounds() for obj in page.get_objects(filter=[3])),
                            key=lambda box: -box[3])
            left,bottom,right,top = bounds[index]
            width,height = page.get_size()
            scale = min(750/(right-left),450/(top-bottom))
            with closing(page.render(scale=scale,crop=(left,bottom,width-right,height-top))) as bitmap:
                with bitmap.to_pil().convert("RGB") as image:
                    image.thumbnail((750,450))
                    output = io.BytesIO()
                    image.save(output,format="JPEG",quality=86)
                    return output.getvalue(),image.width,image.height


def guide_illustration(check):
    """Return (metadata, JPEG, caption) or None if this question has no illustrated example."""
    visual = check.get("visual",{})
    target = visual.get("target")
    check_id = check.get("id","")
    key = re.sub(r"_\d+$","",check_id) if target in {"staff","staff_series"} else check_id
    figure = _CHECKS.get(target,{}).get(key)
    if not figure:
        return None
    page, _, description = _FIGURES[figure]
    if page not in visual.get("reference_pages",[]):
        return None
    try:
        stat = GUIDE.stat()
        rendered = _render(GUIDE,stat.st_mtime_ns,stat.st_size,figure)
    except OSError:
        return None
    if rendered is None:
        return None
    data,width,height = rendered
    mid = f"guide-{GUIDE_SHA256[:12]}-p{page}-fig{figure}"
    meta = {"id":mid,"kind":"guide","path":f"guide/{mid}.jpg","mime":"image/jpeg",
            "source_document":GUIDE.name,"document_sha256":GUIDE_SHA256,"page_number":page,"figure":figure,
            "sha256":hashlib.sha256(data).hexdigest(),"width":width,"height":height,"for_checks":[check_id]}
    caption = (f"Памятка, рис. {figure} (с. {page}). {description} "
               "Для вывода о проверяемом учреждении используйте его собственное изображение.")
    return meta,data,caption
