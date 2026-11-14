import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from bin.agent_hooks import (
    capture_post_session,
    inject_pre_prompt_context,
    install_client_hook,
)
from pipeline.dreaming import remember_memory


def test_inject_pre_prompt_context():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        remember_memory(
            source_dir=source_dir,
            content="Preferire sempre codice asincrono e FastAPI.",
            memory_type="preference",
            title="Async Pattern",
        )
        remember_memory(
            source_dir=source_dir,
            content="Tailscale Funnel usato su porta 8443.",
            memory_type="decision",
            title="Funnel Port",
        )

        mock_reg = {
            "kbs": [
                {
                    "name": "agent-memory",
                    "source_dir": str(source_dir),
                }
            ]
        }

        with patch("bin.agent_hooks.load_registry", return_value=mock_reg):
            # Test match
            res = inject_pre_prompt_context("agent-memory", "quale porta usa Tailscale?")
            assert '<project_memory kb="agent-memory">' in res
            assert "Async Pattern" in res or "Preferire sempre" in res
            assert "Funnel Port" in res or "Tailscale Funnel" in res


def test_capture_post_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_dir = Path(tmpdir)
        mock_reg = {
            "kbs": [
                {
                    "name": "agent-memory",
                    "source_dir": str(source_dir),
                }
            ]
        }

        with patch("bin.agent_hooks.load_registry", return_value=mock_reg):
            res = capture_post_session(
                kb_name="agent-memory",
                raw_text="Abbiamo completato l'integrazione di FastMCP con remember tool.",
                session_id="ses_123",
            )
            assert res["status"] == "ok"
            assert "ses_123" in res["title"]

            # Verify file created
            mem_files = list((source_dir / "memories" / "episodic").rglob("*.md"))
            assert len(mem_files) == 1
            assert "FastMCP" in mem_files[0].read_text()


def test_install_client_hook_claude_code():
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir)
        res = install_client_hook("claude-code", "my-kb", proj_dir)
        assert res["status"] == "ok"
        settings_file = proj_dir / ".claude" / "settings.json"
        assert settings_file.is_file()

        data = json.loads(settings_file.read_text())
        assert "hooks" in data
        assert "UserPromptSubmit" in data["hooks"]
        assert "my-kb" in data["hooks"]["UserPromptSubmit"][0]["command"]

def test_install_client_hook_cursor():
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir)
        res = install_client_hook("cursor", "my-kb", proj_dir)
        assert res["status"] == "ok"
        assert res["client"] == "cursor"
        rules_file = proj_dir / ".cursor" / "rules" / "agent-memory.mdc"
        assert rules_file.is_file()
        assert res["configured_file"] == str(rules_file)

        content = rules_file.read_text(encoding="utf-8")
        assert "remember" in content
        assert "search_memory" in content
        assert "deprecate_memory" in content
        assert "ragcli memory inject --kb my-kb" in content


def test_install_client_hook_codex():
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir)
        res = install_client_hook("codex", "my-kb", proj_dir)
        assert res["status"] == "ok"
        assert res["client"] == "codex"
        instructions_file = proj_dir / ".codex" / "instructions.md"
        assert instructions_file.is_file()
        assert res["configured_file"] == str(instructions_file)

        content = instructions_file.read_text(encoding="utf-8")
        assert "ragcli memory" in content
        assert "my-kb" in content

        # Verify appending when file already exists
        res2 = install_client_hook("codex", "other-kb", proj_dir)
        assert res2["status"] == "ok"
        content_after = instructions_file.read_text(encoding="utf-8")
        assert "my-kb" in content_after
        assert "other-kb" in content_after


def test_install_client_hook_antigravity():
    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir)
        res = install_client_hook("antigravity", "my-kb", proj_dir)
        assert res["status"] == "ok"
        assert res["client"] == "antigravity"
        gemini_file = proj_dir / ".gemini" / "GEMINI.md"
        assert gemini_file.is_file()
        assert res["configured_file"] == str(gemini_file)

        content = gemini_file.read_text(encoding="utf-8")
        assert "ragcli memory recall" in content
        assert "ragcli memory inject" in content
        assert "my-kb" in content


def test_mcp_cursor_add_and_remove():
    from bin.mcp_clients import CLIENTS, REMOVERS, add_cursor, remove_cursor

    assert "cursor" in CLIENTS
    assert "cursor" in REMOVERS

    with tempfile.TemporaryDirectory() as tmpdir:
        proj_dir = Path(tmpdir)
        py = Path("/usr/bin/python3")
        script = Path("/path/to/server.py")

        rc = add_cursor("lightrag-test", py, script, "test-kb", False, source_folder=proj_dir)
        assert rc == 0
        cfg_file = proj_dir / ".cursor" / "mcp.json"
        assert cfg_file.is_file()

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "lightrag-test" in data.get("mcpServers", {})
        assert data["mcpServers"]["lightrag-test"]["args"] == [str(script), "--kb", "test-kb"]

        # Verify removal
        rc_rm = remove_cursor("lightrag-test", source_folder=proj_dir)
        assert rc_rm == 0
        data_after = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert "lightrag-test" not in data_after.get("mcpServers", {})
        assert (proj_dir / ".cursor" / "mcp.json.bak").is_file()
