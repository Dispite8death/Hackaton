import hashlib
import io
import sqlite3
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch
from uuid import uuid4

from PIL import Image

import loadPage
import revisit_pages
import staff_photo_assets as photos
from config import SCHEMA_SQL
from home_image_files import _Download
from http_page import PageResponse, public_connection


SITE = "https://school.example/"
PORTRAIT = SITE+"img/head.jpg"
CARD = """<a class="gw-person-card" href="/person/director">
    <img src="/img/head.jpg" alt="Иванова Анна Ивановна">
    <div class="gw-person-card__name">Иванова Анна Ивановна</div>
    <div class="gw-person-card__post">Директор</div></a>"""


class StaffPhotoTest(unittest.TestCase):
    def setUp(self):
        self.uri = f"file:{uuid4().hex}?mode=memory&cache=shared"
        self.db = self.connect()
        self.addCleanup(self.db.close)
        self.db.executescript(SCHEMA_SQL)
        with self.db:
            self.db.execute("INSERT INTO organization VALUES(1,'Школа',?,'school')", (SITE,))
            self.db.execute("INSERT INTO page VALUES (90,?,1,100,?)", (SITE,CARD))
        output = io.BytesIO()
        Image.new("RGB", (32,28), "blue").save(output,"JPEG")
        self.jpeg = output.getvalue()
        self.digest = hashlib.sha256(self.jpeg).hexdigest()

    def connect(self):
        conn = sqlite3.connect(self.uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def observation(self, oid, url, state="linked"):
        with self.db:
            self.db.execute("""INSERT INTO staff_observation
                (id,page_id,card_selector,is_head,photo_url,photo_state,method)
                VALUES(?,90,?,1,?,?,'template')""", (oid,f"div:nth-child({oid})",url,state))

    def success(self, url, *args):
        return _Download(url,200,data=self.jpeg,mime="image/jpeg",width=32,height=28)

    def test_explicit_selection_deduplicates_and_preserves_observation_identity(self):
        self.observation(1,PORTRAIT)
        self.observation(2,PORTRAIT)
        self.observation(3,SITE+"img/other.jpg")
        with patch.object(photos,"_download_portrait",side_effect=self.success) as download:
            rows = photos.archive_staff_photos(self.db,90,observation_ids=[1,2])
            self.assertEqual([(r["observation_id"],r["sha256"]) for r in rows],[(1,self.digest),(2,self.digest)])
            photos.archive_staff_photos(self.db,90,observation_ids=[1])
            download.assert_called_once()
            with self.assertRaisesRegex(ValueError, "не принадлежит"):
                photos.archive_staff_photos(self.db,90,observation_ids=[1,99])
        self.assertEqual(tuple(self.db.execute("SELECT page_id,image_url FROM home_image_download").fetchone()), (90,PORTRAIT))

    def test_rejects_generic_portrait_placeholder_and_outside_host_before_network(self):
        self.observation(1,"https://static.gosweb.gosuslugi.ru/school/img/fake-portrait.jpg?key=1")
        self.observation(2,SITE+"img/normal.jpg","placeholder")
        self.observation(3,"https://evil.example/image")
        self.observation(4,"http://127.0.0.1/private")
        self.observation(5,None,"absent")
        self.observation(6,"https://user:password@school.example/image")
        with patch.object(photos,"_download_portrait") as download:
            rows = photos.archive_staff_photos(self.db,90)
            self.assertEqual([r["status"] for r in rows], ["placeholder","placeholder","unsafe_url","unsafe_url","absent","unsafe_url"])
            download.assert_not_called()
        self.assertEqual(self.db.execute("SELECT count(*) FROM home_image_download").fetchone()[0],0)

    def test_redirect_and_dns_are_checked_before_reading(self):
        response = MagicMock(status=302)
        response.getheader.return_value = "https://outside.example/image"
        conn = MagicMock()
        conn.getresponse.return_value.__enter__.return_value = response
        with patch.object(photos,"public_connection",return_value=conn) as open_connection:
            result = photos._download_portrait(PORTRAIT,SITE,5)
            self.assertEqual((result.error,result.data),("unsafe_url",None))
            self.assertEqual(open_connection.call_count,1)
            response.read1.assert_not_called()
        import socket
        private = [(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]
        with patch("socket.getaddrinfo",return_value=private),patch("socket.socket") as connect:
            with self.assertRaisesRegex(ValueError,"общедоступным"):
                public_connection("school.example",443,tls=True,timeout=2)
            connect.assert_not_called()

    def test_error_can_be_retried_without_losing_html_date(self):
        self.observation(1,PORTRAIT)
        with patch.object(photos,"_download_portrait",side_effect=[_Download(PORTRAIT,404,"HTTP 404"),self.success(PORTRAIT)]):
            self.assertEqual(photos.archive_staff_photos(self.db,90)[0]["status"],"error")
            result=photos.archive_staff_photos(self.db,90)[0]
            self.assertEqual(result["status"],"saved")
            self.assertGreater(result["fetched_at"],100)
        self.assertEqual(self.db.execute("SELECT date_loaded FROM page WHERE id=90").fetchone()[0],100)

    def test_normal_crawl_and_revisit_archive_each_new_snapshot(self):
        browser = MagicMock()
        browser.fetch.return_value=PageResponse(SITE,200,CARD,loaded_at=123)
        with patch.object(loadPage,"connect_db",self.connect), patch("browser_capture.BrowserCapture",
                return_value=nullcontext(browser)), patch.object(photos,"_download_portrait",side_effect=self.success) as requests:
            org = self.db.execute("SELECT * FROM organization").fetchone()
            run = loadPage.crawl_organization(org,max_pages=1)
            pid = self.db.execute("SELECT page_id FROM crawl_fetch WHERE run_id=?",(run.run_id,)).fetchone()[0]
            report = revisit_pages.revisit_pages(self.db,[pid],capture_factory=lambda **kwargs:nullcontext(browser),
                                                 archive_staff=photos.archive_staff_photos)
            self.assertEqual(requests.call_count,2)
        rows=[tuple(r) for r in self.db.execute("""SELECT d.page_id,s.photo_url,d.sha256 FROM staff_observation s
            JOIN home_image_download d ON s.page_id=d.page_id AND s.photo_url=d.image_url ORDER BY d.page_id""")]
        self.assertEqual(rows,[(pid,PORTRAIT,self.digest),(report[0]["page_id"],PORTRAIT,self.digest)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM home_image_asset").fetchone()[0],1)
