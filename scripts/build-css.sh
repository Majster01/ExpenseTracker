#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
"$PROJECT_DIR/.tailwind/tailwindcss" \
  -i "$PROJECT_DIR/frontend/tailwind/input.css" \
  -o "$PROJECT_DIR/frontend/static/css/app.css" \
  --minify
