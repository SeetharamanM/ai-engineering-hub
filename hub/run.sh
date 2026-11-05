#!/usr/bin/env bash
# Start the AI Engineering Hub web UI. Extra args are passed through, e.g. ./run.sh --port 9000
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -d .venv ]; then
  echo "Creating hub virtual environment…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r requirements.txt
fi
exec .venv/bin/python -m hub_ui "$@"
