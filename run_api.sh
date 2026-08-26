#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$PROJECT_DIR/.venv/bin/python"
UVICORN="$PROJECT_DIR/.venv/bin/uvicorn"

if [ ! -x "$PYTHON" ] || [ ! -x "$UVICORN" ]; then
    echo "Project .venv is missing or incomplete. Run: python3 -m venv .venv" >&2
    exit 1
fi

exec "$UVICORN" backend.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
