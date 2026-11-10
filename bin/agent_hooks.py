#!/usr/bin/env python3
"""Agent hooks for coding agents (Claude Code, Antigravity, Cursor, Codex).

Provides pre-prompt context injection and post-session memory capture,
with client hook configuration management (.claude/settings.json, etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(ROOT / "bin"))

from kb_registry import find_kb, load_registry  # noqa: E402
from pipeline.dreaming import query_memories, remember_memory  # noqa: E402


# Query KB active memories matching the prompt and format an injection context block.
def inject_pre_prompt_context(kb_name: str, prompt: str, max_items: int = 5) -> str:
    reg = load_registry()
    kb_info = find_kb(reg, kb_name)
    if not kb_info:
        return ""

    source_dir = kb_info.get("source_dir")
    if not source_dir:
        return ""

    # Always retrieve active preferences first
    prefs = query_memories(source_dir, query="", memory_type="preference", active_only=True, top_k=3)
    # Then retrieve query-matched decisions and facts
    matched = query_memories(source_dir, query=prompt, active_only=True, top_k=max_items)

    combined = []
    seen_ids = set()
    for m in prefs:
        if m.id not in seen_ids:
            seen_ids.add(m.id)
            combined.append(m)
    for m in matched:
        if m.id not in seen_ids:
            seen_ids.add(m.id)
            combined.append(m)

    if not combined:
        return ""

    lines = [f'<project_memory kb="{kb_name}">']
    for m in combined:
        mtype = m.type.upper()
        # Clean one-line summary
        content_preview = m.content.strip().splitlines()[0][:140]
        title_str = f" ({m.title})" if m.title and m.title not in content_preview else ""
        lines.append(f"- [{mtype}]{title_str}: {content_preview}")
    lines.append("</project_memory>")

    return "\n".join(lines)


# Parse a session transcript or text, extract key decisions or facts, and save episodic/decision memory.
def capture_post_session(
    kb_name: str,
    transcript_path: Path | str | None = None,
    raw_text: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    reg = load_registry()
    kb_info = find_kb(reg, kb_name)
    if not kb_info:
        return {"status": "error", "message": f"KB '{kb_name}' not found"}

    source_dir = kb_info.get("source_dir")
    text = raw_text or ""
    if transcript_path:
        t_file = Path(transcript_path)
        if t_file.is_file():
            text = t_file.read_text(encoding="utf-8", errors="replace")

    if not text.strip():
        return {"status": "skipped", "message": "No transcript content provided"}

    # Extract salient lines or summary
    first_lines = [line.strip() for line in text.splitlines() if line.strip()][:5]
    summary_preview = " ".join(first_lines)[:100] or "Session completion"

    sid = session_id or "session"
    title = f"Episodic Log {sid}: {summary_preview}"
    mem = remember_memory(
        source_dir=source_dir,
        content=text.strip(),
        memory_type="episodic",
        title=title,
        tags=["session", "capture"],
        source=f"hook:{sid}",
    )

    return {
        "status": "ok",
        "memory_id": mem.id,
        "title": mem.title,
    }


# Install hooks in Claude Code project or global settings.
def install_client_hook(
    client: str = "claude-code",
    kb_name: str = "agent-memory",
    target_project_dir: Path | str | None = None,
) -> dict[str, Any]:
    proj_dir = Path(target_project_dir).resolve() if target_project_dir else Path.cwd().resolve()

    if client == "claude-code":
        claude_dir = proj_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file = claude_dir / "settings.json"

        settings_data: dict[str, Any] = {}
        if settings_file.is_file():
            try:
                settings_data = json.loads(settings_file.read_text(encoding="utf-8"))
            except Exception:
                settings_data = {}

        hooks = settings_data.setdefault("hooks", {})
        # Configure UserPromptSubmit hook
        inject_cmd = f"ragcli memory inject --kb {kb_name}"
        capture_cmd = f"ragcli memory capture --kb {kb_name}"

        hooks["UserPromptSubmit"] = [
            {
                "type": "command",
                "command": inject_cmd,
            }
        ]
        hooks["SessionEnd"] = [
            {
                "type": "command",
                "command": capture_cmd,
            }
        ]

        settings_file.write_text(json.dumps(settings_data, indent=2) + "\n", encoding="utf-8")
        return {
            "status": "ok",
            "client": client,
            "configured_file": str(settings_file),
            "hooks": list(hooks.keys()),
        }

    return {
        "status": "error",
        "message": f"Unsupported client: {client}. Currently supported: claude-code",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Hooks for Memory Injection and Capture")
    subparsers = parser.add_subparsers(dest="action")

    # inject
    inj_parser = subparsers.add_parser("inject")
    inj_parser.add_argument("--kb", default="agent-memory", help="Target knowledge base")
    inj_parser.add_argument("--prompt", default="", help="User prompt to match against memories")

    # capture
    cap_parser = subparsers.add_parser("capture")
    cap_parser.add_argument("--kb", default="agent-memory", help="Target knowledge base")
    cap_parser.add_argument("--transcript", default=None, help="Path to transcript file")
    cap_parser.add_argument("--text", default=None, help="Raw transcript text")
    cap_parser.add_argument("--session-id", default=None, help="Session identifier")

    # install
    inst_parser = subparsers.add_parser("install")
    inst_parser.add_argument("--client", default="claude-code", choices=["claude-code"])
    inst_parser.add_argument("--kb", default="agent-memory")
    inst_parser.add_argument("--project-dir", default=None)

    args = parser.parse_args()

    if args.action == "inject":
        # Read from argument or stdin if piped
        prompt = args.prompt
        if not prompt and not sys.stdin.isatty():
            prompt = sys.stdin.read()
        res = inject_pre_prompt_context(args.kb, prompt)
        if res:
            print(res)
    elif args.action == "capture":
        text = args.text
        if not text and not args.transcript and not sys.stdin.isatty():
            text = sys.stdin.read()
        res = capture_post_session(
            kb_name=args.kb,
            transcript_path=args.transcript,
            raw_text=text,
            session_id=args.session_id,
        )
        print(json.dumps(res, indent=2))
    elif args.action == "install":
        res = install_client_hook(
            client=args.client,
            kb_name=args.kb,
            target_project_dir=args.project_dir,
        )
        print(json.dumps(res, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
