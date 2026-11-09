#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
stamp=".venv/kontur-requirements"
if ! cmp -s requirements.txt "$stamp"; then
    .venv/bin/python -m pip install -r requirements.txt
    cp requirements.txt "$stamp"
fi
exec .venv/bin/python -m Engine.site_server --open "$@"
