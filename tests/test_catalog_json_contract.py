"""Check the catalog uses the required JSON snapshot without touching legacy data."""

import json

import pytest

from storage.catalog import Catalog


def test_catalog_persists_sources_in_json(tmp_path):
    with Catalog.open_for_kb("notes", tmp_path) as catalog:
        catalog.add_source("mac", "Notes")
    snapshot = json.loads((tmp_path / "notes" / "catalog.json").read_text())
    assert snapshot["sources"]["mac"]["name"] == "Notes"
    with Catalog.open_for_kb("notes", tmp_path) as catalog:
        assert catalog.get_source("mac")["name"] == "Notes"


def test_existing_sqlite_is_not_silently_replaced(tmp_path):
    directory = tmp_path / "notes"
    directory.mkdir()
    old = directory / "catalog.sqlite"
    old.write_bytes(b"legacy data")
    with pytest.raises(RuntimeError, match="SQLite"):
        Catalog.open_for_kb("notes", tmp_path)
    assert old.read_bytes() == b"legacy data"
    assert not (directory / "catalog.json").exists()