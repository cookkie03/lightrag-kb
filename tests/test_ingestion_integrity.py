"""Verify worker refuses untrusted or incorrectly staged inputs."""

from contextlib import nullcontext

from pipeline.ingestion_worker import IngestionWorker, StagedAction, StagedJobManifest
from storage.catalog import Catalog


class Indexer:
    """Provide a no-op lock for tests that must stop before indexing."""
    writer_lock = nullcontext()

    def recover_pending_checkpoints(self):
        """Return no pending checkpoints."""
        return []


def test_manifest_identity_mismatch_is_rejected(tmp_path):
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        result = worker.process_job("job", StagedJobManifest("other", "kb", "source"))
        assert result.stage != "COMPLETED"


def test_staged_symlink_outside_sandbox_is_rejected(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("pipeline.ingestion_worker.parse_document", lambda **kwargs: calls.append(kwargs))
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        incoming = tmp_path / "staging" / "job" / "incoming"
        incoming.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("outside")
        (incoming / "file.txt").symlink_to(outside)
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        action = StagedAction("add", "file.txt", staged_filename="file.txt")
        result = worker.process_job("job", StagedJobManifest("job", "kb", "source", actions=[action]))
        assert result.stage != "COMPLETED"
        assert not calls
        assert outside.read_text() == "outside"
