import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_access import archive_access
from attached_files import _asset_path,asset_root,attachment_questions,attachment_content,download_pending
from config import SCHEMA_SQL
from home_image_files import _Download,archive_image_urls
from render_store import render_info
from retention import get_policy,maintain,retired_for_page,save_policy


DAY=86400
NOW=400*DAY


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.folder=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db=sqlite3.connect(self.folder/"test.sqlite3")
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("INSERT INTO organization VALUES (1,'Один','https://one.example','school')")
        self.db.execute("INSERT INTO organization VALUES (2,'Два','https://two.example','school')")
        self.page(1,"/team",1)
        self.page(2,"/team",350)
        self.db.commit()

    def page(self,pid,url,day,org=1):
        self.db.execute("INSERT INTO page VALUES(?,?,?,?,?)",(pid,"https://example"+url,org,day*DAY,"html"+str(pid)))

    def render(self,pid,day,pdf=b"pdf",full=b"full"):
        self.db.execute("""INSERT INTO page_render(page_id,captured_at,url,status,viewport_width,viewport_height,
            details,rendered_html,pdf,full_screenshot) VALUES(?,?,'https://example','complete',800,600,'{}','DOM',?,?)""",
            (pid,day*DAY,pdf,full))

    def attachment(self,pid,url,data,day,role="catalog"):
        sha=hashlib.sha256(data).hexdigest()
        path=_asset_path(asset_root(self.db),sha,"pdf")
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        self.db.execute("""INSERT INTO page_attachment(page_id,url,title,role,format_hint,state,fetched_at,final_url,
            http_status,format,sha256,byte_size) VALUES(?,?,'Файл',?,'pdf','saved',?,?,200,'pdf',?,?)""",
            (pid,url,role,day*DAY,url,sha,len(data)))
        return path

    def image(self,pid,url,data,day):
        sha=hashlib.sha256(data).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO home_image_asset VALUES(?,'image/png',100,100,?)",(sha,data))
        self.db.execute("INSERT INTO home_image_download VALUES(?,?,?, ?,200,'saved',NULL,?)",(pid,url,DAY*day,url,sha))
        return sha

    def enable(self):
        self.db.commit()
        save_policy(self.db,{"enabled":True,"days":180})

    def test_disabled_preview_preserves_html_and_only_replaced_kind_is_released(self):
        self.render(1,1)
        self.render(2,350,full=None)
        self.page(3,"/other",1)
        self.render(3,1)
        self.page(4,"/other",350,2)
        self.render(4,350)
        self.db.commit()
        self.assertEqual(get_policy(self.db),{"enabled":False,"days":180})
        preview=maintain(self.db,now=NOW)
        self.assertEqual((preview["snapshots"],preview["render_bytes"]),(1,3))
        self.assertFalse(maintain(self.db,apply=True,now=NOW)["applied"])
        self.assertEqual(retired_for_page(self.db,1),[])
        self.enable()
        result=maintain(self.db,apply=True,now=NOW)
        self.assertTrue(result["applied"])
        self.assertEqual(self.db.execute("SELECT rendered_html,pdf,full_screenshot FROM page_render WHERE page_id=1").fetchone(),
                         ("DOM",None,b"full"))
        self.assertEqual(render_info(self.db,1)["retired_artifacts"][0]["replacement_page_id"],2)
        self.assertEqual(self.db.execute("SELECT text_page FROM page WHERE id=1").fetchone()[0],"html1")
        self.assertEqual(maintain(self.db,apply=True,now=NOW)["materials"],0)

    def test_model_factcheck_and_pending_download_protect_their_whole_snapshot(self):
        for i in range(4):
            self.page(10+i,"/evidence",i+1)
            self.render(10+i,i+1)
        self.page(20,"/evidence",350)
        self.render(20,350)
        self.db.execute("""INSERT INTO model_assessment(page_id,check_id,provider,endpoint,model,outcome,request,elapsed_seconds)
                         VALUES(10,'check','p','e','m','uncertain','{}',1)""")
        self.db.execute("""INSERT INTO factcheck_run(page_id,checked_at,query,minimum_sources,source_count,status,claim,searches)
                         VALUES(11,40,'event',2,0,'review','{}','[]')""")
        self.db.execute("INSERT INTO page_attachment(page_id,url,title,role,format_hint) VALUES(12,'https://f','','catalog','pdf')")
        self.enable()
        result=maintain(self.db,apply=True,now=NOW)
        self.assertEqual(result["snapshots"],1)
        self.assertEqual(self.db.execute("SELECT DISTINCT page_id FROM retention_item").fetchall(),[(13,)])

    def test_sharing_rehydration_and_expired_binding_cannot_be_reused(self):
        url="https://one.example/current.pdf"
        self.page(3,"/another",10)
        old=b"%PDF-old"
        shared=self.attachment(1,url,old,1)
        self.attachment(3,"https://two.example/shared.pdf",old,10)
        self.attachment(2,url,b"%PDF-new",350)
        img="https://one.example/portrait.png"
        sha=self.image(1,img,b"old image",1)
        self.image(2,img,b"new image",350)
        self.enable()
        stat=maintain(self.db,apply=True,now=NOW)
        self.assertEqual((stat["image_bytes"],stat["files"]),(9,0))
        self.assertTrue(shared.exists())
        check=attachment_questions(self.db,1)[0]
        self.assertEqual(check["attachment"]["state"],"retired")
        self.assertIsNone(attachment_content(self.db,1,check))
        self.assertEqual(attachment_content(self.db,3,attachment_questions(self.db,3)[0])[1],old)
        self.assertEqual(self.db.execute("SELECT data FROM home_image_asset WHERE sha256=?",(sha,)).fetchone()[0],b"")
        self.page(4,"/new",370)
        self.db.commit()
        archive_image_urls(self.db,4,[img],5,download=lambda *args:_Download(img,200,None,b"old image","image/png",100,100))
        self.assertEqual(self.db.execute("SELECT data FROM home_image_asset WHERE sha256=?",(sha,)).fetchone()[0],b"old image")
        self.assertEqual(self.db.execute("SELECT page_id FROM available_home_image_download WHERE sha256=?",(sha,)).fetchall(),[(4,)])
        # If the live replacement is ineligible for this older snapshot, the retired binding
        # must not satisfy a retry, even when another owner retained the same physical SHA.
        self.db.execute("UPDATE page_attachment SET fetched_at=0 WHERE page_id=2")
        self.page(5,"/more",1)
        self.db.execute("INSERT INTO page_attachment(page_id,url,title,role,format_hint) VALUES(5,?,'','catalog','pdf')",(url,))
        self.db.commit()
        with patch("attached_files._download",return_value=("error",NOW,url,404,None,None,None,"HTTP 404")) as fetch:
            download_pending(self.db,page_id=5)
            self.assertEqual(fetch.call_count,1)

    def test_safe_disk_release_and_wrong_hash_is_retained(self):
        url="https://one.example/assets/a.pdf"
        old=self.attachment(1,url,b"%PDF-old",1)
        fresh=self.attachment(2,url,b"%PDF-new",350)
        unknown=old.parent/"lecture.pdf"
        unknown.write_bytes(b"private file")
        self.enable()
        with archive_access(self.db):
            self.assertTrue(maintain(self.db,apply=True,now=NOW)["busy"])
        fresh.write_bytes(b"%PDF-bad")
        self.assertEqual(maintain(self.db,apply=True,now=NOW)["materials"],0)
        fresh.write_bytes(b"%PDF-new")
        old.write_bytes(b"%PDF-oth")
        result=maintain(self.db,apply=True,now=NOW)
        self.assertEqual(result["materials"],0)
        self.assertEqual(retired_for_page(self.db,1),[])
        self.assertEqual(old.read_bytes(),b"%PDF-oth")
        self.assertTrue(unknown.exists())
        old.write_bytes(b"%PDF-old")
        # A crash/denied unlink after committing receipts leaves a recoverable outbox.
        with patch("pathlib.Path.unlink",side_effect=PermissionError("busy")):
            result=maintain(self.db,apply=True,now=NOW)
        self.assertTrue(result["errors"])
        self.assertTrue(old.exists())
        result=maintain(self.db,apply=True,now=NOW)
        self.assertEqual(result["removed_disk_bytes"],8)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
