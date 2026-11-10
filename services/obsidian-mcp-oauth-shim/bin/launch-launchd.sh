#!/usr/bin/env bash
# Launcher dedicato a launchd. Usa path relativi al clone locale così il repo
# resta spostabile senza dover riscrivere il wrapper a mano.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_DIR="$(cd "$PROJECT_DIR/../.." && pwd)"

if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

cd "$PROJECT_DIR"
exec "$REPO_DIR/.venv/bin/python" -m uvicorn app.main:app --host "${SHIM_BIND_HOST:-127.0.0.1}" --port "${SHIM_BIND_PORT:-8420}"
