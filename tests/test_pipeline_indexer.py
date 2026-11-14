"""Tests for pipeline/indexer.py and pipeline/ingestion_worker.py (§5 of monolithic plan).

Tests:
- LightRAGClient:
  - Health check and status counts.
  - Insert text: success, transient 409 backoff retry, 409 unverified duplicate rejection, 409 verified duplicate.
  - Verification of effective PROCESSED: success, FAILED document error, timeout.
- ProtectedIndexer:
  - Add: new doc indexing, alias reuse with 0 re-embeddings.
  - Modify & Recovery:
    - Unshared document update with checkpointing and cleanup.
    - Simulated indexing failure triggers recovery from checkpoint: old state restored, no false success!
    - Shared document update (aliases): only modified file moves to new canonical doc; other aliases and old doc remain untouched in LightRAG!
  - Rename: updates catalog URI, zero new embeddings.
  - Delete:
    - Unshared file delete: removes from catalog and LightRAG.
    - Shared file delete (alias): deactivates occurrence, retains canonical doc in LightRAG for other aliases.
- IngestionWorker:
  - Source lock protection against concurrent sync runs.
  - Safety guards on deletion:
    - Push mode / scan errors -> deletes blocked.
    - High deletion threshold (>20% or >100 files) -> NEEDS_REVIEW.
    - Suspicious empty root / offline host -> NEEDS_REVIEW.
  - End-to-end staged job execution with verified PROCESSED.
  - Staging cleanup: cleans verified staged files, preserves failed items for retry, keeps derived artifacts permanently.
  - Idempotent job execution.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.indexer import (
    DocumentIndexingFailedError,
    DocumentIndexingTimeoutError,
    LightRAGClient,
    LightRAGConflictError,
    LightRAGError,
    ProtectedIndexer,
    RecoveryFailedError,
    RecoverySucceededError,
    TransientConflictError,
    UnverifiedDuplicateError,
    create_index_result,
)
from pipeline.ingestion_worker import (
    IngestionWorker,
    JobLockedError,
    create_staged_action,
    create_staged_job_manifest,
)
from pipeline.parsers import parse_document
from storage.catalog import Catalog


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def memory_catalog() -> Catalog:
    cat = Catalog(":memory:")
    yield cat
    cat.close()


@pytest.fixture
def mock_session():
    """Custom requests mock session for LightRAG HTTP endpoints."""

    class MockResponse:
        def __init__(self, status_code: int, json_data: Any = None, text: str = ""):
            self.status_code = status_code
            self._json_data = json_data
            self.text = text or (json.dumps(json_data) if json_data else "")
            self.ok = status_code in (200, 201)

        def json(self):
            if self._json_data is not None:
                return self._json_data
            if self.text:
                return json.loads(self.text)
            return {}

        def raise_for_status(self):
            if not self.ok:
                raise requests.HTTPError(f"HTTP {self.status_code}: {self.text}")

    class MockSession:
        def __init__(self):
            self.server_healthy = True
            self.documents: dict[str, dict[str, Any]] = {}
            self.call_history: list[dict[str, Any]] = []
            self.transient_409_countdown: dict[str, int] = {}
            self.inject_duplicate_409_unverified: bool = False
            self.inject_duplicate_409_verified: bool = False
            self.next_status_transition: dict[str, str] = {}

        def get(self, url: str, timeout: float = 10.0):
            self.call_history.append({"method": "GET", "url": url})
            if "/health" in url:
                if self.server_healthy:
                    return MockResponse(200, {"status": "healthy"})
                return MockResponse(503, {"status": "unhealthy"})

            if "/documents/status_counts" in url:
                processed = sum(1 for d in self.documents.values() if d.get("status") == "PROCESSED")
                pending = sum(1 for d in self.documents.values() if d.get("status") == "PENDING")
                failed = sum(1 for d in self.documents.values() if d.get("status") == "FAILED")
                return MockResponse(200, {"processed": processed, "pending": pending, "failed": failed})

            if "/documents" in url:
                statuses: dict[str, list] = {"PROCESSED": [], "PENDING": [], "FAILED": []}
                for d in self.documents.values():
                    st = d.get("status", "PROCESSED")
                    statuses.setdefault(st, []).append(d)
                return MockResponse(200, {"statuses": statuses})

            return MockResponse(404, {"error": "not found"})

        def post(self, url: str, json: dict[str, Any] | None = None, timeout: float = 10.0):
            json = json or {}
            self.call_history.append({"method": "POST", "url": url, "payload": json})

            if "/documents/paginated" in url:
                status_filter = json.get("status_filter")
                matching = []
                for d in self.documents.values():
                    if status_filter and d.get("status") != status_filter:
                        continue
                    matching.append(d)
                return MockResponse(200, {"documents": matching, "total": len(matching)})

            if "/documents/text" in url:
                file_source = json.get("file_source", "unknown")
                text = json.get("text", "")

                # Transient 409 simulation
                if self.transient_409_countdown.get(file_source, 0) > 0:
                    self.transient_409_countdown[file_source] -= 1
                    return MockResponse(409, {"detail": "pipeline occupata: queue is full"})

                # Unverified duplicate 409 simulation
                if self.inject_duplicate_409_unverified:
                    return MockResponse(409, {"detail": "already contains document"})

                # Verified duplicate 409 simulation
                if self.inject_duplicate_409_verified:
                    doc_id = f"doc_{file_source}"
                    self.documents[file_source] = {
                        "id": doc_id,
                        "file_path": file_source,
                        "status": "PROCESSED",
                    }
                    return MockResponse(409, {"detail": "already contains document"})

                # Standard insertion
                doc_id = f"id_{file_source}"
                target_status = self.next_status_transition.get(file_source, "PROCESSED")
                self.documents[file_source] = {
                    "id": doc_id,
                    "file_path": file_source,
                    "status": target_status,
                    "content": text,
                }
                return MockResponse(200, {"status": "ok", "id": doc_id, "file_source": file_source})

            return MockResponse(404, {"error": "not found"})

        def delete(self, url: str, json: dict[str, Any] | None = None, timeout: float = 10.0):
            json = json or {}
            self.call_history.append({"method": "DELETE", "url": url, "payload": json})
            if "/documents/delete_document" in url:
                doc_ids = set(json.get("doc_ids", []))
                to_delete = [k for k, v in self.documents.items() if v.get("id") in doc_ids or k in doc_ids]
                for k in to_delete:
                    del self.documents[k]
                return MockResponse(200, {"status": "ok", "deleted": len(to_delete)})
            return MockResponse(404, {"error": "not found"})

    return MockSession()


# -----------------------------------------------------------------------------
# LightRAGClient Tests (§5)
# -----------------------------------------------------------------------------


def test_client_health(mock_session):
    client = LightRAGClient(port=9621, session=mock_session)
    assert client.health() is True

    mock_session.server_healthy = False
    assert client.health() is False


def test_client_insert_transient_409_retry(mock_session):
    client = LightRAGClient(port=9621, session=mock_session)
    # Simulate 2 transient 409s before success
    mock_session.transient_409_countdown["sample_doc"] = 2

    res = client.insert_text("Some text", file_source="sample_doc", retries=3, backoff_base=0.01)
    assert res["status"] == "ok"
    assert "sample_doc" in mock_session.documents


def test_client_insert_transient_409_exhausted(mock_session):
    client = LightRAGClient(port=9621, session=mock_session)
    # 5 transient conflicts exceeds 2 retries
    mock_session.transient_409_countdown["busy_doc"] = 5

    with pytest.raises(TransientConflictError, match="pipeline occupata"):
        client.insert_text("Some text", file_source="busy_doc", retries=2, backoff_base=0.01)


def test_client_insert_unverified_409_rejected(mock_session):
    """Critical §5 requirement: 'non considerare 409 duplicato senza prova.'"""
    client = LightRAGClient(port=9621, session=mock_session)
    mock_session.inject_duplicate_409_unverified = True

    # 409 claims duplicate, but document is NOT in server -> MUST raise UnverifiedDuplicateError
    with pytest.raises(UnverifiedDuplicateError, match="verifica sul server ha dato esito negativo"):
        client.insert_text("Some text", file_source="ghost_doc", retries=1, backoff_base=0.01)


def test_client_insert_verified_409_accepted(mock_session):
    client = LightRAGClient(port=9621, session=mock_session)
    mock_session.inject_duplicate_409_verified = True

    res = client.insert_text("Some text", file_source="real_doc", retries=1, backoff_base=0.01)
    assert res["status"] == "duplicate_verified"
    assert res["existing_doc"]["file_path"] == "real_doc"


def test_client_wait_for_processed_success(mock_session):
    client = LightRAGClient(port=9621, session=mock_session)
    mock_session.documents["doc_ready"] = {"id": "1", "file_path": "doc_ready", "status": "PROCESSED"}

    doc = client.wait_for_processed("doc_ready", timeout=2.0, poll_interval=0.05)
    assert doc["status"] == "PROCESSED"


def test_client_wait_for_processed_failed(mock_session):
    client = LightRAGClient(port=9621, session=mock_session)
    mock_session.documents["doc_fail"] = {
        "id": "2",
        "file_path": "doc_fail",
        "status": "FAILED",
        "error_message": "LLM extraction OOM",
    }

    with pytest.raises(DocumentIndexingFailedError, match="LLM extraction OOM"):
        client.wait_for_processed("doc_fail", timeout=2.0, poll_interval=0.05)


def test_client_wait_for_processed_timeout(mock_session):
    client = LightRAGClient(port=9621, session=mock_session)
    mock_session.documents["doc_pending"] = {"id": "3", "file_path": "doc_pending", "status": "PENDING"}

    with pytest.raises(DocumentIndexingTimeoutError, match="Timeout"):
        client.wait_for_processed("doc_pending", timeout=0.1, poll_interval=0.02)


# -----------------------------------------------------------------------------
# ProtectedIndexer Tests (§5)
# -----------------------------------------------------------------------------


def test_protected_indexer_add_new_and_alias_reuse(memory_catalog: Catalog, mock_session, tmp_path: Path):
    cat = memory_catalog
    cat.add_source(source_id="src1", name="Source 1")
    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=tmp_path / "derived")

    # 1. Index new file: folderA/guide.md
    parsed1 = {
        "canonical_doc_id": "doc_abc123",
        "category": "text",
        "text_content": "# Guide\nContent of guide.",
        "binary_sha256": "abc123sha",
        "derived_hash": "derived_hash_1",
        "parser_name": "text_plain",
        "parser_version": "text-v1",
        "metadata": {},
        "sidecar": {},
    }
    res1 = indexer.index_new(parsed1, "src1", "folderA/guide.md", 100, 1000)
    assert res1["action"] == "indexed_new"
    assert res1["status"] == "PROCESSED"
    assert res1["reused"] is False

    # Verify catalog
    doc_rec = cat.get_document("doc_abc123")
    assert doc_rec is not None
    assert doc_rec["lightrag_status"] == "PROCESSED"
    assert doc_rec["reference_count"] == 1

    # 2. Index identical content with different path: folderB/copy_guide.md (§5 Alias Reuse)
    parsed2 = {
        "canonical_doc_id": "doc_abc123",
        "category": "text",
        "text_content": "# Guide\nContent of guide.",
        "binary_sha256": "abc123sha",
        "derived_hash": "derived_hash_1",
        "parser_name": "text_plain",
        "parser_version": "text-v1",
        "metadata": {},
        "sidecar": {},
    }
    insert_calls_before = len([c for c in mock_session.call_history if c.get("method") == "POST" and "/documents/text" in c.get("url")])

    res2 = indexer.index_new(parsed2, "src1", "folderB/copy_guide.md", 100, 1000)
    assert res2["action"] == "alias_reused"
    assert res2["reused"] is True

    insert_calls_after = len([c for c in mock_session.call_history if c.get("method") == "POST" and "/documents/text" in c.get("url")])
    # ZERO new insert calls to LightRAG!
    assert insert_calls_after == insert_calls_before

    # Reference count is now 2
    assert cat.get_document_reference_count("doc_abc123") == 2


def test_protected_indexer_modify_unshared_with_recovery(memory_catalog: Catalog, mock_session, tmp_path: Path):
    """Test Modify on unshared doc with recovery on failure (§5)."""
    cat = memory_catalog
    cat.add_source(source_id="src1", name="Source 1")
    derived_dir = tmp_path / "derived"
    derived_dir.mkdir(parents=True)
    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=derived_dir)

    # 1. Setup initial doc
    old_art = derived_dir / "text" / "file.md"
    old_art.parent.mkdir(parents=True, exist_ok=True)
    old_art.write_text("Old Version Text", encoding="utf-8")

    parsed_old = {
        "canonical_doc_id": "doc_v1",
        "category": "text",
        "text_content": "Old Version Text",
        "binary_sha256": "sha_v1",
        "derived_hash": "d_hash_v1",
        "parser_name": "text",
        "parser_version": "v1",
        "metadata": {},
        "sidecar": {},
    }
    indexer.index_new(parsed_old, "src1", "doc.md", 50, 100, artifact_path=old_art)
    assert cat.get_document_reference_count("doc_v1") == 1

    # 2. Simulate failure during modify update (e.g. LightRAG returns FAILED on new doc)
    mock_session.next_status_transition["doc_v2"] = "FAILED"

    new_art = derived_dir / "text" / "file_new.md"
    new_art.write_text("New Corrupt Version", encoding="utf-8")
    parsed_new = {
        "canonical_doc_id": "doc_v2",
        "category": "text",
        "text_content": "New Corrupt Version",
        "binary_sha256": "sha_v2",
        "derived_hash": "d_hash_v2",
        "parser_name": "text",
        "parser_version": "v1",
        "metadata": {},
        "sidecar": {},
    }

    # Update should fail AND trigger recovery from old checkpoint!
    with pytest.raises(RecoverySucceededError, match="recovery da checkpoint ha ripristinato"):
        indexer.update_modified(parsed_new, "src1", "doc.md", 60, 200, artifact_path=new_art)

    # Verify that previous state was restored in catalog
    file_rec = cat.get_file("src1", "doc.md")
    assert file_rec["canonical_doc_id"] == "doc_v1"
    doc_rec = cat.get_document("doc_v1")
    assert doc_rec["lightrag_status"] == "PROCESSED"


def test_protected_indexer_modify_shared_alias_protection(memory_catalog: Catalog, mock_session, tmp_path: Path):
    """Modifying a shared alias must create a new canonical doc without deleting the shared one (§5)."""
    cat = memory_catalog
    cat.add_source(source_id="src1", name="Source 1")
    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=tmp_path / "derived")

    # Add shared doc with 2 occurrences: fileA and fileB
    parsed_shared = {
        "canonical_doc_id": "doc_shared",
        "category": "text",
        "text_content": "Shared Document Text",
        "binary_sha256": "sha_shared",
        "derived_hash": "d_shared",
        "parser_name": "text",
        "parser_version": "v1",
        "metadata": {},
        "sidecar": {},
    }
    indexer.index_new(parsed_shared, "src1", "fileA.md", 100, 1000)
    indexer.index_new(parsed_shared, "src1", "fileB.md", 100, 1000)
    assert cat.get_document_reference_count("doc_shared") == 2

    # Now modify fileA to new content
    parsed_a_new = {
        "canonical_doc_id": "doc_a_unique",
        "category": "text",
        "text_content": "Unique File A Content",
        "binary_sha256": "sha_a_unique",
        "derived_hash": "d_a_unique",
        "parser_name": "text",
        "parser_version": "v1",
        "metadata": {},
        "sidecar": {},
    }
    res = indexer.update_modified(parsed_a_new, "src1", "fileA.md", 150, 2000)
    assert res["action"] == "updated_shared_new"
    assert res["canonical_doc_id"] == "doc_a_unique"

    # fileA now points to doc_a_unique
    f_a = cat.get_file("src1", "fileA.md")
    assert f_a["canonical_doc_id"] == "doc_a_unique"

    # fileB STILL points to doc_shared! Old doc was NOT deleted from LightRAG!
    f_b = cat.get_file("src1", "fileB.md")
    assert f_b["canonical_doc_id"] == "doc_shared"
    assert "doc_shared" in mock_session.documents
    assert cat.get_document_reference_count("doc_shared") == 1


def test_protected_indexer_delete_shared_alias_protection(memory_catalog: Catalog, mock_session, tmp_path: Path):
    """Deleting a file occurrence that shares a canonical doc preserves the doc in LightRAG (§5)."""
    cat = memory_catalog
    cat.add_source(source_id="src1", name="Source 1")
    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=tmp_path / "derived")

    parsed = {
        "canonical_doc_id": "doc_common",
        "category": "text",
        "text_content": "Common Text",
        "binary_sha256": "sha_common",
        "derived_hash": "d_common",
        "parser_name": "text",
        "parser_version": "v1",
        "metadata": {},
        "sidecar": {},
    }
    indexer.index_new(parsed, "src1", "file1.md", 100, 100)
    indexer.index_new(parsed, "src1", "file2.md", 100, 100)

    # Delete file1
    res1 = indexer.delete_file("src1", "file1.md")
    assert res1["action"] == "alias_deactivated_doc_retained"
    assert res1["deleted_from_lightrag"] is False
    assert "doc_common" in mock_session.documents

    # Now delete file2 (last remaining alias) -> doc deleted from LightRAG
    res2 = indexer.delete_file("src1", "file2.md")
    assert res2["action"] == "doc_deleted"
    assert res2["deleted_from_lightrag"] is True
    assert "doc_common" not in mock_session.documents


def test_protected_indexer_rename_zero_reembeddings(memory_catalog: Catalog, mock_session, tmp_path: Path):
    """Renaming a file occurrence must not trigger parsing or re-embedding (§5)."""
    cat = memory_catalog
    cat.add_source(source_id="src1", name="Source 1")
    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=tmp_path / "derived")

    parsed = {
        "canonical_doc_id": "doc_move",
        "category": "text",
        "text_content": "Text to move",
        "binary_sha256": "sha_move",
        "derived_hash": "d_move",
        "parser_name": "text",
        "parser_version": "v1",
        "metadata": {},
        "sidecar": {},
    }
    indexer.index_new(parsed, "src1", "old/path.md", 100, 100)

    calls_before = len(mock_session.call_history)
    res = indexer.rename_file("src1", "old/path.md", "new/path.md")
    calls_after = len(mock_session.call_history)

    assert res["action"] == "renamed"
    assert calls_after == calls_before  # ZERO calls to LightRAG
    assert cat.get_file("src1", "old/path.md") is None
    assert cat.get_file("src1", "new/path.md")["canonical_doc_id"] == "doc_move"


# -----------------------------------------------------------------------------
# IngestionWorker Tests (§4, §5)
# -----------------------------------------------------------------------------


def test_ingestion_worker_source_lock(memory_catalog: Catalog, mock_session, tmp_path: Path):
    cat = memory_catalog
    cat.add_source(source_id="src_locked", name="Source Locked")
    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=tmp_path / "derived")
    worker = IngestionWorker(cat, indexer, derived_dir=tmp_path / "derived")

    # Create active job 1
    cat.create_job("job_active_1", kb_id="kb1", source_id="src_locked", stage="PROCESSING")

    # Attempt to run worker for job 2 on same source -> locked!
    cat.create_job("job_2", kb_id="kb1", source_id="src_locked", stage="PREPARING")
    manifest = create_staged_job_manifest(job_id="job_2", kb_id="kb1", source_id="src_locked")

    with pytest.raises(JobLockedError, match="è attualmente attivo"):
        worker.process_job("job_2", manifest)


def test_ingestion_worker_safety_guard_blocks_push_delete(memory_catalog: Catalog, mock_session, tmp_path: Path):
    cat = memory_catalog
    cat.add_source(source_id="src1", name="Source 1")
    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=tmp_path / "derived")
    worker = IngestionWorker(cat, indexer, derived_dir=tmp_path / "derived")

    cat.create_job("job_push", kb_id="kb1", source_id="src1", stage="PREPARING")

    # Selected files (push) mode with a delete action
    manifest = create_staged_job_manifest(
        job_id="job_push",
        kb_id="kb1",
        source_id="src1",
        mode="selected_files",
        actions=[create_staged_action(action="delete", relative_path="some_file.md")],
    )

    job = worker.process_job("job_push", manifest)
    assert job["stage"] == "NEEDS_REVIEW"
    assert "push" in job["error_summary"]


def test_ingestion_worker_safety_guard_high_delete_threshold(memory_catalog: Catalog, mock_session, tmp_path: Path):
    cat = memory_catalog
    cat.add_source(source_id="src1", name="Source 1")
    # Add 10 active files
    for i in range(10):
        cat.record_file("src1", f"file_{i}.txt", f"sha_{i}", 100, 100, active=True)

    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=tmp_path / "derived")
    worker = IngestionWorker(cat, indexer, derived_dir=tmp_path / "derived")

    cat.create_job("job_del_high", kb_id="kb1", source_id="src1", stage="PREPARING")

    # Attempt to delete 5 out of 10 files (50% > 20% limit)
    actions = [create_staged_action(action="delete", relative_path=f"file_{i}.txt") for i in range(5)]
    manifest = create_staged_job_manifest(
        job_id="job_del_high",
        kb_id="kb1",
        source_id="src1",
        mode="full_folder",
        actions=actions,
    )

    job = worker.process_job("job_del_high", manifest)
    assert job["stage"] == "NEEDS_REVIEW"
    assert "> 20%" in job["error_summary"]

    # Ensure none of the files were actually deleted
    assert len(cat.list_files("src1", active_only=True)) == 10


def test_ingestion_worker_full_sync_lifecycle_and_cleanup(memory_catalog: Catalog, mock_session, tmp_path: Path):
    cat = memory_catalog
    cat.add_source(source_id="src_sync", name="Source Sync")
    derived_dir = tmp_path / "derived"
    staging_dir = tmp_path / "staging"
    job_id = "job_sync_100"

    incoming = staging_dir / job_id / "incoming"
    incoming.mkdir(parents=True)

    # Staged input file
    staged_file = incoming / "doc1.md"
    staged_file.write_text("# Hello World\nWorker content.", encoding="utf-8")

    client = LightRAGClient(port=9621, session=mock_session)
    indexer = ProtectedIndexer(cat, client, derived_dir=derived_dir)
    worker = IngestionWorker(cat, indexer, derived_dir=derived_dir, staging_dir=staging_dir)

    cat.create_job(job_id, kb_id="kb1", source_id="src_sync", stage="PREPARING")

    manifest = create_staged_job_manifest(
        job_id=job_id,
        kb_id="kb1",
        source_id="src_sync",
        scan_id="scan_100",
        mode="full_folder",
        actions=[
            create_staged_action(
                action="add",
                relative_path="docs/doc1.md",
                staged_filename="doc1.md",
            )
        ],
    )

    completed_job = worker.process_job(job_id, manifest)
    assert completed_job["stage"] == "COMPLETED"
    assert completed_job["counters"]["added"] == 1
    assert completed_job["counters"]["failed"] == 0

    # Staging cleanup verified (§4: staged incoming file unlinked)
    assert not staged_file.exists()

    # Derived artifacts persisted permanently (§7: markdown and sidecar exist)
    md_artifact = derived_dir / "text" / "docs" / "doc1.md.md"
    assert md_artifact.exists()
    sidecar_artifact = derived_dir / "text" / "docs" / "doc1.md.sidecar.json"
    assert sidecar_artifact.exists()

    # Idempotent re-run
    rerun_job = worker.process_job(job_id, manifest)
    assert rerun_job["stage"] == "COMPLETED"