#!/usr/bin/env bash
# Setup post-clone per lightrag-kb (macOS/Linux; su Windows usa WSL o Git Bash).
# Crea un unico runtime Python condiviso per ragcli, MCP, LightRAG e Docling.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "==> Repo: $REPO_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "ERRORE: 'uv' non trovato. Installalo prima di continuare:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

echo "==> Creo runtime condiviso .venv-engine"
if [ ! -d .venv-engine ]; then
  uv venv .venv-engine
else
  echo "    .venv-engine esiste già, skip creazione"
fi

echo "==> Installo LightRAG, Docling e dipendenze nel runtime condiviso"
uv pip install --python .venv-engine/bin/python \
  "lightrag-hku[api]" \
  ollama \
  "docling[asr,ocrmac,easyocr]" \
  requests \
  pyyaml \
  fastmcp

echo "==> Metto ragcli e lightrag-server in PATH"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
chmod +x bin/ragcli
ln -sf "$REPO_DIR/bin/ragcli" "$BIN_DIR/ragcli"
echo "    symlink creato: $BIN_DIR/ragcli -> $REPO_DIR/bin/ragcli"
ln -sf "$REPO_DIR/.venv-engine/bin/lightrag-server" "$BIN_DIR/lightrag-server"
echo "    symlink creato: $BIN_DIR/lightrag-server -> $REPO_DIR/.venv-engine/bin/lightrag-server"

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
  echo "    Ricordati di inserire le tue API key in config/global.env"
else
  echo "==> config/global.env esiste già, skip copia"
fi

echo "==> Setup completato. Verifica con: ragcli list"
