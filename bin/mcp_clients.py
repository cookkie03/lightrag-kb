"""Integrazioni client per `ragcli mcp-add` — registra l'MCP di una KB in tool terzi.

Client supportati: claude-code, claude-desktop, codex, antigravity, cursor.
Ognuno usa un meccanismo diverso: i due con CLI dedicata (claude-code, codex)
invocano il comando di sistema; gli altri editano direttamente il loro
file di config JSON (mcpServers), con backup .bak della versione precedente.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

CLAUDE_DESKTOP_CONFIG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
ANTIGRAVITY_CONFIG = Path.home() / ".gemini" / "config" / "mcp_config.json"


# Construct the standard MCP command and argument dictionary for the lightrag server.
def _server_entry(python: Path, script: Path, kb_name: str) -> dict:
    return {"command": str(python), "args": [str(script), "--kb", kb_name]}


# Merge one server without discarding other settings; back up before overwriting.
def _merge_json_mcp_server(path: Path, mcp_name: str, entry: dict, print_only: bool) -> int:
    if print_only:
        print(f"--- {path} (mcpServers.{mcp_name}) ---")
        print(json.dumps(entry, indent=2))
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads(path.read_text()) if path.exists() else {}
    config.setdefault("mcpServers", {})[mcp_name] = entry
    if path.exists():
        shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(f"Registrato in {path}")
    return 0


# Resolve the Cursor project configuration file path (.cursor/mcp.json).
def _cursor_config_path(source_folder: Path | None = None) -> Path:
    if source_folder and (source_folder / ".cursor" / "mcp.json").exists():
        return source_folder / ".cursor" / "mcp.json"
    cwd_path = Path.cwd() / ".cursor" / "mcp.json"
    if cwd_path.exists():
        return cwd_path
    base = source_folder or Path.cwd()
    return base / ".cursor" / "mcp.json"


# Register the project MCP server in Claude Code using its CLI.
def add_claude_code(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
                     source_folder: Path | None = None) -> int:
    cwd = Path.cwd()
    cmd = ["claude", "mcp", "add", "--scope", "project", mcp_name,
           "--", str(python), str(script), "--kb", kb_name]
    print(f"Comando di registrazione (progetto locale: {cwd}):\n  " + " ".join(cmd))
    if print_only:
        return 0
    rc = subprocess.call(cmd, cwd=cwd)
    if rc == 0:
        print(f"MCP '{mcp_name}' registrato in {cwd}/.mcp.json (scope project). "
              f"Verifica con /mcp in Claude Code aprendo quel progetto.")
    return rc


# Register the server into Claude Desktop global config JSON.
def add_claude_desktop(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
                        source_folder: Path | None = None) -> int:
    rc = _merge_json_mcp_server(CLAUDE_DESKTOP_CONFIG, mcp_name, _server_entry(python, script, kb_name), print_only)
    if rc == 0 and not print_only:
        print("Riavvia Claude Desktop per applicare le modifiche.")
    return rc


# Register the server globally via Codex CLI.
def add_codex(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
              source_folder: Path | None = None) -> int:
    # codex CLI non supporta mcp_servers per-progetto: config.toml è sempre globale.
    cmd = ["codex", "mcp", "add", mcp_name, "--", str(python), str(script), "--kb", kb_name]
    print("Codex non supporta MCP locali al progetto, registro globalmente.\nComando di registrazione:\n  " + " ".join(cmd))
    if print_only:
        return 0
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"MCP '{mcp_name}' registrato in Codex CLI. Verifica con /mcp nella TUI di codex.")
    return rc


# Register the server into Antigravity mcp_config.json.
def add_antigravity(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
                     source_folder: Path | None = None) -> int:
    rc = _merge_json_mcp_server(ANTIGRAVITY_CONFIG, mcp_name, _server_entry(python, script, kb_name), print_only)
    if rc == 0 and not print_only:
        print("Riavvia Antigravity (o ricarica i MCP server) per applicare le modifiche.")
    return rc


# Register the KB MCP server in the Cursor project configuration file (.cursor/mcp.json).
def add_cursor(mcp_name: str, python: Path, script: Path, kb_name: str, print_only: bool,
               source_folder: Path | None = None) -> int:
    path = _cursor_config_path(source_folder)
    rc = _merge_json_mcp_server(path, mcp_name, _server_entry(python, script, kb_name), print_only)
    if rc == 0 and not print_only:
        print("Riavvia Cursor (o ricarica i server MCP) per applicare le modifiche.")
    return rc


CLIENTS = {
    "claude-code": add_claude_code,
    "claude-desktop": add_claude_desktop,
    "codex": add_codex,
    "antigravity": add_antigravity,
    "cursor": add_cursor,
}


# Remove a JSON-configured server only after successfully reading its config.
def _remove_json_mcp_server(path: Path, mcp_name: str) -> int:
    if not path.exists():
        print(f"File {path} non esiste.")
        return 0
    try:
        config = json.loads(path.read_text())
    except Exception as e:
        print(f"Errore nel caricamento di {path}: {e}")
        return 1
    servers = config.get("mcpServers", {})
    if mcp_name in servers:
        servers.pop(mcp_name, None)
        shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
        path.write_text(json.dumps(config, indent=2) + "\n")
        print(f"Rimosso da {path}")
    else:
        print(f"Server '{mcp_name}' non trovato in {path}")
    return 0


# Remove the server registration from Claude Code project or global scope.
def remove_claude_code(mcp_name: str, source_folder: Path | None = None) -> int:
    cwd = Path.cwd()
    if (cwd / ".mcp.json").exists():
        cmd = ["claude", "mcp", "remove", "--scope", "project", mcp_name]
        print(f"Comando di de-registrazione (progetto locale: {cwd}):\n  " + " ".join(cmd))
        try:
            rc = subprocess.call(cmd, cwd=cwd)
            if rc == 0:
                print(f"MCP '{mcp_name}' rimosso da {cwd}/.mcp.json.")
            else:
                print(f"Fallito: comando ha restituito {rc}.")
            return rc
        except Exception as e:
            print(f"Errore durante la rimozione da Claude Code: {e}")
            return 1
    cmd = ["claude", "mcp", "remove", mcp_name]
    print("Nessun .mcp.json nella directory corrente; rimozione dal registro globale.\nComando di de-registrazione:\n  " + " ".join(cmd))
    try:
        rc = subprocess.call(cmd)
        if rc == 0:
            print(f"MCP '{mcp_name}' rimosso da Claude Code.")
        else:
            print(f"Fallito: comando ha restituito {rc}.")
        return rc
    except Exception as e:
        print(f"Errore durante la rimozione da Claude Code: {e}")
        return 1


# Remove the server from Claude Desktop global config.
def remove_claude_desktop(mcp_name: str, source_folder: Path | None = None) -> int:
    return _remove_json_mcp_server(CLAUDE_DESKTOP_CONFIG, mcp_name)


# Remove the server from Codex CLI global registration.
def remove_codex(mcp_name: str, source_folder: Path | None = None) -> int:
    cmd = ["codex", "mcp", "remove", mcp_name]
    print("Comando di de-registrazione:\n  " + " ".join(cmd))
    try:
        rc = subprocess.call(cmd)
        if rc == 0:
            print(f"MCP '{mcp_name}' rimosso da Codex CLI.")
        else:
            print(f"Fallito: comando ha restituito {rc}.")
        return rc
    except Exception as e:
        print(f"Errore durante la rimozione da Codex: {e}")
        return 1


# Remove the server from Antigravity configuration.
def remove_antigravity(mcp_name: str, source_folder: Path | None = None) -> int:
    return _remove_json_mcp_server(ANTIGRAVITY_CONFIG, mcp_name)


# Remove the specified server from .cursor/mcp.json and create a backup of the original config.
def remove_cursor(mcp_name: str, source_folder: Path | None = None) -> int:
    path = _cursor_config_path(source_folder)
    return _remove_json_mcp_server(path, mcp_name)


REMOVERS = {
    "claude-code": remove_claude_code,
    "claude-desktop": remove_claude_desktop,
    "codex": remove_codex,
    "antigravity": remove_antigravity,
    "cursor": remove_cursor,
}