from __future__ import annotations

from pathlib import Path


def test_setup_script_contains_linux_bootstrap_phases() -> None:
    script = Path("setup.sh").read_text()

    assert ".venv" in script
    assert "pytest" in script
    assert "DOCLING_PLATFORM" in script
    assert "PUBLIC_MCP_KB" in script
    assert "lightrag-mcp@" in script
    assert "systemctl --user daemon-reload" in script
    assert "tailscale serve --bg" in script
    assert "tailscale funnel --bg" in script
    assert "ragcli doctor" in script


def test_setup_docs_reference_doctor_service_and_funnel() -> None:
    readme = Path("README.md").read_text()

    assert "./setup.sh" in readme
    assert "ragcli doctor" in readme
    assert "ragcli service status" in readme
    assert "tailscale funnel" in readme
