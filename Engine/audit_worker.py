"""A bounded, unattended cycle over the archive. Each stage commits its own evidence."""

import argparse
import json
import os
import threading
import time
from contextlib import closing, contextmanager
from importlib import import_module
from pathlib import Path


DEFAULTS = {
    "interval_minutes":60, "revisit_days":7, "organizations_per_tick":1,
    "pages_per_org":16, "max_depth":3, "frontier_pages":6, "attachment_files":12,
    "model_checks":4, "factcheck_items":2, "news_months":12,
}
_MEMORY_LOCK = threading.Lock()


def _engine(module):
    """Delay project imports until the CLI has selected its database."""
    return import_module("."+module,__package__) if __package__ else import_module(module)


def _validated(values):
    settings=dict(DEFAULTS)
    for name,value in values.items():
        if name not in settings:
            raise ValueError(f"Неизвестная настройка цикла: {name}")
        if name=="news_months" and (type(value) is not int or not 1<=value<=1200):
            raise ValueError("Глубина архива новостей должна быть от 1 до 1200 календарных месяцев")
        minimum=1 if name in {"interval_minutes","revisit_days","pages_per_org"} else 0
        if type(value) is not int or not minimum<=value<2**31:
            raise ValueError(f"{name}: требуется целое число от {minimum}")
        settings[name]=value
    return settings


def get_settings(conn):
    return _validated(_engine("model_settings").setting(conn,"audit_worker_settings",{}))


def save_settings(conn, values):
    settings=_validated({**get_settings(conn),**values})
    with conn:
        _engine("model_settings").set_setting(conn,"audit_worker_settings",settings)
    return settings


def last_status(conn):
    row=conn.execute("""SELECT id,started_at,finished_at,state,stage,settings,stages FROM audit_worker_cycle
                        ORDER BY id DESC LIMIT 1""").fetchone()
    if not row:
        return None
    return {"id":row[0],"started_at":row[1],"finished_at":row[2],"state":row[3],"stage":row[4],
            "settings":json.loads(row[5]),"stages":json.loads(row[6])}


@contextmanager
def _cycle_lock(conn):
    # File ownership, including release on process termination, belongs to the OS.
    db=conn.execute("PRAGMA database_list").fetchone()[2]
    if not db:
        acquired=_MEMORY_LOCK.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                _MEMORY_LOCK.release()
        return
    path=Path(db)
    with path.with_name("."+path.name+".audit-worker.lock").open("a+b") as f:
        if os.name=="nt":
            import msvcrt
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True


def _crawl(conn, settings, refresh_now):
    crawl_organization=_engine("loadPage").crawl_organization
    cadence=settings["revisit_days"]*86400
    retry=min(cadence,settings["interval_minutes"]*60)
    rows=conn.execute("""SELECT o.* FROM organization o LEFT JOIN crawl_run r ON r.id=(
        SELECT max(id) FROM crawl_run WHERE id_organization=o.id AND purpose='audit')
        WHERE (r.id IS NULL OR (r.state!='running' OR r.started_at<unixepoch()-86400)
            AND (? OR coalesce(r.finished_at,r.started_at) + CASE WHEN r.state IN ('failed','interrupted')
                                     THEN ? ELSE ? END <=unixepoch()))
        AND NOT EXISTS(SELECT 1 FROM material_backfill b WHERE b.organization_id=o.id
                       AND b.state='working' AND b.lease_until>unixepoch())
        ORDER BY coalesce(r.finished_at,r.started_at,0),o.id LIMIT ?""",
        (int(refresh_now),retry,cadence,settings["organizations_per_tick"])).fetchall()
    visits=[]
    errors=0
    for org in rows:
        try:
            result=crawl_organization(org,max_pages=settings["pages_per_org"],max_depth=settings["max_depth"],
                                      archive_files=False,db=conn,news_months=settings["news_months"])
            visits.append({"organization_id":org["id"],"run_id":result.run_id,"pages":result.loaded,
                           "errors":result.errors,"state":result.state,"stop_reason":result.reason})
            errors+=result.errors
        except Exception as error:
            conn.rollback()
            visits.append({"organization_id":org["id"],"error":str(error)[:600]})
            errors+=1
    return {"processed":len(visits),"visits":visits,"errors":errors}


def _frontier(conn, settings):
    backfill=_engine("backfill_materials")
    stored=_engine("model_settings")
    resolve_url=_engine("navigation").resolve_url
    previous=stored.setting(conn,"audit_frontier_last_page",0)
    window_day=_engine("news_window").bounds(settings["news_months"])[1]
    window_key=[settings["news_months"],window_day]
    if stored.setting(conn,"audit_frontier_news_window",None)!=window_key:
        previous=0
    last=conn.execute("SELECT coalesce(max(id),0) FROM page").fetchone()[0]
    orgs=[(org,resolve_url(site,""),kind) for org,site,kind in conn.execute("""SELECT o.id,o.site_url,o.kind
        FROM organization o WHERE EXISTS(SELECT 1 FROM page p WHERE p.id_organization=o.id AND p.id>? AND p.id<=?)
        OR EXISTS(SELECT 1 FROM material_backfill b WHERE b.organization_id=o.id AND b.state='working'
                  AND b.lease_until<=unixepoch())
        ORDER BY o.id""",(previous,last))]
    if previous==0:
        backfill.seed(conn,orgs,news_months=settings["news_months"])
    backfill.seed(conn,orgs,since=int(time.time())-settings["revisit_days"]*86400,news_months=settings["news_months"])
    with conn:
        stored.set_setting(conn,"audit_frontier_last_page",last)
        stored.set_setting(conn,"audit_frontier_news_window",window_key)
        # Some transport failures are temporary; keep the previous attempt before retrying.
        conn.execute("""INSERT OR IGNORE INTO material_backfill_visit
            SELECT fetch_id,source_page_id,relation FROM material_backfill WHERE fetch_id IS NOT NULL""")
        conn.execute("""UPDATE material_backfill SET state='pending',run_id=NULL,fetch_id=NULL,lease_until=NULL
            WHERE state='failed' AND EXISTS (SELECT 1 FROM crawl_fetch f WHERE f.id=material_backfill.fetch_id
                AND (f.http_status IS NULL OR f.http_status IN (408,425,429) OR f.http_status BETWEEN 500 AND 599)
                AND f.fetched_at+min(86400,300*(1 << min(8,(SELECT count(*)-1 FROM material_backfill_visit v
                    JOIN crawl_fetch a ON a.id=v.fetch_id JOIN crawl_run ar ON ar.id=a.run_id
                    WHERE ar.id_organization=material_backfill.organization_id AND a.url=material_backfill.url
                    AND a.page_id IS NULL))))<=unixepoch())""")
    # Fairly distribute a tick across institutions at the same priority.
    selection=conn.execute("""SELECT o.id,o.site_url,o.kind,min(b.priority) AS rank FROM material_backfill b
        JOIN organization o ON o.id=b.organization_id WHERE b.state='pending' AND coalesce(b.lease_until,0)<=unixepoch()
        AND NOT EXISTS (SELECT 1 FROM material_backfill w WHERE w.organization_id=b.organization_id AND w.state='working')
        GROUP BY o.id ORDER BY rank,(SELECT count(*) FROM material_backfill h WHERE h.organization_id=o.id
        AND h.state IN ('saved','failed')),o.id LIMIT ?""",(settings["frontier_pages"],)).fetchall()
    executed=0
    visits=[]
    for i,(oid,site,kind,_) in enumerate(selection):
        take=max(1,(settings["frontier_pages"]-executed)//(len(selection)-i))
        try:
            executed+=backfill.process_org((oid,resolve_url(site,""),kind),take,25,15,db=conn,runs=visits,
                                           news_months=settings["news_months"])
        except Exception as exc:
            conn.rollback()
            visits.append({"organization_id":oid,"errors":1,"error":str(exc)[:600]})
    counts=dict(conn.execute("SELECT state,count(*) FROM material_backfill GROUP BY state"))
    return {"processed":executed,"indexed_organizations":len(orgs),"visits":visits,
            "errors":sum(v["errors"] for v in visits),"frontier":counts}


def _attachments(conn, settings):
    files=_engine("attached_files")
    with conn:
        indexed=files.index_attachments(conn)
    stats=files.download_pending(conn,limit=settings["attachment_files"],workers=2,retry_transient=True)
    queued=_engine("backfill_materials").queue_failed_document_cards(conn)
    return {"indexed":indexed,"recovery_pages_queued":queued,**stats}


def _models(conn, settings):
    profiles=_engine("model_settings")
    profile=profiles.get_profile(conn)
    ready=_engine("local_model_service").prepare_local_service(profile,key=profiles.active_secret(conn,profile))
    if not ready["ready"]:
        return {"processed":0,"service":ready,"stop_reason":ready["detail"]}
    result=_engine("model_batch").batch_assess(conn,limit=settings["model_checks"],include_photos=True,rotate=True,
                                              latest_only=True)
    return {**result,"service":ready}


def _factcheck(conn, settings):
    if not _engine("model_settings").setting(conn,"factcheck_auto",False):
        return {"processed":0,"enabled":False}
    runs=[row[0] for row in conn.execute("""SELECT DISTINCT f.run_id FROM crawl_fetch f JOIN page_passport p
               ON p.page_id=f.page_id WHERE p.page_kind IN ('news_detail','event_detail') ORDER BY f.run_id""")]
    result=_engine("factcheck_batch").batch_check(conn,runs,limit=settings["factcheck_items"])
    return {key:value for key,value in result.items() if not key.endswith("_ids") or key=="processed_ids"}


def _retention(conn, settings):
    retention=_engine("retention")
    if not retention.get_policy(conn)["enabled"]:
        return {"state":"disabled","processed":0}
    return retention.maintain(conn,apply=True,limit=200)


def run_once(conn, *, overrides=None, refresh_now=False):
    """Use the supplied database in every stage, including nested browser visits."""
    settings=_validated({**get_settings(conn),**(overrides or {})})
    with _cycle_lock(conn) as acquired:
        if not acquired:
            return {"state":"busy","stages":{}}
        with conn:
            conn.execute("""UPDATE audit_worker_cycle SET state='interrupted',finished_at=unixepoch()
                            WHERE state='running'""")
            cycle=conn.execute("""INSERT INTO audit_worker_cycle(state,settings,stages)
                                  VALUES('running',?,'{}')""",(json.dumps(settings),)).lastrowid
        state="completed"
        stages={}
        steps=(("crawl","organizations_per_tick",lambda c,s:_crawl(c,s,refresh_now)),
               ("frontier","frontier_pages",_frontier),("attachments","attachment_files",_attachments),
               ("factcheck","factcheck_items",_factcheck),("models","model_checks",_models),
               ("retention",None,_retention))
        for name,budget,action in steps:
            with conn:
                conn.execute("UPDATE audit_worker_cycle SET stage=? WHERE id=?",(name,cycle))
            started=time.monotonic()
            try:
                value=action(conn,settings) if budget is None or settings[budget] else {"state":"disabled","processed":0}
                if value.get("errors") or value.get("error") or value.get("stop_reason"):
                    state="partial"
            except (KeyboardInterrupt,SystemExit):
                conn.rollback()
                stages[name]={"state":"interrupted","seconds":round(time.monotonic()-started,3)}
                with conn:
                    conn.execute("""UPDATE audit_worker_cycle SET state='interrupted',finished_at=unixepoch(),
                        stages=? WHERE id=?""",(json.dumps(stages,ensure_ascii=False),cycle))
                raise
            except Exception as exc:
                conn.rollback()
                state="partial"
                value={"error":f"{type(exc).__name__}: {exc}"[:900]}
            value["seconds"]=round(time.monotonic()-started,3)
            stages[name]=value
            with conn:
                conn.execute("UPDATE audit_worker_cycle SET stages=? WHERE id=?",
                             (json.dumps(stages,ensure_ascii=False),cycle))
        with conn:
            conn.execute("UPDATE audit_worker_cycle SET state=?,stage=NULL,finished_at=unixepoch() WHERE id=?",(state,cycle))
        return last_status(conn)


def main(argv=None):
    parser=argparse.ArgumentParser(description="Автоматический цикл аудита школ и детских садов")
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once",action="store_true")
    mode.add_argument("--loop",action="store_true",help="периодический запуск в текущем процессе")
    mode.add_argument("--status",action="store_true")
    parser.add_argument("--db",type=Path,help="база проекта")
    parser.add_argument("--refresh-now",action="store_true",help="в первом цикле взять самые давно посещённые учреждения")
    for name in DEFAULTS:
        parser.add_argument("--"+name.replace("_","-"),type=int,default=None)
    args=parser.parse_args(argv)
    path=args.db.expanduser().resolve() if args.db else None
    if path:
        os.environ["HACKATON_DB_PATH"]=str(path)
    # Scheduled tasks can start in System32. All database and artifact paths follow --db.
    options={k:getattr(args,k) for k in DEFAULTS if getattr(args,k) is not None}
    with closing(_engine("config").connect_db(path)) as conn:
        _engine("crawl_store").initialize_db(conn)
        if args.status:
            print(json.dumps(last_status(conn),ensure_ascii=False))
            return 0
        refresh=args.refresh_now
        while True:
            report=run_once(conn,overrides=options,refresh_now=refresh)
            print(json.dumps(report,ensure_ascii=False),flush=True)
            if not args.loop:
                return int(report["state"]=="partial")
            refresh=False
            interval=options.get("interval_minutes",get_settings(conn)["interval_minutes"])
            time.sleep(interval*60)


if __name__=="__main__":
    raise SystemExit(main())
