#!/usr/bin/env bash
# Ultron one-command launcher.
# Usage:  ./run.sh           (http://localhost:8799  ·  dashboard: /dashboard)
#         PORT=9000 ./run.sh
#         PYTHON=python3.11 ./run.sh
# Live mode (real model / Hermes) is opt-in via env, otherwise it runs fail-closed in local/demo (fake) mode:
#         ULTRON_UI_GENERATOR=model ULTRON_MODULE_SYNTH=model ULTRON_ADAPTER=pinned-hermes \
#         ULTRON_MODEL_BASE_URL=... ULTRON_MODEL_API_KEY=... ULTRON_MODEL_NAME=... ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: '$PY' not found. Install Python 3.11+ (macOS: brew install python@3.11) or set PYTHON=..." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "[ultron] creating virtualenv (.venv) ..."
  "$PY" -m venv .venv
fi

echo "[ultron] installing dependencies ..."
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -e ".[dev]"

PORT="${PORT:-8799}"
echo "[ultron] starting server → http://localhost:${PORT}   (dashboard: http://localhost:${PORT}/dashboard)"
exec ./.venv/bin/python -m uvicorn ultron.app.server:create_app --factory --host 127.0.0.1 --port "${PORT}"
