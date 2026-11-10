"""Verify worker refuses untrusted or incorrectly staged inputs."""

from contextlib import nullcontext

from pipeline.indexer import LightRAGError, ProtectedIndexer
from pipeline.ingestion_worker import IngestionWorker, StagedAction, StagedJobManifest
from pipeline.parsers import parse_document, save_derived_artifacts
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


# Simulate only the remote backend; parsing, persistence and recovery remain real.
class RecoveryBackend:
    # Start with an empty backend and no simulated insertion failure.
    def __init__(self):
        self.documents = {}
        self.rejected_content = None

    # Reject the new version while allowing checkpoint content to be restored.
    def insert_text(self, text, file_source):
        if text == self.rejected_content:
            self.rejected_content = None
            raise LightRAGError("new version rejected")
        self.documents[file_source] = {
            "id": file_source,
            "status": "PROCESSED",
            "content": text,
        }
        return self.documents[file_source]

    # Expose the stored backend content to the real indexer.
    def wait_for_processed(self, file_source):
        return self.documents[file_source]

    # Remove the old version before the real indexer attempts replacement.
    def delete_documents(self, doc_ids):
        for doc_id in doc_ids:
            self.documents.pop(doc_id, None)
        return True


# Catch a checkpoint that reads newly overwritten text instead of the old version.
def test_worker_recovery_preserves_old_content_at_same_artifact_path(tmp_path):
    old_input = tmp_path / "note.txt"
    old_input.write_text("VERSIONE_1", encoding="utf-8")
    derived = tmp_path / "derived"
    staging = tmp_path / "staging"
    backend = RecoveryBackend()

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        indexer = ProtectedIndexer(catalog, backend, derived_dir=derived)
        old_parsed = parse_document(old_input)
        old_artifact, _ = save_derived_artifacts(old_parsed, derived, "note.txt")
        indexer.index_new(
            old_parsed, "source", "note.txt", 10, 1, artifact_path=old_artifact
        )
        assert old_artifact.read_text(encoding="utf-8") == "VERSIONE_1"

        incoming = staging / "job" / "incoming"
        incoming.mkdir(parents=True)
        new_input = incoming / "note.txt"
        new_input.write_text("VERSIONE_2", encoding="utf-8")
        backend.rejected_content = "VERSIONE_2"
        catalog.create_job("job", "kb", source_id="source")
        worker = IngestionWorker(catalog, indexer, derived, staging)
        action = StagedAction("modify", "note.txt", staged_filename="note.txt")
        manifest = StagedJobManifest("job", "kb", "source", actions=[action])

        result = worker.process_job("job", manifest)

        assert result.stage == "FAILED_RETRYABLE"
        assert new_input.read_text(encoding="utf-8") == "VERSIONE_2"
        assert catalog.get_file("source", "note.txt").canonical_doc_id == old_parsed.canonical_doc_id
        assert backend.documents[old_parsed.canonical_doc_id]["content"] == "VERSIONE_1"
        assert old_artifact.read_text(encoding="utf-8") == "VERSIONE_1"
