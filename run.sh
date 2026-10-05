#!/bin/sh
# 5S Vision - start on Linux / macOS
cd "$(dirname "$0")" || exit 1
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8780}"
