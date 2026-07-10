import base64
import hashlib
import secrets
import time

import jwt

from . import config


def gen_client_id() -> str:
    return "client_" + secrets.token_urlsafe(16)


def gen_auth_code() -> str:
    return secrets.token_urlsafe(32)


def gen_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return constant_time_eq(computed, code_challenge)


def issue_access_token(client_id: str) -> str:
    now = int(time.time())
    payload = {
        "iss": config.ISSUER_URL,
        "aud": config.ISSUER_URL,
        "sub": client_id,
        "iat": now,
        "exp": now + config.ACCESS_TOKEN_TTL,
        "scope": "mcp",
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def verify_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[config.JWT_ALGORITHM],
            audience=config.ISSUER_URL,
            issuer=config.ISSUER_URL,
        )
    except jwt.PyJWTError:
        return None
