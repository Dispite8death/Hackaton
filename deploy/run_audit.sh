#!/usr/bin/env bash
# Еженедельный запуск аудита: отчёт кладётся в reports/<дата> и копируется в reports/latest.
set -euo pipefail
cd "$(dirname "$0")/.."
STAMP=$(date +%F)
.venv/bin/python -m audit run --out "reports/$STAMP" ${AUDIT_EXTRA_ARGS:-}
rm -rf reports/latest && cp -r "reports/$STAMP" reports/latest
