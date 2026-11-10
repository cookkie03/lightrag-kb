import os
import secrets
from pathlib import Path

from dotenv import load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if not ENV_PATH.exists():
    ENV_PATH.touch(mode=0o600)

load_dotenv(ENV_PATH)


# Riutilizza un segreto configurato oppure lo genera e lo persiste nel file protetto.
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

HOST = os.getenv("SHIM_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.getenv("SHIM_BIND_PORT", os.getenv("PORT", "8420")))
ISSUER_URL = os.getenv(
    "ISSUER_URL",
    os.getenv("SHIM_PUBLIC_URL", f"http://{HOST}:{PORT}"),
).rstrip("/")

LIGHTRAG_MCP_URL = os.getenv(
    "LIGHTRAG_MCP_URL",
    os.getenv("OBSIDIAN_MCP_URL", "http://127.0.0.1:8743/mcp"),
).rstrip("/")
LIGHTRAG_MCP_VERIFY_SSL = os.getenv(
    "LIGHTRAG_MCP_VERIFY_SSL",
    os.getenv("OBSIDIAN_VERIFY_SSL", "false"),
).lower() == "true"

ACCESS_TOKEN_TTL = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "3600"))
REFRESH_TOKEN_TTL = int(os.getenv("REFRESH_TOKEN_TTL_SECONDS", str(60 * 60 * 24 * 30)))
AUTH_CODE_TTL = int(os.getenv("AUTH_CODE_TTL_SECONDS", "300"))

DB_PATH = os.getenv("DB_PATH") or str(BASE_DIR / "shim.db")

JWT_ALGORITHM = "HS256"
