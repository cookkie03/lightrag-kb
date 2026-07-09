#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

import mcp_clients
from kb_registry import (
    INGEST_SCRIPT,
    MCP_SCRIPT,
    KB_ROOT,
    ensure_kb_dirs,
    find_kb,
    global_env,
    load_registry,
    merged_runtime_env,
    next_port,
    save_registry,
    storage_mode,
    write_kb_env,
)


def port_pid(port: int) -> str | None:
    result = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    return result.stdout.strip() or None


def health(port: int) -> bool:
    try:
        return requests.get(f"http://127.0.0.1:{port}/health", timeout=2).ok
    except Exception:
        return False


def _ensure_runtime(env: dict[str, str]) -> tuple[Path, Path]:
    engine_python = Path(env["ENGINE_PYTHON"]).expanduser()
    server_bin = Path(env["LIGHTRAG_SERVER_BIN"]).expanduser()
    missing = [str(p) for p in (engine_python, server_bin) if not p.exists()]
    if missing:
        sys.exit(
            "Runtime condiviso non pronto:\n"
            + "\n".join(f"  - manca {item}" for item in missing)
            + "\nEsegui `./setup.sh` per installare LightRAG + Docling nel runtime unico."
        )
    return engine_python, server_bin


def _wait_for_health(port: int, timeout: float = 30.0) -> bool:
    started = time.time()
    while time.time() - started < timeout:
        if health(port):
            return True
        time.sleep(0.5)
    return False


def _wait_for_port_release(port: int, timeout: float = 10.0) -> bool:
    started = time.time()
    while time.time() - started < timeout:
        if not port_pid(port):
            return True
        time.sleep(0.5)
    return False


def _stop_server(kb: dict) -> bool:
    pid = port_pid(kb["port"])
    if not pid:
        return True
    for part in pid.split():
        subprocess.run(["kill", part], capture_output=True, text=True)
    if _wait_for_port_release(kb["port"]):
        return True
    pid = port_pid(kb["port"])
    if not pid:
        return True
    for part in pid.split():
        subprocess.run(["kill", "-9", part], capture_output=True, text=True)
    return _wait_for_port_release(kb["port"], timeout=3.0)


def _kb_server_state(kb: dict) -> str:
    if health(kb["port"]):
        return "up"
    if port_pid(kb["port"]):
        return "zombie"
    return "down"


def _deregister_all_mcp(kb: dict) -> list[str]:
    mcp_name = f"lightrag-{kb['name']}"
    warnings: list[str] = []
    for client, fn in mcp_clients.REMOVERS.items():
        try:
            rc = fn(mcp_name, Path(kb["source_dir"]))
        except Exception as exc:
            warnings.append(f"{client}: {exc}")
            continue
        if rc != 0:
            warnings.append(f"{client}: exit code {rc}")
    return warnings


def _safe_kb_delete_target(kb: dict) -> Path:
    data_dir = Path(kb["data_dir"]).resolve()
    source_data_dir = (Path(kb["source_dir"]).resolve() / ".lightrag").resolve()
    repo_data_dir = (KB_ROOT / kb["name"]).resolve()
    if data_dir == source_data_dir or data_dir == repo_data_dir:
        return data_dir
    sys.exit(
        f"Refuso di sicurezza: data_dir inattesa per '{kb['name']}': {data_dir}\n"
        f"Attesi solo:\n"
        f"  - {source_data_dir}\n"
        f"  - {repo_data_dir}"
    )


def cmd_add(args: argparse.Namespace) -> None:
    env = global_env()
    registry = load_registry(env)
    if find_kb(registry, args.name):
        sys.exit(f"KB '{args.name}' già esistente.")
    source_dir = Path(args.source_dir).expanduser().resolve()
    if not source_dir.is_dir():
        sys.exit(f"Cartella sorgente inesistente: {source_dir}")
    data_dir = source_dir / ".lightrag"
    kb = {
        "name": args.name,
        "source_dir": str(source_dir),
        "data_dir": str(data_dir),
        "derived_dir": str(data_dir / "derived"),
        "rag_storage": str(data_dir / "rag_storage"),
        "port": args.port or next_port(registry, env),
        "exclude": [],
    }
    ensure_kb_dirs(kb)
    write_kb_env(kb, env)
    registry["kbs"].append(kb)
    save_registry(registry)
    print(f"✓ KB '{args.name}' creata.")
    print(f"  source     : {kb['source_dir']}")
    print(f"  data dir   : {kb['data_dir']}")
    print(f"  rag_storage: {kb['rag_storage']}")
    print(f"  derived    : {kb['derived_dir']}")
    print(f"  porta      : {kb['port']}")
    print(f"\nProssimi passi:\n  ragcli start {args.name}\n  ragcli ingest {args.name}\n  ragcli mcp add {args.name}")


def cmd_list(_: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    if not registry["kbs"]:
        print("Nessuna KB. Crea con: ragcli add <name> <source_dir>")
        return
    print(f"{'NAME':<22}{'PORT':<7}{'MODE':<8}{'SERVER':<8}{'EXCL':<6}SOURCE")
    for kb in registry["kbs"]:
        print(
            f"{kb['name']:<22}{kb['port']:<7}{storage_mode(kb):<8}{_kb_server_state(kb):<8}"
            f"{len(kb.get('exclude', [])):<6}{kb['source_dir']}"
        )


def cmd_exclude_add(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    patterns = kb.setdefault("exclude", [])
    if args.pattern in patterns:
        print(f"Pattern già presente: {args.pattern}")
        return
    patterns.append(args.pattern)
    save_registry(registry)
    write_kb_env(kb)
    print(f"✓ Escluso da '{args.name}': {args.pattern}")


def cmd_exclude_rm(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    patterns = kb.get("exclude", [])
    if args.pattern not in patterns:
        sys.exit(f"Pattern non presente: {args.pattern}")
    patterns.remove(args.pattern)
    if not patterns:
        kb["exclude"] = []
    save_registry(registry)
    write_kb_env(kb)
    print(f"✓ Rimossa esclusione da '{args.name}': {args.pattern}")


def cmd_start(args: argparse.Namespace) -> None:
    env = global_env()
    registry = load_registry(env)
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    ensure_kb_dirs(kb)
    write_kb_env(kb, env)
    _, server_bin = _ensure_runtime(env)
    state = _kb_server_state(kb)
    if state == "up":
        print(f"'{args.name}' già attiva su :{kb['port']}")
        return
    if state == "zombie":
        sys.exit(
            f"Server '{args.name}' non healthy ma porta :{kb['port']} occupata. "
            f"Esegui `ragcli stop {args.name}` e poi rilancia `ragcli start {args.name}`."
        )
    log_path = Path(kb["data_dir"]) / "lightrag.log"
    with log_path.open("ab") as log_handle:
        subprocess.Popen(
            [
                str(server_bin),
                "--host",
                "127.0.0.1",
                "--port",
                str(kb["port"]),
                "--input-dir",
                kb["source_dir"],
                "--working-dir",
                kb["rag_storage"],
            ],
            cwd=kb["data_dir"],
            env=merged_runtime_env(kb, env),
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    if not _wait_for_health(kb["port"]):
        sys.exit(
            f"Il server di '{args.name}' non è diventato healthy. "
            f"Controlla il log in {log_path}"
        )
    print(f"✓ Server '{args.name}' attivo su http://127.0.0.1:{kb['port']}")


def cmd_stop(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    state = _kb_server_state(kb)
    if state == "down":
        print(f"'{args.name}' già spenta.")
        return
    if not _stop_server(kb):
        sys.exit(
            f"Impossibile fermare '{args.name}' su :{kb['port']}. "
            f"Controlla manualmente il processo {port_pid(kb['port']) or '?'}."
        )
    print(f"✓ Server '{args.name}' fermato.")


def cmd_remove(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    state = _kb_server_state(kb)
    if state != "down":
        sys.exit(
            f"Fermare prima il server di '{args.name}' con `ragcli stop {args.name}`."
        )
    delete_target = _safe_kb_delete_target(kb)
    mcp_warnings = _deregister_all_mcp(kb)
    registry["kbs"] = [entry for entry in registry["kbs"] if entry["name"] != args.name]
    save_registry(registry)
    if delete_target.exists():
        shutil.rmtree(delete_target)
    print(f"✓ KB '{args.name}' rimossa.")
    print(f"  source preservata: {kb['source_dir']}")
    print(f"  stato eliminato   : {delete_target}")
    if mcp_warnings:
        print("\nAvvisi durante la pulizia MCP:")
        for warning in mcp_warnings:
            print(f"  - {warning}")


def cmd_ingest(args: argparse.Namespace) -> None:
    env = global_env()
    registry = load_registry(env)
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    engine_python, _ = _ensure_runtime(env)
    state = _kb_server_state(kb)
    if state == "down":
        sys.exit(
            f"Server '{args.name}' non attivo. Avvialo prima con `ragcli start {args.name}`."
        )
    if state == "zombie":
        sys.exit(
            f"Server '{args.name}' non healthy su :{kb['port']}. "
            f"Esegui `ragcli stop {args.name}`, poi `ragcli start {args.name}`, quindi riprova."
        )
    write_kb_env(kb, env)
    cmd = [str(engine_python), str(INGEST_SCRIPT), "--kb", args.name]
    if args.force:
        cmd.append("--force")
    sys.exit(subprocess.call(cmd, env=merged_runtime_env(kb, env)))


def cmd_move(args: argparse.Namespace) -> None:
    env = global_env()
    registry = load_registry(env)
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    if _kb_server_state(kb) != "down":
        sys.exit(
            f"Fermare prima il server di '{args.name}' con `ragcli stop {args.name}`."
        )
    current = Path(kb["data_dir"]).resolve()
    if storage_mode(kb) == "source":
        destination = (KB_ROOT / kb["name"]).resolve()
    else:
        destination = (Path(kb["source_dir"]) / ".lightrag").resolve()
    if current == destination:
        print(f"Nessuno spostamento necessario: già in {destination}")
        return
    if destination.exists() and any(destination.iterdir()):
        sys.exit(f"Destinazione non vuota: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if current.exists():
        shutil.move(str(current), str(destination))
    else:
        destination.mkdir(parents=True, exist_ok=True)
    kb["data_dir"] = str(destination)
    kb["derived_dir"] = str(destination / "derived")
    kb["rag_storage"] = str(destination / "rag_storage")
    ensure_kb_dirs(kb)
    write_kb_env(kb, env)
    save_registry(registry)
    print(f"✓ KB '{args.name}' spostata in {destination}")


def cmd_mcp_add(args: argparse.Namespace) -> None:
    env = global_env()
    registry = load_registry(env)
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' non trovata.")
    engine_python, _ = _ensure_runtime(env)
    mcp_name = f"lightrag-{args.name}"
    clients = list(mcp_clients.CLIENTS) if args.client == "all" else [args.client]
    rc = 0
    for client in clients:
        if len(clients) > 1:
            print(f"\n== {client} ==")
        fn = mcp_clients.CLIENTS[client]
        if fn(mcp_name, engine_python, MCP_SCRIPT, args.name, args.print_only, Path(kb["source_dir"])) != 0:
            rc = 1
    sys.exit(rc)


def cmd_mcp_rm(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
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
            if fn(mcp_name, Path(kb["source_dir"])) != 0:
                rc = 1
        except Exception as exc:
            print(f"Errore durante la de-registrazione da {client}: {exc}")
            rc = 1
    sys.exit(rc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ragcli",
        description="CLI minimale per gestire KB LightRAG WebUI-first con runtime condiviso.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="crea una KB puntando a una cartella sorgente")
    add.add_argument("name")
    add.add_argument("source_dir")
    add.add_argument("--port", type=int, help="porta del server LightRAG")
    add.set_defaults(func=cmd_add)

    list_cmd = sub.add_parser("list", help="elenca le KB registrate")
    list_cmd.set_defaults(func=cmd_list)

    ingest = sub.add_parser("ingest", help="sync incrementale della sorgente verso la KB già avviata")
    ingest.add_argument("name")
    ingest.add_argument("--force", action="store_true", help="rigenera derivati e riallinea forzatamente la KB")
    ingest.set_defaults(func=cmd_ingest)

    exclude = sub.add_parser("exclude", help="gestione pattern di esclusione")
    exclude_sub = exclude.add_subparsers(dest="exclude_cmd", required=True)
    ex_add = exclude_sub.add_parser("add", help="aggiunge un pattern di esclusione")
    ex_add.add_argument("name")
    ex_add.add_argument("pattern")
    ex_add.set_defaults(func=cmd_exclude_add)
    ex_rm = exclude_sub.add_parser("rm", help="rimuove un pattern di esclusione")
    ex_rm.add_argument("name")
    ex_rm.add_argument("pattern")
    ex_rm.set_defaults(func=cmd_exclude_rm)

    start = sub.add_parser("start", help="avvia il server di una KB")
    start.add_argument("name")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="ferma il server di una KB")
    stop.add_argument("name")
    stop.set_defaults(func=cmd_stop)

    remove = sub.add_parser("remove", help="elimina tutta la KB preservando solo i file sorgente")
    remove.add_argument("name")
    remove.set_defaults(func=cmd_remove)

    mcp = sub.add_parser("mcp", help="registra o rimuove l'MCP nei client supportati")
    mcp_sub = mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcp_add = mcp_sub.add_parser("add", help="registra l'MCP della KB")
    mcp_add.add_argument("name")
    mcp_add.add_argument("--client", choices=list(mcp_clients.CLIENTS) + ["all"], default="claude-code")
    mcp_add.add_argument("--print-only", action="store_true")
    mcp_add.set_defaults(func=cmd_mcp_add)
    mcp_rm = mcp_sub.add_parser("rm", help="rimuove l'MCP della KB")
    mcp_rm.add_argument("name")
    mcp_rm.add_argument("--client", choices=list(mcp_clients.REMOVERS) + ["all"], default="claude-code")
    mcp_rm.set_defaults(func=cmd_mcp_rm)

    move = sub.add_parser("move", help="sposta la data_dir tra source path e repo locale")
    move.add_argument("name")
    move.set_defaults(func=cmd_move)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
