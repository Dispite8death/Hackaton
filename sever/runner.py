"""Выполнение проверок движком audit/ для веб-интерфейса.

Используются те же компоненты, что и в CLI (python -m audit run): SiteAuditor, Fetcher с дисковым
кэшем, ИИ-бэкенд audit.llm и перекрёстная сверка audit.crossval.finalize. Собственный краулер
«Севера» не используется. Добавлены только наблюдаемость и управление: прогресс по страницам,
остановка, промежуточные отчёты, сохранение в SQLite и выгрузки audit/ для каждого запуска.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from audit.checks import Options, SiteAuditor
from audit.crossval import compute_score, finalize
from audit.fetcher import Fetcher
from audit.llm import LLMChecker, LLMSettings, OpenAICompatChecker
from audit.models import Finding, SiteResult

from . import store
from .adapter import adapt, site_from_dict

log = logging.getLogger("sever")
LOCAL = timezone(timedelta(hours=11))   # Магадан

QUICK = {"max_news": 5, "max_persons": 10}


class Cancelled(BaseException):
    """Остановка запуска. BaseException — чтобы её не перехватил обработчик ошибок отдельных проверок
    SiteAuditor.run (он ловит Exception и продолжил бы следующие шаги)."""


class RunConflict(Exception):
    pass


class CancellableFetcher(Fetcher):
    def __init__(self, *args, cancelled: Callable[[], bool] = lambda: False, **kw):
        super().__init__(*args, **kw)
        self.cancelled = cancelled

    def get(self, url):
        if self.cancelled():
            raise Cancelled()
        return super().get(url)

    def image_hash(self, url, max_bytes=3_000_000):
        if self.cancelled():
            raise Cancelled()
        return super().image_hash(url, max_bytes)


class TrackedAuditor(SiteAuditor):
    """SiteAuditor без изменения логики: только сообщает, какой раздел сейчас загружается."""

    def __init__(self, *args, on_page: Callable[[str, int], None], **kw):
        super().__init__(*args, **kw)
        self.on_page = on_page

    def get(self, url, section=""):
        page = super().get(url, section)
        self.on_page(section, self.r.pages_fetched)
        return page


# Точки подмены для тестов и демонстрации без сети и без вызовов модели.
FETCHER_FACTORY: Callable[..., Fetcher] | None = None
LLM_FACTORY: Callable[[LLMSettings], object] | None = None


def cache_dir() -> Path:
    import os
    return Path(os.environ.get("AUDIT_CACHE_DIR") or store.ROOT / ".cache")


def llm_settings(cfg: dict) -> LLMSettings:
    """Настройки модели audit.llm: окружение и .env (AUDIT_LLM_*) — основа, сохранённые в интерфейсе значения
    их уточняют. Пустое поле интерфейса не затирает значение из .env."""
    override = {"provider": cfg.get("llm_provider"), "base_url": cfg.get("base_url"), "api_key": cfg.get("api_key"),
                "model": cfg.get("model"), "web_search": cfg.get("web_search"), "json_mode": cfg.get("json_mode"),
                "timeout": cfg.get("llm_timeout"), "max_tokens": cfg.get("llm_max_tokens"),
                "reasoning_effort": cfg.get("reasoning_effort")}
    return LLMSettings.from_env(**{k: v for k, v in override.items() if v not in (None, "")})


def make_llm(cfg: dict):
    settings = llm_settings(cfg)
    if LLM_FACTORY:
        checker = LLM_FACTORY(settings)
    elif settings.provider == "openai":
        checker = OpenAICompatChecker(settings)
    else:
        checker = LLMChecker(settings.model)
    checker.settings = settings
    return checker


def make_fetcher(cfg: dict, cancelled: Callable[[], bool]) -> Fetcher:
    if FETCHER_FACTORY:
        return FETCHER_FACTORY(cfg, cancelled)
    return CancellableFetcher(cache_dir(), ttl_hours=cfg["cache_ttl"], use_cache=cfg["use_cache"],
                              max_concurrency=cfg["http_concurrency"], timeout=cfg["timeout"], cancelled=cancelled)


def short_name(name: str) -> str:
    m = re.search(r'["«]([^"»]+)["»]', name)
    return m[1] if m else name[:85]


def domain_of(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).hostname or ""


# ------------------------------------------------------------------------------------ запуск
class Job:
    def __init__(self, run_id: str, orgs: list[dict], cfg: dict, mode: str):
        self.id, self.orgs, self.cfg, self.mode = run_id, orgs, cfg, mode
        self.cancel_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name=f"run-{run_id}", daemon=True)
        self._progress_lock = threading.Lock()
        self._last_progress = 0.0
        self._pending = ""
        self._flush_scheduled = False
        self.done_event = threading.Event()
        self.llm = None
        self.ai_base = {"state": "disabled", "findings": [], "events": []}

    # -------------------------------------------------------------- служебное
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def progress(self, text: str, force: bool = False) -> None:
        """Строка состояния запуска. Записи в БД прореживаются (не чаще 4 раз в секунду), но последнее
        значение не теряется: его допишет следующий вызов или фоновый сброс."""
        with self._progress_lock:
            self._pending = text
            t = time.monotonic()
            if not force and t - self._last_progress < 0.25:
                if not self._flush_scheduled:
                    self._flush_scheduled = True
                    threading.Timer(0.3, self._flush).start()
                return
            self._last_progress = t
        self._write(text)

    def _flush(self) -> None:
        with self._progress_lock:
            self._flush_scheduled = False
            text, self._last_progress = self._pending, time.monotonic()
        if text and not self.done_event.is_set():
            self._write(text)

    def _write(self, text: str) -> None:
        with store.connect() as db:
            db.execute("UPDATE runs SET current=? WHERE id=? AND status IN ('queued','running')", (text, self.id))

    def options(self) -> Options:
        c = self.cfg
        quick = self.mode == "quick"
        return Options(today=datetime.now(LOCAL).date(),
                       max_news_details=min(c["max_news"], QUICK["max_news"]) if quick else c["max_news"],
                       max_persons=min(c["max_persons"], QUICK["max_persons"]) if quick else c["max_persons"],
                       image_hashes=False if quick else c["image_hashes"],
                       llm_news_per_site=0 if quick or not self.llm else c["llm_news"])

    def site_limitations(self, opts: Options) -> list[str]:
        out = []
        if self.mode == "quick":
            out.append(f"Быстрый обзор: до {opts.max_news_details} свежих новостей и {opts.max_persons} карточек "
                       "сотрудников на раздел, без сравнения фото и без ИИ. Результат не равен полной проверке.")
        else:
            out.append(f"Детально проверено не более {opts.max_news_details} свежих новостей и {opts.max_persons} "
                       "карточек сотрудников на раздел.")
        if self.llm:
            out.append(f"ИИ ({self.llm.settings.describe()}) проверял не более {opts.llm_news_per_site} свежих новостей "
                       "на сайт; его выводы требуют подтверждения инспектором.")
        if self.cfg["use_cache"] and not FETCHER_FACTORY:
            out.append(f"Страницы могли быть взяты из кэша audit/ (не старше {self.cfg['cache_ttl']} ч).")
        return out

    def ai_for(self, r: SiteResult) -> dict:
        ai = dict(self.ai_base)
        if not self.llm:
            return ai
        used = sum("model" in (n.event_date_candidates or {}) for n in r.news)
        ai.update(state="ok" if used else "skipped", news_checked=used)
        return ai

    # -------------------------------------------------------------- основной цикл
    def run(self) -> None:
        with store.connect() as db:
            db.execute("UPDATE runs SET status='running' WHERE id=?", (self.id,))
        finished: list[tuple[dict, SiteResult, dict, float]] = []
        started = time.monotonic()
        try:
            if self.cfg["llm_enabled"] and self.mode != "quick":
                try:
                    self.llm = make_llm(self.cfg)
                    self.ai_base = {"state": "ok", "model": self.llm.settings.model,
                                    "describe": self.llm.settings.describe(), "findings": [], "events": []}
                except Exception as e:  # noqa: BLE001 — без модели правила audit работают полностью
                    self.ai_base = {"state": "error", "error": f"ИИ не подключён: {e}"[:300], "findings": [], "events": []}
            fetcher = make_fetcher(self.cfg, self.cancelled)
            opts = self.options()
            lims = self.site_limitations(opts)

            def one(org: dict):
                if self.cancelled():
                    return None
                label = short_name(org["name"])
                r = SiteResult(domain=domain_of(org["url"]), name=org["name"], kind=org["kind"])
                auditor = TrackedAuditor(fetcher, r, opts, self.llm,
                                         on_page=lambda section, n: self.progress(f"{label} / {section or 'Страницы'} · {n} стр."))
                self.progress(f"{label} / Главная страница", force=True)
                t0 = time.perf_counter()
                try:
                    auditor.run()
                except Cancelled:
                    return None
                except Exception as e:  # noqa: BLE001 — сбой одного сайта не останавливает запуск
                    log.exception("%s: проверка сайта прервана", r.domain)
                    r.add(Finding("Служебное", f"Проверка сайта прервана ошибкой: {type(e).__name__}: {e}"[:300], "low",
                                  "Повторите проверку; при повторении ошибки сообщите разработчикам.", r.base + "/",
                                  check="internal_error"))
                seconds = round(time.perf_counter() - t0, 2)
                compute_score(r)   # предварительный балл; окончательный — после сверки между сайтами
                metrics = {"total_seconds": seconds, "pages_fetched": r.pages_fetched}
                data = adapt(org, r, pages=auditor.pages, ai=self.ai_for(r), mode=self.mode, preliminary=True,
                             limitations=lims + ["Промежуточный результат: сверка между сайтами запуска ещё не выполнена."],
                             metrics=metrics)
                store.save_report(self.id, org["id"], data)
                with store.connect() as db:
                    db.execute("UPDATE runs SET done=done+1 WHERE id=?", (self.id,))
                return org, r, auditor.pages, seconds

            with ThreadPoolExecutor(max(1, self.cfg["concurrency"])) as ex:
                for res in ex.map(one, self.orgs):
                    if res:
                        finished.append(res)
            self.progress("Сверка между сайтами и итоговые отчёты", force=True)
            self.finalize(finished, lims, http=dict(fetcher.stats), duration=time.monotonic() - started)
            status = "cancelled" if self.cancelled() else "completed"
            with store.connect() as db:
                db.execute("UPDATE runs SET status=?,finished=?,current=NULL WHERE id=?", (status, store.now(), self.id))
        except Exception as e:  # noqa: BLE001
            log.exception("Запуск %s завершился ошибкой", self.id)
            store.finish_partial(self.id, "Запуск завершился ошибкой до сверки между сайтами.")
            with store.connect() as db:
                db.execute("UPDATE runs SET status='interrupted',finished=?,current=NULL,error=? WHERE id=?",
                           (store.now(), f"{type(e).__name__}: {e}"[:300], self.id))
        finally:
            self.done_event.set()
            JOBS.pop(self.id, None)

    def finalize(self, finished, lims, http: dict | None = None, duration: float = 0) -> None:
        """Перекрёстная сверка audit.crossval по сайтам этого запуска и итоговые отчёты."""
        if not finished:
            return
        sites = [r for _, r, _, _ in finished]
        summary = finalize(sites)
        note = (f"Сверка между сайтами (эталон, одинаковые фото-заглушки) выполнена среди {len(sites)} "
                f"сайт(ов) этого запуска.")
        if self.cancelled():
            note += " Запуск остановлен: остальные сайты не проверялись."
        for org, r, pages, seconds in finished:
            data = adapt(org, r, pages=pages, ai=self.ai_for(r), mode=self.mode, preliminary=False,
                         limitations=lims + [note], metrics={"total_seconds": seconds, "pages_fetched": r.pages_fetched})
            store.save_report(self.id, org["id"], data)
        try:
            write_audit_outputs(self.id, sites, summary, extra={
                "mode": self.mode, "duration_sec": round(duration), "http": http or {},
                "llm": ({"model": self.llm.settings.model, **self.llm.usage} if self.llm else None)})
        except Exception:  # noqa: BLE001 — выгрузки audit/ не должны ломать сохранённые отчёты
            log.exception("Не удалось построить выгрузки audit/ для запуска %s", self.id)


def write_audit_outputs(run_id: str, sites: list[SiteResult], summary: dict, extra: dict | None = None) -> Path:
    """Те же файлы, что строит CLI: results.json, index.html, sites/*.html, summary.xlsx, findings.csv."""
    from audit.report import render_all
    out = store.runs_dir() / run_id
    out.mkdir(parents=True, exist_ok=True)
    summary = dict(summary)
    summary.update({"generated_at": datetime.now().isoformat(timespec="seconds"), "sites_total": len(sites),
                    "sites_unavailable": sum(r.status != "ok" for r in sites), **(extra or {})})
    # поля, которые CLI записывает в сводку и использует шаблон отчёта audit/
    summary.setdefault("audit_date", datetime.now(LOCAL).date().isoformat())
    summary.setdefault("duration_sec", 0)
    summary.setdefault("http", {"requests": 0, "cache_hits": 0, "errors": 0})
    summary.setdefault("llm", None)
    with open(out / "results.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "sites": [r.to_dict() for r in sites]}, f, ensure_ascii=False, indent=1)
    render_all(sites, summary, out)
    return out


JOBS: dict[str, Job] = {}
_START_LOCK = threading.Lock()


def start_run(org_ids: list[int] | None = None, mode: str = "full") -> dict:
    if mode not in ("full", "quick"):
        raise ValueError("Неизвестный режим проверки")
    with _START_LOCK:
        if JOBS:
            raise RunConflict("Проверка уже выполняется. Дождитесь завершения или остановите её.")
        orgs = [{k: v for k, v in o.items() if k != "latest"} for o in store.organizations()]
        if org_ids:
            orgs = [o for o in orgs if o["id"] in set(org_ids)]
        if not orgs:
            raise ValueError("Учреждения не выбраны")
        cfg = store.settings(secret=True)
        rid = uuid.uuid4().hex[:12]
        options = {k: v for k, v in cfg.items() if k != "api_key"}
        options["mode"] = mode
        with store.connect() as db:
            db.execute("INSERT INTO runs(id,created,status,total,options) VALUES(?,?,?,?,?)",
                       (rid, store.now(), "queued", len(orgs), json.dumps(options, ensure_ascii=False)))
        job = Job(rid, orgs, cfg, mode)
        JOBS[rid] = job
        job.thread.start()
    return {"id": rid, "total": len(orgs)}


def cancel_run(run_id: str) -> None:
    job = JOBS.get(run_id)
    if not job:
        raise RunConflict("Проверка уже завершена")
    job.cancel_event.set()


# ------------------------------------------------------------------------------------ импорт
def import_results(path: str | Path) -> dict:
    """Загружает сохранённый результат CLI (results.json) как завершённый запуск — без сети и без модели.

    Найденные замечания, баллы и сверка между сайтами берутся из файла без пересчёта.
    Тексты страниц CLI не сохраняет, поэтому вкладка «Доказательства» у таких отчётов пустая.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    sites = [site_from_dict(d) for d in data["sites"]]
    by_domain = {domain_of(o["url"]): o for o in store.organizations()}
    rid = uuid.uuid4().hex[:12]
    with store.connect() as db:
        db.execute("INSERT INTO runs(id,created,status,total,done,finished,options) VALUES(?,?,?,?,?,?,?)",
                   (rid, store.now(), "completed", len(sites), len(sites), store.now(),
                    json.dumps({"mode": "import", "source": str(path), "audit_date": summary.get("audit_date")},
                               ensure_ascii=False)))
        for r in sites:
            if r.domain not in by_domain:
                cur = db.execute("INSERT INTO organizations(name,url,kind) VALUES(?,?,?)",
                                 (r.name, f"https://{r.domain}/", r.kind))
                by_domain[r.domain] = {"id": cur.lastrowid, "name": r.name, "url": f"https://{r.domain}/", "kind": r.kind}
    note = [f"Импорт сохранённого результата CLI ({Path(path).name}, дата аудита {summary.get('audit_date', '—')}). "
            "Тексты страниц в файле не сохраняются, поэтому снимков страниц в отчёте нет."]
    for r in sites:
        org = by_domain[r.domain]
        ai = {"state": "ok" if summary.get("llm") else "disabled", "findings": [], "events": []}
        store.save_report(rid, org["id"], adapt(org, r, pages=None, ai=ai, mode="import", preliminary=False,
                                                limitations=note, checked_at=r.checked_at))
    write_audit_outputs(rid, sites, summary)
    return {"id": rid, "total": len(sites)}


# ------------------------------------------------------------------------------------ расписание
def next_schedule(cfg: dict, after: datetime | None = None) -> str:
    local = after or datetime.now(LOCAL)
    d = local.replace(hour=cfg["schedule_hour"], minute=0, second=0, microsecond=0)
    if d <= local:
        d += timedelta(days=cfg["schedule_interval_days"])
    return d.isoformat()


def scheduler_tick() -> None:
    cfg = store.settings(secret=True)
    if not cfg["schedule_enabled"]:
        return
    if not cfg.get("next_run"):
        store.save_settings({"next_run": next_schedule(cfg)})
        return
    if datetime.fromisoformat(cfg["next_run"]) <= datetime.now(LOCAL) and not JOBS:
        start_run()
        store.save_settings({"next_run": next_schedule(cfg)})


def start_scheduler(stop: threading.Event) -> threading.Thread:
    def loop():
        while not stop.wait(20):
            try:
                scheduler_tick()
            except Exception:  # noqa: BLE001
                log.exception("Ошибка планировщика")
    t = threading.Thread(target=loop, name="scheduler", daemon=True)
    t.start()
    return t
