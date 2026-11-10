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
