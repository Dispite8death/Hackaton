"""Офлайн-демо веб-интерфейса: настоящий движок audit/, но сайт — локальная копия в разметке Госвеба,
а ИИ-модель — подмена (без сети и без расхода агрегатора).

    python scripts/sever-offline-demo.py [--port 8765] [--data /tmp/sever-demo]

В реестр добавляется «Школа Демо» (sh-demo-r44…). Остальные учреждения в демо недоступны —
их проверка покажет состояние «Нет доступа» без охвата и оценки.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", default=str(Path(os.environ.get("TEMP", "/tmp")) / "sever-demo"))
    a = p.parse_args()
    os.environ["SEVER_DATA_DIR"] = a.data
    os.environ["AUDIT_CACHE_DIR"] = str(Path(a.data) / "cache")

    from fake_gosweb import BASE, DOMAIN, FakeFetcher, FakeLLM, build_site
    from sever import runner, store

    site = {DOMAIN: build_site(runner.datetime.now(runner.LOCAL).date())}
    runner.FETCHER_FACTORY = lambda cfg, cancelled: FakeFetcher(site, cancelled, {DOMAIN: 0.05})
    runner.LLM_FACTORY = lambda settings: FakeLLM(settings)
    store.init()
    with store.connect() as db:
        db.execute("INSERT OR IGNORE INTO organizations(name,url,kind) VALUES(?,?,?)",
                   ("МБОУ «Школа Демо» (офлайн-копия)", BASE + "/", "school"))
    import uvicorn
    print(f"Офлайн-демо: http://127.0.0.1:{a.port}  (данные: {a.data})")
    uvicorn.run("sever.app:app", host="127.0.0.1", port=a.port, workers=1, log_level="warning")


if __name__ == "__main__":
    main()
