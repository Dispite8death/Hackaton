"""Командная строка: python -m audit run …"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

from .checks import Options, SiteAuditor
from .crossval import finalize
from .fetcher import Fetcher
from .llm import make_checker
from .models import SiteResult

log = logging.getLogger("audit")


def load_sites(path: str, only: list[str] | None, kind: str | None, limit: int | None) -> list[SiteResult]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row["domain"].strip()
            if only and not any(o in d for o in only):
                continue
            if kind and row["kind"] != kind:
                continue
            rows.append(SiteResult(domain=d, name=row["name"].strip(), kind=row["kind"].strip()))
    return rows[:limit] if limit else rows


def cmd_run(a: argparse.Namespace) -> int:
    today = date.fromisoformat(a.today) if a.today else date.today()
    sites = load_sites(a.sites, a.only.split(",") if a.only else None, a.kind, a.limit)
    if not sites:
        log.error("Нет сайтов для проверки")
        return 2
    fetcher = Fetcher(a.cache_dir, ttl_hours=a.cache_ttl, use_cache=not a.no_cache,
                      max_concurrency=a.concurrency, timeout=a.timeout)
    llm = make_checker(a.llm, a.llm_model, provider=a.llm_provider, base_url=a.llm_base_url,
                       web_search=a.llm_web_search)
    opts = Options(today=today, max_news_details=a.max_news, max_persons=a.max_persons,
                   image_hashes=not a.no_images, llm_news_per_site=a.llm_news if llm else 0)
    started = time.time()
    log.info("Проверка %d сайтов, дата аудита %s", len(sites), today)

    def work(r: SiteResult) -> SiteResult:
        t0 = time.time()
        SiteAuditor(fetcher, r, opts, llm).run()
        log.info("✓ %-55s %-11s замечаний: %3d  (%.0f c, %d стр.)", r.domain, r.status, len(r.findings),
                 time.time() - t0, r.pages_fetched)
        return r

    done = []
    with ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(work, r) for r in sites]
        for fut in as_completed(futs):
            done.append(fut.result())
    done.sort(key=lambda r: [s.domain for s in sites].index(r.domain))
    summary = finalize(done)
    summary.update({
        "audit_date": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "duration_sec": round(time.time() - started),
        "sites_total": len(done),
        "sites_unavailable": sum(r.status != "ok" for r in done),
        "http": fetcher.stats,
        "llm": ({"model": llm.model, **llm.usage} if llm else None),
    })
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "results.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "sites": [r.to_dict() for r in done]}, f, ensure_ascii=False, indent=1)
    from .report import render_all
    render_all(done, summary, out)
    log.info("Готово за %d c. Отчёт: %s", summary["duration_sec"], (out / "index.html").resolve())
    return 0


def cmd_llm_test(a: argparse.Namespace) -> int:
    """Проверка подключения к ИИ: один тестовый запрос с новостью, содержащей заведомую ошибку."""
    llm = make_checker(True, a.llm_model, provider=a.llm_provider, base_url=a.llm_base_url,
                       web_search=a.llm_web_search)
    if not llm:
        print("ИИ-бэкенд не настроен — см. README, раздел «Настройка ИИ».")
        return 2
    print("Настройки:", llm.settings.describe())
    text = ("16 октября в нашей школе прошёл праздник, посвящённый Дню отца, который в 2026 году отмечается "
            "16 октября. Ребята подготовили концерт для пап.")
    t0 = time.time()
    res = llm.check_news("День отца в школе", text, datetime(2026, 10, 19, 10, 0), "Тестовая школа")
    print(f"Время ответа: {time.time() - t0:.1f} c, расход: {llm.usage}")
    if res is None:
        print("Ответ не получен или не разобран — подробности в предупреждениях выше.")
        return 1
    print("Дата события:", res.get("event_date"), "|", res.get("event_date_basis", "")[:120])
    for c in res.get("claims", []):
        print(f"  [{c['status']}] {c['claim'][:90]} — {c.get('note', '')[:120]} ({c['source'][:100]})")
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    from .report import load_results, render_all
    sites, summary = load_results(Path(a.results))
    render_all(sites, summary, Path(a.out or Path(a.results).parent))
    return 0


def load_dotenv(path: str = ".env") -> None:
    """Переменные из .env (ключи ИИ-шлюза) — без перезаписи уже заданных в окружении."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _llm_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("ИИ-бэкенд (переопределяют AUDIT_LLM_* из окружения / .env)")
    g.add_argument("--llm-provider", choices=["auto", "openai", "anthropic"], default=None,
                   help="openai — любой OpenAI-совместимый сервер, в т.ч. локальный; anthropic — Claude API")
    g.add_argument("--llm-base-url", default=None, help="например http://localhost:11434/v1 (Ollama)")
    g.add_argument("--llm-model", default=None)
    g.add_argument("--llm-web-search", choices=["auto", "google", "off"], default=None)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="audit", description="ИИ-краулер аудита сайтов школ и детских садов (Госвеб)")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="проверить сайты и построить отчёт")
    r.add_argument("--sites", default="data/institutions.csv", help="CSV: name,domain,kind")
    r.add_argument("--only", help="подстроки доменов через запятую")
    r.add_argument("--kind", choices=["school", "kindergarten"])
    r.add_argument("--limit", type=int)
    r.add_argument("--out", default="reports/latest")
    r.add_argument("--today", help="дата аудита YYYY-MM-DD (по умолчанию — сегодня)")
    r.add_argument("--workers", type=int, default=6, help="сайтов параллельно")
    r.add_argument("--concurrency", type=int, default=12, help="макс. одновременных HTTP-запросов")
    r.add_argument("--timeout", type=float, default=25)
    r.add_argument("--cache-dir", default=".cache")
    r.add_argument("--cache-ttl", type=float, default=12, help="часы")
    r.add_argument("--no-cache", action="store_true")
    r.add_argument("--max-news", type=int, default=15, help="новостей на сайт для проверки срока/фактов")
    r.add_argument("--max-persons", type=int, default=40, help="карточек сотрудников на раздел")
    r.add_argument("--no-images", action="store_true", help="не скачивать фото для поиска заглушек")
    r.add_argument("--llm", action="store_true", help="факт-чекинг новостей ИИ-агентом Claude с веб-поиском")
    r.add_argument("--llm-news", type=int, default=3, help="новостей на сайт для ИИ-проверки")
    _llm_args(r)
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(fn=cmd_run)
    t = sub.add_parser("llm-test", help="проверить подключение к ИИ-модели одним тестовым запросом")
    _llm_args(t)
    t.add_argument("-v", "--verbose", action="store_true")
    t.set_defaults(fn=cmd_llm_test)
    rep = sub.add_parser("report", help="перестроить HTML/XLSX из results.json")
    rep.add_argument("results")
    rep.add_argument("--out")
    rep.add_argument("-v", "--verbose", action="store_true")
    rep.set_defaults(fn=cmd_report)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
