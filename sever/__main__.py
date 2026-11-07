"""Запуск веб-интерфейса и служебные команды.

    python -m sever                         веб-интерфейс на http://127.0.0.1:8765
    python -m sever serve --port 8766       другой порт
    python -m sever import reports/latest/results.json
                                            загрузить сохранённый результат CLI без сети и без модели
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import store


def prepare() -> None:
    os.chdir(store.ROOT)                     # .env и кэш audit/ — в корне репозитория, как у CLI
    from audit.cli import load_dotenv
    load_dotenv()
    os.environ.setdefault("AUDIT_CACHE_DIR", str(store.ROOT / ".cache"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sever", description="Веб-интерфейс «Север» поверх движка audit/")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="запустить веб-интерфейс (по умолчанию)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    i = sub.add_parser("import", help="загрузить results.json, построенный `python -m audit run`")
    i.add_argument("results")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    prepare()
    if a.cmd == "import":
        from .runner import import_results
        store.init()
        res = import_results(a.results)
        print(f"Импортировано отчётов: {res['total']}. Запуск {res['id']} доступен в разделе «Проверки».")
        return 0
    import uvicorn
    # один процесс: планировщик и очередь работают внутри него
    uvicorn.run("sever.app:app", host=getattr(a, "host", "127.0.0.1"), port=getattr(a, "port", 8765), workers=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
