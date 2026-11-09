"""Select the exact photographic evidence for a question, and label the case illustrations separately."""

if __package__:
    from .visual_guide import guide_illustration
else:
    from visual_guide import guide_illustration

_PHOTO_TABLES = {"school_main":"home_main_photo", "canteen":"home_canteen_photo"}
_SUFFIX = {"image/jpeg":"jpg","image/png":"png","image/webp":"webp","image/gif":"gif",
           "image/avif":"avif","image/bmp":"bmp"}


def _observed(conn,page_id,visual):
    target = visual["target"]
    if target in _PHOTO_TABLES:
        table = _PHOTO_TABLES[target]
        row = conn.execute(f"""SELECT c.image_url,a.data,a.mime,a.width,a.height,a.sha256,d.fetched_at
            FROM {table} c JOIN available_home_image_download d ON d.page_id=c.page_id AND d.image_url=c.image_url
            JOIN home_image_asset a USING(sha256) WHERE c.page_id=? AND c.status='found' AND d.status='saved'""",
            (page_id,)).fetchone()
        if not row:
            return []
        url,data,mime,width,height,digest,captured_at = row
        label = "Главное изображение школы" if target=="school_main" else "Изображение в блоке столовой"
        return [({"id":f"p{page_id}-{target}-{digest}","kind":"image","source":"original","photo_url":url,
                  "path":f"images/{digest}.{_SUFFIX.get(mime,'bin')}","sha256":digest,"width":width,
                  "height":height,"captured_at":captured_at,"partial":False},data,label)]
    if target not in ("staff","staff_series"):
        return []
    if __package__:
        from .staff_visual import staff_visual_material
    else:
        from staff_visual import staff_visual_material
    images = []
    fingerprints = set()
    omitted = []
    series = target=="staff_series"
    for oid in visual["observation_ids"]:
        pic = staff_visual_material(conn,page_id,oid,displayed=visual.get("displayed",False))
        if pic is None:
            omitted.append({"observation_id":oid,"reason":"no_bound_image"})
            continue
        if series:
            if pic.get("provenance",{}).get("subject")=="card":
                omitted.append({"observation_id":oid,"reason":"whole_card_only"})
                continue
            linked = conn.execute("SELECT sha256 FROM available_home_image_download WHERE page_id=? AND image_url=? AND status='saved'",
                                  (page_id,pic["photo_url"])).fetchone()
            keys = {pic["sha256"],linked[0]} if linked and linked[0] else {pic["sha256"]}
            if keys & fingerprints:
                omitted.append({"observation_id":oid,"reason":"duplicate_image"})
                continue
            fingerprints.update(keys)
        meta = {k:v for k,v in pic.items() if k not in ("data","material_id")}
        meta.update(id=pic["material_id"],kind="image",observation_id=oid)
        where = "исходный файл изображения" if pic["source"]=="original" else "область изображения на снимке страницы"
        if pic.get("provenance",{}).get("subject")=="card":
            where = "кадр карточки сотрудника"
        images.append((meta,pic["data"],f"Карточка №{oid}, {where}"))
    if series:
        if len(images)<2:
            return []
        for meta,_,_ in images:
            meta["compared_observation_ids"] = [m["observation_id"] for m,_,_ in images]
            meta["selection_omissions"] = omitted
    return images


def visual_bundle(conn,page_id,check,*,include_guide=True,cache=None):
    """Each tuple contains exportable metadata, original bytes and the caption shown to the reviewer."""
    visual = check["visual"]
    key = (page_id,visual["target"],tuple(visual.get("observation_ids",())),visual.get("displayed",False))
    if cache is None:
        cache = {}
    if key not in cache:
        cache[key] = _observed(conn,page_id,visual)
    bundle = [(dict(meta,for_checks=[check["id"]]),data,caption) for meta,data,caption in cache[key]]
    if not bundle or not include_guide:
        return bundle
    guide = guide_illustration(check)
    if guide:
        bundle.append(guide)
    return bundle
