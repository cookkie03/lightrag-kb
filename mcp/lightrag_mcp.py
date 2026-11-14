#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests
from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from kb_registry import find_kb, load_registry  # noqa: E402

STRATEGY_TO_MODE = {
    "balanced": "mix",
    "entity_focus": "local",
    "theme_focus": "global",
    "hybrid": "hybrid",
    "vector_only": "naive",
    "llm_only": "bypass",
}

DOCUMENT_SORT_FIELDS = {"created_at", "updated_at", "id", "file_path"}
SORT_DIRECTIONS = {"asc", "desc"}

parser = argparse.ArgumentParser()
parser.add_argument("--kb", required=True)
parser.add_argument("--transport", choices=("stdio", "http", "streamable-http", "sse"), default="stdio")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8743)
parser.add_argument("--path", default="/mcp")
args, _ = parser.parse_known_args()

registry = load_registry()
kb = find_kb(registry, args.kb)
if not kb:
    raise SystemExit(f"KB '{args.kb}' not found.")

PORT = kb["port"]
BASE = f"http://127.0.0.1:{PORT}"
KB_NAME = args.kb
mcp = FastMCP(f"lightrag-{KB_NAME}")


# Probe the selected KB backend without raising on connection failures.
def _server_up() -> bool:
    try:
        return requests.get(f"{BASE}/health", timeout=3).ok
    except Exception:
        return False


# Preserve text responses and format structured results as readable JSON.
def _serialize(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


# Map supported client strategies to backend modes, rejecting unknown values.
def _normalize_strategy(strategy: str | None) -> str:
    if strategy is None:
        return "mix"
    mode = STRATEGY_TO_MODE.get(strategy)
    if mode is None:
        allowed = ", ".join(sorted(STRATEGY_TO_MODE))
        raise ValueError(f"Invalid strategy: {strategy}. Allowed values: {allowed}.")
    return mode


# Validate client context fields and translate them to backend payload names.
def _normalize_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if context is None:
        return {}
    allowed = {
        "top_k",
        "chunk_top_k",
        "max_entity_tokens",
        "max_relation_tokens",
        "max_total_tokens",
        "rerank",
        "hl_keywords",
        "ll_keywords",
        "prompt_override",
    }
    unknown = sorted(set(context) - allowed)
    if unknown:
        raise ValueError(
            "Unrecognized context parameters: "
            + ", ".join(unknown)
            + ". Allowed fields: "
            + ", ".join(sorted(allowed))
            + "."
        )
    payload = {
        "top_k": context.get("top_k"),
        "chunk_top_k": context.get("chunk_top_k"),
        "max_entity_tokens": context.get("max_entity_tokens"),
        "max_relation_tokens": context.get("max_relation_tokens"),
        "max_total_tokens": context.get("max_total_tokens"),
        "enable_rerank": context.get("rerank"),
        "hl_keywords": context.get("hl_keywords"),
        "ll_keywords": context.get("ll_keywords"),
        "user_prompt": context.get("prompt_override"),
    }
    return {k: v for k, v in payload.items() if v is not None}


# Return an actionable offline message before attempting a backend request.
def _ensure_server() -> str | None:
    if _server_up():
        return None
    return (
        f"⚠ Server for KB '{KB_NAME}' is not running on :{PORT}. "
        f"Start it with `ragcli start {KB_NAME}`."
    )


# Fetch and serialize a backend resource, omitting unspecified parameters.
def _get(path: str, **params: Any) -> str:
    offline = _ensure_server()
    if offline:
        return offline
    params = {k: v for k, v in params.items() if v is not None}
    response = requests.get(f"{BASE}{path}", params=params, timeout=30)
    response.raise_for_status()
    return _serialize(response.json())


# Send specified payload fields to the selected backend and return its JSON.
def _post(path: str, payload: dict[str, Any], timeout: int = 300) -> Any:
    offline = _ensure_server()
    if offline:
        return offline
    payload = {k: v for k, v in payload.items() if v is not None}
    response = requests.post(f"{BASE}{path}", json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


# Register a tool that queries the selected KB and returns its answer.
@mcp.tool()
def ask(
    question: str,
    strategy: str = "balanced",
    response_format: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    payload = {
        "query": question,
        "mode": _normalize_strategy(strategy),
        "response_type": response_format,
        **_normalize_context(context),
    }
    data = _post("/query", payload)
    if isinstance(data, str):
        return data
    return _serialize(data.get("response", data) if isinstance(data, dict) else data)


# Register a tool that returns the backend's structured retrieval context.
@mcp.tool()
def retrieve_context(
    question: str,
    strategy: str = "balanced",
    context: dict[str, Any] | None = None,
) -> str:
    payload = {
        "query": question,
        "mode": _normalize_strategy(strategy),
        **_normalize_context(context),
    }
    data = _post("/query/data", payload)
    return _serialize(data)


# Register a paginated document inventory tool with optional status counts.
@mcp.tool()
def find_documents(
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    sort_field: str = "updated_at",
    sort_direction: str = "desc",
    include_counts: bool = False,
) -> str:
    if sort_field not in DOCUMENT_SORT_FIELDS:
        allowed = ", ".join(sorted(DOCUMENT_SORT_FIELDS))
        raise ValueError(f"Invalid sort_field: {sort_field}. Allowed values: {allowed}.")
    if sort_direction not in SORT_DIRECTIONS:
        allowed = ", ".join(sorted(SORT_DIRECTIONS))
        raise ValueError(f"Invalid sort_direction: {sort_direction}. Allowed values: {allowed}.")
    data = _post(
        "/documents/paginated",
        {
            "status_filter": status,
            "page": page,
            "page_size": page_size,
            "sort_field": sort_field,
            "sort_direction": sort_direction,
        },
    )
    if isinstance(data, str):
        return data
    if not include_counts:
        return _serialize(data)
    counts = _get("/documents/status_counts")
    try:
        counts_data = json.loads(counts)
    except Exception:
        return counts
    return _serialize({"documents": data, "status_counts": counts_data})


if __name__ == "__main__":
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            path=args.path,
        )
