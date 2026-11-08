"""Verify durable, coordinated JSON snapshots for KB catalog data."""

import json

import pytest

from storage.json_store import JsonStore


def test_committed_snapshot_is_visible_to_other_instances(tmp_path):
    path = tmp_path / "catalog.json"
    first = JsonStore(path)
    second = JsonStore(path)
    with first.transaction() as data:
        data["sources"]["source-1"] = {"name": "Notes"}
    with second.transaction() as data:
        assert data["sources"]["source-1"]["name"] == "Notes"
    assert json.loads(path.read_text())["version"] == 1


def test_failed_transaction_does_not_modify_snapshot(tmp_path):
    store = JsonStore(tmp_path / "catalog.json")
    with store.transaction() as data:
        data["sources"]["original"] = {}
    with pytest.raises(RuntimeError):
        with store.transaction() as data:
            data["sources"].clear()
            raise RuntimeError("abort")
    with store.transaction() as data:
        assert "original" in data["sources"]


def test_corrupt_snapshot_is_never_overwritten(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text("invalid json")
    with pytest.raises(ValueError):
        with JsonStore(path).transaction():
            pass
    assert path.read_text() == "invalid json"
