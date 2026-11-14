"""Verify staged content is checked before parsing or indexing."""

from contextlib import nullcontext

from pipeline.ingestion_worker import IngestionWorker, create_staged_action, create_staged_job_manifest
from storage.catalog import Catalog


class Indexer:
    """Provide a lock without touching an external LightRAG server."""
    writer_lock = nullcontext()

    def recover_pending_checkpoints(self):
        """Indicate no interrupted indexing work."""
        return []


def test_checksum_mismatch_keeps_staged_file_and_skips_parser(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("pipeline.ingestion_worker.parse_document", lambda **kwargs: calls.append(kwargs))
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        incoming = tmp_path / "staging" / "job" / "incoming"
        incoming.mkdir(parents=True)
        staged = incoming / "file.txt"
        staged.write_text("actual data")
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        action = create_staged_action("add", "file.txt", binary_sha256="0" * 64, staged_filename="file.txt")
        result = worker.process_job("job", create_staged_job_manifest("job", "kb", "source", actions=[action]))
        assert result["stage"] == "FAILED_RETRYABLE"
        assert not calls
        assert staged.read_text() == "actual data"