import hashlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
import zipfile
from datetime import time as clock_time
from pathlib import Path
from unittest.mock import patch

import attached_files as files
from review_contract import allowed_material_ids,make_request


def package(entries):
    result=io.BytesIO()
    with zipfile.ZipFile(result,"w") as archive:
        for name,data in entries.items():
            archive.writestr(name,data)
    return result.getvalue()


class AttachmentsTest(unittest.TestCase):
    def setUp(self):
        self.folder=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db=sqlite3.connect(self.folder/"test.sqlite3")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE organization(id INTEGER PRIMARY KEY);
            CREATE TABLE inactive_organization(id INTEGER PRIMARY KEY);
            CREATE VIEW active_organization AS SELECT id FROM organization EXCEPT SELECT id FROM inactive_organization;
            CREATE TABLE page(id PRIMARY KEY,date_loaded INTEGER,id_organization INTEGER DEFAULT 1);
            CREATE TABLE crawl_fetch(page_id,run_id);
            CREATE TABLE institution_document_link(page_id,catalog,section,url,title,format);
            CREATE TABLE school_schedule_page(page_id,evidence);
            INSERT INTO organization VALUES (1);
            INSERT INTO page(id,date_loaded) VALUES(1,10),(2,20),(3,30);
            INSERT INTO crawl_fetch VALUES(1,1),(2,1),(3,2);
        """+files.ATTACHMENT_SCHEMA_SQL)
        self.url="https://school.example/docs/timetable"
        self.db.executemany("INSERT INTO institution_document_link VALUES(?,1,'documents',?,'Документ','pdf')",
                            ((pid,self.url) for pid in (1,2,3)))
        self.db.execute("INSERT INTO school_schedule_page VALUES(1,?)",(json.dumps({"materials":[
            {"url":self.url,"caption":"Расписание 1 класса","kind":"file"}]}),))

    def save(self,pid,data,fmt):
        sha=hashlib.sha256(data).hexdigest()
        path=files._asset_path(files.asset_root(self.db),sha,fmt)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        self.db.execute("""UPDATE page_attachment SET state='saved',fetched_at=25,sha256=?,format=?,byte_size=?,
                           final_url=url,http_status=200 WHERE page_id=?""",(sha,fmt,len(data),pid))
        self.db.commit()
        return path

    def test_removed_sites_keep_evidence_but_skip_network_queue(self):
        files.index_attachments(self.db, run_id=1)
        self.db.execute("INSERT INTO inactive_organization VALUES(1)")
        self.db.commit()
        with patch.object(files, "_download", side_effect=AssertionError("unexpected download")):
            self.assertEqual(files.download_pending(self.db)["total"], 0)
        self.assertGreater(self.db.execute("SELECT count(*) FROM page_attachment").fetchone()[0], 0)

    def test_run_filter_schedule_precedence_reuse_and_fresh_snapshot(self):
        self.assertGreater(files.index_attachments(self.db,run_id=1),0)
        self.assertEqual(files.index_attachments(self.db,run_id=1),0)
        self.assertEqual(files.attachment_questions(self.db,3),[])
        source=files.attachment_questions(self.db,1)[0]
        self.assertEqual(source["attachment"]["role"],"schedule")
        self.assertIsNone(files.attachment_content(self.db,1,source))
        self.save(1,b"%PDF-1.7\n","pdf")
        with patch.object(files,"_download",side_effect=AssertionError("unexpected download")):
            self.assertEqual(files.download_pending(self.db,run_id=1)["total"],0)
        q=files.attachment_questions(self.db,2)[0]
        meta,body=files.attachment_content(self.db,2,q)
        self.assertEqual((body,meta["captured_at"]),(b"%PDF-1.7\n",25))
        self.assertIsNone(files.attachment_content(self.db,1,q))
        self.assertEqual(meta["sha256"],q["attachment"]["sha256"])
        files.index_attachments(self.db,page_id=3)
        with patch.object(files,"_download",return_value=("error",31,self.url,404,None,None,None,"HTTP 404")) as req:
            stats=files.download_pending(self.db,page_id=3)
            self.assertEqual((stats["error"],req.call_count),(1,1))
        self.assertEqual(files.download_pending(self.db,page_id=3)["total"],0)

    def test_no_false_material_for_broken_source_or_other_question(self):
        files.index_attachments(self.db,page_id=1)
        stored=self.save(1,b"%PDF-1.7\n","pdf")
        check=files.attachment_questions(self.db,1)[0]
        self.assertIsNotNone(files.attachment_content(self.db,1,check))
        stored.write_bytes(b"x"*stored.stat().st_size)
        self.assertIsNone(files.attachment_content(self.db,1,check))
        stored.unlink()
        self.assertIsNone(files.attachment_content(self.db,1,check))

    def test_periodic_retry_waits_and_keeps_permanent_404_out_of_queue(self):
        files.index_attachments(self.db,run_id=1)
        with self.db:
            self.db.execute("UPDATE page_attachment SET state='error',http_status=404,error='HTTP 404',fetched_at=1,attempts=1 WHERE page_id=1")
            self.db.execute("UPDATE page_attachment SET state='error',http_status=503,error='HTTP 503',fetched_at=unixepoch(),attempts=1 WHERE page_id=2")
        with patch.object(files,"_download",return_value=("error",int(time.time()),self.url,503,None,None,None,"HTTP 503")) as req:
            self.assertEqual(files.download_pending(self.db,retry_transient=True)["total"],0)
            self.db.execute("UPDATE page_attachment SET fetched_at=1 WHERE page_id=2")
            self.assertEqual(files.download_pending(self.db,retry_transient=True)["total"],1)
        self.assertEqual(req.call_count,1)
        self.assertEqual(self.db.execute("SELECT attempts FROM page_attachment ORDER BY page_id").fetchall(),[(1,),(2,)])

    def test_two_workers_never_fetch_a_leased_url_twice(self):
        files.index_attachments(self.db,page_id=1)
        self.db.execute("INSERT INTO attachment_download_lease VALUES(?,'other-worker',unixepoch()+600)",(self.url,))
        self.db.commit()
        with patch.object(files,"_download",side_effect=AssertionError("URL already leased")):
            stats=files.download_pending(self.db)
            self.assertEqual((stats["saved"],stats["error"],stats["leased_elsewhere"]),(0,0,1))
        self.assertEqual(files.attachment_questions(self.db,1)[0]["attachment"]["state"],"pending")

    def test_members_are_separate_and_bound_to_individual_archived_bytes(self):
        files.index_attachments(self.db,page_id=1)
        data=package({"reading/grade-1.pdf":b"%PDF-1.7\nA","reading/grade-2.pdf":b"%PDF-1.7\nB",
                      "../ignore.exe":b"MZ","only-directory/":b""})
        self.save(1,data,"zip")
        parent,first,second=files.attachment_questions(self.db,1)
        m1,b1=files.attachment_content(self.db,1,first)
        m2,b2=files.attachment_content(self.db,1,second)
        self.assertNotEqual(m1["id"],m2["id"])
        self.assertEqual((b1,b2),(b"%PDF-1.7\nA",b"%PDF-1.7\nB"))
        self.assertEqual(m1["archive_sha256"],parent["attachment"]["sha256"])
        req=make_request({"organization_id":1,"run_id":1,"page_id":1},[first,second],[m1,m2])
        self.assertEqual(allowed_material_ids(req["checks"][0],req["materials"]),[m1["id"]])
        self.assertEqual(files.attachment_content(self.db,1,parent)[1],data)
        altered=dict(first,attachment=dict(first["attachment"],member_index=1))
        self.assertIsNone(files.attachment_content(self.db,1,altered))

    def test_stream_partial_html_and_traversal_cannot_become_saved_files(self):
        class Response(io.BytesIO):
            headers={}
        root=self.folder/"assets"
        for raw,length in ((b"<html>error</html>",None),(b"%PDF-partial","20"),(b"%PDF-oversized","1000000")):
            response=Response(raw)
            response.headers={"Content-Length":length} if length else {}
            with self.assertRaises(ValueError):
                files._save_response(response,root,self.url,"pdf",time.monotonic()+2,1000)
        self.assertEqual(list(root.rglob("*")),[])
        payload=b"%PDF-original"
        dest=files._asset_path(root,hashlib.sha256(payload).hexdigest(),"pdf")
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"custom file")
        with self.assertRaisesRegex(ValueError,"сумма"):
            files._save_response(Response(payload),root,self.url,"pdf",time.monotonic()+5,1000)
        self.assertEqual(dest.read_bytes(),b"custom file")
        with self.assertRaises(ValueError):
            files._asset_path(root,"../example","pdf")
        for bad_url in ("file:///tmp/a.pdf","http://name:password@school.example/a","https://school.example:9222/a"):
            with self.assertRaises(ValueError):
                files._request(bad_url,2)

    def test_passive_office_text_preserves_table_cells_and_ignores_html_and_dtd(self):
        content=package({"[Content_Types].xml":"", "word/document.xml":
            "<document><body><p><r><t>Расписание</t></r></p><tbl><tr><tc><p><t>1А</t></p></tc>"
            "<tc><p><t>09:00</t></p></tc></tr></tbl></body></document>"})
        self.assertEqual(files.identify(content,"pdf"),"docx")
        self.assertEqual(files.office_text(content,"docx"),"Расписание\n1А | 09:00")
        with self.assertRaises(ValueError):
            files.office_text(package({"word/document.xml":"<!DOCTYPE a><a/>"}),"docx")
        with self.assertRaises(ValueError):
            files.identify(b"<html>error</html>","docx")

    def test_excel_exposes_the_formatted_lesson_time_and_ignores_formula_code(self):
        from openpyxl import Workbook
        book=Workbook()
        sheet=book.active
        sheet.title="Расписание"
        sheet.append(["1А","Математика",clock_time(9,15),"=NOW()"])
        output=io.BytesIO()
        book.save(output)
        book.close()
        text=files.office_text(output.getvalue(),"xlsx")
        self.assertIn("C1=09:15:00",text)
        self.assertIn("B1=Математика",text)
        self.assertNotIn("NOW",text)
