"""Exercise both portal workers, including losing the entire server process."""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from urllib.request import ProxyHandler, build_opener

from Engine import crawl_store


class QuickCheckpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_second_site_resumes_and_empty_cache_preserves_report(self):
        from ai_audit_web import db, operations, reports
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(db, "DB_PATH", root / "quick.db"))
        self.enterContext(patch.object(operations.core, "CACHE_DIR", root / "cache"))
        db.init()
        with closing(db.conn()) as conn, conn:
            conn.executemany("INSERT INTO sites(name,url) VALUES(?,?)",
                             [("Первое","https://one.example"),("Второе","https://two.example")])
        sites = db.get_sites()
        success = {"score":80, "metrics":{"pages":1}, "findings":[]}
        async def wait_for_job():
            async with operations.audit_lock:
                return operations.status()
        with patch.object(operations.core, "audit_site", side_effect=[success, RuntimeError("Сбой"), success]) as audit:
            await operations.start("online", sites)
            job = await wait_for_job()
            self.assertEqual((job["state"], job["done"]), ("error",1))
            self.assertEqual(len(reports.latest()),1)
            await operations.start(resume_id=job["id"])
            self.assertEqual((await wait_for_job())["done"],2)
            self.assertEqual(audit.await_count,3)
        await operations.start("cache", sites)
        empty = await wait_for_job()
        self.assertEqual((empty["state"], len(empty["notes"])), ("completed",2))
        self.assertEqual(len(reports.latest()),2)


@unittest.skipUnless(os.environ.get("HACKATON_TEST_LOCAL_QWEN") == "1", "opt-in local Qwen at 127.0.0.1:1234")
class LocalModelJobsTest(unittest.IsolatedAsyncioTestCase):
    async def test_archive_answer_and_quick_report(self):
        import asyncio
        from Engine import config, model_settings, operation_log, portal_jobs, site_server
        from ai_audit_web import db, integration, operations

        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(config, "DB_PATH", str(root / "archive.db")))
        self.enterContext(patch.object(integration, "DATA_DIR", root))
        self.enterContext(patch.object(db, "DB_PATH", root / "quick.db"))
        site_server.prepare_database()
        integration.initialize()
        with closing(config.connect_db()) as conn:
            with conn:
                model_settings.set_setting(conn, "portal_ai_enabled", True)
                model_settings.save_profile(conn, model_settings.DEFAULTS["lmstudio"])
            run_id = crawl_store.start_run(conn, 1)
            url = "https://school.example/raspisanie/"
            crawl_store.record_fetch(conn, run_id, url, "schedule", 1, status=200, final_url=url,
                html="<main><h1>Расписание</h1><p>В понедельник математика в 09:00, чтение в 10:00.</p></main>")
            crawl_store.finish_run(conn, run_id, "completed", reason="fixture")
            preview = portal_jobs.preview(conn, [1], "ai")
            self.assertGreater(preview["ready"], 0)
            lock = Lock()
            portal_jobs.start(conn, [1], "ai", lock, preview["ticket"])
            self.assertTrue(await asyncio.to_thread(lock.acquire, timeout=240))
            lock.release()
            result = operation_log.status(conn)
            self.assertEqual(result["state"], "completed", result["detail"])
            self.assertGreater(result["processed"]["ai"], 0)
        site = db.get_sites()[0]
        run_id = db.start_run(1)
        db.save_site_result(run_id, site, {"score":92, "findings":[{"agent":"contacts","severity":"MEDIUM",
                             "message":"В сохранённом разделе контактов не найден номер телефона."}]})
        db.finish_run(run_id, "done", 92)
        result_id = db.get_last_site_result(site["id"])["id"]
        await operations.start("summary", [site], result_id=result_id)
        async with asyncio.timeout(240):
            while operations.audit_lock.locked():
                await asyncio.sleep(.1)
        self.assertEqual(operations.status()["state"], "completed", operations.status()["detail"])
        self.assertTrue(db.get_last_site_result(site["id"])["llm_summary"])


@unittest.skipUnless(os.environ.get("HACKATON_TEST_BROWSER") == "1", "opt-in portal browser test")
class PortalLifecycleTest(unittest.TestCase):
    def test_tab_close_server_restart_and_resume(self):
        from playwright.sync_api import expect
        from Engine.browser_capture import BrowserCapture

        root = Path(self.enterContext(TemporaryDirectory()))
        blocked = set()
        gate = Event()
        class Site(BaseHTTPRequestHandler):
            def handle(self):
                try:
                    super().handle()
                except ConnectionResetError:
                    pass  # The test kills a client during an in-flight visit.

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.rstrip("/") in blocked:
                    gate.wait(30)
                page = b"<html><body><h1>School</h1><p>Welcome to our school.</p></body></html>"
                self.send_response(200 if self.path.rstrip("/") in ("/one","/two") else 404)
                self.send_header("Content-Type","text/html")
                self.end_headers()
                try:
                    self.wfile.write(page)
                except OSError:
                    pass

        fixture = self.enterContext(ThreadingHTTPServer(("127.0.0.1",0),Site))
        Thread(target=fixture.serve_forever,daemon=True).start()
        self.addCleanup(fixture.shutdown)
        self.addCleanup(gate.set)
        path = root / "archive.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            crawl_store.initialize_db(conn)
            for index, route in enumerate(("/one","/two"),1):
                conn.execute("INSERT INTO organization(name,site_url,kind) VALUES(?,?,'school')",
                             ("Школа " + str(index), f"http://127.0.0.1:{fixture.server_port}{route}"))
        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1",0))
            port = sock.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        client = build_opener(ProxyHandler({}))
        def get(uri):
            with client.open(base+uri, timeout=3) as response:
                return json.load(response)

        def until(check, timeout=60):
            deadline = time.monotonic()+timeout
            while time.monotonic()<deadline:
                try:
                    if value := check():
                        return value
                except OSError:
                    pass
                time.sleep(.15)
            self.fail("Timed out waiting for portal state")

        def start():
            p = subprocess.Popen([sys.executable,"-m","Engine.site_server","--port",str(port)],
                env={**os.environ,"HACKATON_DB_PATH":str(path),"HACKATON_QUICK_DIR":str(root/"quick")},
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            def stop():
                if p.poll() is None:
                    p.kill()
                p.wait(10)
            self.addCleanup(stop)
            until(lambda:get("/healthz"))
            return stop

        stop = start()
        with BrowserCapture() as capture:
            browser = capture.browser
            page = browser.new_page(viewport={"width":960,"height":720})
            page.goto(base+"/admin")
            radios = page.locator("#org-list input")
            expect(radios).to_have_count(2)
            self.assertLess(radios.first.bounding_box()["width"],25)
            self.assertLess(abs(radios.first.bounding_box()["x"]-radios.last.bounding_box()["x"]),2)
            page.get_by_text("Открыть словарь",exact=True).click()
            phrase = "несколько слов в названии школьного здания " * 3
            page.locator("#word").fill(phrase)
            page.locator("#word-form button").click()
            expect(page.locator("#words")).to_contain_text(phrase.strip())
            page.keyboard.press("Escape")
            page.locator("#ai-enabled").check()
            page.locator("#endpoint").fill("http://127.0.0.1:1/v1")
            page.locator("#verify-model").click()
            expect(page.locator("#connection-result")).to_contain_text("не прошла",timeout=15000)
            page.reload()
            expect(page.locator("#ai-enabled")).to_be_checked()
            expect(page.locator("#connection-result")).to_contain_text("Нет ответа")
            page.locator("#ai-enabled").uncheck()
            page.locator("#model button.submit").click()
            expect(page.locator("#ai-status")).to_have_text("ИИ выключен")
            page.locator('[data-scope="many"]').click()
            for box in page.locator("#org-list input").all():
                box.check()
            page.locator("#operation").select_option("download_algorithm")
            page.locator("#plan").click()
            expect(page.locator("#run-selection")).to_be_enabled()
            blocked.add("/two")
            page.locator("#run-selection").click()
            until(lambda:get("/api/operations")["archive"]["state"]=="running")
            page.close()
            until(lambda:get("/api/operations")["archive"]["done"]==2)
            stop()
            stop = start()
            self.assertEqual(get("/api/operations")["archive"]["state"],"interrupted")
            blocked.clear()
            gate.set()
            page = browser.new_page()
            page.goto(base+"/admin")
            page.locator("#resume-job").click()
            until(lambda:get("/api/operations")["archive"]["state"]=="completed")
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM crawl_run WHERE id_organization=1").fetchone()[0],1)
                self.assertEqual(conn.execute("SELECT count(*) FROM crawl_run WHERE state='running'").fetchone()[0],0)
            page.goto(base+"/quick/run")
            gate.clear()
            blocked.add("/one")
            page.get_by_role("button",name="Выбрать все",exact=True).click()
            page.get_by_role("button",name="Проверить сайты",exact=True).click()
            until(lambda:get("/api/operations")["quick"]["state"]=="running")
            page.close()
            until(lambda:get("/api/operations")["quick"]["done"]==1)
            stop()
            stop = start()
            self.assertEqual(get("/api/operations")["quick"]["state"],"interrupted")
            blocked.clear()
            gate.set()
            page = browser.new_page()
            page.goto(base+"/quick/run")
            page.get_by_role("button",name="Продолжить незаконченный этап").click()
            until(lambda:get("/api/operations")["quick"]["state"]=="completed")
            with closing(sqlite3.connect(root/"quick"/"audit.db")) as conn:
                self.assertEqual(conn.execute("SELECT count(*),count(DISTINCT site_id),count(DISTINCT run_id) FROM run_sites").fetchone(),(2,2,1))
            page.goto(base)
            expect(page.locator("#recent-operations a")).to_have_count(2)
            page.goto(base+"/site/1")
            expect(page.get_by_role("button", name="Проверить сайт", exact=True)).to_be_visible()
            expect(page.get_by_text("Время работы:", exact=False)).to_be_visible()
            page.goto(base+"/quick/ai?site_id=1")
            expect(page.get_by_role("button", name="Краткая сводка с ИИ")).to_be_disabled()
        stop()
