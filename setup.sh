#!/usr/bin/env bash
# Setup post-clone per lightrag-kb (macOS/Linux; su Windows usa WSL o Git Bash).
# Crea un unico runtime Python condiviso per ragcli, MCP, LightRAG, Docling e shim OAuth.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

OS_NAME="$(uname -s)"
BIN_DIR="$HOME/.local/bin"
VENV_DIR="$REPO_DIR/.venv"
ENGINE_PY="$VENV_DIR/bin/python"
SHIM_DIR="$REPO_DIR/services/obsidian-mcp-oauth-shim"
GLOBAL_ENV="$REPO_DIR/config/global.env"
GLOBAL_EXAMPLE="$REPO_DIR/config/global.env.example"
SHIM_ENV="$SHIM_DIR/.env"
SHIM_EXAMPLE="$SHIM_DIR/.env.example"

upsert_env_var() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp
  mkdir -p "$(dirname "$file")"
  touch "$file"
  tmp="$(mktemp)"
  if grep -Eq "^${key}=" "$file"; then
    sed -E "s|^${key}=.*|${key}=${value}|" "$file" > "$tmp"
    mv "$tmp" "$file"
  else
    cat "$file" > "$tmp"
    printf "%s=%s\n" "$key" "$value" >> "$tmp"
    mv "$tmp" "$file"
  fi
}

detect_tailscale_url() {
  local public_port="${1:-8443}"
  python3 - "$public_port" <<'PY'
import json
import shutil
import subprocess
import sys

public_port = sys.argv[1]
tailscale = shutil.which("tailscale")
if not tailscale:
    sys.exit(0)

result = subprocess.run([tailscale, "status", "--json"], capture_output=True, text=True)
if result.returncode != 0:
    sys.exit(0)

try:
    payload = json.loads(result.stdout)
except json.JSONDecodeError:
    sys.exit(0)

self_data = payload.get("Self") or {}
dns_name = (self_data.get("DNSName") or "").rstrip(".")
if dns_name:
    suffix = "" if public_port in {"", "443"} else f":{public_port}"
    print(f"https://{dns_name}{suffix}")
PY
}

docling_platform_for_os() {
  case "$OS_NAME" in
    Linux) printf 'linux\n' ;;
    Darwin) printf 'mac\n' ;;
    *) printf 'auto\n' ;;
  esac
}

read_env_var() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key {print substr($0, index($0, "=") + 1)}' "$file" | tail -n1
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi

  echo "==> Installo uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"

  if ! command -v uv >/dev/null 2>&1; then
    echo "ERRORE: installazione di uv fallita." >&2
    exit 1
  fi
}

ensure_ollama() {
  if command -v ollama >/dev/null 2>&1; then
    return
  fi

  case "$OS_NAME" in
    Linux|Darwin)
      echo "==> Installo Ollama"
      curl -fsSL https://ollama.com/install.sh | sh
      hash -r
      ;;
    *)
      echo "ERRORE: Ollama non trovato e installazione automatica non supportata su $OS_NAME." >&2
      exit 1
      ;;
  esac

  if ! command -v ollama >/dev/null 2>&1; then
    echo "ERRORE: installazione di Ollama fallita." >&2
    exit 1
  fi
}

pull_ollama_model() {
  local model_name="$1"
  if [ -z "$model_name" ]; then
    return
  fi

  echo "==> Scarico modello Ollama $model_name"
  ollama pull "$model_name"
}

kb_exists() {
  local kb_name="$1"
  "$ENGINE_PY" - "$kb_name" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path("bin").resolve()))
from kb_registry import find_kb, load_registry  # noqa: E402

name = sys.argv[1]
sys.exit(0 if find_kb(load_registry(), name) else 1)
PY
}

write_public_kb_env() {
  local kb_name="$1"
  "$ENGINE_PY" - "$kb_name" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path("bin").resolve()))
from kb_registry import find_kb, global_env, load_registry, write_kb_env  # noqa: E402

name = sys.argv[1]
env = global_env()
registry = load_registry(env)
kb = find_kb(registry, name)
if kb is None:
    raise SystemExit(f"KB '{name}' non trovata.")
write_kb_env(kb, env)
PY
}

echo "==> Repo: $REPO_DIR"

ensure_uv

echo "==> Creo runtime condiviso .venv"
if [ ! -d "$VENV_DIR" ]; then
  uv venv "$VENV_DIR"
else
  echo "    .venv esiste già, skip creazione"
fi

echo "==> Installo LightRAG, Docling, shim OAuth e dipendenze nel runtime condiviso"
uv pip install --python "$ENGINE_PY" \
  "lightrag-hku[api]" \
  ollama \
  "docling[asr,ocrmac,easyocr,rapidocr]" \
  requests \
  pyyaml \
  fastmcp \
  pytest
uv pip install --python "$ENGINE_PY" -r "$SHIM_DIR/requirements.txt"

echo "==> Metto ragcli e lightrag-server in PATH"
mkdir -p "$BIN_DIR"
chmod +x bin/ragcli
ln -sf "$REPO_DIR/bin/ragcli" "$BIN_DIR/ragcli"
ln -sf "$VENV_DIR/bin/lightrag-server" "$BIN_DIR/lightrag-server"
echo "    symlink creati in $BIN_DIR"

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

if [ ! -f "$GLOBAL_ENV" ]; then
  echo "==> Creo config/global.env da global.env.example"
  cp "$GLOBAL_EXAMPLE" "$GLOBAL_ENV"
else
  echo "==> config/global.env esiste già, lo aggiorno senza perdere i valori presenti"
fi

upsert_env_var "$GLOBAL_ENV" "ENGINE_PYTHON" "$VENV_DIR/bin/python"
upsert_env_var "$GLOBAL_ENV" "LIGHTRAG_SERVER_BIN" "$VENV_DIR/bin/lightrag-server"
upsert_env_var "$GLOBAL_ENV" "DOCLING_PYTHON" "$VENV_DIR/bin/python"
upsert_env_var "$GLOBAL_ENV" "LLM_MODEL" "gemma4:e2b"
upsert_env_var "$GLOBAL_ENV" "DOCLING_PLATFORM" "$(docling_platform_for_os)"
upsert_env_var "$GLOBAL_ENV" "MCP_BIND_HOST" "$(read_env_var "$GLOBAL_ENV" "MCP_BIND_HOST" || true)"
upsert_env_var "$GLOBAL_ENV" "MCP_BIND_PORT" "$(read_env_var "$GLOBAL_ENV" "MCP_BIND_PORT" || true)"
upsert_env_var "$GLOBAL_ENV" "MCP_PATH" "$(read_env_var "$GLOBAL_ENV" "MCP_PATH" || true)"
upsert_env_var "$GLOBAL_ENV" "PUBLIC_MCP_KB" "$(read_env_var "$GLOBAL_ENV" "PUBLIC_MCP_KB" || true)"
upsert_env_var "$GLOBAL_ENV" "SHIM_BIND_HOST" "$(read_env_var "$GLOBAL_ENV" "SHIM_BIND_HOST" || true)"
upsert_env_var "$GLOBAL_ENV" "SHIM_BIND_PORT" "$(read_env_var "$GLOBAL_ENV" "SHIM_BIND_PORT" || true)"
upsert_env_var "$GLOBAL_ENV" "TAILSCALE_FUNNEL_PORT" "$(read_env_var "$GLOBAL_ENV" "TAILSCALE_FUNNEL_PORT" || true)"

for key in MCP_BIND_HOST MCP_BIND_PORT MCP_PATH SHIM_BIND_HOST SHIM_BIND_PORT TAILSCALE_FUNNEL_PORT; do
  if [ -z "$(read_env_var "$GLOBAL_ENV" "$key")" ]; then
    case "$key" in
      MCP_BIND_HOST) upsert_env_var "$GLOBAL_ENV" "$key" "127.0.0.1" ;;
      MCP_BIND_PORT) upsert_env_var "$GLOBAL_ENV" "$key" "8743" ;;
      MCP_PATH) upsert_env_var "$GLOBAL_ENV" "$key" "/mcp" ;;
      SHIM_BIND_HOST) upsert_env_var "$GLOBAL_ENV" "$key" "127.0.0.1" ;;
      SHIM_BIND_PORT) upsert_env_var "$GLOBAL_ENV" "$key" "8420" ;;
      TAILSCALE_FUNNEL_PORT) upsert_env_var "$GLOBAL_ENV" "$key" "8443" ;;
    esac
  fi
done

if [ ! -f "$SHIM_ENV" ]; then
  echo "==> Creo la config dello shim da .env.example"
  cp "$SHIM_EXAMPLE" "$SHIM_ENV"
fi

TAILSCALE_FUNNEL_PORT="$(read_env_var "$GLOBAL_ENV" "TAILSCALE_FUNNEL_PORT")"
SHIM_PUBLIC_URL="$(read_env_var "$GLOBAL_ENV" "SHIM_PUBLIC_URL")"
if [ -z "$SHIM_PUBLIC_URL" ] || [ "$SHIM_PUBLIC_URL" = "https://your-node.your-tailnet.ts.net" ]; then
  DETECTED_URL="$(detect_tailscale_url "${TAILSCALE_FUNNEL_PORT:-8443}" || true)"
  if [ -n "$DETECTED_URL" ]; then
    SHIM_PUBLIC_URL="$DETECTED_URL"
    upsert_env_var "$GLOBAL_ENV" "SHIM_PUBLIC_URL" "$SHIM_PUBLIC_URL"
  fi
fi

SHIM_BIND_HOST="$(read_env_var "$GLOBAL_ENV" "SHIM_BIND_HOST")"
SHIM_BIND_PORT="$(read_env_var "$GLOBAL_ENV" "SHIM_BIND_PORT")"
MCP_BIND_HOST="$(read_env_var "$GLOBAL_ENV" "MCP_BIND_HOST")"
MCP_BIND_PORT="$(read_env_var "$GLOBAL_ENV" "MCP_BIND_PORT")"
MCP_PATH="$(read_env_var "$GLOBAL_ENV" "MCP_PATH")"
PUBLIC_MCP_KB="$(read_env_var "$GLOBAL_ENV" "PUBLIC_MCP_KB")"
PUBLISH_READY=0

upsert_env_var "$SHIM_ENV" "SHIM_BIND_HOST" "${SHIM_BIND_HOST:-127.0.0.1}"
upsert_env_var "$SHIM_ENV" "SHIM_BIND_PORT" "${SHIM_BIND_PORT:-8420}"
upsert_env_var "$SHIM_ENV" "PORT" "${SHIM_BIND_PORT:-8420}"
upsert_env_var "$SHIM_ENV" "LIGHTRAG_MCP_URL" "http://${MCP_BIND_HOST:-127.0.0.1}:${MCP_BIND_PORT:-8743}${MCP_PATH:-/mcp}"
upsert_env_var "$SHIM_ENV" "LIGHTRAG_MCP_VERIFY_SSL" "false"
if [ -n "$SHIM_PUBLIC_URL" ]; then
  upsert_env_var "$SHIM_ENV" "ISSUER_URL" "$SHIM_PUBLIC_URL"
fi

LLM_PROVIDER="$(read_env_var "$GLOBAL_ENV" "LLM_PROVIDER")"
LLM_MODEL="$(read_env_var "$GLOBAL_ENV" "LLM_MODEL")"
EMBEDDING_MODEL="$(read_env_var "$GLOBAL_ENV" "EMBEDDING_MODEL")"
if [ "${LLM_PROVIDER:-ollama}" = "ollama" ]; then
  ensure_ollama
  pull_ollama_model "$LLM_MODEL"
  pull_ollama_model "$EMBEDDING_MODEL"
fi

if [ "$OS_NAME" = "Linux" ]; then
  echo "==> Installo unit systemd --user"
  "$ENGINE_PY" bin/ragcli.py service install-units
  systemctl --user daemon-reload

  if [ -n "${PUBLIC_MCP_KB:-}" ]; then
    if kb_exists "$PUBLIC_MCP_KB"; then
      echo "==> Preparo env e servizi per la KB pubblica '$PUBLIC_MCP_KB'"
      write_public_kb_env "$PUBLIC_MCP_KB"
      systemctl --user enable --now "lightrag-server@${PUBLIC_MCP_KB}.service"
      systemctl --user enable --now "lightrag-watchdog@${PUBLIC_MCP_KB}.service"
      systemctl --user enable --now "lightrag-mcp@${PUBLIC_MCP_KB}.service"
      systemctl --user enable --now obsidian-mcp-oauth-shim.service
      PUBLISH_READY=1
    else
      echo "    PUBLIC_MCP_KB='$PUBLIC_MCP_KB' non trovata nel registry: salto avvio servizi pubblici"
    fi
  else
    echo "    PUBLIC_MCP_KB non configurata: installo solo le unit senza avviare MCP pubblico e shim"
  fi

  if [ "$PUBLISH_READY" -eq 1 ] && command -v tailscale >/dev/null 2>&1; then
    echo "==> Configuro publish Tailscale per lo shim"
    tailscale serve --bg "${TAILSCALE_FUNNEL_PORT:-8443}" "http://${SHIM_BIND_HOST:-127.0.0.1}:${SHIM_BIND_PORT:-8420}"
    tailscale funnel --bg "${TAILSCALE_FUNNEL_PORT:-8443}"
    tailscale funnel status || true
  elif [ "$PUBLISH_READY" -eq 1 ]; then
    echo "    Tailscale non trovato: salto la publish pubblica dello shim"
  else
    echo "    Publish Tailscale non configurato perché il backend MCP pubblico non è stato avviato"
  fi
else
  echo "==> Sistema non Linux: salto installazione systemd --user e Funnel"
fi

echo "==> Verifica finale"
"$BIN_DIR/ragcli" doctor || true

echo "==> Setup completato."
echo "    Comandi utili:"
echo "      ragcli doctor"
echo "      ragcli service status"
if [ "$PUBLISH_READY" -eq 1 ] && [ -n "${SHIM_PUBLIC_URL:-}" ]; then
  echo "      URL pubblico shim: $SHIM_PUBLIC_URL/mcp"
elif [ -n "${SHIM_PUBLIC_URL:-}" ]; then
  echo "      URL shim configurato (non pubblicato): $SHIM_PUBLIC_URL/mcp"
fi
