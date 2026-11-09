"""Image-scoped questions for one archived institution page."""

import re
from collections import defaultdict
from functools import lru_cache

from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError

if __package__:
    from .render_store import staff_source_html
    from .staff_identity import full_name_key
    from .staff_visual import staff_visual_material
else:
    from render_store import staff_source_html
    from staff_identity import full_name_key
    from staff_visual import staff_visual_material


SERIES_LIMIT = 3
_LIST_CLASSES = re.compile(r"^(?:tpl-block-list-objects|row|staff-list|staff-grid|employee-list|"
                           r"person-list|personnel-list|cards-list|card-grid|team-list)$", re.I)
_UNSURE = "Если нужная деталь неразличима или изображение не относится к указанной карточке, выберите uncertain."


def _question(key, label, instruction, target, scope, pages, *, ids=(), when="", displayed=False,
              classification=False, selected_series=False):
    visual = {"target":target, "label":label, "rule_scope":scope, "reference_pages":list(pages),
              "observation_ids":list(ids), "conditional":bool(when), "displayed":displayed,
              "manual_only":scope=="advisory", "include_in_score":scope!="advisory" and not classification}
    if when:
        visual["applies_when"] = when
        instruction += (" Условие применимости — "+when+". Если условие явно не выполнено, выберите not_applicable. "
                        "Если по изображению условие нельзя проверить, выберите uncertain.")
    if selected_series:
        visual["selection"] = selected_series
        instruction += " Оцените только показанную выборку из одного списка; вывод относится к этим карточкам."
    if scope=="advisory":
        instruction += (" Это ручное наблюдение по иллюстрации из раздела памятки об ОМСУ. "
                        "Описывайте только видимое. Результат не определяет соответствие школы или сада требованиям.")
    return {"id":key, "instruction":instruction, "material_kinds":["image"], "visual":visual}


def _school_home():
    main = "school_main"
    guide = "school_guide"
    building = "основной предмет главного снимка — здание"
    group = "главный снимок показывает групповой портрет"
    canteen = "на снимке различимо помещение столовой"
    return [
        _question("school_main_photo","Допустимый вид главного изображения",
                  "Виден ли на главном изображении хотя бы один из вариантов памятки: здание школы, школьный логотип "
                  "или групповой портрет коллектива? Назовите вид и непосредственно видимые признаки. "
                  "Для атрибуции опирайтесь только на читаемое обозначение или подтверждённую привязку изображения. "
                  "Не устанавливайте личности и место работы изображённых людей по внешности. При недостатке признаков — uncertain.",
                  main,guide,[10,11,12]),
        _question("school_main_building","Тип изображения — здание",
                  "Является ли здание основным изображённым объектом? Укажите видимый признак архитектурного объекта.",
                  main,guide,[10,11,12],classification=True),
        _question("school_main_logo","Тип изображения — логотип",
                  "Является ли эмблема или логотип основным предметом главного изображения? Сообщите, "
                  "какие графические элементы или надпись различимы. Простое присутствие флага этого не доказывает.",
                  main,guide,[10],classification=True),
        _question("school_main_group","Тип изображения — групповой портрет",
                  "Является ли основным сюжетом групповой портрет людей, вместе позирующих перед камерой? "
                  "Случайные прохожие и толпа на мероприятии не доказывают групповой портрет коллектива. "
                  "Не определяйте имена, должности или членство в коллективе по внешности.",
                  main,guide,[11],classification=True),
        _question("school_main_horizon","Здание — ровный горизонт",
                  "Выглядят ли линии горизонта или надёжные ориентиры фасада ровными? Перспектива сама по себе "
                  "недостаточна для вывода о наклоне. Если нет подходящего ориентира, выберите uncertain.",
                  main,guide,[11,12],when=building),
        _question("school_main_building_visible","Здание — читаемость фасада",
                  "Хорошо ли различимо здание как главный объект кадра? В качестве признака укажите видимую часть "
                  "фасада или конкретное препятствие, закрывающее существенную часть здания. Снег сам по себе допустим.",
                  main,guide,[11,12],when=building),
        _question("school_main_group_light","Групповой портрет — освещение",
                  "Позволяет ли яркое и достаточно равномерное освещение рассмотреть лица участников группового портрета?",
                  main,guide,[11],when=group),
        _question("school_main_group_attire","Групповой портрет — парадная одежда",
                  "Виден ли у группы парадный характер одежды? Назовите наблюдаемые детали. При смешанной "
                  "или плохо различимой одежде выберите uncertain. Не оценивайте характер, профессию или настроение людей.",
                  main,guide,[11],when=group),
        _question("canteen_photo","Фото помещения столовой",
                  "Видно ли на изображении помещение для приёма пищи? Приведите признаки помещения. "
                  "Меню, тарелка с едой и логотип по отдельности не показывают помещение столовой.",
                  "canteen",guide,[16]),
        _question("canteen_level","Столовая — наклон кадра",
                  "Выровнен ли кадр столовой относительно надёжных горизонталей или вертикалей помещения? "
                  "При отсутствии ориентира выберите uncertain.","canteen",guide,[16],when=canteen),
        _question("canteen_framing","Столовая — крупность помещения",
                  "Достаточно ли крупно показана обеденная зона, чтобы различить её обстановку? "
                  "Обоснуйте ответ видимыми столами, посадочными местами или иными деталями помещения.",
                  "canteen",guide,[16],when=canteen),
    ]


def _staff_questions(row, school):
    oid, _, _, _, head, _, _, _ = row
    ids = [oid]
    portrait = "в карточке показана фотография человека с различимой головой"
    def ask(key,label,text,scope,pages,**opts):
        return _question(f"{key}_{oid}",label,text,"staff",scope,pages,ids=ids,**opts)

    result = [ask("staff_portrait","Карточка — фото человека",
                  "Есть ли в области фотографии карточки снимок реального человека? Обоснуйте по изображению. "
                  "Шаблонный силуэт, эмблема, флаг и рисунок лица не подтверждают наличие фотографии. "
                  "Идентифицировать человека и сверять его с ФИО не требуется. "+_UNSURE,
                  "case",[13,14] if school else [])]
    if school:
        result.extend([
            ask("staff_placeholder","Карточка — шаблонное изображение",
                "Именно в области фото видна шаблонная заглушка, например силуэт с подписью или типовой макет? "
                "Пустой фон или отсутствие картинки сами по себе этого не доказывают. "+_UNSURE,
                "school_guide",[13,14],classification=True),
            ask("staff_head_in_frame","Портрет — голова целиком",
                "Целиком ли помещается голова человека в отображённой рамке фотографии, включая макушку? "
                "Поясной и погрудный портрет допустимы. Если границы отображённой фотографии неизвестны, выберите uncertain.",
                "school_guide",[13],when=portrait,displayed=True),
            ask("staff_sharp","Портрет — чёткость",
                "Достаточно ли чётко различимы детали лица на портрете? Если ухудшение могло возникнуть "
                "при уменьшении предоставленного материала и исходник оценить нельзя, выберите uncertain.",
                "school_guide",[13],when=portrait),
        ])
    if head:
        result.extend([
            ask("advisory_head_clothing","Справочно — одежда на фото руководителя",
                "Различимы ли на снимке видимые признаки делового стиля одежды, например пиджак или жакет? "
                "Опишите одежду без оценки человека и его профессионализма. Галстук для школы или сада не обязателен.",
                "advisory",[1,2],when="на фото руководителя видна одежда"),
            ask("advisory_flag","Справочно — российский триколор",
                "Виден ли предмет, различимый как флаг с горизонтальными белой, синей и красной полосами? "
                "Цвета одежды или декора сами по себе не доказывают наличие флага. "
                "Наличие флага в этом кадре не требуется.", "advisory",[1,3],classification=True),
            ask("advisory_flag_stripes","Справочно — порядок полос флага",
                "Расположены ли три горизонтальные полосы сверху вниз в порядке белая, синяя, красная? "
                "При складках, перекрытии или неясной ориентации выберите uncertain. "
                "Вывод относится только к изображению полотнища.", "advisory",[3],
                when="виден различимый российский триколор с понятной ориентацией"),
        ])
    return result


def _staff_lists(conn, page_id, rows):
    candidates = [r for r in rows if r[5] and r[6]=="linked" and (r[2] or r[7])]
    if len(candidates)<2:
        return []
    soup = BeautifulSoup(staff_source_html(conn,page_id) or "", "html.parser")
    groups = defaultdict(list)
    for row in candidates:
        try:
            node = soup.select_one(row[1])
        except SelectorSyntaxError:
            continue
        if node is None:
            continue
        for parent in node.parents:
            if parent.name in ("body","main","html","[document]"):
                break
            if (parent.name in ("ul","ol","table","tbody") or parent.get("role")=="list"
                    or any(_LIST_CLASSES.fullmatch(c) for c in parent.get("class",()))):
                groups[id(parent)].append(row)
                break
            if parent.name in ("article","section","aside"):
                break
    return [g for g in groups.values() if len(g)>=2]


def _pick_series(groups, image_hashes):
    """Pick up to three distinct observations of distinct people and distinct images from one list."""
    for group in groups:
        selected, seen = [], set()
        for oid,_,name,_,_,url,_,profile in group:
            name = full_name_key(name) or " ".join((name or "").casefold().split())
            keys = {(field,value) for field,value in (("url",url),("profile",profile),("name",name)) if value}
            if seen & keys:
                continue
            hashes = image_hashes(oid)
            if hashes is None:
                continue
            keys.update(("digest",digest) for digest in hashes if digest)
            if seen & keys:
                continue
            seen.update(keys)
            selected.append(oid)
            if len(selected)==SERIES_LIMIT:
                break
        if len(selected)>=2:
            return selected
    return []


def _positioned_rows(groups, positions):
    """Rows of cards within one list, in visual order. Allow small CSS rounding differences."""
    for group in groups:
        aligned = []
        for row in sorted((r for r in group if r[0] in positions),key=lambda r:positions[r[0]]):
            top = positions[row[0]][0]
            if not aligned or abs(top-aligned[-1][0])>2:
                aligned.append((top,[]))
            aligned[-1][1].append(row)
        for _,items in aligned:
            if len(items)>=2:
                yield items


def _series(conn,page_id,rows):
    groups = _staff_lists(conn,page_id,rows)
    if not groups:
        return {}
    originals = dict(conn.execute("""SELECT s.id,d.sha256 FROM staff_observation s JOIN available_home_image_download d
        ON d.page_id=s.page_id AND d.image_url=s.photo_url WHERE s.page_id=? AND d.status='saved'""",(page_id,)))
    positions = {}
    measured = set()
    for oid,top,left in conn.execute("""SELECT s.id,
        CASE WHEN json_valid(e.visual_details) THEN json_extract(e.visual_details,'$.rect[1]') END,
        CASE WHEN json_valid(e.visual_details) THEN json_extract(e.visual_details,'$.rect[0]') END
        FROM staff_observation s JOIN staff_element e ON e.observation_id=s.id AND e.field='photo'
        WHERE s.page_id=? AND e.expected_value=s.photo_url AND e.css_loaded=1 AND e.render_source='live'
        AND e.visual_status IN ('visible','out_of_view')""",(page_id,)):
        measured.add(oid)
        if type(top) in (int,float) and type(left) in (int,float):
            positions[oid] = (top,left)

    @lru_cache(maxsize=None)
    def verified(oid,displayed):
        # Preselection only removes impossible candidates. The actual material resolver
        # verifies the DOM binding, screenshot provenance, occlusion and decoded raster.
        if oid not in measured and (displayed or oid not in originals):
            return None
        pic = staff_visual_material(conn,page_id,oid,displayed=displayed)
        if not pic or pic.get("partial") or (displayed and pic["source"]=="original"):
            return None
        return pic["source"],(pic["sha256"],originals.get(oid))

    def actual_hashes(oid,displayed=False,original_only=False):
        data = verified(oid,displayed)
        return data[1] if data and (not original_only or data[0]=="original") else None

    fallback = _pick_series(groups,lambda oid:(originals.get(oid),))
    missing = (fallback,"unverified","В архиве не подтверждены два разных изображения из этого списка.")
    crops = lambda oid:actual_hashes(oid,True)
    displayed = _pick_series(_positioned_rows(groups,positions),crops)
    displayed_selection = (displayed,"same_css_row","Проверены кадры разных карточек из одной строки одного списка.")
    if not displayed:
        displayed = _pick_series(groups,crops)
        displayed_selection = ((displayed,"same_list","Проверены кадры разных карточек одного списка.")
                               if displayed else missing)
    background = _pick_series(groups,lambda oid:actual_hashes(oid,original_only=True) if oid in originals else None)
    background_selection = (background,"originals","Проверены исходные файлы разных карточек одного списка.")
    if not background:
        background = _pick_series(groups,actual_hashes)
        background_selection = ((background,"available_images","Подобраны разные исходные фото или кадры одного списка.")
                                if background else missing)
    return {"background":background_selection,"scale":displayed_selection,"crowns":displayed_selection}


def _series_questions(selections):
    specs = (
        ("background","Серия — единообразие фона",
         "Согласованы ли фоны фотографий: единый фон, близкое визуальное оформление либо все портреты без фона?",False),
        ("scale","Серия — масштаб лиц",
         "Приблизительно одинаков ли относительный размер лиц в рамках отображённых карточек?",True),
        ("crowns","Серия — уровень макушек",
         "Примерно на одной ли относительной высоте находятся макушки от верхнего края отображённых рамок портретов?",True),
    )
    result = []
    for key,label,text,displayed in specs:
        ids,mode,description = selections.get(key,([],None,None))
        if not ids:
            continue
        text += " Для вывода нужны сопоставимые изображения хотя бы двух выбранных карточек. "
        if mode=="unverified":
            text += "Подтверждённой пары изображений нет. Выберите uncertain."
        else:
            text += "При несопоставимом кадрировании выберите uncertain."
        check = _question(f"staff_series_{key}_{ids[0]}",label,text,"staff_series","school_guide",[13,14],ids=ids,
                          displayed=displayed,selected_series=description,
                          when="в выбранных карточках показаны портреты людей")
        check["visual"]["selection_mode"] = mode
        result.append(check)
    return result


def visual_questions(conn,page_id):
    page = conn.execute("""SELECT o.kind,b.page_kind,f.depth FROM page p JOIN organization o ON o.id=p.id_organization
        LEFT JOIN page_passport b ON b.page_id=p.id LEFT JOIN crawl_fetch f ON f.page_id=p.id WHERE p.id=?""",
                        (page_id,)).fetchone()
    if page is None:
        return []
    school = page[0]=="school"
    result = _school_home() if school and (page[1]=="home" or page[2]==0) else []
    rows = conn.execute("""SELECT id,card_selector,full_name,role,is_head,photo_url,photo_state,profile_url
                           FROM staff_observation WHERE page_id=? ORDER BY id""",(page_id,)).fetchall()
    for row in rows:
        result.extend(_staff_questions(row,school))
    if school:
        result.extend(_series_questions(_series(conn,page_id,rows)))
    return result
