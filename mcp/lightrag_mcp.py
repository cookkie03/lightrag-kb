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
REGISTRY = HOME / "config" / "registry.yaml"

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

_DISABLED_MSG = (
    f"⛔ KB '{KB_NAME}' è disabilitata (`ragcli kb disable`): nessuna query/ingest/modifica "
    f"è permessa finché non viene riattivata con `ragcli kb enable {KB_NAME}`."
)


def _kb_enabled() -> bool:
    """Rilegge il registry ad ogni chiamata (non in cache), così se la KB viene
    disabilitata mentre questo processo MCP è già in esecuzione, il blocco è immediato."""
    try:
        reg = yaml.safe_load(REGISTRY.read_text()) or {"kbs": []}
    except Exception:
        return True  # in dubbio non blocchiamo per un errore di lettura registry
    kb_now = next((k for k in reg.get("kbs", []) if k["name"] == KB_NAME), None)
    return bool(kb_now) and kb_now.get("enabled", True)


def _server_up() -> bool:
    try:
        return requests.get(f"{BASE}/health", timeout=3).ok
    except Exception:
        return False


def _get(path: str, **params) -> str:
    if not _kb_enabled():
        return _DISABLED_MSG
    if not _server_up():
        return f"⚠ Server KB '{KB_NAME}' non attivo su :{PORT}."
    params = {k: v for k, v in params.items() if v is not None}
    r = requests.get(f"{BASE}{path}", params=params, timeout=30)
    r.raise_for_status()
    return str(r.json())


def _post(path: str, payload: dict, timeout: int = 300):
    if not _kb_enabled():
        return _DISABLED_MSG
    if not _server_up():
        return f"⚠ Il server della KB '{KB_NAME}' non è attivo su :{PORT}. Avvialo con `ragcli start {KB_NAME}` o il toggle."
    payload = {k: v for k, v in payload.items() if v is not None}
    r = requests.post(f"{BASE}{path}", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _delete(path: str, payload: dict | None = None, timeout: int = 60):
    if not _kb_enabled():
        return _DISABLED_MSG
    if not _server_up():
        return f"⚠ Il server della KB '{KB_NAME}' non è attivo su :{PORT}. Avvialo con `ragcli start {KB_NAME}` o il toggle."
    if payload is not None:
        payload = {k: v for k, v in payload.items() if v is not None}
    r = requests.delete(f"{BASE}{path}", json=payload, timeout=timeout)
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
def insert_texts(texts: list[str], sources: list[str] | None = None) -> str:
    """Inserisce più testi in batch nella knowledge base '%s' (con embedding + estrazione).

    texts: lista di testi da inserire.
    sources: lista di etichette sorgente, una per testo (opzionale, stesso ordine di texts).
    """ % KB_NAME
    data = _post("/documents/texts", {"texts": texts, "file_sources": sources})
    return str(data)


@mcp.tool()
def upload_document(file_path: str) -> str:
    """Carica un file (PDF, docx, txt, md, ecc.) nella knowledge base '%s' per l'indicizzazione.

    file_path: percorso assoluto del file sul filesystem locale.
    Il file viene copiato nella input dir della KB e processato in background
    (usa pipeline_status per seguirne l'avanzamento).
    """ % KB_NAME
    if not _kb_enabled():
        return _DISABLED_MSG
    if not _server_up():
        return f"⚠ Server KB '{KB_NAME}' non attivo su :{PORT}."
    p = Path(file_path).expanduser()
    if not p.is_file():
        return f"⚠ File non trovato: {p}"
    with p.open("rb") as f:
        r = requests.post(f"{BASE}/documents/upload", files={"file": (p.name, f)}, timeout=300)
    r.raise_for_status()
    return str(r.json())


@mcp.tool()
def list_documents(
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    sort_field: str = "updated_at",
    sort_direction: str = "desc",
) -> str:
    """Elenca i documenti indicizzati nella KB '%s', paginati.

    status: filtra per stato — pending, parsing, analyzing, processing, processed, failed.
        Se omesso, mostra tutti gli stati.
    page / page_size: paginazione (page_size 10-200).
    sort_field: created_at, updated_at, id, file_path.
    sort_direction: asc o desc.
    """ % KB_NAME
    data = _post("/documents/paginated", {
        "status_filter": status,
        "page": page,
        "page_size": page_size,
        "sort_field": sort_field,
        "sort_direction": sort_direction,
    })
    return str(data)


@mcp.tool()
def document_status_counts() -> str:
    """Conteggio dei documenti della KB '%s' per stato (pending/processing/processed/failed/...).""" % KB_NAME
    return _get("/documents/status_counts")


@mcp.tool()
def pipeline_status() -> str:
    """Stato della pipeline di indicizzazione della KB '%s' (in corso? quale job? a che punto?).

    Utile da controllare prima di interrogare dati che potrebbero essere stale, o per
    sapere se un ingest/scan è ancora in esecuzione.
    """ % KB_NAME
    return _get("/documents/pipeline_status")


@mcp.tool()
def scan_for_new_documents() -> str:
    """Avvia una scansione della input dir della KB '%s' per indicizzare nuovi file non ancora processati.

    Operazione asincrona in background; usa pipeline_status per seguirne l'avanzamento.
    Fallisce con 'scanning_skipped_pipeline_busy' se la pipeline è già occupata.
    """ % KB_NAME
    data = _post("/documents/scan", {})
    return str(data)


@mcp.tool()
def reprocess_failed_documents() -> str:
    """Rimette in coda per il riprocessamento i documenti della KB '%s' falliti in precedenza.""" % KB_NAME
    data = _post("/documents/reprocess_failed", {})
    return str(data)


@mcp.tool()
def delete_document(doc_ids: list[str], delete_file: bool = False, delete_llm_cache: bool = False) -> str:
    """Elimina uno o più documenti (e tutti i dati derivati: chunk, embedding, grafo) dalla KB '%s'.

    doc_ids: lista di ID documento (vedi list_documents per ottenerli).
    delete_file: se True, elimina anche il file sorgente dalla input dir.
    delete_llm_cache: se True, elimina anche la cache LLM di estrazione per quei documenti.
    Operazione irreversibile, eseguita in background.
    """ % KB_NAME
    data = _delete("/documents/delete_document", {
        "doc_ids": doc_ids,
        "delete_file": delete_file,
        "delete_llm_cache": delete_llm_cache,
    })
    return str(data)


@mcp.tool()
def query_data(
    question: str,
    mode: str = "mix",
    top_k: int | None = None,
    chunk_top_k: int | None = None,
) -> str:
    """Retrieval strutturato grezzo sulla KB '%s', SENZA generazione LLM.

    A differenza di query(), non produce una risposta in linguaggio naturale: restituisce
    direttamente entità, relazioni, chunk di testo e riferimenti usati per il retrieval.
    Utile per ispezionare cosa il grafo/vettoriale ha trovato, o per farlo ragionare
    a Claude stesso invece che al LLM del server (zero costo del modello locale, ma il
    ragionamento sui dati grezzi consuma token Claude).

    mode: local, global, hybrid, naive, mix, bypass (bypass restituisce array vuoti).
    top_k: entità (local) o relazioni (global) da recuperare.
    chunk_top_k: chunk di testo da recuperare.
    """ % KB_NAME
    data = _post("/query/data", {
        "query": question,
        "mode": mode,
        "top_k": top_k,
        "chunk_top_k": chunk_top_k,
    })
    return str(data)


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
def create_entity(name: str, description: str, entity_type: str | None = None) -> str:
    """Crea manualmente una nuova entità nel grafo della KB '%s'.

    name: nome univoco dell'entità.
    description: descrizione testuale.
    entity_type: categoria (es. PERSON, ORGANIZATION, LOCATION, CONCEPT). Opzionale.
    Utile per aggiungere conoscenza al grafo che il testo sorgente non rendeva esplicita,
    o per creare nodi-ponte prima di collegarli con create_relation.
    """ % KB_NAME
    entity_data = {"description": description, "entity_type": entity_type}
    data = _post("/graph/entity/create", {"entity_name": name, "entity_data": entity_data})
    return str(data)


@mcp.tool()
def update_entity(
    name: str,
    updated_data: dict,
    allow_rename: bool = False,
    allow_merge: bool = False,
) -> str:
    """Aggiorna le proprietà (o rinomina) un'entità del grafo della KB '%s'.

    name: nome attuale dell'entità.
    updated_data: dizionario con le proprietà da aggiornare (es. {"description": "...", "entity_name": "Nuovo Nome"}
        per rinominare — in tal caso serve allow_rename=True).
    allow_rename: consente di cambiare entity_name.
    allow_merge: se la rinomina collide con un'entità esistente, le fonde invece di fallire.
    """ % KB_NAME
    data = _post("/graph/entity/edit", {
        "entity_name": name,
        "updated_data": updated_data,
        "allow_rename": allow_rename,
        "allow_merge": allow_merge,
    })
    return str(data)


@mcp.tool()
def delete_entity(name: str) -> str:
    """Elimina un'entità e tutte le sue relazioni dal grafo della KB '%s'. Irreversibile.""" % KB_NAME
    data = _delete("/graph/entity/delete", {"entity_name": name})
    return str(data)


@mcp.tool()
def merge_entities(entities_to_change: list[str], entity_to_change_into: str) -> str:
    """Fonde più entità duplicate/malscritte in una sola, preservando tutte le relazioni, nella KB '%s'.

    entities_to_change: nomi delle entità da eliminare (le loro relazioni vengono trasferite).
    entity_to_change_into: entità di destinazione, che sopravvive e riceve tutte le relazioni.
    Usalo per correggere casi come entità duplicate o varianti di nome (es. "NY" / "New York").
    """ % KB_NAME
    data = _post("/graph/entities/merge", {
        "entities_to_change": entities_to_change,
        "entity_to_change_into": entity_to_change_into,
    })
    return str(data)


@mcp.tool()
def create_relation(
    source: str,
    target: str,
    description: str,
    keywords: str | None = None,
    weight: float = 1.0,
) -> str:
    """Crea una relazione tra due entità esistenti nel grafo della KB '%s'.

    source / target: nomi delle entità (devono già esistere — usa create_entity se necessario).
    description: descrizione testuale della relazione.
    keywords: parole chiave separate da virgola che descrivono il tipo di relazione.
    weight: forza/importanza della relazione (default 1.0).
    """ % KB_NAME
    relation_data = {"description": description, "keywords": keywords, "weight": weight}
    data = _post("/graph/relation/create", {
        "source_entity": source,
        "target_entity": target,
        "relation_data": relation_data,
    })
    return str(data)


@mcp.tool()
def update_relation(source: str, target: str, updated_data: dict) -> str:
    """Aggiorna le proprietà di una relazione esistente tra due entità della KB '%s'.

    source / target: nomi delle entità collegate dalla relazione.
    updated_data: dizionario con le proprietà da aggiornare (es. {"description": "...", "weight": 2.0}).
    """ % KB_NAME
    data = _post("/graph/relation/edit", {
        "source_id": source,
        "target_id": target,
        "updated_data": updated_data,
    })
    return str(data)


@mcp.tool()
def delete_relation(source: str, target: str) -> str:
    """Elimina la relazione tra due entità dal grafo della KB '%s'. Irreversibile.""" % KB_NAME
    data = _delete("/graph/relation/delete", {"source_entity": source, "target_entity": target})
    return str(data)


@mcp.tool()
def kb_status() -> str:
    """Stato della knowledge base '%s' (abilitata, server attivo, porta, WebUI).""" % KB_NAME
    enabled = _kb_enabled()
    up = _server_up()
    return (f"KB: {KB_NAME}\nAbilitata: {'sì' if enabled else 'NO (disabilitata con ragcli kb disable)'}\n"
            f"Server: {'ATTIVO' if up else 'SPENTO'}\n"
            f"Porta: {PORT}\nWebUI: {BASE}\nSorgente: {kb['source_folder']}")


if __name__ == "__main__":
    mcp.run()
