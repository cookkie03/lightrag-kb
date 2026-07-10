#!/bin/zsh
# Launcher dedicato a launchd. Deve essere copiato in ~/.local/bin perché
# launchd su questo Mac non avvia in modo affidabile interpreti Python sotto
# ~/Documents; il progetto vero rimane nel repository.
set -euo pipefail

PROJECT_DIR="/Users/luca/Documents/lightrag-kb/services/obsidian-mcp-oauth-shim"
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8420
