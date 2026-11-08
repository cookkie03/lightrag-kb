"""Reject malformed ingestion requests before touching source files."""

from contextlib import nullcontext

from pipeline.ingestion_worker import IngestionWorker, StagedAction, StagedJobManifest
from storage.catalog import Catalog


class Indexer:
    """Supply the worker lock without external indexing services."""

    writer_lock = nullcontext()

    def recover_pending_checkpoints(self):
        """Report no pending recovery work."""
        return []


def test_unknown_action_does_not_complete_or_delete_staged_file(tmp_path):
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        incoming = tmp_path / "staging" / "job" / "incoming"
        incoming.mkdir(parents=True)
        staged = incoming / "file.txt"
        staged.write_text("original")
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        manifest = StagedJobManifest("job", "kb", "source", actions=[StagedAction("unknown", "file.txt")])
        result = worker.process_job("job", manifest)
        assert result.stage != "COMPLETED"
        assert staged.read_text() == "original"
