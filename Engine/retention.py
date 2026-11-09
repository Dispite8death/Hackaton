"""Retire replaced binary evidence. Page HTML, captured facts and audit history remain queryable."""

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass

if __package__:
    from .retention_records import RETENTION_ITEM_SQL
    from .archive_access import ArchiveBusy,archive_access
    from .attached_files import _asset_path,asset_root,managed_path
else:
    from retention_records import RETENTION_ITEM_SQL
    from archive_access import ArchiveBusy,archive_access
    from attached_files import _asset_path,asset_root,managed_path


RETENTION_SCHEMA_SQL=RETENTION_ITEM_SQL+"""
CREATE TABLE IF NOT EXISTS retention_policy (
    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
    days INTEGER NOT NULL CHECK(days BETWEEN 1 AND 36500)
);
CREATE TABLE IF NOT EXISTS retention_disk_queue (
    sha256 TEXT NOT NULL,format TEXT NOT NULL,byte_size INTEGER NOT NULL,
    PRIMARY KEY(sha256,format)
);
CREATE INDEX IF NOT EXISTS retention_image_owners ON home_image_download(sha256);
CREATE INDEX IF NOT EXISTS retention_attachment_owners ON page_attachment(sha256);
"""
RENDER=("pdf","screenshot","full_screenshot")


def _has(conn,name):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())


def get_policy(conn):
    row=conn.execute("SELECT enabled,days FROM retention_policy WHERE id=1").fetchone() if _has(conn,"retention_policy") else None
    return {"enabled":bool(row[0]),"days":row[1]} if row else {"enabled":False,"days":180}


def save_policy(conn,values):
    if not isinstance(values,dict):
        raise ValueError("Требуется словарь настроек хранения")
    enabled,days=values.get("enabled"),values.get("days")
    if type(enabled) is not bool or type(days) is not int or not 1<=days<=36500:
        raise ValueError("Укажите режим очистки и срок хранения в днях от 1 до 36500")
    with conn:
        conn.execute("""INSERT INTO retention_policy VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET
                     enabled=excluded.enabled,days=excluded.days""",(int(enabled),days))
    return get_policy(conn)


def retired_for_page(conn,page_id):
    if not _has(conn,"retention_item"):
        return []
    cursor=conn.execute("""SELECT kind,source,removed_at,replacement_page_id,byte_size,sha256 FROM retention_item
                          WHERE page_id=? ORDER BY kind,source""",(page_id,))
    names=[c[0] for c in cursor.description]
    return [dict(zip(names,row)) for row in cursor]


def missing_material_note(conn,page_id,check):
    removed=retired_for_page(conn,page_id)
    if check.get("attachment"):
        row=conn.execute("SELECT url FROM page_attachment WHERE id=? AND page_id=?",(check["attachment"]["id"],page_id)).fetchone()
        removed=[r for r in removed if row and r["kind"]=="attachment" and r["source"]==row[0]]
    else:
        kinds={"full":"full_screenshot","viewport":"screenshot","image":"image","pdf":"pdf"}
        selected={kinds[k] for k in check.get("material_kinds",[]) if k in kinds}
        removed=[r for r in removed if r["kind"] in selected]
    if removed:
        newest=", ".join(str(pid) for pid in sorted({r["replacement_page_id"] for r in removed}))
        return f"Часть архивных материалов этой проверки очищена по сроку хранения. Замену можно посмотреть на снимках № {newest}. Доказательств для вывода недостаточно."
    return None


@dataclass(frozen=True)
class Item:
    page:int
    kind:str
    source:str
    category:str
    size:int
    collected:int
    digest:str|None=None
    fmt:str|None=None
    replacement_ready:bool=True

    @property
    def key(self):
        return self.page,self.kind,self.source


def _protected(conn):
    queries=["SELECT page_id FROM model_assessment","SELECT page_id FROM factcheck_run",
             "SELECT page_id FROM factcheck_job WHERE state!='done'",
             "SELECT f.page_id FROM crawl_fetch f JOIN crawl_run r ON r.id=f.run_id WHERE r.state='running'",
             "SELECT page_id FROM page_attachment WHERE state IN ('pending','error')",
             "SELECT page_id FROM page_index_issue",
             "SELECT s.page_id FROM staff_observation s JOIN staff_element e ON e.observation_id=s.id WHERE e.visual_status='pending'"]
    if _has(conn,"material_backfill"):
        queries.append("SELECT source_page_id FROM material_backfill WHERE state NOT IN ('saved','covered_by_attachment')")
    return {row[0] for row in conn.execute(" UNION ".join(queries))}


def _controlled_file(root,digest,fmt):
    path=_asset_path(root,digest,fmt)
    managed_path(root.parent,root,path.parent,path)
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("Оригинал недоступен в управляемом архиве")
    return path


def _matches(root,digest,fmt,size,checked):
    key=digest,fmt,size
    if key not in checked:
        try:
            p=_controlled_file(root,digest,fmt)
            if p.stat().st_size!=size:
                checked[key]=False
            else:
                with p.open("rb") as stream:
                    checked[key]=hashlib.file_digest(stream,"sha256").hexdigest()==digest
        except (ValueError,OSError):
            checked[key]=False
    return checked[key]


def _snapshot_items(conn,needed):
    result=defaultdict(list)
    for row in conn.execute("""SELECT page_id,captured_at,status,json_extract(details,'$.full_screenshot_coverage'),
        json_extract(details,'$.print_comparison.state'),length(pdf),length(screenshot),length(full_screenshot) FROM page_render"""):
        pid,stamp,status,coverage,printing,*sizes=row
        if pid in needed:
            for kind,size in zip(RENDER,sizes):
                if size:
                    usable=status=="complete"
                    if kind=="pdf" and printing not in (None,"no_major_change_observed"):
                        usable=False
                    if kind=="full_screenshot" and coverage not in (None,"document_bounds_captured"):
                        usable=False
                    result[pid].append(Item(pid,kind,"",kind,size,stamp,replacement_ready=usable))
    photo_types={}
    for table in ("home_main_photo","home_canteen_photo","garden_building_photo"):
        for pid,url in conn.execute(f"SELECT page_id,image_url FROM {table} WHERE image_url IS NOT NULL"):
            photo_types[pid,url]=table
    for pid,url,sha,size,stamp in conn.execute("""SELECT d.page_id,d.image_url,d.sha256,length(a.data),d.fetched_at
            FROM home_image_download d JOIN home_image_asset a USING(sha256) WHERE d.status='saved' AND length(a.data)>0"""):
        if pid in needed:
            result[pid].append(Item(pid,"image",url,photo_types.get((pid,url),url),size,stamp,sha))
    for pid,url,role,sha,fmt,size,stamp in conn.execute("""SELECT page_id,url,role,sha256,format,byte_size,fetched_at
            FROM page_attachment WHERE state='saved'"""):
        if pid in needed:
            # A different document in the same large catalogue is never a replacement.
            result[pid].append(Item(pid,"attachment",url,role+" "+url,size,stamp,sha,fmt))
    return result


def _plan(conn,cutoff,limit):
    pages=defaultdict(list)
    for pid,org,url,dated in conn.execute("SELECT id,id_organization,url,date_loaded FROM page ORDER BY date_loaded DESC,id DESC"):
        pages[org,url].append((pid,dated))
    groups=[rows for rows in pages.values() if len(rows)>1 and rows[-1][1]<=cutoff]
    if not groups:
        return []
    retired={tuple(r) for r in conn.execute("SELECT page_id,kind,source FROM retention_item")}
    needed={pid for rows in groups for pid,_ in rows}
    evidence=_snapshot_items(conn,needed)
    pinned=_protected(conn)
    plan=[]
    chosen=0
    checked={}
    known_images={}
    root=None
    for group in groups:
        latest=group[0][0]
        newer={}
        for pid,dated in group:
            items=[item for item in evidence[pid] if item.key not in retired]
            if pid!=latest and 0<dated<=cutoff and pid not in pinned:
                matches=[]
                for item in items:
                    if not 0<item.collected<=cutoff:
                        continue
                    for replacement in newer.get((item.kind,item.category),()):
                        if item.kind=="attachment":
                            root=root or asset_root(conn)
                            if (not _matches(root,item.digest,item.fmt,item.size,checked) or
                                not _matches(root,replacement.digest,replacement.fmt,replacement.size,checked)):
                                continue
                        if item.kind=="image":
                            for digest in (item.digest,replacement.digest):
                                if digest not in known_images:
                                    blob=conn.execute("SELECT data FROM home_image_asset WHERE sha256=?",(digest,)).fetchone()
                                    known_images[digest]=bool(blob and hashlib.sha256(blob[0]).hexdigest()==digest)
                            if not (known_images[item.digest] and known_images[replacement.digest]):
                                continue
                        matches.append((item,replacement.page))
                        break
                if matches:
                    plan.extend(matches)
                    chosen+=1
                    if chosen>=limit:
                        return plan
            for item in items:
                if item.replacement_ready and dated>0 and item.collected>0:
                    newer.setdefault((item.kind,item.category),[]).append(item)
    return plan


def _unowned(conn,plan):
    """Release shared bytes only after every captured binding has been retired."""
    target={item.key for item,_ in plan}
    retired={tuple(r) for r in conn.execute("SELECT page_id,kind,source FROM retention_item")}
    candidates={(item.kind,item.digest) for item,_ in plan if item.digest}
    free={}
    for kind,digest in candidates:
        if kind=="attachment":
            refs=conn.execute("""SELECT page_id,url,byte_size,format FROM page_attachment
                WHERE state='saved' AND sha256=?""",(digest,)).fetchall()
        else:
            refs=conn.execute("""SELECT d.page_id,d.image_url,length(a.data),NULL FROM home_image_download d
                JOIN home_image_asset a USING(sha256) WHERE d.status='saved' AND d.sha256=?""",(digest,)).fetchall()
        if refs and all((pid,kind,url) in target or (pid,kind,url) in retired for pid,url,_,_ in refs):
            for _,_,size,fmt in refs:
                free[kind,digest,fmt]=size
    return free


def _release_sql(conn,plan,free,now):
    for item,replacement in plan:
        conn.execute("INSERT INTO retention_item VALUES(?,?,?,?,?,?,?)",(*item.key,now,replacement,item.size,item.digest))
        if item.kind in RENDER:
            conn.execute(f"UPDATE page_render SET {item.kind}=NULL WHERE page_id=?",(item.page,))
    for (kind,sha,fmt),size in free.items():
        if kind=="image":
            conn.execute("UPDATE home_image_asset SET data=x'' WHERE sha256=?",(sha,))
        else:
            conn.execute("INSERT OR IGNORE INTO retention_disk_queue VALUES(?,?,?)",(sha,fmt,size))


def _flush_disk(conn,limit):
    removed=0
    errors=[]
    jobs=conn.execute("SELECT sha256,format,byte_size FROM retention_disk_queue LIMIT ?",(limit,)).fetchall()
    for sha,fmt,size in jobs:
        live=conn.execute("""SELECT 1 FROM page_attachment a WHERE a.state='saved' AND a.sha256=?
              AND NOT EXISTS(SELECT 1 FROM retention_item t WHERE t.page_id=a.page_id AND t.kind='attachment'
                             AND t.source=a.url) LIMIT 1""",(sha,)).fetchone()
        if not live:
            root=asset_root(conn)
            try:
                expected=_asset_path(root,sha,fmt)
                if expected.exists() or expected.is_symlink():
                    if not _matches(root,sha,fmt,size,{}):
                        raise ValueError("Не совпал архивный путь или SHA-256")
                    _controlled_file(root,sha,fmt).unlink()
                    removed+=size
            except (ValueError,OSError) as error:
                errors.append(str(error))
                continue
        conn.execute("DELETE FROM retention_disk_queue WHERE sha256=? AND format=?",(sha,fmt))
    return removed,errors


def maintain(conn,*,apply=False,now=None,limit=200):
    if type(limit) is not int or limit<=0:
        raise ValueError("Укажите положительный лимит снимков")
    now=int(time.time()) if now is None else int(now)
    policy=get_policy(conn)
    result={**policy,"cutoff":now-policy["days"]*86400,"applied":False,"busy":False,"snapshots":0,
            "materials":0,"files":0,"render_bytes":0,"image_bytes":0,"attachment_bytes":0,"bytes":0,
            "removed_disk_bytes":0,"errors":[]}
    if apply and not policy["enabled"]:
        return result

    def inspect():
        plan=_plan(conn,result["cutoff"],limit)
        free=_unowned(conn,plan) if plan else {}
        result.update(snapshots=len({item.page for item,_ in plan}),materials=len(plan),
                      render_bytes=sum(item.size for item,_ in plan if item.kind in RENDER),
                      image_bytes=sum(size for (kind,_,_),size in free.items() if kind=="image"),
                      attachment_bytes=sum(size for (kind,_,_),size in free.items() if kind=="attachment"),
                      files=sum(kind=="attachment" for kind,_,_ in free))
        result["bytes"]=result["render_bytes"]+result["image_bytes"]+result["attachment_bytes"]
        return plan,free

    if not apply:
        inspect()
        return result
    if conn.in_transaction:
        raise ValueError("Завершите текущую транзакцию перед очисткой архива")
    try:
        with archive_access(conn,exclusive=True):
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if get_policy(conn)!=policy:
                    return result
                plan,free=inspect()
                _release_sql(conn,plan,free,now)
            # The outbox commits before unlinking. A crash can leave a queued file but
            # cannot leave an unreported missing live material. A later run resumes it.
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if get_policy(conn)["enabled"]:
                    result["removed_disk_bytes"],result["errors"]=_flush_disk(conn,max(100,limit*5))
            result["applied"]=True
    except ArchiveBusy:
        result["busy"]=True
    return result
