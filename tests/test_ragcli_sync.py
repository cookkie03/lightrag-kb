"""Exercise the machine-readable sync CLI against an isolated catalog."""

import json
import subprocess
from pathlib import Path

import pytest
import ragcli
from storage.catalog import Catalog


def test_cli_wrapper_can_import_catalog_from_other_directory(tmp_path):
    wrapper = Path(__file__).resolve().parents[1] / "bin" / "ragcli"
    result = subprocess.run([str(wrapper), "sync", "prepare", "--kb", "missing", "--source", "source", "--mode", "full", "--json"], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode != 0
    assert "ModuleNotFoundError" not in result.stderr
    assert "missing" in result.stdout


def test_job_wait_returns_failure_for_failed_job(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ragcli, "KB_ROOT", tmp_path)
    monkeypatch.setattr(ragcli, "global_env", lambda: {})
    monkeypatch.setattr(ragcli, "load_registry", lambda env: {"kbs": [{"name": "kb"}]})
    with Catalog.open_for_kb("kb", tmp_path) as catalog:
        catalog.create_job("job", "kb", source_id="source", stage="FAILED_RETRYABLE")
    args = ragcli.build_parser().parse_args(["job", "wait", "job", "--kb", "kb", "--json"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["state"] == "FAILED_RETRYABLE"


def test_job_status_reports_catalog_state(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ragcli, "KB_ROOT", tmp_path)
    monkeypatch.setattr(ragcli, "global_env", lambda: {})
    monkeypatch.setattr(ragcli, "load_registry", lambda env: {"kbs": [{"name": "kb"}]})
    with Catalog.open_for_kb("kb", tmp_path) as catalog:
        catalog.create_job("job", "kb", source_id="source", stage="PROCESSING")
    args = ragcli.build_parser().parse_args(["job", "status", "job", "--kb", "kb", "--json"])
    args.func(args)
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["state"] == "PROCESSING"
    assert result["job_id"] == "job"


def test_unknown_source_produces_json_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ragcli, "KB_ROOT", tmp_path)
    monkeypatch.setattr(ragcli, "global_env", lambda: {})
    monkeypatch.setattr(ragcli, "load_registry", lambda env: {"kbs": [{"name": "kb"}]})
    args = ragcli.build_parser().parse_args(["sync", "prepare", "--kb", "kb", "--source", "missing", "--mode", "full", "--json"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_prepare_cli_returns_job_and_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ragcli, "KB_ROOT", tmp_path)
    monkeypatch.setattr(ragcli, "global_env", lambda: {})
    monkeypatch.setattr(ragcli, "load_registry", lambda env: {"kbs": [{"name": "kb"}]})
    with Catalog.open_for_kb("kb", tmp_path) as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/notes")
    args = ragcli.build_parser().parse_args(["sync", "prepare", "--kb", "kb", "--source", "source", "--mode", "full", "--json"])
    args.func(args)
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["source"] == "source"
    with Catalog.open_for_kb("kb", tmp_path) as catalog:
        assert catalog.get_job(result["job_id"])["stage"] == "PREPARING"
