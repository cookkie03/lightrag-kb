#!/usr/bin/env python3
"""ragcli — gestione knowledge base LightRAG locali (multi-KB).

Usa il config centrale ~/lightrag-kb/config/global.env e il registro
~/lightrag-kb/config/registry.yaml. Ogni KB ha un lightrag-server dedicato su
porta propria, working dir e input dir isolati, e un MCP server collegabile
a Claude Code.

Comandi raggruppati in stile docker/kubectl:
  kb       gestione delle knowledge base (new, rm, ls, info, move, reset, regen, enable, disable, insert, exclude)
  server   gestione dei server lightrag (start, stop, restart, status)
  ingest   pipeline di estrazione (run, status, cancel, kill, recover)
  mcp      registrazione MCP nei client (add, rm)
  query    interrogazione di una KB (top-level) + query inspect
  status   dashboard globale
  search   comando legacy, non legato alle KB
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

import mcp_clients

HOME = Path(__file__).resolve().parent.parent
CONFIG = HOME / "config" / "global.env"
REGISTRY = HOME / "config" / "registry.yaml"
KB_DIR = HOME / "kb"
MCP_SCRIPT = HOME / "mcp" / "lightrag_mcp.py"
INGEST_SCRIPT = HOME / "bin" / "ingest.py"
MCP_PYTHON = HOME / ".venv-mcp" / "bin" / "python"
LIGHTRAG_SERVER = Path.home() / ".local" / "bin" / "lightrag-server"

# ---------------------------------------------------------------- helpers

def load_env(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def load_registry() -> dict:
    if not REGISTRY.exists():
        return {"kbs": []}
    data = yaml.safe_load(REGISTRY.read_text()) or {}
    data.setdefault("kbs", [])
    return data


def save_registry(data: dict) -> None:
    header = (
        "# Registro delle knowledge base LightRAG (gestito da ragcli, editabile a mano).\n"
    )
    REGISTRY.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))


def find_kb(reg: dict, name: str) -> dict | None:
    return next((k for k in reg["kbs"] if k["name"] == name), None)


def kb_data_dir(kb: dict) -> Path:
    """Dir dove vivono i dati della KB (indice, OCR, .env, log).

    Se il registry ha `data_dir` lo usa (es. <sorgente>/.lightrag), altrimenti
    fallback alla vecchia posizione locale kb/<nome>/. Così le KB già esistenti
    continuano a funzionare finché non vengono migrate con `ragcli kb move`."""
    d = kb.get("data_dir")
    if d:
        return Path(d).expanduser()
    return KB_DIR / kb["name"]


def port_pid(port: int) -> str | None:
    r = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    return r.stdout.strip() or None


def health(port: int) -> bool:
    try:
        return requests.get(f"http://127.0.0.1:{port}/health", timeout=2).ok
    except Exception:
        return False


def next_port(reg: dict, g: dict) -> int:
    base = int(g.get("BASE_PORT", "9621"))
    used = {int(k["port"]) for k in reg["kbs"]}
    p = base
    while p in used:
        p += 1
    return p


def _count_files(d: Path) -> int | None:
    if not d.is_dir():
        return None
    return sum(1 for p in d.rglob("*") if p.is_file())


def _pipeline_status(port: int, timeout: float = 5) -> dict | None:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/documents/pipeline_status", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _kill_orphan_ingest() -> list[str]:
    """Killa eventuali processi ingest.py orfani (non lanciati da ragcli stesso). Ritorna i PID uccisi."""
    r = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    killed = []
    for line in r.stdout.splitlines():
        if "ingest.py" in line and "ps" not in line and "ragcli" not in line:
            parts = line.split()
            if len(parts) > 1:
                pid = parts[1]
                subprocess.run(["kill", "-9", pid])
                killed.append(pid)
    return killed


def _cancel_pipeline(port: int) -> dict:
    r = requests.post(f"http://127.0.0.1:{port}/documents/cancel_pipeline", timeout=30)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- .env gen

def write_kb_env(kb: dict, g: dict) -> Path:
    name = kb["name"]
    kdir = kb_data_dir(kb)
    working = kdir / "rag_storage"
    inputs = kdir / "inputs"
    working.mkdir(parents=True, exist_ok=True)
    inputs.mkdir(parents=True, exist_ok=True)

    provider = kb.get("provider", g.get("LLM_PROVIDER", "ollama"))
    is_openrouter = (provider == "openrouter")

    if is_openrouter:
        llm_binding = "openai"
        llm_host = g.get("OPENROUTER_HOST", "https://openrouter.ai/api/v1")
        llm_model = kb.get("llm_model", g.get("OPENROUTER_LLM_MODEL", "openrouter/owl-alpha"))
        llm_api_key = g.get("OPENROUTER_API_KEY", "")
        emb_binding = "openai"
        emb_host = g.get("OPENROUTER_HOST", "https://openrouter.ai/api/v1")
        emb_model = g.get("OPENROUTER_EMBEDDING_MODEL", "nvidia/llama-nemotron-embed-vl-1b-v2:free")
        emb_dim = g.get("OPENROUTER_EMBEDDING_DIM", "2048")
        emb_api_key = llm_api_key
        emb_use_base64 = g.get("EMBEDDING_USE_BASE64", "false")
    else:
        ollama_host = g.get("OLLAMA_HOST", "http://localhost:11434")
        llm_binding = "ollama"
        llm_host = ollama_host
        llm_model = kb.get("llm_model", g.get("LLM_MODEL", "gemma4:e2b"))
        llm_api_key = None
        emb_binding = "ollama"
        emb_host = ollama_host
        emb_model = g.get("EMBEDDING_MODEL", "nomic-embed-text-v2-moe:latest")
        emb_dim = g.get("EMBEDDING_DIM", "768")
        emb_api_key = None
        emb_use_base64 = g.get("EMBEDDING_USE_BASE64", "true")

    env_lines = [
        "# Generato da ragcli — NON editare a mano (usa `ragcli kb regen` dopo aver",
        "# cambiato config/global.env). Override per-KB vanno nel config/registry.yaml.",
        f"# provider: {provider}",
        "HOST=127.0.0.1",
        f"PORT={kb['port']}",
        f"WORKING_DIR={working}",
        f"INPUT_DIR={inputs}",
        f"LLM_BINDING={llm_binding}",
        f"LLM_BINDING_HOST={llm_host}",
        f"LLM_MODEL={llm_model}",
    ]
    if llm_api_key:
        env_lines.append(f"LLM_BINDING_API_KEY={llm_api_key}")
    if not is_openrouter:
        env_lines.append(f"OLLAMA_LLM_NUM_CTX={g.get('OLLAMA_LLM_NUM_CTX', '16384')}")
        env_lines.append(f"OLLAMA_LLM_NUM_PREDICT={g.get('OLLAMA_LLM_NUM_PREDICT', '8192')}")
    env_lines += [
        f"EMBEDDING_BINDING={emb_binding}",
        f"EMBEDDING_BINDING_HOST={emb_host}",
        f"EMBEDDING_MODEL={emb_model}",
        f"EMBEDDING_DIM={emb_dim}",
        f"EMBEDDING_USE_BASE64={emb_use_base64}",
    ]
    if emb_api_key:
        env_lines.append(f"EMBEDDING_BINDING_API_KEY={emb_api_key}")
    if not is_openrouter:
        env_lines.append(f"OLLAMA_EMBEDDING_NUM_CTX={g.get('OLLAMA_EMBEDDING_NUM_CTX', '8192')}")
    env_lines += [
        f"RERANK_BINDING={g.get('RERANK_BINDING', 'null')}",
        "# --- robustezza pipeline di ingest ---",
        f"LLM_TIMEOUT={g.get('LLM_TIMEOUT', '900')}",
        f"TIMEOUT={g.get('TIMEOUT', '900')}",
        f"MAX_ASYNC={g.get('MAX_ASYNC', '2')}",
        f"EMBEDDING_TIMEOUT={g.get('EMBEDDING_TIMEOUT', '120')}",
        "# --- qualità RAG ---",
        f"SUMMARY_LANGUAGE={kb.get('summary_language', g.get('SUMMARY_LANGUAGE', 'Italian'))}",
        f"MAX_GLEANING={g.get('MAX_GLEANING', '1')}",
        f"CHUNK_SIZE={g.get('CHUNK_SIZE', '1200')}",
        f"CHUNK_OVERLAP_SIZE={g.get('CHUNK_OVERLAP_SIZE', '150')}",
        f"COSINE_THRESHOLD={g.get('COSINE_THRESHOLD', '0.25')}",
    ]
    envp = kdir / ".env"
    envp.write_text("\n".join(env_lines) + "\n")
    return envp


# ---------------------------------------------------------------- kb group

def cmd_kb_new(args):
    reg = load_registry()
    g = load_env(CONFIG)
    if find_kb(reg, args.name):
        sys.exit(f"KB '{args.name}' esiste già.")
    src = Path(args.source_folder).expanduser().resolve()
    if not src.is_dir():
        sys.exit(f"Cartella sorgente inesistente: {src}")
    kb = {
        "name": args.name,
        "source_folder": str(src),
        "port": args.port or next_port(reg, g),
        "ocr_backend": args.ocr or g.get("OCR_BACKEND", "docling"),
        "enabled": True,
    }
    # Posizione dei dati della KB: default <sorgente>/.lightrag (vicino ai dati
    # grezzi). --data-dir per una posizione custom, --local per la vecchia kb/<nome>.
    if args.data_dir:
        kb["data_dir"] = str(Path(args.data_dir).expanduser().resolve())
    elif not args.local:
        kb["data_dir"] = str(src / ".lightrag")
    if args.llm_model:
        kb["llm_model"] = args.llm_model
    if args.lang:
        kb["summary_language"] = args.lang
    if args.provider:
        kb["provider"] = args.provider
    reg["kbs"].append(kb)
    save_registry(reg)
    write_kb_env(kb, g)
    print(f"✓ KB '{args.name}' creata.")
    print(f"  sorgente : {src}")
    print(f"  dati KB  : {kb_data_dir(kb)}")
    print(f"  porta    : {kb['port']}  (WebUI: http://127.0.0.1:{kb['port']})")
    print(f"  provider : {kb.get('provider', g.get('LLM_PROVIDER', 'ollama'))}")
    print(f"  OCR      : {kb['ocr_backend']}")
    print(f"\nProssimi passi:\n  ragcli server start {args.name}\n  ragcli ingest run {args.name}\n  ragcli mcp add {args.name}")


def cmd_kb_regen(args):
    reg = load_registry()
    g = load_env(CONFIG)
    targets = reg["kbs"] if args.name == "all" else [find_kb(reg, args.name)]
    for kb in targets:
        if not kb:
            sys.exit(f"KB '{args.name}' non trovata.")
        write_kb_env(kb, g)
        print(f"✓ .env rigenerato per '{kb['name']}'")


def cmd_kb_move(args):
    """Sposta i dati di una KB esistente nella nuova posizione (default
    <sorgente>/.lightrag) e aggiorna il registry + .env. Ferma il server se attivo."""
    reg = load_registry()
    g = load_env(CONFIG)
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    src = Path(kb["source_folder"]).expanduser()
    if args.local:
        dest = (KB_DIR / kb["name"]).resolve()
    else:
        dest = (Path(args.data_dir).expanduser() if args.data_dir
                else src / ".lightrag").resolve()
    old = kb_data_dir(kb).resolve()
    if old == dest:
        print(f"KB '{args.name}' è già in {dest}.")
        return
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"Destinazione già esistente e non vuota: {dest}")
    if port_pid(kb["port"]):
        _stop_one(kb)
        time.sleep(1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if old.exists():
        shutil.move(str(old), str(dest))
        print(f"  spostato {old} → {dest}")
    else:
        dest.mkdir(parents=True, exist_ok=True)
        print(f"  nessun dato precedente in {old}: creata {dest} vuota")
    if args.local:
        kb.pop("data_dir", None)
    else:
        kb["data_dir"] = str(dest)
    save_registry(reg)
    write_kb_env(kb, g)  # riscrive .env con WORKING_DIR/INPUT_DIR aggiornati
    print(f"✓ KB '{args.name}' migrata. Rilancia: ragcli server start {args.name}")


def cmd_kb_reset(args):
    """Azzera l'indice di una KB per ripartire da zero (utile dopo run falliti o
    stato incoerente). Di default PRESERVA l'OCR già fatto (cartella inputs/) e
    cancella solo l'indice LightRAG (rag_storage/) e la cache locale di ingest:
    così il re-ingest reinserisce tutto pulito riusando l'OCR. Con --hard
    cancella anche inputs/ (re-OCR completo)."""
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    kdir = kb_data_dir(kb)
    if not args.yes:
        extra = " + inputs/ (re-OCR)" if args.hard else " (preservo inputs/ OCR)"
        resp = input(f"Azzero l'indice di '{args.name}'{extra}. Confermi? [y/N] ")
        if resp.strip().lower() not in ("y", "yes", "s", "si"):
            print("Annullato.")
            return
    if port_pid(kb["port"]):
        _stop_one(kb)
    targets = [kdir / "rag_storage", kdir / ".ocr_cache.json"]
    if args.hard:
        targets.append(kdir / "inputs")
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        elif t.exists():
            t.unlink()
    print(f"✓ KB '{args.name}' azzerata. Rilancia: ragcli server start {args.name} && ragcli ingest run {args.name}")


def cmd_kb_rm(args):
    """Elimina una KB: de-registra l'MCP da tutti i client, la rimuove dal registro
    e opzionalmente cancella i suoi dati su disco."""
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")

    if not args.yes:
        action = "cancellando" if args.purge else "conservando"
        resp = input(f"Elimino la KB '{args.name}' ({action} i dati su disco). Confermi? [y/N] ")
        if resp.strip().lower() not in ("y", "yes", "s", "si"):
            print("Annullato.")
            return

    # Arresta il server se attivo
    if port_pid(kb["port"]):
        _stop_one(kb)
        time.sleep(1)

    # De-registra l'MCP da tutti i client
    mcp_name = f"lightrag-{args.name}"
    for fn in mcp_clients.REMOVERS.values():
        try:
            fn(mcp_name)
        except Exception as e:
            print(f"Errore durante la de-registrazione MCP: {e}")

    # Rimuove dal registro
    reg["kbs"] = [k for k in reg["kbs"] if k["name"] != args.name]
    save_registry(reg)

    # Gestione dati su disco
    kdir = kb_data_dir(kb)
    if args.purge:
        if kdir.exists():
            shutil.rmtree(kdir, ignore_errors=True)
            print(f"✓ Dati su disco cancellati in {kdir}")
    else:
        print(f"  I dati della KB sono stati conservati in {kdir}")

    print(f"✓ KB '{args.name}' eliminata con successo.")


def cmd_kb_ls(args):
    reg = load_registry()
    if not reg["kbs"]:
        print("Nessuna KB. Crea con: ragcli kb new <nome> <cartella>")
        return
    g = load_env(CONFIG)
    print(f"{'NAME':<16}{'PORT':<7}{'STATO':<8}{'PROVIDER':<12}{'OCR':<15}{'EN':<4}SORGENTE")
    for kb in reg["kbs"]:
        up = "UP" if health(kb["port"]) else ("port" if port_pid(kb["port"]) else "down")
        en = "yes" if kb.get("enabled", True) else "no"
        prov = kb.get("provider", g.get("LLM_PROVIDER", "ollama"))
        print(f"{kb['name']:<16}{kb['port']:<7}{up:<8}{prov:<12}{kb.get('ocr_backend','-'):<15}{en:<4}{kb['source_folder']}")


def cmd_kb_info(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    g = load_env(CONFIG)
    kdir = kb_data_dir(kb)
    up = health(kb["port"])
    pid = port_pid(kb["port"])
    n_inputs = _count_files(kdir / "inputs")
    n_storage = _count_files(kdir / "rag_storage")
    print(f"KB              : {kb['name']}")
    print(f"sorgente        : {kb['source_folder']}")
    print(f"dati KB         : {kdir}")
    print(f"porta           : {kb['port']}  (WebUI: http://127.0.0.1:{kb['port']})")
    print(f"provider        : {kb.get('provider', g.get('LLM_PROVIDER', 'ollama'))}")
    print(f"OCR backend     : {kb.get('ocr_backend', '-')}")
    print(f"abilitata       : {'sì' if kb.get('enabled', True) else 'no'}")
    n_excl = len(kb.get("exclude", []))
    print(f"esclusioni      : {n_excl} pattern (ragcli kb exclude ls {kb['name']})" if n_excl else "esclusioni      : nessuna")
    print(f"server          : {'UP' if up else ('avviato ma non risponde (PID ' + pid + ')' if pid else 'down')}")
    print(f"file in inputs/    : {n_inputs if n_inputs is not None else 'n/a (cartella assente)'}")
    print(f"file in rag_storage: {n_storage if n_storage is not None else 'n/a (cartella assente)'}")


def cmd_kb_enable(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    kb["enabled"] = True
    save_registry(reg)
    print(f"✓ KB '{args.name}' abilitata (verrà inclusa in `ragcli server start all`).")


def cmd_kb_disable(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    kb["enabled"] = False
    save_registry(reg)
    print(f"✓ KB '{args.name}' disabilitata (verrà saltata da `ragcli server start all`).")


def cmd_kb_exclude_add(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    patterns = kb.setdefault("exclude", [])
    if args.pattern in patterns:
        print(f"· pattern già presente: {args.pattern}")
        return
    patterns.append(args.pattern)
    save_registry(reg)
    print(f"✓ esclusa da '{args.name}': {args.pattern}  (effettiva dal prossimo `ragcli ingest run`)")


def cmd_kb_exclude_rm(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    patterns = kb.get("exclude", [])
    if args.pattern not in patterns:
        sys.exit(f"pattern non presente in '{args.name}': {args.pattern}")
    patterns.remove(args.pattern)
    if not patterns:
        kb.pop("exclude", None)
    save_registry(reg)
    print(f"✓ rimossa esclusione da '{args.name}': {args.pattern}")


def cmd_kb_exclude_ls(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    patterns = kb.get("exclude", [])
    if not patterns:
        print(f"Nessuna esclusione per '{args.name}'.")
        return
    for pat in patterns:
        print(f"  {pat}")


def cmd_kb_insert(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    if not health(kb["port"]):
        sys.exit(f"Server '{args.name}' non attivo.")
    url = f"http://127.0.0.1:{kb['port']}/documents/text"
    try:
        r = requests.post(url, json={"text": args.text, "file_source": args.source}, timeout=300)
        r.raise_for_status()
        print("✓ Testo inserito con successo.")
    except Exception as e:
        sys.exit(f"Errore: {e}")


# ---------------------------------------------------------------- server group

def _ollama_has_model(host: str, name: str) -> bool:
    """True se il modello (anche senza tag :latest) è già presente in Ollama."""
    try:
        r = requests.get(f"{host}/api/tags", timeout=5)
        r.raise_for_status()
        have = {m.get("name", "") for m in r.json().get("models", [])}
    except Exception:
        return True  # in dubbio non blocchiamo l'avvio
    base = name.split(":")[0]
    return name in have or any(h == name or h.split(":")[0] == base for h in have)


def ensure_ollama_models(kb: dict, g: dict) -> None:
    """Portabilità/self-heal: se la KB usa Ollama, scarica i modelli citati in
    global.env (LLM + embedding) quando mancano. Così spostando o ripristinando
    la cartella lightrag-kb su un'altra macchina basta `ragcli server start` e i
    modelli vengono riscaricati da soli, senza Modelfile o passi manuali."""
    provider = kb.get("provider", g.get("LLM_PROVIDER", "ollama"))
    if provider != "ollama":
        return  # openrouter & co.: nessun modello locale da garantire
    host = g.get("OLLAMA_HOST", "http://localhost:11434")
    wanted = [
        kb.get("llm_model", g.get("LLM_MODEL", "")),
        g.get("EMBEDDING_MODEL", ""),
    ]
    for model in [m for m in wanted if m]:
        if _ollama_has_model(host, model):
            continue
        print(f"  ⤓ modello Ollama mancante '{model}': lo scarico (una tantum)…")
        rc = subprocess.call(["ollama", "pull", model])
        if rc != 0:
            print(f"  ⚠ 'ollama pull {model}' fallito (rc={rc}); avvio comunque, "
                  f"ma l'ingest fallirà finché il modello non è disponibile.")


def _start_one(kb, g):
    port = kb["port"]
    if port_pid(port):
        print(f"  '{kb['name']}' già attiva su :{port}")
        return
    ensure_ollama_models(kb, g)
    kdir = kb_data_dir(kb)
    if not (kdir / ".env").exists():
        write_kb_env(kb, g)
    log = f"/tmp/lightrag-{kb['name']}.log"
    with open(log, "ab") as lf:
        subprocess.Popen(
            [str(LIGHTRAG_SERVER)],
            cwd=str(kdir), stdout=lf, stderr=lf,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    print(f"  avvio '{kb['name']}' su :{port} … (log: {log})")


def cmd_server_start(args):
    reg = load_registry()
    g = load_env(CONFIG)
    targets = ([k for k in reg["kbs"] if k.get("enabled", True)]
               if args.name == "all" else [find_kb(reg, args.name)])
    for kb in targets:
        if not kb:
            sys.exit(f"KB '{args.name}' non trovata.")
        _start_one(kb, g)


def _stop_one(kb):
    pid = port_pid(kb["port"])
    if pid:
        for p in pid.split():
            subprocess.run(["kill", p])
        print(f"  spenta '{kb['name']}' (:{kb['port']}, PID {pid})")
    else:
        print(f"  '{kb['name']}' già spenta")


def cmd_server_stop(args):
    reg = load_registry()
    targets = reg["kbs"] if args.name == "all" else [find_kb(reg, args.name)]
    for kb in targets:
        if not kb:
            sys.exit(f"KB '{args.name}' non trovata.")
        _stop_one(kb)


def cmd_server_restart(args):
    cmd_server_stop(args)
    time.sleep(1)
    cmd_server_start(args)


def _wait_health(port, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if health(port):
            return True
        time.sleep(2)
    return False


def cmd_server_status(args):
    reg = load_registry()
    for kb in reg["kbs"]:
        up = health(kb["port"])
        print(f"{kb['name']:<16} server={'UP' if up else 'down':<5} "
              f":{kb['port']}  mcp=lightrag-{kb['name']}")


# ---------------------------------------------------------------- ingest group

def _ensure_pipeline_free(kb: dict, timeout: float = 30) -> bool:
    """Se la pipeline è busy, la cancella e attende che torni libera.
    Ritorna True se al termine la pipeline è libera (o lo era già)."""
    d = _pipeline_status(kb["port"])
    if d is None or not d.get("busy"):
        return True
    print(f"  pipeline di '{kb['name']}' occupata: richiedo cancellazione automatica…")
    try:
        _cancel_pipeline(kb["port"])
    except Exception as e:
        print(f"  ⚠ cancellazione pipeline fallita: {e}")
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = _pipeline_status(kb["port"])
        if d is None or not d.get("busy"):
            print("  ✓ pipeline liberata.")
            return True
        time.sleep(2)
    return False


def cmd_ingest_run(args):
    reg = load_registry()
    g = load_env(CONFIG)
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    if not health(kb["port"]):
        print(f"Server '{kb['name']}' non attivo: lo avvio…")
        _start_one(kb, g)
        if not _wait_health(kb["port"]):
            sys.exit("Server non risponde. Controlla il log /tmp/lightrag-%s.log" % kb["name"])

    # Auto-correttivo: se la pipeline è bloccata, la libera e ripulisce gli
    # orfani prima di lanciare il nuovo ingest, evitando il manuale
    # `ingest cancel` -> `ingest kill` -> retry.
    if not _ensure_pipeline_free(kb):
        sys.exit(f"Pipeline di '{args.name}' ancora occupata dopo il tentativo di cancellazione. "
                  f"Riprova con `ragcli ingest recover {args.name}`.")
    killed = _kill_orphan_ingest()
    if killed:
        print(f"  rimossi processi ingest.py orfani: {', '.join(killed)}")

    cmd = [str(MCP_PYTHON), str(INGEST_SCRIPT), "--kb", args.name]
    if args.force:
        cmd.append("--force")
    if getattr(args, "add", False):
        cmd.append("--add")
    if getattr(args, "background", False):
        # Esecuzione detached: nuova sessione + stdio su file, così l'ingest
        # sopravvive alla chiusura del terminale e libera la shell.
        log = f"/tmp/ragcli-ingest-{args.name}.log"
        with open(log, "wb") as lf:
            proc = subprocess.Popen(
                cmd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        print(f"✓ Ingest di '{args.name}' avviato in background (PID {proc.pid}).")
        print(f"  puoi chiudere il terminale.")
        print(f"  log    : {log}")
        print(f"  segui  : tail -f {log}")
        print(f"  stato  : ragcli ingest status {args.name}")
        return
    sys.exit(subprocess.call(cmd))


def cmd_ingest_status(args):
    """Mostra lo stato del pipeline di estrazione (busy/idle, job corrente, ultimo messaggio)."""
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    if not health(kb["port"]):
        sys.exit(f"Server '{args.name}' non attivo.")
    r = requests.get(f"http://127.0.0.1:{kb['port']}/documents/pipeline_status", timeout=30)
    r.raise_for_status()
    d = r.json()
    if not d.get("busy"):
        print(f"'{args.name}': pipeline libero (non sta processando nulla).")
        return
    print(f"'{args.name}': pipeline OCCUPATO")
    print(f"  job: {d.get('job_name')}  (avviato: {d.get('job_start')})")
    print(f"  ultimo messaggio: {d.get('latest_message')}")
    if args.verbose:
        for m in d.get("history_messages", [])[-args.lines:]:
            print(f"    {m}")


def cmd_ingest_cancel(args):
    """Cancella il pipeline di estrazione in corso (POST /documents/cancel_pipeline).
    Usalo quando un'estrazione e' bloccata/troppo lenta (es. rate-limit LLM) e
    impedisce nuovi ingest con errore 409 Conflict. I documenti gia' completati
    restano PROCESSED, quelli in corso vengono marcati FAILED e si possono
    rilanciare con un nuovo `ragcli ingest run`."""
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    if not health(kb["port"]):
        sys.exit(f"Server '{args.name}' non attivo.")
    d = _cancel_pipeline(kb["port"])
    if d.get("status") == "not_busy":
        print(f"'{args.name}': pipeline già libero, niente da cancellare.")
    else:
        print(f"'{args.name}': cancellazione richiesta ({d.get('message', d.get('status'))}). "
              f"Verifica con `ragcli ingest status {args.name}` che torni libero, poi rilancia l'ingest.")


def cmd_ingest_kill(args):
    killed = _kill_orphan_ingest()
    if killed:
        for pid in killed:
            print(f"Killed orphan ingest.py pid: {pid}")
    else:
        print("No orphan ingest.py processes found.")


def cmd_ingest_recover(args):
    """Sequenza completa di recupero: cancella la pipeline bloccata e uccide
    eventuali processi ingest.py orfani, poi suggerisce di rilanciare l'ingest."""
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    if health(kb["port"]):
        try:
            d = _cancel_pipeline(kb["port"])
            if d.get("status") == "not_busy":
                print(f"'{args.name}': pipeline già libero.")
            else:
                print(f"'{args.name}': cancellazione richiesta ({d.get('message', d.get('status'))}).")
        except Exception as e:
            print(f"  ⚠ cancellazione pipeline fallita: {e}")
    else:
        print(f"'{args.name}': server non attivo, salto la cancellazione pipeline.")
    killed = _kill_orphan_ingest()
    if killed:
        print(f"  rimossi processi ingest.py orfani: {', '.join(killed)}")
    else:
        print("  nessun processo ingest.py orfano trovato.")
    print(f"\n✓ Recovery completata. Rilancia con: ragcli ingest run {args.name}")


# ---------------------------------------------------------------- query

def cmd_query(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    if not health(kb["port"]):
        sys.exit(f"Server '{args.name}' non attivo. Avvialo prima.")
    url = f"http://127.0.0.1:{kb['port']}/query"
    try:
        r = requests.post(url, json={"query": args.question, "mode": args.mode}, timeout=300)
        r.raise_for_status()
        data = r.json()
        res = data.get("response", data) if isinstance(data, dict) else str(data)
        print(res)
    except Exception as e:
        sys.exit(f"Errore durante la query: {e}")


def cmd_query_inspect(args):
    """Mostra se una KB è pronta per essere interrogata: salute server,
    quanti file sono stati ingeriti/indicizzati, stato della pipeline."""
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    kdir = kb_data_dir(kb)
    up = health(kb["port"])
    n_inputs = _count_files(kdir / "inputs")
    n_storage = _count_files(kdir / "rag_storage")
    print(f"KB                 : {kb['name']}")
    print(f"server             : {'UP' if up else 'down'}  (:{kb['port']})")
    print(f"file in inputs/    : {n_inputs if n_inputs is not None else 'n/a'}")
    print(f"file in rag_storage: {n_storage if n_storage is not None else 'n/a'}")
    if not up:
        print("pipeline           : n/a (server non attivo)")
        return
    d = _pipeline_status(kb["port"])
    if d is None:
        print("pipeline           : err (richiesta fallita)")
    else:
        print(f"pipeline           : {'busy' if d.get('busy') else 'idle'}")


# ---------------------------------------------------------------- mcp group

def cmd_mcp_add(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    mcp_name = f"lightrag-{args.name}"
    clients = list(mcp_clients.CLIENTS) if args.client == "all" else [args.client]
    rc = 0
    for client in clients:
        if len(clients) > 1:
            print(f"\n== {client} ==")
        fn = mcp_clients.CLIENTS[client]
        if fn(mcp_name, MCP_PYTHON, MCP_SCRIPT, args.name, args.print_only) != 0:
            rc = 1
    sys.exit(rc)


def cmd_mcp_rm(args):
    reg = load_registry()
    kb = find_kb(reg, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    mcp_name = f"lightrag-{args.name}"
    clients = list(mcp_clients.REMOVERS) if args.client == "all" else [args.client]
    rc = 0
    for client in clients:
        if len(clients) > 1:
            print(f"\n== {client} ==")
        fn = mcp_clients.REMOVERS[client]
        try:
            if fn(mcp_name) != 0:
                rc = 1
        except Exception as e:
            print(f"Errore durante la de-registrazione da {client}: {e}")
            rc = 1
    sys.exit(rc)


# ---------------------------------------------------------------- global status

def cmd_status(args):
    """Dashboard globale: per ogni KB, stato server, porta, provider, enabled e
    stato della pipeline di ingest. Robusto a errori per singola KB: una KB che
    non risponde non deve impedire di vedere lo stato delle altre."""
    reg = load_registry()
    if not reg["kbs"]:
        print("Nessuna KB. Crea con: ragcli kb new <nome> <cartella>")
        return
    g = load_env(CONFIG)
    print(f"{'NAME':<16}{'SERVER':<8}{'PORT':<7}{'PROVIDER':<12}{'EN':<4}PIPELINE")
    for kb in reg["kbs"]:
        try:
            up = health(kb["port"])
        except Exception:
            up = False
        prov = kb.get("provider", g.get("LLM_PROVIDER", "ollama"))
        en = "yes" if kb.get("enabled", True) else "no"
        if not up:
            pipeline = "n/a"
        else:
            try:
                d = _pipeline_status(kb["port"])
                pipeline = ("busy" if d.get("busy") else "idle") if d is not None else "err"
            except Exception:
                pipeline = "err"
        print(f"{kb['name']:<16}{'UP' if up else 'down':<8}{kb['port']:<7}{prov:<12}{en:<4}{pipeline}")


# ---------------------------------------------------------------- legacy

def cmd_search(args):
    path = Path("/Users/luca/Library/CloudStorage/OneDrive-Personale/BPEA/Rifiuti/rapportorifiutiurbani_2025.md")
    if not path.exists():
        sys.exit("File non trovato.")
    query = args.query.lower()
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for idx, line in enumerate(f, 1):
            if query in line.lower():
                print(f"{idx}: {line.strip()[:150]}")


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog="ragcli", description="Gestione KB LightRAG locali")
    sub = p.add_subparsers(dest="cmd", required=True)

    # -- kb --
    kb_p = sub.add_parser("kb", help="gestione delle knowledge base")
    kb_sub = kb_p.add_subparsers(dest="kb_cmd", required=True)

    c = kb_sub.add_parser("new", help="crea una nuova KB da una cartella")
    c.add_argument("name")
    c.add_argument("source_folder")
    c.add_argument("--port", type=int)
    c.add_argument("--ocr", choices=["mineru", "mineru-cloud", "glmocr", "docling"])
    c.add_argument("--llm-model", dest="llm_model")
    c.add_argument("--lang", help="lingua del knowledge graph (es. Italian, English)")
    c.add_argument("--provider", choices=["ollama", "openrouter"],
                   help="provider LLM+embedding (default: valore in global.env)")
    c.add_argument("--data-dir", dest="data_dir",
                   help="dove salvare i dati della KB (default: <sorgente>/.lightrag)")
    c.add_argument("--local", action="store_true",
                   help="salva i dati in kb/<nome>/ dentro il progetto (vecchio comportamento)")
    c.set_defaults(func=cmd_kb_new)

    c = kb_sub.add_parser("rm", help="elimina una KB (de-registra MCP, rimuove dal registry; scegli se tenere o cancellare i dati)")
    c.add_argument("name")
    g_delete = c.add_mutually_exclusive_group(required=True)
    g_delete.add_argument("--keep-data", action="store_true", help="conserva rag_storage/, inputs/ su disco")
    g_delete.add_argument("--purge", action="store_true", help="cancella anche i dati indicizzati su disco")
    c.add_argument("--yes", "-y", action="store_true", help="non chiedere conferma")
    c.set_defaults(func=cmd_kb_rm)

    c = kb_sub.add_parser("ls", help="elenca le KB e il loro stato")
    c.set_defaults(func=cmd_kb_ls)

    c = kb_sub.add_parser("info", help="dettagli completi di una KB (path, porta, stato, conteggio file)")
    c.add_argument("name")
    c.set_defaults(func=cmd_kb_info)

    c = kb_sub.add_parser("move", help="sposta i dati di una KB nella sorgente (default <sorgente>/.lightrag)")
    c.add_argument("name")
    g_migrate = c.add_mutually_exclusive_group()
    g_migrate.add_argument("--data-dir", dest="data_dir",
                           help="posizione custom (default: <sorgente>/.lightrag)")
    g_migrate.add_argument("--local", action="store_true",
                           help="riporta i dati in kb/<nome>/ dentro il progetto (reciproco di kb new --local)")
    c.set_defaults(func=cmd_kb_move)

    c = kb_sub.add_parser("reset", help="azzera l'indice di una KB per ripartire da zero (preserva l'OCR)")
    c.add_argument("name")
    c.add_argument("--hard", action="store_true", help="cancella anche inputs/ (re-OCR completo)")
    c.add_argument("--yes", "-y", action="store_true", help="non chiedere conferma")
    c.set_defaults(func=cmd_kb_reset)

    c = kb_sub.add_parser("regen", help="rigenera l'.env da global.env (name o 'all')")
    c.add_argument("name")
    c.set_defaults(func=cmd_kb_regen)

    c = kb_sub.add_parser("enable", help="abilita una KB (inclusa in `server start all`)")
    c.add_argument("name")
    c.set_defaults(func=cmd_kb_enable)

    c = kb_sub.add_parser("disable", help="disabilita una KB (esclusa da `server start all`)")
    c.add_argument("name")
    c.set_defaults(func=cmd_kb_disable)

    c = kb_sub.add_parser("exclude", help="gestisci i pattern di file/cartelle esclusi dall'ingest")
    exclude_sub = c.add_subparsers(dest="exclude_cmd", required=True)

    e = exclude_sub.add_parser("add", help="aggiungi un pattern fnmatch relativo alla sorgente "
                                            "(es. 'Bozze/*' per una cartella, '*.tmp' per un'estensione)")
    e.add_argument("name")
    e.add_argument("pattern")
    e.set_defaults(func=cmd_kb_exclude_add)

    e = exclude_sub.add_parser("rm", help="rimuovi un pattern di esclusione")
    e.add_argument("name")
    e.add_argument("pattern")
    e.set_defaults(func=cmd_kb_exclude_rm)

    e = exclude_sub.add_parser("ls", help="elenca i pattern di esclusione di una KB")
    e.add_argument("name")
    e.set_defaults(func=cmd_kb_exclude_ls)

    c = kb_sub.add_parser("insert", help="inserisce testo personalizzato nella KB")
    c.add_argument("name")
    c.add_argument("text")
    c.add_argument("--source", default="manual")
    c.set_defaults(func=cmd_kb_insert)

    # -- server --
    server_p = sub.add_parser("server", help="gestione dei server lightrag")
    server_sub = server_p.add_subparsers(dest="server_cmd", required=True)

    for name, fn, helptext in [
        ("start", cmd_server_start, "avvia il server (name o 'all')"),
        ("stop", cmd_server_stop, "ferma il server (name o 'all')"),
        ("restart", cmd_server_restart, "riavvia il server (name o 'all')"),
    ]:
        c = server_sub.add_parser(name, help=helptext)
        c.add_argument("name")
        c.set_defaults(func=fn)

    c = server_sub.add_parser("status", help="riepilogo server + MCP")
    c.set_defaults(func=cmd_server_status)

    # -- ingest --
    ingest_p = sub.add_parser("ingest", help="pipeline di estrazione (OCR + embedding)")
    ingest_sub = ingest_p.add_subparsers(dest="ingest_cmd", required=True)

    c = ingest_sub.add_parser("run", help="OCR + embedding della cartella della KB (sync a specchio, auto-correttivo)")
    c.add_argument("name")
    c.add_argument("--force", action="store_true", help="re-OCR di tutti i file")
    c.add_argument("--add", action="store_true", help="solo-aggiunta: non elimina nulla dal KB")
    c.add_argument("--background", "-b", action="store_true",
                   help="esegui in background (detached): puoi chiudere il terminale")
    c.set_defaults(func=cmd_ingest_run)

    c = ingest_sub.add_parser("status", help="stato del pipeline di estrazione (busy/idle)")
    c.add_argument("name")
    c.add_argument("-v", "--verbose", action="store_true", help="mostra la cronologia messaggi")
    c.add_argument("--lines", type=int, default=15, help="quante righe di cronologia mostrare (con -v)")
    c.set_defaults(func=cmd_ingest_status)

    c = ingest_sub.add_parser("cancel", help="cancella il pipeline di estrazione bloccato/lento (sblocca i 409)")
    c.add_argument("name")
    c.set_defaults(func=cmd_ingest_cancel)

    c = ingest_sub.add_parser("kill", help="kill orphan ingest.py processes")
    c.set_defaults(func=cmd_ingest_kill)

    c = ingest_sub.add_parser("recover", help="cancella pipeline bloccata + uccide processi orfani, in un solo comando")
    c.add_argument("name")
    c.set_defaults(func=cmd_ingest_recover)

    # -- mcp --
    mcp_p = sub.add_parser("mcp", help="registrazione MCP nei client (Claude Code, Claude Desktop, Codex, Antigravity)")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_cmd", required=True)

    c = mcp_sub.add_parser("add", help="registra l'MCP della KB in Claude Code, Claude Desktop, Codex o Antigravity")
    c.add_argument("name")
    c.add_argument("--client", choices=list(mcp_clients.CLIENTS) + ["all"], default="claude-code",
                    help="client di destinazione (default: claude-code)")
    c.add_argument("--print-only", action="store_true")
    c.set_defaults(func=cmd_mcp_add)

    c = mcp_sub.add_parser("rm", help="de-registra l'MCP della KB da uno o tutti i client, senza eliminare la KB")
    c.add_argument("name")
    c.add_argument("--client", choices=list(mcp_clients.REMOVERS) + ["all"], default="claude-code",
                    help="client da cui de-registrare (default: claude-code)")
    c.set_defaults(func=cmd_mcp_rm)

    # -- query (top-level): `ragcli query <name> <question>` oppure `ragcli query inspect <name>` --
    query_p = sub.add_parser("query", help="invia una query alla KB, o ispeziona la sua prontezza con `query inspect <name>`")
    query_p.add_argument("name", help="nome della KB, oppure 'inspect' seguito dal nome")
    query_p.add_argument("rest", nargs="*", help=argparse.SUPPRESS)
    query_p.add_argument("--mode", default="mix", choices=["mix", "local", "global", "hybrid", "naive"])
    query_p.set_defaults(func=None)

    # -- status (dashboard globale) --
    c = sub.add_parser("status", help="dashboard globale: KB + server + porte + provider + pipeline")
    c.set_defaults(func=cmd_status)

    args = p.parse_args()

    if args.cmd == "query":
        if args.name == "inspect":
            if not args.rest:
                query_p.error("serve il nome della KB: `ragcli query inspect <name>`")
            cmd_query_inspect(argparse.Namespace(name=args.rest[0]))
        else:
            if not args.rest:
                query_p.error("serve la domanda: `ragcli query <name> <question>`")
            cmd_query(argparse.Namespace(name=args.name, question=" ".join(args.rest), mode=args.mode))
        return

    args.func(args)


if __name__ == "__main__":
    main()
