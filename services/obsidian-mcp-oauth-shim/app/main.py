import html
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from . import config, db, security

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("obsidian-mcp-oauth-shim")

HOP_BY_HOP = {"connection", "content-length", "transfer-encoding", "keep-alive", "upgrade"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    db.purge_expired()
    app.state.http_client = httpx.AsyncClient(verify=config.OBSIDIAN_VERIFY_SSL, timeout=None)
    log.info("shim avviato, issuer=%s, obsidian_mcp=%s", config.ISSUER_URL, config.OBSIDIAN_MCP_URL)
    yield
    await app.state.http_client.aclose()


app = FastAPI(title="Obsidian MCP OAuth Shim", lifespan=lifespan)


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "ok", "service": "obsidian-mcp-oauth-shim"}


@app.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    return JSONResponse({
        "issuer": config.ISSUER_URL,
        "authorization_endpoint": f"{config.ISSUER_URL}/authorize",
        "token_endpoint": f"{config.ISSUER_URL}/token",
        "registration_endpoint": f"{config.ISSUER_URL}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    return JSONResponse({
        "resource": f"{config.ISSUER_URL}/mcp",
        "authorization_servers": [config.ISSUER_URL],
    })


# --------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# --------------------------------------------------------------------------

@app.post("/register")
async def register(request: Request):
    body = await request.json()
    redirect_uris = body.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "redirect_uris is required"},
            status_code=400,
        )

    client_id = security.gen_client_id()
    client_name = body.get("client_name", "unknown")
    db.create_client(client_id, client_name, redirect_uris)

    return JSONResponse({
        "client_id": client_id,
        "client_id_issued_at": int(time.time()),
        "redirect_uris": redirect_uris,
        "client_name": client_name,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }, status_code=201)


# --------------------------------------------------------------------------
# Authorize (consent screen)
# --------------------------------------------------------------------------

def _consent_page(*, client_id, redirect_uri, state, code_challenge, code_challenge_method,
                   response_type, error=None):
    esc = html.escape
    error_html = f'<p class="error">{esc(error)}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>Autorizza accesso a Obsidian MCP</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 420px; margin: 80px auto; padding: 0 20px; }}
  .box {{ border: 1px solid #ddd; border-radius: 12px; padding: 24px; }}
  input[type=password] {{ width: 100%; padding: 10px; margin: 12px 0; box-sizing: border-box; font-size: 16px; }}
  button {{ width: 100%; padding: 12px; font-size: 16px; border-radius: 8px; border: none; cursor: pointer; }}
  .approve {{ background: #2563eb; color: white; margin-bottom: 8px; }}
  .deny {{ background: #eee; color: #333; }}
  .error {{ color: #c0392b; }}
  code {{ background: #f3f3f3; padding: 2px 4px; border-radius: 4px; }}
</style>
</head>
<body>
  <div class="box">
    <h2>Autorizza accesso al tuo Obsidian</h2>
    <p>Un client sta richiedendo accesso all'MCP del tuo vault Obsidian.</p>
    <p>Client ID: <code>{esc(client_id)}</code></p>
    {error_html}
    <form method="POST" action="/authorize">
      <input type="hidden" name="client_id" value="{esc(client_id)}">
      <input type="hidden" name="redirect_uri" value="{esc(redirect_uri)}">
      <input type="hidden" name="state" value="{esc(state or '')}">
      <input type="hidden" name="code_challenge" value="{esc(code_challenge)}">
      <input type="hidden" name="code_challenge_method" value="{esc(code_challenge_method)}">
      <input type="hidden" name="response_type" value="{esc(response_type)}">
      <input type="password" name="passcode" placeholder="Passcode" autofocus required>
      <button class="approve" type="submit" name="decision" value="approve">Autorizza</button>
      <button class="deny" type="submit" name="decision" value="deny">Nega</button>
    </form>
  </div>
</body>
</html>"""


@app.get("/authorize")
async def authorize_get(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str = "S256",
    state: str | None = None,
):
    if response_type != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if code_challenge_method != "S256":
        return JSONResponse({"error": "invalid_request", "error_description": "only S256 is supported"}, status_code=400)

    client = db.get_client(client_id)
    if not client:
        return JSONResponse({"error": "invalid_client"}, status_code=400)
    if redirect_uri not in client["redirect_uris"]:
        return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri not registered"}, status_code=400)

    return HTMLResponse(_consent_page(
        client_id=client_id, redirect_uri=redirect_uri, state=state,
        code_challenge=code_challenge, code_challenge_method=code_challenge_method,
        response_type=response_type,
    ))


@app.post("/authorize")
async def authorize_post(
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form(...),
    response_type: str = Form(...),
    passcode: str = Form(...),
    decision: str = Form(...),
):
    client = db.get_client(client_id)
    if not client or redirect_uri not in client["redirect_uris"]:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    if decision != "approve":
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}error=access_denied&state={state}", status_code=302)

    if not security.constant_time_eq(passcode, config.CONSENT_PASSCODE):
        return HTMLResponse(_consent_page(
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            code_challenge=code_challenge, code_challenge_method=code_challenge_method,
            response_type=response_type, error="Passcode errato.",
        ), status_code=401)

    code = security.gen_auth_code()
    db.store_auth_code(code, client_id, redirect_uri, code_challenge, state)

    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{sep}code={code}&state={state}", status_code=302)


# --------------------------------------------------------------------------
# Token endpoint
# --------------------------------------------------------------------------

def _oauth_error(error: str, description: str = "", status_code: int = 400):
    body = {"error": error}
    if description:
        body["error_description"] = description
    return JSONResponse(body, status_code=status_code)


@app.post("/token")
async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")

    if grant_type == "authorization_code":
        code = form.get("code")
        redirect_uri = form.get("redirect_uri")
        client_id = form.get("client_id")
        code_verifier = form.get("code_verifier")

        if not all([code, redirect_uri, client_id, code_verifier]):
            return _oauth_error("invalid_request", "missing parameters")

        record = db.consume_auth_code(code)
        if not record:
            return _oauth_error("invalid_grant", "code invalid, expired or already used")
        if record["client_id"] != client_id or record["redirect_uri"] != redirect_uri:
            return _oauth_error("invalid_grant", "client_id or redirect_uri mismatch")
        if not security.verify_pkce(code_verifier, record["code_challenge"]):
            return _oauth_error("invalid_grant", "PKCE verification failed")

        access_token = security.issue_access_token(client_id)
        refresh_token = security.gen_opaque_token()
        db.store_refresh_token(security.hash_token(refresh_token), client_id)

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": config.ACCESS_TOKEN_TTL,
            "refresh_token": refresh_token,
            "scope": "mcp",
        })

    if grant_type == "refresh_token":
        refresh_token = form.get("refresh_token")
        client_id = form.get("client_id")
        if not refresh_token:
            return _oauth_error("invalid_request", "missing refresh_token")

        token_hash = security.hash_token(refresh_token)
        record = db.get_refresh_token(token_hash)
        if not record or record["revoked"] or record["expires_at"] < int(time.time()):
            return _oauth_error("invalid_grant", "refresh token invalid, revoked or expired")
        if client_id and record["client_id"] != client_id:
            return _oauth_error("invalid_grant", "client_id mismatch")

        db.revoke_refresh_token(token_hash)
        new_refresh_token = security.gen_opaque_token()
        db.store_refresh_token(security.hash_token(new_refresh_token), record["client_id"])
        access_token = security.issue_access_token(record["client_id"])

        return JSONResponse({
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": config.ACCESS_TOKEN_TTL,
            "refresh_token": new_refresh_token,
            "scope": "mcp",
        })

    return _oauth_error("unsupported_grant_type", f"grant_type '{grant_type}' not supported")


# --------------------------------------------------------------------------
# MCP proxy
# --------------------------------------------------------------------------

def _current_obsidian_api_key() -> str:
    try:
        with open(config.OBSIDIAN_PLUGIN_DATA_JSON) as f:
            key = json.load(f).get("apiKey")
        if key:
            return key
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return config.OBSIDIAN_API_KEY


def _unauthorized():
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer resource_metadata="{config.ISSUER_URL}/.well-known/oauth-protected-resource"'},
    )


async def _proxy(request: Request, path: str):
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return _unauthorized()
    token_str = auth_header[7:].strip()
    claims = security.verify_access_token(token_str)
    if not claims:
        return _unauthorized()

    target_url = f"{config.OBSIDIAN_MCP_URL}/mcp/{path}"
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ({"host", "authorization"} | HOP_BY_HOP)
    }
    forward_headers["authorization"] = f"Bearer {_current_obsidian_api_key()}"

    body = await request.body()
    client: httpx.AsyncClient = request.app.state.http_client

    upstream_req = client.build_request(
        request.method, target_url,
        headers=forward_headers, params=request.query_params,
        content=body if body else None,
    )
    upstream = await client.send(upstream_req, stream=True)

    async def gen():
        async for chunk in upstream.aiter_raw():
            yield chunk
        await upstream.aclose()

    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
    return StreamingResponse(gen(), status_code=upstream.status_code, headers=resp_headers)


@app.api_route("/mcp", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def mcp_proxy_root(request: Request):
    return await _proxy(request, "")


@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def mcp_proxy_path(path: str, request: Request):
    return await _proxy(request, path)
