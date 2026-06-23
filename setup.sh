#!/usr/bin/env bash
# Setup post-clone per lightrag-kb (macOS/Linux; su Windows usa WSL o Git Bash).
# Crea il venv per ragcli/MCP, installa LightRAG come uv tool, mette ragcli in PATH
# e prepara config/global.env. Eseguibile più volte senza problemi (idempotente).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "==> Repo: $REPO_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERRORE: 'uv' non trovato. Installalo prima di continuare:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

echo "==> Installo LightRAG come uv tool (con supporto ollama)"
uv tool install "lightrag-hku[api]" --with ollama

echo "==> Creo venv .venv-mcp per ragcli/MCP server"
if [ ! -d .venv-mcp ]; then
  uv venv .venv-mcp
else
  echo "    .venv-mcp esiste già, skip creazione"
fi

echo "==> Installo le dipendenze Python in .venv-mcp"
uv pip install --python .venv-mcp/bin/python requests pyyaml fastmcp

echo "==> Metto ragcli in PATH"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
chmod +x bin/ragcli
ln -sf "$REPO_DIR/bin/ragcli" "$BIN_DIR/ragcli"
echo "    symlink creato: $BIN_DIR/ragcli -> $REPO_DIR/bin/ragcli"

case ":$PATH:" in
  *":$BIN_DIR:"*)
    echo "    $BIN_DIR è già nel PATH"
    ;;
  *)
    echo "    ATTENZIONE: $BIN_DIR non è nel PATH."
    echo "    Aggiungi questa riga al tuo ~/.bashrc o ~/.zshrc:"
    echo "        export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

if [ ! -f config/global.env ]; then
  echo "==> Creo config/global.env da global.env.example"
  cp config/global.env.example config/global.env
  echo "    Ricordati di inserire le tue API key (OpenRouter, MinerU cloud) in config/global.env"
else
  echo "==> config/global.env esiste già, skip copia"
fi

echo "==> Setup completato. Verifica con: ragcli status"
