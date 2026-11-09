"""Keep originals published on case pages, with one traceable binding for each snapshot and URL."""

import argparse
import hashlib
import io
import os
import re
import tempfile
import time
import uuid
import zipfile
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from functools import lru_cache
from http.client import HTTPException
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

if __package__:
    from .archive_access import uses_archive
    from .retention_records import RETENTION_ITEM_SQL
    from .http_page import public_connection
else:
    from archive_access import uses_archive
    from retention_records import RETENTION_ITEM_SQL
    from http_page import public_connection


ATTACHMENT_SCHEMA_SQL = RETENTION_ITEM_SQL+"""
CREATE TABLE IF NOT EXISTS page_attachment (
    id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL REFERENCES page(id) ON DELETE CASCADE,
    url TEXT NOT NULL, title TEXT NOT NULL, role TEXT NOT NULL, format_hint TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','saved','error')),
    fetched_at INTEGER, final_url TEXT, http_status INTEGER, format TEXT, sha256 TEXT,
    byte_size INTEGER, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    CHECK(state != 'saved' OR (length(sha256)=64 AND byte_size>0 AND format IS NOT NULL)),
    UNIQUE(page_id,url)
);
CREATE INDEX IF NOT EXISTS attachment_queue ON page_attachment(state,role,page_id);
CREATE INDEX IF NOT EXISTS attachment_url_time ON page_attachment(url,fetched_at);
CREATE TABLE IF NOT EXISTS attachment_download_lease (
    url TEXT PRIMARY KEY,token TEXT NOT NULL,until INTEGER NOT NULL
);
CREATE VIEW IF NOT EXISTS available_page_attachment AS SELECT a.* FROM page_attachment a
    WHERE NOT EXISTS(SELECT 1 FROM retention_item t WHERE t.page_id=a.page_id AND t.kind='attachment' AND t.source=a.url);
"""
CASE_SECTIONS = ("documents","information","programs","staff_resources","schedule","about","management","staff","team")
MAX_BYTES = 256 * 1024**2
MAX_OFFICE_XML = 32 * 1024**2
_TYPES = {"pdf","doc","docx","xls","xlsx","ppt","pptx","odt","ods","odp","csv","txt","rtf",
          "jpg","png","webp","gif","bmp","tiff","zip","7z","rar"}
_MEMBER_TYPES=(_TYPES-{"zip","7z","rar","rtf","ppt"}) | {"jpeg","tif"}
_RANK = {"schedule":0, "catalog":1, "information":2, "programs":3}


def _page_filter(page_id,run_id):
    conditions=[]
    args=[]
    if page_id is not None:
        conditions.append("page_id=?")
        args.append(page_id)
    if run_id is not None:
        conditions.append("page_id IN (SELECT page_id FROM crawl_fetch WHERE run_id=?)")
        args.append(run_id)
    return "".join(" AND "+s for s in conditions),tuple(args)


def index_attachments(conn, page_id=None, *, run_id=None):
    """Index known evidence only; the caller owns the transaction."""
    where,params=_page_filter(page_id,run_id)
    before = conn.total_changes
    conn.execute("""INSERT INTO page_attachment(page_id,url,title,role,format_hint)
        SELECT page_id,url,coalesce(title,''),CASE WHEN catalog=1 THEN 'catalog' ELSE section END,coalesce(format,'')
        FROM institution_document_link WHERE url IS NOT NULL AND (catalog=1 OR section IN ("""
        + ",".join("?" for _ in CASE_SECTIONS) + "))" + where + " ON CONFLICT(page_id,url) DO NOTHING",
        (*CASE_SECTIONS,*params))
    conn.execute("""INSERT INTO page_attachment(page_id,url,title,role,format_hint)
        SELECT page_id,json_extract(m.value,'$.url'),
               coalesce(nullif(json_extract(m.value,'$.caption'),''),json_extract(m.value,'$.label'),''),
               'schedule',''
        FROM school_schedule_page,json_each(evidence,'$.materials') m
        WHERE json_extract(m.value,'$.kind') IN ('file','image','embedded')""" + where +
        """ ON CONFLICT(page_id,url) DO UPDATE SET role='schedule',title=excluded.title
            WHERE page_attachment.role!='schedule' OR page_attachment.title!=excluded.title""",params)
    return conn.total_changes-before


def asset_root(conn):
    location = conn.execute("PRAGMA database_list").fetchone()[2]
    if not location:
        raise ValueError("Архив оригиналов требует файловой базы SQLite")
    return Path(location).resolve().parent / ".audit-assets" / "attachments"


def _asset_path(root, digest, fmt):
    if not re.fullmatch(r"[0-9a-f]{64}",digest or "") or fmt not in _TYPES:
        raise ValueError("Некорректная ссылка на файл архива")
    return root / digest[:2] / f"{digest}.{fmt}"


def managed_path(*entries):
    if any(entry.is_symlink() or getattr(entry,"is_junction",lambda:False)() for entry in entries):
        raise ValueError("Путь архива содержит символическую ссылку или junction")


def identify(data, hint):
    """Recognize file signatures; HTML error pages or executable bytes never become review materials."""
    return _identify_stream(data[:4096],io.BytesIO(data),hint)


def _identify_stream(data,stream,hint):
    if not data:
        raise ValueError("Пустой ответ")
    if data[:1024].lstrip().startswith(b"%PDF-"):
        return "pdf"
    for sign,fmt in ((b"\xff\xd8\xff","jpg"),(b"\x89PNG\r\n\x1a\n","png"),(b"GIF87a","gif"),(b"GIF89a","gif"),
                     (b"II*\0","tiff"),(b"MM\0*","tiff"),(b"BM","bmp"),(b"7z\xbc\xaf\x27\x1c","7z"),
                     (b"Rar!\x1a\x07","rar"),(b"{\\rtf","rtf")):
        if data.startswith(sign):
            return fmt
    if data[:4]==b"RIFF" and data[8:12]==b"WEBP":
        return "webp"
    if data.startswith(b"PK\x03\x04"):
        with zipfile.ZipFile(stream) as archive:
            names = set(archive.namelist())
            for entry,fmt in (("word/document.xml","docx"),("xl/workbook.xml","xlsx"),("ppt/presentation.xml","pptx")):
                if entry in names and "[Content_Types].xml" in names:
                    return fmt
            if "mimetype" in names and archive.getinfo("mimetype").file_size<256:
                mime=archive.read("mimetype")
                for key,fmt in ((b"text","odt"),(b"spreadsheet","ods"),(b"presentation","odp")):
                    if mime==b"application/vnd.oasis.opendocument."+key:
                        return fmt
        return "zip"
    if data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")) and hint in {"doc","xls","ppt"}:
        return hint
    if hint in {"csv","txt"} and b"\0" not in data[:4096] and not data.lstrip().startswith((b"<",b"MZ")):
        return hint
    raise ValueError("Ответ не содержит поддерживаемый документ или скан")


def _request(url, timeout):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http","https"} or parsed.username is not None or parsed.password is not None:
        raise ValueError("Нужна открытая HTTP(S)-ссылка")
    host = (parsed.hostname or "").encode("idna").decode()
    port = parsed.port or (443 if parsed.scheme=="https" else 80)
    if port not in {80,443} or not host:
        raise ValueError("Некорректный адрес публикации")
    path = quote(parsed.path or "/",safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        path += "?" + quote(parsed.query,safe="=&%/+?:@!$'()*,-._~;")
    conn = public_connection(host,port,tls=parsed.scheme=="https",timeout=timeout)
    try:
        conn.request("GET",path,headers={"User-Agent":"Hackaton-EduSiteAudit/1.0","Accept-Encoding":"identity"})
        return conn,conn.getresponse()
    except BaseException:
        conn.close()
        raise


def _save_response(response,root,final,hint,deadline,max_bytes):
    length=response.headers.get("Content-Length")
    if length and int(length)>max_bytes:
        raise ValueError(f"Оригинал превышает {max_bytes//1024**2} МиБ")
    managed_path(root.parent,root)
    root.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(dir=root,prefix=".receiving-")
    try:
        digest=hashlib.sha256()
        size=0
        with os.fdopen(fd,"w+b") as stream:
            while True:
                if time.monotonic()>deadline:
                    raise TimeoutError("Истекло время загрузки")
                chunk=response.read1(min(131072,max_bytes+1-size))
                if not chunk:
                    break
                size+=len(chunk)
                if size>max_bytes:
                    raise ValueError(f"Оригинал превышает {max_bytes//1024**2} МиБ")
                stream.write(chunk)
                digest.update(chunk)
            if length and size!=int(length):
                raise ValueError("Передана только часть оригинала")
            stream.seek(0)
            fmt=_identify_stream(stream.read(4096),stream,hint if hint in _TYPES else
                                 Path(unquote(urlsplit(final).path)).suffix.lstrip(".").lower())
        sha=digest.hexdigest()
        path=_asset_path(root,sha,fmt)
        managed_path(root.parent,root,path.parent,path)
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            if os.name=="nt":
                os.rename(name,path)  # Windows rename never replaces an existing target.
            else:
                os.link(name,path)
        except FileExistsError:
            with path.open("rb") as existing:
                if path.stat().st_size!=size or hashlib.file_digest(existing,"sha256").hexdigest()!=sha:
                    raise ValueError("Контрольная сумма уже существующего файла по архивному пути отличается")
        return fmt,sha,size
    finally:
        Path(name).unlink(missing_ok=True)


def _download(root,url,hint,timeout,max_bytes=MAX_BYTES):
    final,status = url,None
    try:
        deadline = time.monotonic()+timeout
        for _ in range(6):
            remaining = deadline-time.monotonic()
            if remaining<=0:
                raise TimeoutError("Истекло время загрузки")
            conn,response = _request(final,remaining)
            with closing(conn), closing(response):
                status = response.status
                if status in {301,302,303,307,308} and response.headers.get("Location"):
                    final = urljoin(final,response.headers["Location"])
                    continue
                if status!=200:
                    raise ValueError(f"HTTP {status}")
                fmt,digest,size=_save_response(response,root,final,hint,deadline,max_bytes)
            return ("saved",int(time.time()),final,status,fmt,digest,size,None)
        raise ValueError("Слишком много перенаправлений")
    except (OSError,HTTPException,ValueError,zipfile.BadZipFile) as error:
        return ("error",int(time.time()),final,status,None,None,None,str(error))


def _reuse_available(conn,where,args):
    # A published URL can appear on many snapshots. Only reuse an actual later retrieval,
    # so a fresh visit never silently inherits bytes fetched before its HTML was observed.
    conn.execute("""WITH eligible AS MATERIALIZED (
            SELECT a.id AS target,(SELECT b.id FROM available_page_attachment b
                WHERE b.state='saved' AND b.url=a.url AND b.fetched_at>=p.date_loaded
                ORDER BY b.fetched_at,b.id LIMIT 1) AS source_id
            FROM (SELECT * FROM page_attachment WHERE """ + where + """) a JOIN page p ON p.id=a.page_id
        ) UPDATE page_attachment AS target SET
              (state,fetched_at,final_url,http_status,format,sha256,byte_size,error)=
              (source.state,source.fetched_at,source.final_url,source.http_status,source.format,source.sha256,source.byte_size,NULL)
          FROM eligible JOIN page_attachment source ON source.id=eligible.source_id WHERE target.id=eligible.target""",
          args)


@uses_archive
def download_pending(conn, *, workers=8, timeout=45, roles=(), limit=None, retry=False, progress=None,
                     run_id=None,page_id=None,max_bytes=MAX_BYTES,only_errors=False,retry_transient=False,
                     new_urls_after=None,max_attempts=None):
    """One request per URL across pending bindings. Fairly rotate origins and keep transactions short."""
    where="state IN ('pending','error')" if retry else "state='pending'"
    if retry_transient and not retry:
        # Retry a transport interruption after 5, 10, ... minutes (up to one day).
        # Leave a published 404 or rejected format for explicit inspection.
        transient="""(http_status IN (408,425,429) OR http_status BETWEEN 500 AND 599
            OR error LIKE '%timed out%' OR error LIKE '%timeout%' OR error LIKE 'Истекло время%'
            OR error LIKE '%getaddrinfo%' OR error LIKE '%connection%reset%' OR error LIKE '%connection%refused%'
            OR error LIKE '[WinError 10060]%' OR error LIKE '[WinError 10054]%' OR error LIKE '[WinError 10061]%'
            OR error LIKE '%closed connection%' OR error LIKE '%удаленн%хост%' OR error LIKE '%удалённ%хост%'
            OR error='Передана только часть оригинала')"""
        where="""(state='pending' OR (state='error' AND """+transient+"""
                 AND fetched_at+min(86400,300*(1 << min(9,max(0,attempts-1))))<=unixepoch()))"""
    if only_errors:
        where="state='error'"
    if roles:
        where += " AND role IN (" + ",".join("?" for _ in roles) + ")"
    scope,args=_page_filter(page_id,run_id)
    scope += " AND page_id IN (SELECT p.id FROM page p JOIN active_organization o ON o.id=p.id_organization)"
    if max_attempts is not None:
        scope+=" AND attempts<?"
        args=(*args,max_attempts)
    if new_urls_after is not None:
        scope += " AND url NOT IN (SELECT url FROM page_attachment WHERE page_id<=?)"
        args=(*args,new_urls_after)
    with conn:
        _reuse_available(conn,where+scope,(*roles,*args))
    rows=conn.execute("SELECT id,url,format_hint,role FROM page_attachment WHERE "+where+scope+" ORDER BY id",
                      (*roles,*args))
    jobs={}
    for item,url,hint,role in rows:
        if url not in jobs:
            jobs[url]=[[],hint,_RANK.get(role,4)]
        jobs[url][0].append(item)
        jobs[url][2]=min(jobs[url][2],_RANK.get(role,4))
    queues=defaultdict(deque)
    for url,(ids,hint,rank) in sorted(jobs.items(),key=lambda pair:pair[1][2]):
        queues[urlsplit(url).hostname].append((rank,url,ids,hint))
    stats={"saved":0,"error":0,"bytes":0,"total":len(jobs),"leased_elsewhere":0}
    if not jobs:
        return stats
    root=asset_root(conn)
    token=uuid.uuid4().hex
    with ThreadPoolExecutor(max_workers=workers) as pool:
        active={}
        while queues or active:
            candidates=sorted(queues,key=lambda host:queues[host][0][0])
            busy={host for host,_,_ in active.values()}
            for host in candidates:
                if len(active)>=workers or (limit is not None and stats["saved"]+stats["error"]+len(active)>=limit):
                    break
                if host in busy:
                    continue
                _,url,ids,hint=queues[host].popleft()
                if not queues[host]:
                    del queues[host]
                with conn:
                    claimed=conn.execute("""INSERT INTO attachment_download_lease VALUES(?,?,unixepoch()+?)
                        ON CONFLICT(url) DO UPDATE SET token=excluded.token,until=excluded.until
                        WHERE attachment_download_lease.until<=unixepoch() RETURNING url""",
                        (url,token,int(timeout)+90)).fetchone()
                if not claimed:
                    stats["leased_elsewhere"]+=1
                    continue
                active[pool.submit(_download,root,url,hint,timeout,max_bytes)]=(host,url,ids)
            if not active:
                if queues and (limit is None or stats["saved"]+stats["error"]<limit):
                    continue
                break
            done,_=wait(active,return_when=FIRST_COMPLETED)
            for future in done:
                _,url,ids=active.pop(future)
                result=future.result()
                with conn:
                    if not conn.execute("DELETE FROM attachment_download_lease WHERE url=? AND token=? RETURNING url",
                                        (url,token)).fetchone():
                        stats["leased_elsewhere"]+=1
                        continue
                    conn.executemany("""UPDATE page_attachment SET state=?,fetched_at=?,final_url=?,http_status=?,
                        format=?,sha256=?,byte_size=?,error=?,attempts=attempts+1 WHERE id=? AND state!='saved'""",
                        ((*result,item) for item in ids))
                stats[result[0]]+=1
                stats["bytes"]+=result[6] or 0
                if progress:
                    progress(stats)
    return stats


def _member_title(info):
    title=info.filename
    if not (info.flag_bits & 0x800):
        # Windows archives without the UTF-8 flag use OEM codepage 866 for Cyrillic filenames.
        try:
            candidate=title.encode("cp437").decode("cp866")
            if re.search("[А-Яа-яЁё]",candidate):
                title=candidate
        except UnicodeError:
            pass
    return title


@lru_cache(maxsize=48)
def _archive_directory(path,modified,size):
    members=[]
    try:
        with zipfile.ZipFile(path) as archive:
            for index,info in enumerate(archive.infolist()):
                fmt=Path(info.filename).suffix.lower().lstrip(".")
                if not info.is_dir() and fmt in _MEMBER_TYPES:
                    members.append((index,_member_title(info),fmt,info.file_size,info.CRC,bool(info.flag_bits & 1)))
    except (OSError,zipfile.BadZipFile,ValueError):
        pass
    return tuple(members)


@lru_cache(maxsize=48)
def _archive_hash(path,modified,size):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def attachment_questions(conn, page_id):
    rows=conn.execute("""SELECT a.id,a.role,a.title,coalesce(a.format,nullif(a.format_hint,''),'unknown'),
                      CASE WHEN t.page_id IS NULL THEN a.state ELSE 'retired' END,a.sha256 FROM page_attachment a
                      LEFT JOIN retention_item t ON t.page_id=a.page_id AND t.kind='attachment' AND t.source=a.url
                      WHERE a.page_id=? ORDER BY CASE a.role WHEN 'schedule' THEN 0 WHEN 'catalog' THEN 1 ELSE 2 END,a.id""",
                      (page_id,))
    checks=[]
    for item,role,title,fmt,state,digest in rows:
        label=title[:160] if title else f"файл № {item}"
        if role=="schedule":
            instruction=("Содержит ли сам файл расписание занятий учреждения? Для подтверждения укажите "
                         "видимые класс или группу, предмет и день либо время. Описывайте период только при его наличии. "
                         "Отличайте расписание звонков, консультаций и экзаменов от расписания занятий.")
        elif role=="programs":
            instruction=("Содержит ли сам оригинал образовательную программу, соответствующую названию ссылки? "
                         "Назовите опубликованное название, направление и доступные характеристики, "
                         "например возраст или класс, продолжительность и учебное содержание. "
                         "В доказательстве приведите конкретный текст. Если программа только упомянута, отметьте это.")
        else:
            instruction=("Соответствует ли содержание опубликованного оригинала названию ссылки? "
                         "Укажите вид, тему документа и конкретный фрагмент, на котором основан вывод. "
                         "При общем названии, пустой форме или неразборчивом тексте укажите неопределённость.")
        check={"id":f"attachment_{role}_{item}","kind":"any","instruction":instruction,
               "material_kinds":["attachment"],"evidence_policy":{"sources":["attachment"],"aggregation":"exists"},
               "attachment":{"id":item,"role":role,"format":fmt,"label":label,"title":title,
                             "state":state,"sha256":digest}}
        checks.append(check)
        if fmt=="zip" and state=="saved":
            path=_asset_path(asset_root(conn),digest,fmt)
            try:
                managed_path(path.parent.parent.parent,path.parent.parent,path.parent,path)
                stat=path.stat()
                if _archive_hash(path,stat.st_mtime_ns,stat.st_size)!=digest:
                    continue
            except (OSError,ValueError):
                continue
            for index,name,member_fmt,size,crc,encrypted in _archive_directory(path,stat.st_mtime_ns,stat.st_size):
                attachment=dict(check["attachment"],member_index=index,member_name=name,member_crc=crc,
                                format=member_fmt,label=name[:160],title=name)
                if encrypted or size>MAX_BYTES:
                    attachment["state"]="unavailable"
                checks.append(dict(check,id=f"{check['id']}_member_{index}",attachment=attachment))
    return checks


def attachment_content(conn, page_id, check):
    selected=check.get("attachment") or {}
    row=conn.execute("""SELECT id,url,final_url,title,role,format,sha256,byte_size,fetched_at
        FROM available_page_attachment WHERE id=? AND page_id=? AND role=? AND state='saved'""",
        (selected.get("id"),page_id,selected.get("role"))).fetchone()
    if not row:
        return None
    item,url,final,title,role,fmt,digest,size,fetched=row
    if selected.get("sha256") and selected["sha256"]!=digest:
        return None
    path=_asset_path(asset_root(conn),digest,fmt)
    member=selected.get("member_index")
    try:
        managed_path(path.parent.parent.parent,path.parent.parent,path.parent,path)
        stat=path.stat()
        if stat.st_size!=size:
            return None
        if member is None:
            data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=digest:
                return None
        elif _archive_hash(path,stat.st_mtime_ns,stat.st_size)!=digest:
            return None
    except (FileNotFoundError,ValueError):
        return None
    archive_meta={}
    if member is not None:
        if fmt!="zip" or type(member) is not int or member<0:
            return None
        try:
            with zipfile.ZipFile(path) as archive:
                info=archive.infolist()[member]
                if (info.file_size>MAX_BYTES or info.flag_bits&1 or info.CRC!=selected.get("member_crc")
                    or _member_title(info)!=selected.get("member_name")):
                    return None
                data=archive.read(info)
                fmt=identify(data,Path(info.filename).suffix.lower().lstrip("."))
                if fmt not in _MEMBER_TYPES:
                    return None
        except (IndexError,zipfile.BadZipFile,RuntimeError,ValueError):
            return None
        archive_meta={"archive_sha256":digest,"member_index":member,"member_name":_member_title(info),
                      "archive_title":title}
        title=archive_meta["member_name"]
        size=len(data)
        digest=hashlib.sha256(data).hexdigest()
    scope=f"-m{member}" if member is not None else ""
    meta={"id":f"p{page_id}-attachment-{item}{scope}-{digest[:16]}","kind":"attachment","format":fmt,
          "path":f"attachments/{digest}.{fmt}","sha256":digest,"byte_size":size,"partial":False,
          "for_checks":[check["id"]],"source_url":url,"final_url":final,"title":title,"role":role,
          "attachment_id":item,"page_id":page_id,"captured_at":fetched,**archive_meta}
    return meta,data


def _part(archive,name,budget):
    info=archive.getinfo(name)
    if info.file_size>budget[0]:
        raise ValueError("Распакованный XML превышает лимит 32 МиБ")
    budget[0]-=info.file_size
    return ElementTree.fromstring(archive.read(info),forbid_dtd=True)


def _local(node):
    return node.tag.rsplit("}",1)[-1]


def _word_text(root):
    lines=[]
    for p in root.iter():
        if _local(p)=="p":
            line="".join(n.text or "" for n in p.iter() if _local(n)=="t")
            if line.strip():
                lines.append(line)
    return "\n".join(lines)


def _xlsx_text(archive,budget):
    from openpyxl import load_workbook
    if sum(item.file_size for item in archive.infolist() if item.filename.endswith((".xml",".rels")))>budget[0]:
        raise ValueError("Распакованный XML таблицы превышает лимит 32 МиБ")
    # With data_only=True only the saved result of a formula is read. Number formats
    # distinguish a time of day or date from its underlying Excel serial value.
    with closing(load_workbook(archive.fp,read_only=True,data_only=True,keep_links=False)) as book:
        lines=[]
        for sheet in book.worksheets:
            sheet.reset_dimensions()
            lines.append("Лист "+sheet.title)
            for row in sheet.rows:
                values=[f"{cell.coordinate}={cell.value}" for cell in row if cell.value is not None]
                if values:
                    lines.append(" | ".join(values))
        return "\n".join(lines)


def office_text(data,fmt):
    """Extract passive document text. No macros, embedded objects, formula or external link evaluation."""
    if fmt in {"csv","txt"}:
        for encoding in ("utf-8-sig","cp1251"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        raise ValueError("Не определена кодировка текста")
    if fmt=="doc":
        from legacy_doc import LegacyDocError,extract_text
        try:
            return extract_text(data).text
        except LegacyDocError as exc:
            raise ValueError("Не удалось прочитать текст DOC: "+str(exc)) from exc
    if fmt=="xls":
        import xlrd
        try:
            with xlrd.open_workbook(file_contents=data,on_demand=True,ragged_rows=True) as book:
                lines=[]
                for name in book.sheet_names():
                    sheet=book.sheet_by_name(name)
                    lines.append("Лист "+name)
                    for row in range(sheet.nrows):
                        values=[]
                        for column,cell in enumerate(sheet.row(row)):
                            if cell.ctype==xlrd.XL_CELL_EMPTY:
                                continue
                            value=cell.value
                            if cell.ctype==xlrd.XL_CELL_DATE:
                                stamp=xlrd.xldate_as_datetime(value,book.datemode)
                                value=stamp.time().isoformat() if 0<=value<1 else stamp.isoformat(" ")
                            values.append(f"{xlrd.colname(column)}{row+1}={value}")
                        if values:
                            lines.append(" | ".join(values))
                    book.unload_sheet(name)
                return "\n".join(lines)
        except (xlrd.XLRDError,IndexError,OSError) as exc:
            raise ValueError("Не удалось прочитать таблицу XLS: "+str(exc)) from exc
    if fmt not in {"docx","xlsx","pptx","odt","ods","odp"}:
        raise ValueError(f"Для формата {fmt} нет извлечения текста")
    budget=[MAX_OFFICE_XML]
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if fmt=="docx":
                # Preserve row/column relationships when reviewing a timetable grid.
                document=_part(archive,"word/document.xml",budget)
                body=next((node for node in document.iter() if _local(node)=="body"),document)
                lines=[]
                for block in body:
                    if _local(block)=="tbl":
                        for row in (el for el in block if _local(el)=="tr"):
                            lines.append(" | ".join(_word_text(cell).replace("\n"," / ") for cell in row if _local(cell)=="tc"))
                    else:
                        lines.append(_word_text(block))
                return "\n".join(line for line in lines if line.strip())
            if fmt=="xlsx":
                return _xlsx_text(archive,budget)
            if fmt=="pptx":
                slides=sorted((name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml",name)),
                              key=lambda name:int(re.search(r"(\d+)\.xml$",name)[1]))
                return "\n".join(_word_text(_part(archive,name,budget)) for name in slides)
            root=_part(archive,"content.xml",budget)
            return "\n".join("".join(n.itertext()) for n in root.iter() if _local(n) in {"p","h"})
    except (zipfile.BadZipFile,KeyError,ElementTree.ParseError,DefusedXmlException,RuntimeError) as exc:
        raise ValueError("Невозможно прочитать структуру документа") from exc


def main():
    if __package__:
        from .config import connect_db
    else:
        from config import connect_db
    parser=argparse.ArgumentParser(description="Сохранить исходные файлы и сканы, опубликованные на страницах кейса")
    parser.add_argument("--role",action="append",default=[])
    parser.add_argument("--run",type=int,help="только снимки конкретного обхода")
    parser.add_argument("--page",type=int,help="только конкретный снимок")
    parser.add_argument("--new-urls-after",type=int,help="только URL, впервые встретившиеся после ID снимка")
    parser.add_argument("--workers",type=int,default=8,choices=range(1,33),metavar="1..32")
    parser.add_argument("--timeout",type=int,default=45)
    parser.add_argument("--max-mib",type=int,default=MAX_BYTES//1024**2,help="лимит размера одного оригинала")
    parser.add_argument("--max-attempts",type=int,help="пропустить привязки с указанным числом попыток")
    parser.add_argument("--limit",type=int)
    parser.add_argument("--retry-errors",action="store_true")
    parser.add_argument("--only-errors",action="store_true",help="повторить только ранее неудачные загрузки")
    parser.add_argument("--retry-transient",action="store_true",help="включить временные отказы после задержки")
    parser.add_argument("--index-only",action="store_true")
    args=parser.parse_args()
    if args.timeout<=0 or args.max_mib<=0 or args.limit is not None and args.limit<1:
        parser.error("Тайм-аут и лимит должны быть положительными")
    with closing(connect_db()) as conn:
        conn.executescript(ATTACHMENT_SCHEMA_SQL)
        with conn:
            print("indexed",index_attachments(conn,args.page,run_id=args.run),flush=True)
            if args.index_only:
                scope,arguments=_page_filter(args.page,args.run)
                _reuse_available(conn,"state='pending'"+scope,arguments)
        if not args.index_only:
            def progress(stats):
                if (stats["saved"]+stats["error"])%100==0:
                    print(stats,flush=True)
            print(download_pending(conn,workers=args.workers,timeout=args.timeout,roles=args.role,
                                   limit=args.limit,retry=args.retry_errors,progress=progress,
                                   run_id=args.run,page_id=args.page,max_bytes=args.max_mib*1024**2,
                                   only_errors=args.only_errors,retry_transient=args.retry_transient,
                                   new_urls_after=args.new_urls_after,max_attempts=args.max_attempts),flush=True)


if __name__=="__main__":
    main()
