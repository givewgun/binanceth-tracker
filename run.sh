#!/usr/bin/env bash
# Convenience launcher: sets up a virtualenv on first run, then serves the app.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv…"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "No .env found — copying .env.example. Add your API keys, then re-run."
  cp .env.example .env
  exit 1
fi

exec ./.venv/bin/python -m app.main "$@"
