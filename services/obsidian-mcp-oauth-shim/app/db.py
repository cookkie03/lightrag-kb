import json
import sqlite3
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT,
    redirect_uris TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    state TEXT,
    expires_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def create_client(client_id: str, client_name: str, redirect_uris: list[str]):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO clients (client_id, client_name, redirect_uris, created_at) VALUES (?, ?, ?, ?)",
            (client_id, client_name, json.dumps(redirect_uris), int(time.time())),
        )


def get_client(client_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        if not row:
            return None
        return {
            "client_id": row["client_id"],
            "client_name": row["client_name"],
            "redirect_uris": json.loads(row["redirect_uris"]),
            "created_at": row["created_at"],
        }


def list_clients():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM clients ORDER BY created_at DESC").fetchall()
        return [
            {
                "client_id": r["client_id"],
                "client_name": r["client_name"],
                "redirect_uris": json.loads(r["redirect_uris"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def delete_client(client_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM clients WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM auth_codes WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM refresh_tokens WHERE client_id = ?", (client_id,))


def store_auth_code(code: str, client_id: str, redirect_uri: str, code_challenge: str, state: str | None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO auth_codes (code, client_id, redirect_uri, code_challenge, state, expires_at, used) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (code, client_id, redirect_uri, code_challenge, state, int(time.time()) + config.AUTH_CODE_TTL),
        )


def consume_auth_code(code: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM auth_codes WHERE code = ?", (code,)).fetchone()
        if not row:
            return None
        if row["used"] or row["expires_at"] < int(time.time()):
            return None
        conn.execute("UPDATE auth_codes SET used = 1 WHERE code = ?", (code,))
        return dict(row)


def store_refresh_token(token_hash: str, client_id: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO refresh_tokens (token_hash, client_id, expires_at, revoked, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (token_hash, client_id, int(time.time()) + config.REFRESH_TOKEN_TTL, int(time.time())),
        )


def get_refresh_token(token_hash: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
        return dict(row) if row else None


def revoke_refresh_token(token_hash: str):
    with get_conn() as conn:
        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))


def revoke_all_refresh_tokens_for_client(client_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE client_id = ?", (client_id,))


def purge_expired():
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("DELETE FROM auth_codes WHERE expires_at < ?", (now,))
        conn.execute("DELETE FROM refresh_tokens WHERE expires_at < ? AND revoked = 0", (now - 86400,))
