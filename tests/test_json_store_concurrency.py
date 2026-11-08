"""Exercise cross-process JSON updates and corrupt snapshot protection."""

from concurrent.futures import ProcessPoolExecutor

import pytest

from storage.catalog import Catalog


def _increment(path):
    """Increment one shared job counter from an independent process."""
    with Catalog(path) as catalog:
        for _ in range(15):
            catalog.increment_job_counter("job", "added")


def test_concurrent_processes_do_not_lose_job_updates(tmp_path):
    path = tmp_path / "catalog.json"
    with Catalog(path) as catalog:
        catalog.create_job("job", "kb")
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_increment, [path] * 4))
    with Catalog(path) as catalog:
        assert catalog.get_job("job").counters["added"] == 60


def test_invalid_collection_record_does_not_replace_snapshot(tmp_path):
    path = tmp_path / "catalog.json"
    original = '{"version":1,"sources":{"broken":null},"files":{},"documents":{},"jobs":{}}'
    path.write_text(original)
    with pytest.raises(ValueError):
        with Catalog(path) as catalog:
            catalog.add_source("good", "Good")
    assert path.read_text() == original
