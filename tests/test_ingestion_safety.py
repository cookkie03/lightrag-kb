"""Reject malformed ingestion requests before touching source files."""

from contextlib import nullcontext

import pytest

from pipeline.ingestion_worker import IngestionWorker, create_staged_action, create_staged_job_manifest
from storage.catalog import Catalog


class Indexer:
    """Supply the worker lock without external indexing services."""

    writer_lock = nullcontext()

    def recover_pending_checkpoints(self):
        """Report no pending recovery work."""
        return []

    def delete_file(self, source_id, relative_path):
        """Fail if a safety test reaches the destructive backend."""
        raise AssertionError("unexpected deletion")


def test_unknown_action_does_not_complete_or_delete_staged_file(tmp_path):
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        incoming = tmp_path / "staging" / "job" / "incoming"
        incoming.mkdir(parents=True)
        staged = incoming / "file.txt"
        staged.write_text("original")
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        manifest = create_staged_job_manifest("job", "kb", "source", actions=[create_staged_action("unknown", "file.txt")])
        result = worker.process_job("job", manifest)
        assert result["stage"] != "COMPLETED"
        assert staged.read_text() == "original"


def test_job_completed_while_waiting_for_lock_is_not_reprocessed(tmp_path):
    class CompleteOnEnter:
        """Simulate another worker completing the job before this lock is acquired."""
        def __enter__(self):
            catalog.update_job("job", stage="COMPLETED")

        def __exit__(self, *args):
            pass

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        indexer = Indexer()
        indexer.writer_lock = CompleteOnEnter()
        worker = IngestionWorker(catalog, indexer, tmp_path / "derived")
        manifest = create_staged_job_manifest("job", "kb", "source", actions=[create_staged_action("unknown", "file")])
        assert worker.process_job("job", manifest)["stage"] == "COMPLETED"


def test_active_job_is_not_hidden_by_many_newer_finished_jobs(tmp_path):
    from pipeline.ingestion_worker import JobLockedError

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("active", "kb", source_id="source", stage="PROCESSING")
        for number in range(51):
            catalog.create_job(f"done-{number}", "kb", source_id="source", stage="COMPLETED")
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived")
        with pytest.raises(JobLockedError):
            worker.acquire_kb_lock("kb", "new-job")


def test_scan_with_errors_does_not_advance_generation(tmp_path):
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        result = worker.process_job("job", create_staged_job_manifest("job", "kb", "source", scan_complete=True, scan_errors=1))
        assert result["stage"] == "COMPLETED"
        assert catalog.get_source("source")["last_complete_scan_id"] is None


@pytest.mark.parametrize("content", ["[]", "null"])
def test_non_object_file_manifest_is_marked_for_review(tmp_path, content):
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        directory = tmp_path / "staging" / "job"
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(content)
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        assert worker.process_job("job")["stage"] == "NEEDS_REVIEW"


def test_file_manifest_requires_authoritative_admission(tmp_path):
    import json

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/original")
        catalog.create_job("job", "kb", source_id="source")
        directory = tmp_path / "staging" / "job"
        directory.mkdir(parents=True)
        manifest = {"job_id": "job", "kb_id": "kb", "source_id": "source", "origin_host": "mac", "origin_folder": "/wrong", "scan_generation": None, "mode": "full", "scan_complete": True, "scan_errors": [], "actions": [], "files": []}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        result = worker.process_job("job")
        assert result["stage"] == "NEEDS_REVIEW"


def test_job_identifier_cannot_escape_staging(tmp_path):
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("../outside", "kb", source_id="source")
        (tmp_path / "staging").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "manifest.json").write_text('{"job_id":"../outside","kb_id":"kb","source_id":"source","scan_complete":true,"scan_errors":0}')
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        result = worker.process_job("../outside")
        assert result["stage"] != "COMPLETED"


def test_suspicious_empty_source_is_blocked_even_with_unrelated_action(tmp_path):
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.record_file("source", "old.txt", "a" * 64, 1, 1)
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived")
        manifest = create_staged_job_manifest("job", "kb", "source", actions=[create_staged_action("delete", "old.txt"), create_staged_action("modify", "old.txt")])
        safe, _ = worker.validate_safety_guards(manifest)
        assert not safe


def test_unknown_source_cannot_authorize_deletion(tmp_path):
    with Catalog(":memory:") as catalog:
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived")
        manifest = create_staged_job_manifest("job", "kb", "missing", actions=[create_staged_action("delete", "file.txt")])
        safe, _ = worker.validate_safety_guards(manifest)
        assert not safe


def test_failed_upload_prevents_deletion_even_if_delete_is_first(tmp_path):
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source")
        catalog.create_job("job", "kb", source_id="source")
        worker = IngestionWorker(catalog, Indexer(), tmp_path / "derived", tmp_path / "staging")
        manifest = create_staged_job_manifest("job", "kb", "source", actions=[
            create_staged_action("delete", "old.txt"),
            create_staged_action("add", "missing.txt"),
        ])
        result = worker.process_job("job", manifest)
        assert result["stage"] != "COMPLETED"
        assert result["counters"]["deleted"] == 0
        assert result["counters"]["failed"] == 1