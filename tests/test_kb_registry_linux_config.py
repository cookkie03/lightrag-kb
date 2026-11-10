from __future__ import annotations

from pathlib import Path

from kb_registry import global_env


def test_global_env_exposes_linux_docling_defaults(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "global.env"
    config_file.write_text(
        "\n".join(
            [
                "ENGINE_PYTHON=/tmp/venv/bin/python",
                "LIGHTRAG_SERVER_BIN=/tmp/venv/bin/lightrag-server",
                "DOCLING_PLATFORM=linux",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr("kb_registry.CONFIG", config_file)

    env = global_env()

    assert env["DOCLING_PLATFORM"] == "linux"
    assert env["DOCLING_ASR_ENGINE"] == "whisper_turbo_native"
    assert env["DOCLING_OCR_ENGINE"] in {"rapidocr", "easyocr", "auto"}
    assert env["MCP_BIND_PORT"] == "8743"
    assert env["SHIM_BIND_HOST"] == "127.0.0.1"
    assert env["TAILSCALE_FUNNEL_PORT"] == "8443"


def test_global_env_normalizes_linux_platform_before_defaults(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "global.env"
    config_file.write_text(
        "\n".join(
            [
                "ENGINE_PYTHON=/tmp/venv/bin/python",
                "LIGHTRAG_SERVER_BIN=/tmp/venv/bin/lightrag-server",
                "DOCLING_PLATFORM= Linux ",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr("kb_registry.CONFIG", config_file)

    env = global_env()

    assert env["DOCLING_PLATFORM"] == "linux"
    assert env["DOCLING_ASR_ENGINE"] == "whisper_turbo_native"
