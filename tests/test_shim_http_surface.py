from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import httpx


def _load_shim_app(tmp_path: Path):
    db_path = tmp_path / "shim-test.db"
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name)
    import os

    os.environ["DB_PATH"] = str(db_path)
    os.environ["SHIM_PUBLIC_URL"] = "https://mcp.debian.tailscale.net"
    os.environ["ISSUER_URL"] = "https://mcp.debian.tailscale.net"
    os.environ["LIGHTRAG_MCP_URL"] = "http://127.0.0.1:8743/mcp"
    module = importlib.import_module("app.main")
    return module.app


def test_discovery_endpoints_remain_available(tmp_path: Path) -> None:
    app = _load_shim_app(tmp_path)

    async def _run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            root = await client.get("/")
            authz = await client.get("/.well-known/oauth-authorization-server")
            resource = await client.get("/.well-known/oauth-protected-resource")

            assert root.status_code == 200
            assert root.json()["service"] == "lightrag-mcp-oauth-shim"
            assert authz.status_code == 200
            assert resource.status_code == 200
            assert resource.json()["resource"].endswith("/mcp")

    asyncio.run(_run())
