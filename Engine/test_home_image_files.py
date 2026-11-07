import hashlib
import io
import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from home_image_files import HOME_IMAGE_SCHEMA_SQL, _allowed, _raster, archive_home_images, export_home_image
from test_browser_capture import server


class HomeImageFilesTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE page(id INTEGER PRIMARY KEY,url TEXT,date_loaded INTEGER);
            CREATE TABLE home_main_photo(page_id INTEGER PRIMARY KEY,image_url TEXT,status TEXT);
            CREATE TABLE home_canteen_photo(page_id INTEGER PRIMARY KEY,image_url TEXT,status TEXT);
            CREATE TABLE garden_building_photo(page_id INTEGER PRIMARY KEY,image_url TEXT,status TEXT);
        """ + HOME_IMAGE_SCHEMA_SQL)
        output = io.BytesIO()
        Image.new("RGB", (3, 2), (21, 141, 255)).save(output, format="PNG")
        self.png = output.getvalue()
        self.png_route = (200, {"Content-Type": "application/octet-stream"}, self.png)

    def page(self, url, main, canteen=None):
        with self.db:
            pid = self.db.execute("INSERT INTO page(url,date_loaded) VALUES (?,1)", (url,)).lastrowid
            self.db.execute("INSERT INTO home_main_photo VALUES (?,?,'candidate')", (pid, main))
            self.db.execute("INSERT INTO home_canteen_photo VALUES (?,?,'ambiguous')", (pid, canteen))
        return pid

    def test_shared_bytes_across_snapshots_and_categories(self):
        routes = {"/photo": self.png_route, "/renamed": (302, {"Location": "/photo"}, b"")}
        with server(routes) as (site, calls):
            pid = self.page(site + "/", "/photo", "/photo")
            self.assertEqual(archive_home_images(self.db, pid), 1)
            self.assertEqual(archive_home_images(self.db, pid), 1)
            other = self.page(site + "/", "/renamed")
            self.assertEqual(archive_home_images(self.db, other), 1)
            self.assertEqual((calls["/photo"], calls["/renamed"]), (2, 1))
        self.assertEqual(self.db.execute("SELECT sha256,mime,width,height FROM home_image_asset").fetchall(),
                         [(hashlib.sha256(self.png).hexdigest(), "image/png", 3, 2)])
        url, status, fetched_at = self.db.execute(
            "SELECT final_url,status,fetched_at FROM home_image_download WHERE page_id=?", (other,)
        ).fetchone()
        self.assertEqual((url, status), (site + "/photo", "saved"))
        self.assertGreater(fetched_at, 1)
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            path = Path(tmp.name)
        path.unlink()
        try:
            self.assertEqual(export_home_image(self.db, pid, "canteen_photo", path), path)
            self.assertEqual(path.read_bytes(), self.png)
            with self.assertRaises(FileExistsError):
                export_home_image(self.db, pid, "main_photo", path)
        finally:
            path.unlink(missing_ok=True)

    def test_http_failure_can_be_retried_html_is_preserved(self):
        routes = {}
        with server(routes) as (site, calls):
            pid = self.page(site + "/", "/later")
            self.assertEqual(archive_home_images(self.db, pid), 0)
            self.assertEqual(self.db.execute("SELECT status,http_status,sha256 FROM home_image_download").fetchone(),
                             ("error", 404, None))
            routes["/later"] = self.png_route
            self.assertEqual(archive_home_images(self.db, pid), 1)
            self.assertEqual(calls["/later"], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM page").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT status,error FROM home_image_download").fetchall(),
                         [("saved", None)])

    def test_invalid_content_limits_and_redirect_restrictions(self):
        routes = {"/not-image": (200, {"Content-Type": "image/jpeg"}, b"<svg><script/></svg>"),
                  "/offsite": (302, {"Location": "https://other-school.example/picture.jpg"}, b""),
                  "/large": self.png_route}
        with server(routes) as (site, _):
            p = self.page(site, "/not-image", "/offsite")
            self.assertEqual(archive_home_images(self.db, p), 0)
            bad = dict(self.db.execute("SELECT image_url,error FROM home_image_download"))
            self.assertIn("растровое", bad["/not-image"])
            self.assertIn("предел", bad["/offsite"])
            p = self.page(site, "/large")
            with patch("home_image_files._MAX_PIXELS", 5):
                self.assertEqual(archive_home_images(self.db, p), 0)
                self.assertIn("пиксел", self.db.execute("SELECT error FROM home_image_download WHERE page_id=?",
                                                        (p,)).fetchone()[0])
            with patch("home_image_files._MAX_BYTES", len(self.png) - 1):
                self.assertEqual(archive_home_images(self.db, p), 0)
            self.assertEqual(self.db.execute("SELECT count(*) FROM home_image_asset").fetchone()[0], 0)
        self.assertTrue(_allowed("https://static.gosweb.gosuslugi.ru/a.png", "https://school.example/"))
        self.assertFalse(_allowed("https://static.gosweb.gosuslugi.ru.example/", "https://school.example/"))
        self.assertFalse(_allowed("https://user@media.gosweb.gosuslugi.ru/", "https://school.example/"))

    def test_no_network_during_transaction_or_for_empty_candidate(self):
        pid = self.page("https://school.example/", None, "")
        with patch("home_image_files._download") as downloader:
            self.assertEqual(archive_home_images(self.db, pid), 0)
            self.db.execute("BEGIN")
            with self.assertRaises(ValueError):
                archive_home_images(self.db, pid)
            self.db.rollback()
            downloader.assert_not_called()

    def test_jpeg_draft_validates_within_budget_but_preserves_source_dimensions(self):
        stream = io.BytesIO()
        Image.new("RGB", (96, 64)).save(stream, "JPEG")
        with patch("home_image_files._MAX_PIXELS", 140):
            self.assertEqual(_raster(stream.getvalue()), ("image/jpeg", 96, 64))
            with patch("home_image_files._MAX_JPEG_SOURCE_PIXELS", 400):
                with self.assertRaisesRegex(ValueError, "исходных пикселей"):
                    _raster(stream.getvalue())
            stream = io.BytesIO()
            Image.new("RGB", (96, 64)).save(stream, "JPEG", progressive=True)
            with self.assertRaisesRegex(ValueError, "Прогрессивный JPEG"):
                _raster(stream.getvalue())

    def test_mpo_validates_every_frame_with_a_fresh_jpeg_decoder(self):
        stream = io.BytesIO()
        Image.new("RGB", (96, 64)).save(stream, "MPO", save_all=True, append_images=[Image.new("RGB", (64, 64))])
        data = stream.getvalue()
        with patch("home_image_files._MAX_PIXELS", 180):
            self.assertEqual(_raster(data), ("image/jpeg", 96, 64))
            with self.assertRaises(ValueError):
                _raster(data[:-16])
        with patch("home_image_files._MAX_FRAMES", 1), self.assertRaisesRegex(ValueError, "лимит кадров"):
            _raster(data)

    def test_unexpected_decoder_format_returns_a_diagnostic(self):
        with patch.object(Image, "open", return_value=nullcontext(SimpleNamespace(format="unexpected"))):
            with self.assertRaisesRegex(ValueError, "Неизвестный формат"):
                _raster(b"content")


if __name__ == "__main__":
    unittest.main()
