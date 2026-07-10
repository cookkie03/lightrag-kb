import os
import secrets
from pathlib import Path

from dotenv import load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if not ENV_PATH.exists():
    ENV_PATH.touch(mode=0o600)

load_dotenv(ENV_PATH)


def _ensure_secret(key: str, nbytes: int = 32) -> str:
    value = os.getenv(key)
    if not value:
        value = secrets.token_urlsafe(nbytes)
        set_key(str(ENV_PATH), key, value, quote_mode="never")
        os.environ[key] = value
        os.chmod(ENV_PATH, 0o600)
    return value


JWT_SECRET = _ensure_secret("JWT_SECRET", 48)
CONSENT_PASSCODE = _ensure_secret("CONSENT_PASSCODE", 12)

ISSUER_URL = os.getenv("ISSUER_URL", "http://127.0.0.1:8420").rstrip("/")
PORT = int(os.getenv("PORT", "8420"))

OBSIDIAN_MCP_URL = os.getenv("OBSIDIAN_MCP_URL", "https://127.0.0.1:27124").rstrip("/")
OBSIDIAN_API_KEY = os.getenv("OBSIDIAN_API_KEY", "")
OBSIDIAN_VERIFY_SSL = os.getenv("OBSIDIAN_VERIFY_SSL", "false").lower() == "true"

# Percorso del data.json del plugin "Local REST API with MCP": contiene la
# apiKey autoritativa. Il plugin la rigenera (insieme al certificato TLS)
# quando la sua config non è ancora presente su un device che sincronizza lo
# stesso vault via iCloud — leggerla da qui invece che da OBSIDIAN_API_KEY
# rende lo shim resiliente a queste rigenerazioni: OBSIDIAN_API_KEY resta
# solo come fallback se questo file non è raggiungibile.
OBSIDIAN_PLUGIN_DATA_JSON = os.getenv(
    "OBSIDIAN_PLUGIN_DATA_JSON",
    "/Users/luca/Library/Mobile Documents/iCloud~md~obsidian/Documents/Second-Brain"
    "/.obsidian/plugins/obsidian-local-rest-api/data.json",
)

ACCESS_TOKEN_TTL = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))
REFRESH_TOKEN_TTL = int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 30)))
AUTH_CODE_TTL = int(os.getenv("AUTH_CODE_TTL_SECONDS", "300"))

DB_PATH = os.getenv("DB_PATH") or str(BASE_DIR / "shim.db")

JWT_ALGORITHM = "HS256"
