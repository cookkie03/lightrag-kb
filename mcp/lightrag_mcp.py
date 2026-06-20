#!/usr/bin/env python3
"""lightrag_mcp.py — MCP server per UNA knowledge base LightRAG.

Espone a Claude Code gli strumenti per interrogare e alimentare la KB,
inoltrando alle API REST del lightrag-server della KB (porta dal registry.yaml).

Avvio:  python lightrag_mcp.py --kb <name>
Registrazione: ragcli mcp-add <name>
"""
from __future__ import annotations

import argparse
from pathlib import Path

import requests
import yaml
from fastmcp import FastMCP

HOME = Path(__file__).resolve().parent.parent
REGISTRY = HOME / "registry.yaml"

ap = argparse.ArgumentParser()
ap.add_argument("--kb", required=True)
args, _ = ap.parse_known_args()

reg = yaml.safe_load(REGISTRY.read_text()) or {"kbs": []}
kb = next((k for k in reg["kbs"] if k["name"] == args.kb), None)
if not kb:
    raise SystemExit(f"KB '{args.kb}' non trovata in {REGISTRY}")

PORT = kb["port"]
BASE = f"http://127.0.0.1:{PORT}"
KB_NAME = args.kb

mcp = FastMCP(f"lightrag-{KB_NAME}")


def _server_up() -> bool:
    try:
        return requests.get(f"{BASE}/health", timeout=3).ok
    except Exception:
        return False


def _get(path: str, **params) -> str:
    if not _server_up():
        return f"⚠ Server KB '{KB_NAME}' non attivo su :{PORT}."
    params = {k: v for k, v in params.items() if v is not None}
    r = requests.get(f"{BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return str(r.json())


def _post(path: str, payload: dict, timeout: int = 300):
    if not _server_up():
        return f"⚠ Il server della KB '{KB_NAME}' non è attivo su :{PORT}. Avvialo con `ragcli start {KB_NAME}` o il toggle."
    payload = {k: v for k, v in payload.items() if v is not None}
    r = requests.post(f"{BASE}{path}", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


@mcp.tool()
def query(
    question: str,
    mode: str = "mix",
    top_k: int | None = None,
    chunk_top_k: int | None = None,
    max_entity_tokens: int | None = None,
    max_relation_tokens: int | None = None,
    max_total_tokens: int | None = None,
    only_need_context: bool = False,
    only_need_prompt: bool = False,
    response_type: str | None = None,
    enable_rerank: bool | None = None,
    hl_keywords: list[str] | None = None,
    ll_keywords: list[str] | None = None,
    user_prompt: str | None = None,
) -> str:
    """Interroga la knowledge base '%s'.

    mode: mix (default, ibrido), local, global, hybrid, naive, bypass.
    - naive  : solo retrieval vettoriale sui chunk, senza grafo
    - local  : dettagli/entità specifiche
    - global : temi e relazioni d'insieme
    - hybrid : local + global combinati
    - mix    : vettoriale + grafo (consigliato per uso generale)
    - bypass : nessun retrieval, query diretta al LLM

    top_k: numero di entità (local) o relazioni (global) da recuperare.
    chunk_top_k: numero di chunk di testo da recuperare.
    max_entity_tokens / max_relation_tokens / max_total_tokens: budget token per il contesto.
    only_need_context: se True, ritorna solo il contesto recuperato (entità/relazioni/chunk)
        senza generare una risposta — utile per ispezionare cosa il retrieval ha trovato
        o per farlo ragionare a Claude stesso invece che al LLM del server.
    only_need_prompt: se True, ritorna solo il prompt completo che verrebbe inviato al LLM.
    response_type: formato desiderato, es. 'Bullet Points', 'Single Paragraph'.
    enable_rerank: abilita/disabilita il rerank dei chunk (default server: True).
    hl_keywords / ll_keywords: keyword alto/basso livello per guidare il retrieval
        (se omesse, generate automaticamente dal LLM).
    user_prompt: prompt custom che sostituisce il template di default.
    """ % KB_NAME
    data = _post("/query", {
        "query": question,
        "mode": mode,
        "top_k": top_k,
        "chunk_top_k": chunk_top_k,
        "max_entity_tokens": max_entity_tokens,
        "max_relation_tokens": max_relation_tokens,
        "max_total_tokens": max_total_tokens,
        "only_need_context": only_need_context or None,
        "only_need_prompt": only_need_prompt or None,
        "response_type": response_type,
        "enable_rerank": enable_rerank,
        "hl_keywords": hl_keywords,
        "ll_keywords": ll_keywords,
        "user_prompt": user_prompt,
    })
    if isinstance(data, str):
        return data
    return data.get("response", data) if isinstance(data, dict) else str(data)


@mcp.tool()
def insert_text(text: str, source: str = "mcp-input") -> str:
    """Inserisce nuovo testo nella knowledge base '%s' (con embedding + estrazione).""" % KB_NAME
    _post("/documents/text", {"text": text, "file_source": source})
    return f"✓ Testo inserito in '{KB_NAME}' (sorgente: {source})."


@mcp.tool()
def search_entities(q: str, limit: int = 50) -> str:
    """Cerca entità/etichette nel grafo della KB '%s' per nome (fuzzy match).

    Utile per scoprire come è scritta esattamente un'entità prima di usarla
    in get_entity_graph, o per esplorare cosa contiene il grafo su un argomento.
    """ % KB_NAME
    return _get("/graph/label/search", q=q, limit=limit)


@mcp.tool()
def list_entities(popular: bool = False, limit: int = 300) -> str:
    """Elenca le entità (etichette) presenti nel grafo della KB '%s'.

    popular: se True, ordina per grado di connessione (entità più centrali/rilevanti)
        invece che alfabeticamente.
    """ % KB_NAME
    if popular:
        return _get("/graph/label/popular", limit=limit)
    return _get("/graph/label/list")


@mcp.tool()
def entity_exists(name: str) -> str:
    """Verifica se un'entità con questo nome esiste nel grafo della KB '%s'.""" % KB_NAME
    return _get("/graph/entity/exists", name=name)


@mcp.tool()
def get_entity_graph(label: str, max_depth: int = 3, max_nodes: int = 1000) -> str:
    """Estrae il sottografo (nodi+relazioni) attorno a un'entità della KB '%s'.

    label: nome dell'entità di partenza (usa search_entities per trovarla).
    max_depth: profondità massima dei salti dal nodo di partenza.
    max_nodes: massimo numero di nodi da restituire.
    Restituisce nodi e archi del sottografo (entità, relazioni, descrizioni).
    """ % KB_NAME
    return _get("/graphs", label=label, max_depth=max_depth, max_nodes=max_nodes)


@mcp.tool()
def kb_status() -> str:
    """Stato della knowledge base '%s' (server attivo, porta, WebUI).""" % KB_NAME
    up = _server_up()
    return (f"KB: {KB_NAME}\nServer: {'ATTIVO' if up else 'SPENTO'}\n"
            f"Porta: {PORT}\nWebUI: {BASE}\nSorgente: {kb['source_folder']}")


if __name__ == "__main__":
    mcp.run()
