import argparse
import pytest
from bin.ragcli import cmd_config_show, cmd_config_set, build_parser
from bin.kb_registry import global_env, upsert_global_env


def test_config_show_runs_cleanly(capsys):
    parser = build_parser()
    args = parser.parse_args(["config", "show"])
    args.func(args)
    captured = capsys.readouterr().out
    assert "LIGHTRAG-KB CONFIGURATION PANEL" in captured
    assert "FASE 2: PARSING & EXTRACTION" in captured
    assert "Pipeline Audio/Video" in captured


def test_config_set_toggles_pipeline_and_models(tmp_path, monkeypatch, capsys):
    test_env = tmp_path / "global.env"
    test_env.write_text("LLM_MODEL=test-llm\nEMBEDDING_MODEL=test-emb\nDOCLING_ASR_ENGINE=whisper_turbo_native\n")

    import kb_registry
    monkeypatch.setattr(kb_registry, "CONFIG", test_env)
    monkeypatch.setattr("bin.kb_registry.CONFIG", test_env)

    parser = build_parser()
    args = parser.parse_args([
        "config", "set",
        "--disable-audio",
        "--enable-pdf",
        "--llm", "qwen3.5:4b",
        "--embedding", "embeddinggemma-2:latest",
    ])
    args.func(args)
    captured = capsys.readouterr().out
    assert "Configurazione aggiornata con successo" in captured
    assert "PIPELINE_ENABLE_AUDIO = 0" in captured
    assert "EMBEDDING_DIM = 768" in captured

    content = test_env.read_text()
    assert "PIPELINE_ENABLE_AUDIO=0" in content
    assert "LLM_MODEL=qwen3.5:4b" in content
    assert "EMBEDDING_DIM=768" in content


def test_config_set_rejects_non_whisper_asr():
    parser = build_parser()
    args = parser.parse_args(["config", "set", "--asr", "invalid_asr_model"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert "al momento è supportato solo Whisper" in str(exc.value)
