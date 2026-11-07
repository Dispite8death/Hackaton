#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import bs4, PIL, reportlab, keyring, pypdfium2, tldextract, playwright' 2>/dev/null; then
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m Engine.site_server --open "$@"
