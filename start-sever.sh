#!/bin/sh
# Веб-интерфейс «Север» поверх движка audit/: http://127.0.0.1:8765
set -eu
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
exec .venv/bin/python -m sever serve --port "${PORT:-8765}"
