#!/bin/sh
set -eu
cd -P "$(dirname "$0")"
case "${1:-}" in
    --help|-h)
        printf '%s\n' 'sh start.sh [--port 8765]  — открыть портал' \
            'sh start.sh --headless [--port 8765]  — портал без открытия браузера' \
            'sh start.sh --once [параметры]  — выполнить один цикл аудита' \
            'sh start.sh --loop [параметры]  — повторять цикл с интервалом из настроек' \
            'sh start.sh --status [параметры]  — состояние последнего цикла' \
            'sh start.sh --install-browser  — установить Chromium для снимков сайтов'
        exit 0 ;;
esac
if [ -x .venv/bin/python ]; then
    default_venv=.venv
else
    default_venv=.venv-linux
fi
venv=${KONTUR_VENV:-$default_venv}
python="$venv/bin/python"
if [ ! -x "$python" ] || ! "$python" -m pip --version >/dev/null 2>&1; then
    if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
        echo 'Для запуска нужен Python 3.11 или новее.' >&2
        exit 1
    fi
    if ! python3 -m venv "$venv"; then
        echo 'Не удалось создать виртуальное окружение. В Ubuntu установите python3-venv.' >&2
        exit 1
    fi
fi
stamp="$venv/kontur-requirements"
if ! cmp -s requirements.txt "$stamp"; then
    "$python" -m pip install -r requirements.txt
    cp requirements.txt "$stamp"
fi
case "${1:-}" in
    --install-browser) exec "$python" -m playwright install --only-shell chromium ;;
    --once|--loop|--status) exec "$python" -m Engine.audit_worker "$@" ;;
    --headless) shift; exec "$python" -m Engine.site_server "$@" ;;
    *) exec "$python" -m Engine.site_server --open "$@" ;;
esac
