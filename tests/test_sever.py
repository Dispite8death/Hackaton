"""Интеграция интерфейса «Север» с движком audit/: адаптер и сквозной сценарий через настоящий HTTP.

Сеть и модель подменены: сайт — офлайн-копия в разметке Госвеба (tests/fake_gosweb.py), модель — FakeLLM.
Сценарий: выбрать учреждение → запустить → увидеть состояние → открыть отчёт → найти доказательство →
сохранить решение и выгрузки → перезапустить сервис и открыть тот же результат.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

pytest.importorskip("starlette")
uvicorn = pytest.importorskip("uvicorn")
requests = pytest.importorskip("requests")

from audit.models import Finding, SiteResult  # noqa: E402
from fake_gosweb import BASE, DOMAIN, FakeFetcher, FakeLLM, build_site  # noqa: E402
from sever import runner, store  # noqa: E402
from sever.adapter import adapt  # noqa: E402

ORG = {"id": 1, "name": "Школа «Демо»", "url": BASE + "/", "kind": "school"}
SLOW = "sh-slow-r44.gosweb.gosuslugi.ru"


# ------------------------------------------------------------------------------ адаптер
def test_no_findings_without_executed_checks_is_not_success():
    r = SiteResult(domain=DOMAIN, name="Школа", kind="school")   # проверки не выполнялись: метрик нет
    d = adapt(ORG, r)
    statuses = {c["status"] for c in d["checks"]}
    assert "pass" not in statuses and "fail" not in statuses
    assert d["coverage"] is None and d["score"] is None


def test_unavailable_site_has_no_coverage_and_is_review():
    r = SiteResult(domain=DOMAIN, name="Школа", kind="school", status="unavailable", error="HTTP 503")
    r.add(Finding("Сайт", "Сайт недоступен (HTTP 503) — проверка не выполнена", "critical", "Проверить", BASE,
                  check="site_unavailable"))
    d = adapt(ORG, r)
    assert d["status"] == "unavailable" and d["coverage"] is None and d["score"] is None
    assert [c["status"] for c in d["checks"]] == ["review"]


def test_failed_step_marks_its_items_unknown_not_passed():
    r = SiteResult(domain=DOMAIN, name="Школа", kind="school")
    r.metrics.update(news_days_since_last=3, news_last="2026-11-01")
    r.add(Finding("Служебное", "Проверка «check_news» не выполнена: KeyError", "low", "Сообщить", check="internal_error"))
    news = [c for c in adapt(ORG, r)["checks"] if c["section"] == "Новости"]
    assert news and all(c["status"] == "unknown" for c in news)


def test_audit_severity_and_evidence_preserved():
    r = SiteResult(domain=DOMAIN, name="Школа", kind="school")
    r.add(Finding("Документы", "Не найден обязательный документ: Устав", "critical", "Разместить", BASE + "/d/",
                  ["строка 1", "строка 2"], "doc_ustav"))
    c = adapt(ORG, r)["checks"][0]
    assert (c["status"], c["severity"], c["check"]) == ("fail", "critical", "doc_ustav")
    assert c["evidence"] == "строка 1\nстрока 2"


# ------------------------------------------------------------------------------ сервер
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self):
        from sever.app import app
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.url = f"http://127.0.0.1:{self.port}"
        self.session = requests.get(self.url + "/api/state").json()["session"]

    def get(self, path, **kw):
        return requests.get(self.url + path, timeout=10, **kw)

    def send(self, method, path, json=None, session=True):
        headers = {"X-Audit-Session": self.session} if session else {}
        return requests.request(method, self.url + path, json=json, headers=headers, timeout=10)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SEVER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AUDIT_CACHE_DIR", str(tmp_path / "cache"))
    for k in ("AUDIT_LLM_BASE_URL", "AUDIT_LLM_MODEL", "AUDIT_LLM_PROVIDER", "AUDIT_LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    fake = {"fetchers": [], "llms": [], "delay": {}}
    sites = {DOMAIN: build_site(runner.datetime.now(runner.LOCAL).date()),
             SLOW: build_site(runner.datetime.now(runner.LOCAL).date())}

    def fetcher(cfg, cancelled):
        f = FakeFetcher(sites, cancelled, fake["delay"])
        fake["fetchers"].append(f)
        return f

    def llm(settings):
        m = FakeLLM(settings)
        fake["llms"].append(m)
        return m
    monkeypatch.setattr(runner, "FETCHER_FACTORY", fetcher)
    monkeypatch.setattr(runner, "LLM_FACTORY", llm)
    store.init()
    with store.connect() as db:
        for domain, name in ((DOMAIN, "МБОУ «Школа Демо»"), (SLOW, "МБОУ «Медленная школа»")):
            db.execute("INSERT INTO organizations(name,url,kind) VALUES(?,?,'school')", (name, f"https://{domain}/"))
    ids = {o["url"]: o["id"] for o in store.organizations()}
    fake["demo"], fake["slow"] = ids[BASE + "/"], ids[f"https://{SLOW}/"]
    fake["unavailable"] = store.organizations()[0]["id"]     # сайт из реестра, которого нет в офлайн-копии
    yield fake


def _wait_run(srv, rid, seen_current, timeout=30):
    for _ in range(int(timeout / 0.05)):
        run = next(r for r in srv.get("/api/state").json()["runs"] if r["id"] == rid)
        if run.get("current"):
            seen_current.append(run["current"])
        if run["status"] not in ("queued", "running"):
            return run
        time.sleep(0.05)
    raise AssertionError("запуск не завершился")


def test_full_scenario_run_report_evidence_save_and_reopen(env):
    srv = Server()
    try:
        assert "Север" in srv.get("/").text
        st = srv.get("/api/state").json()
        assert st["engine"] == "audit" and len(st["organizations"]) >= 82
        # ИИ включаем в настройках интерфейса; модель подменена
        cfg = dict(st["settings"], llm_enabled=True, llm_provider="openai", base_url="http://localhost:11434/v1",
                   model="qwen-test", llm_news=2)
        assert srv.send("PUT", "/api/settings", cfg).json()["llm_effective"].endswith("без веб-поиска")
        # изменения без токена сессии запрещены
        assert srv.send("POST", "/api/runs", {"organization_ids": [env["demo"]]}, session=False).status_code == 403

        env["delay"][DOMAIN] = 0.03
        run = srv.send("POST", "/api/runs", {"organization_ids": [env["demo"], env["unavailable"]], "mode": "full"}).json()
        assert run["total"] == 2
        assert srv.send("POST", "/api/runs", {"organization_ids": [env["demo"]]}).status_code == 409
        seen = []
        final = _wait_run(srv, run["id"], seen)
        assert final["status"] == "completed" and final["done"] == 2 and final["outputs"]
        assert any("стр." in c for c in seen), seen           # видно состояние: учреждение / раздел · N стр.

        reports = {r["org_id"]: r for r in srv.get("/api/reports").json()}
        demo, down = reports[env["demo"]], reports[env["unavailable"]]
        assert not demo["preliminary"] and demo["coverage"] and demo["score"] is not None
        assert down["status"] == "unavailable" and down["coverage"] is None and down["score"] is None

        rep = srv.get(f"/api/reports/{demo['id']}").json()
        d = rep["data"]
        assert d["engine"] == "audit" and "raw" not in d
        # доказательство: замечание о заглушке → сохранённый текст той же страницы
        stub = next(c for c in d["checks"] if c["check"] == "placeholders")
        assert stub["status"] == "fail" and stub["severity"] == "high"
        page = next(p for p in d["pages"] if p["url"].rstrip("/") == stub["url"].rstrip("/"))
        assert "Примерно от 100 до 200 символов" in page["text"]
        # расхождение алгоритма и подменной модели по дате события — задача инспектора, не нарушение
        dispute = next(c for c in d["checks"] if c["check"] == "news_event_date_review")
        assert dispute["status"] == "review" and dispute["method"] == "llm"
        assert sum(len(m.calls) for m in env["llms"]) == 2      # не больше llm_news новостей на сайт
        assert d["ai"]["state"] == "ok" and any(n["event_date_status"] == "review" for n in d["news"])
        assert all(c["status"] in ("pass", "fail", "review", "unknown") for c in d["checks"])

        # решение инспектора сохраняется отдельно от неизменяемого отчёта
        assert srv.send("PUT", f"/api/reviews/{stub['fingerprint']}", {"state": "confirmed", "note": "Заглушка есть"}).ok
        csv_text = srv.get(f"/api/reports/{demo['id']}/export/csv").content.decode("utf-8-sig")
        assert "Подтверждено;Заглушка есть" in csv_text and "placeholders" in csv_text
        assert "Печать" in srv.get(f"/api/reports/{demo['id']}/export/html").text
        assert srv.get(f"/api/reports/{demo['id']}/export/json").json()["data"]["raw"]["domain"] == DOMAIN
        # выгрузки audit/ по запуску — те же файлы, что строит CLI
        for name in ("index.html", "summary.xlsx", "findings.csv", "results.json"):
            assert srv.get(f"/runs/{run['id']}/{name}").status_code == 200, name
        assert srv.get(f"/runs/{run['id']}/..%2F..%2Fsever.sqlite3").status_code == 404
        comp = srv.get("/api/comparison").json()
        school = next(g for g in comp if g["kind"] == "school")
        assert school["reference"]["org_id"] == env["demo"]
        summary = srv.get("/api/export/summary.csv").content.decode("utf-8-sig")
        assert "Балл audit" in summary and DOMAIN in summary
    finally:
        srv.stop()

    # повторное открытие после перезапуска сервиса: тот же отчёт и то же решение инспектора
    srv = Server()
    try:
        again = srv.get(f"/api/reports/{demo['id']}").json()
        assert again["data"]["checks"] == rep["data"]["checks"]
        assert again["reviews"][stub["fingerprint"]]["state"] == "confirmed"
        assert srv.get(f"/runs/{run['id']}/summary.xlsx").status_code == 200
    finally:
        srv.stop()


def test_cancel_keeps_finished_reports_and_marks_run(env):
    srv = Server()
    try:
        cfg = dict(srv.get("/api/state").json()["settings"], concurrency=1)
        srv.send("PUT", "/api/settings", cfg)
        env["delay"][SLOW] = 0.3
        run = srv.send("POST", "/api/runs", {"organization_ids": [env["demo"], env["slow"]]}).json()
        for _ in range(400):
            if next(r for r in srv.get("/api/state").json()["runs"] if r["id"] == run["id"])["done"] >= 1:
                break
            time.sleep(0.05)
        assert srv.send("POST", f"/api/runs/{run['id']}/cancel").ok
        final = _wait_run(srv, run["id"], [])
        assert final["status"] == "cancelled"
        reports = srv.get("/api/reports").json()
        assert [r["org_id"] for r in reports] == [env["demo"]]       # незавершённый сайт не выдаётся за проверенный
        d = srv.get(f"/api/reports/{reports[0]['id']}").json()["data"]
        assert not d["preliminary"] and any("остановлен" in x for x in d["limitations"])
        assert srv.send("POST", f"/api/runs/{run['id']}/cancel").status_code == 409
    finally:
        srv.stop()


def test_quick_mode_skips_model_and_import_needs_no_network(env, tmp_path):
    srv = Server()
    try:
        cfg = dict(srv.get("/api/state").json()["settings"], llm_enabled=True, base_url="http://localhost:1/v1")
        srv.send("PUT", "/api/settings", cfg)
        run = srv.send("POST", "/api/runs", {"organization_ids": [env["demo"]], "mode": "quick"}).json()
        assert _wait_run(srv, run["id"], [])["mode"] == "quick"
        assert not env["llms"]                                        # быстрый обзор не вызывает модель
        d = srv.get(f"/api/reports/{srv.get('/api/reports').json()[0]['id']}").json()["data"]
        assert d["mode"] == "quick" and d["ai"]["state"] == "disabled"
        assert any("Быстрый обзор" in x for x in d["limitations"])
        assert srv.get("/api/state").json()["settings"]["max_news"] == 15   # настройки не изменились

        # импорт сохранённого результата CLI: без обращения к сайтам и модели
        calls = sum(f.calls for f in env["fetchers"])
        res = runner.import_results(store.runs_dir() / run["id"] / "results.json")
        assert sum(f.calls for f in env["fetchers"]) == calls and not env["llms"]
        imported = [r for r in srv.get("/api/reports").json() if r["run_id"] == res["id"]]
        assert len(imported) == 1 and imported[0]["mode"] == "import" and imported[0]["coverage"]
        di = srv.get(f"/api/reports/{imported[0]['id']}").json()["data"]
        assert di["pages"] == [] and any("Импорт" in x for x in di["limitations"])
        assert {c["check"] for c in di["checks"] if c["origin"] == "audit"} == \
               {c["check"] for c in d["checks"] if c["origin"] == "audit"}
    finally:
        srv.stop()


def test_settings_validation_and_site_url_rules(env):
    srv = Server()
    try:
        bad = dict(srv.get("/api/state").json()["settings"], base_url="ftp://x")
        assert srv.send("PUT", "/api/settings", bad).status_code == 422
        r = srv.send("POST", "/api/organizations", {"name": "Школа", "url": "https://x.example/path/", "kind": "school"})
        assert r.status_code == 422 and "без пути" in r.json()["detail"]
        r = srv.send("POST", "/api/organizations", {"name": "Школа", "url": "http://127.0.0.1/", "kind": "school"})
        assert r.status_code == 422
    finally:
        srv.stop()
