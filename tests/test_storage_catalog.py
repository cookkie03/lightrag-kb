"""Unit tests for storage/catalog.py according to §3 of the monocentric plan.

Tests:
- JSON snapshot initialization and validation per KB
- Sources CRUD, validation, deletion policy, scan state
- Documents CRUD, synthetic canonical doc IDs, reference count reconstruction
- Files CRUD, (source_id, relative_path) uniqueness, rename without re-embedding
- Path collision handling: same basename in different folders (both identical and distinct content)
- Provenance / evidence resolution from canonical_doc_id to original sources
- Exact duplicate clustering across sources (§8 A1)
- Jobs lifecycle, idempotency key uniqueness, stages, counters, warnings, resume markers
- find_documents pagination and filtering (§6)
- Scan reconciliation (marking unseen files inactive)
- Atomic transactions and rollbacks
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.catalog import (
    DEFAULT_JOB_COUNTERS,
    VALID_JOB_STAGES,
    VALID_SOURCE_MODES,
    Catalog,
    DocumentRecord,
    FileRecord,
    JobRecord,
    SourceRecord,
    generate_canonical_doc_id,
)


@pytest.fixture
def memory_catalog() -> Catalog:
    cat = Catalog(":memory:")
    yield cat
    cat.close()


@pytest.fixture
def disk_catalog(tmp_path: Path) -> Catalog:
    db_file = tmp_path / "catalog.json"
    cat = Catalog(db_file)
    yield cat
    cat.close()


# -----------------------------------------------------------------------------
# Initialization & Setup Tests
# -----------------------------------------------------------------------------


def test_catalog_init_memory_and_file(tmp_path: Path):
    with Catalog(":memory:") as cat:
        assert cat.path is None
        cat.add_source("local", "Local")
        assert cat.get_source("local").name == "Local"

    db_path = tmp_path / "test_kb" / "catalog.json"
    with Catalog(db_path) as cat:
        assert cat.path == db_path.resolve()
        cat.add_source("local", "Local")
    assert json.loads(db_path.read_text())["version"] == 1


def test_catalog_open_for_kb(tmp_path: Path):
    kb_root = tmp_path / "kbs"
    cat = Catalog.open_for_kb("kb-alpha", kb_root=kb_root)
    try:
        expected_path = kb_root / "kb-alpha" / "catalog.json"
        assert cat.path == expected_path.resolve()
        cat.add_source("local", "Local")
        assert expected_path.exists()
    finally:
        cat.close()


def test_canonical_doc_id_generation():
    sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    doc_id = generate_canonical_doc_id(sha)
    assert doc_id == "doc_e3b0c44298fc1c14"

    # With revision > 1
    doc_id_rev2 = generate_canonical_doc_id(sha, revision=2)
    assert doc_id_rev2 == "doc_e3b0c44298fc1c14_r2"

    # Empty hash fallback
    assert generate_canonical_doc_id("") == "doc_empty"


# -----------------------------------------------------------------------------
# Sources Tests (§3)
# -----------------------------------------------------------------------------


def test_source_lifecycle(memory_catalog: Catalog):
    cat = memory_catalog

    # Create source
    src = cat.add_source(
        source_id="mac-brain",
        name="MacBook Second Brain",
        origin_host="macbook-pro",
        origin_folder="/Users/luca/Second-Brain",
        mode="full_folder",
        include_patterns=["*.md", "*.pdf"],
        exclude_patterns=[".git/*", "*.tmp"],
        deletion_policy="guarded",
        status="active",
    )
    assert src.id == "mac-brain"
    assert src.name == "MacBook Second Brain"
    assert src.include_patterns == ["*.md", "*.pdf"]
    assert src.exclude_patterns == [".git/*", "*.tmp"]
    assert src.mode == "full_folder"
    assert src.last_complete_scan_id is None

    # Retrieve source
    fetched = cat.get_source("mac-brain")
    assert fetched is not None
    assert fetched.id == src.id
    assert fetched.to_dict()["name"] == "MacBook Second Brain"

    # Non-existent source
    assert cat.get_source("non-existent") is None

    # Invalid mode validation
    with pytest.raises(ValueError, match="Invalid source mode"):
        cat.add_source("bad-src", "Bad Source", mode="invalid_mode")

    # Update source
    updated = cat.update_source(
        "mac-brain",
        name="Updated Name",
        include_patterns=["*.txt"],
        status="paused",
    )
    assert updated.name == "Updated Name"
    assert updated.include_patterns == ["*.txt"]
    assert updated.status == "paused"

    # Update source scan id
    scanned = cat.update_source_scan("mac-brain", "scan-2026-11-06-001")
    assert scanned.last_complete_scan_id == "scan-2026-11-06-001"

    # List sources with and without filter
    cat.add_source("nas-archive", "NAS Archive", mode="selected_files", status="active")
    assert len(cat.list_sources()) == 2
    assert len(cat.list_sources(status="active")) == 1
    assert cat.list_sources(status="active")[0].id == "nas-archive"
    assert len(cat.list_sources(status="paused")) == 1

    # Delete source
    assert cat.delete_source("nas-archive") is True
    assert cat.delete_source("nas-archive") is False
    assert len(cat.list_sources()) == 1


# -----------------------------------------------------------------------------
# Documents & Reference Counts Tests (§3, §5)
# -----------------------------------------------------------------------------


def test_document_lifecycle_and_reference_counts(memory_catalog: Catalog):
    cat = memory_catalog
    cat.add_source("src1", "Source 1")

    # Upsert document
    doc = cat.upsert_document(
        canonical_doc_id="doc_canon_100",
        derived_hash="sha256_derived_100",
        extracted_artifact_path="text/doc_canon_100.md",
        lightrag_doc_id="lr_doc_100",
        lightrag_status="PROCESSED",
        parser_version="docling-v1.0",
    )
    assert doc.canonical_doc_id == "doc_canon_100"
    assert doc.lightrag_status == "PROCESSED"
    assert doc.reference_count == 0

    # Lookups by lightrag_doc_id and derived_hash
    assert cat.get_document_by_lightrag_id("lr_doc_100") is not None
    assert cat.get_document_by_derived_hash("sha256_derived_100") is not None
    assert cat.get_document_by_lightrag_id("non-existent") is None

    # Reference count reconstruction:
    # Add file 1 referencing doc
    cat.record_file(
        source_id="src1",
        relative_path="notes/project.md",
        binary_sha256="bin_sha_100",
        size=1024,
        mtime_ns=1700000000000,
        canonical_doc_id="doc_canon_100",
        active=True,
    )
    doc = cat.get_document("doc_canon_100")
    assert doc is not None
    assert doc.reference_count == 1
    assert cat.get_document_reference_count("doc_canon_100") == 1

    # Add duplicate file 2 (alias) referencing same canonical doc
    cat.record_file(
        source_id="src1",
        relative_path="backup/project_copy.md",
        binary_sha256="bin_sha_100",
        size=1024,
        mtime_ns=1700000000000,
        canonical_doc_id="doc_canon_100",
        active=True,
    )
    assert cat.get_document_reference_count("doc_canon_100") == 2

    # Attempting to delete when referenced should fail
    deleted = cat.delete_document_if_unreferenced("doc_canon_100")
    assert deleted is False
    assert cat.get_document("doc_canon_100") is not None

    # Deactivate file 1
    cat.set_file_active("src1", "notes/project.md", False)
    assert cat.get_document_reference_count("doc_canon_100", active_only=True) == 1
    assert cat.get_document_reference_count("doc_canon_100", active_only=False) == 2

    # Deactivate file 2
    cat.set_file_active("src1", "backup/project_copy.md", False)
    assert cat.get_document_reference_count("doc_canon_100", active_only=True) == 0

    # Now unreferenced document can be safely deleted (§5)
    deleted = cat.delete_document_if_unreferenced("doc_canon_100")
    assert deleted is True
    assert cat.get_document("doc_canon_100") is None


def test_update_document_status(memory_catalog: Catalog):
    cat = memory_catalog
    cat.upsert_document("doc_status_test", lightrag_status="PENDING")

    updated = cat.update_document_status("doc_status_test", "PROCESSED", lightrag_doc_id="lr_id_999")
    assert updated.lightrag_status == "PROCESSED"
    assert updated.lightrag_doc_id == "lr_id_999"

    with pytest.raises(KeyError):
        cat.update_document_status("non_existent_doc", "FAILED")


# -----------------------------------------------------------------------------
# Files & Occurrences Tests (§3, §5)
# -----------------------------------------------------------------------------


def test_file_lifecycle_and_unique_constraint(memory_catalog: Catalog):
    cat = memory_catalog
    cat.add_source("src1", "Source 1")

    # Record file
    f = cat.record_file(
        source_id="src1",
        relative_path="research/paper.pdf",
        binary_sha256="sha_abc_1",
        size=5000,
        mtime_ns=123456789,
        canonical_doc_id=None,
        active=True,
        last_seen_scan_id="scan-1",
    )
    assert f.source_id == "src1"
    assert f.relative_path == "research/paper.pdf"
    assert f.active is True
    initial_id = f.id

    # Foreign key enforcement: non-existent canonical doc raises IntegrityError
    with pytest.raises(ValueError):
        cat.record_file(
            source_id="src1",
            relative_path="other.pdf",
            binary_sha256="sha_abc",
            size=100,
            mtime_ns=100,
            canonical_doc_id="non_existent_doc_id",
        )

    # Upsert canonical document first to satisfy foreign key (§3)
    cat.upsert_document("doc_new_canon", lightrag_status="PROCESSED")

    # Upsert with new metadata (modified file) keeps same id but updates fields
    f_updated = cat.record_file(
        source_id="src1",
        relative_path="research/paper.pdf",
        binary_sha256="sha_abc_2_changed",
        size=5500,
        mtime_ns=987654321,
        canonical_doc_id="doc_new_canon",
        active=True,
        last_seen_scan_id="scan-2",
    )
    assert f_updated.id == initial_id
    assert f_updated.binary_sha256 == "sha_abc_2_changed"
    assert f_updated.size == 5500
    assert f_updated.canonical_doc_id == "doc_new_canon"
    assert f_updated.last_seen_scan_id == "scan-2"

    # List files
    all_files = cat.list_files("src1")
    assert len(all_files) == 1

    # Find by hash
    matches = cat.find_files_by_hash("sha_abc_2_changed")
    assert len(matches) == 1
    assert matches[0].relative_path == "research/paper.pdf"

    # Hard delete
    assert cat.delete_file_record("src1", "research/paper.pdf") is True
    assert cat.get_file("src1", "research/paper.pdf") is None


def test_file_rename_preserves_mapping(memory_catalog: Catalog):
    """Core test §11 #3: Rename without modifying hash preserves canonical doc."""
    cat = memory_catalog
    cat.add_source("src1", "Source 1")
    cat.upsert_document("doc_stable_id", lightrag_doc_id="lr_123", lightrag_status="PROCESSED")

    cat.record_file(
        source_id="src1",
        relative_path="old_folder/my_doc.md",
        binary_sha256="stable_sha256",
        size=2048,
        mtime_ns=1000,
        canonical_doc_id="doc_stable_id",
        active=True,
    )

    # Rename file
    renamed = cat.rename_file("src1", "old_folder/my_doc.md", "new_folder/my_doc.md")
    assert renamed.relative_path == "new_folder/my_doc.md"
    assert renamed.canonical_doc_id == "doc_stable_id"
    assert renamed.binary_sha256 == "stable_sha256"

    # Old path must not exist
    assert cat.get_file("src1", "old_folder/my_doc.md") is None
    # New path must exist
    assert cat.get_file("src1", "new_folder/my_doc.md") is not None

    # Non-existent source file raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        cat.rename_file("src1", "non_existent.md", "target.md")

    # Rename into an existing active file raises FileExistsError
    cat.record_file("src1", "existing_active.md", "sha_other", 100, 100)
    with pytest.raises(FileExistsError):
        cat.rename_file("src1", "new_folder/my_doc.md", "existing_active.md")


# -----------------------------------------------------------------------------
# Collision & Evidence Resolution Tests (§3, §11 #5)
# -----------------------------------------------------------------------------


def test_collision_same_filename_different_folders_and_evidence(memory_catalog: Catalog):
    """Core release test §11 #5:

    Collision 'a/report.pdf' and 'b/report.pdf' with distinct and identical content
    without losing either reference.
    """
    cat = memory_catalog
    cat.add_source(
        source_id="src_mac",
        name="MacBook",
        origin_host="macbook",
        origin_folder="/Users/luca/Docs",
    )

    # Case 1: Identical content (same sha256 -> same canonical doc)
    doc_shared = cat.upsert_document(
        canonical_doc_id="doc_shared_report",
        lightrag_doc_id="lr_shared",
        lightrag_status="PROCESSED",
        extracted_artifact_path="text/shared_report.md",
    )

    cat.record_file(
        source_id="src_mac",
        relative_path="a/report.pdf",
        binary_sha256="sha_identical",
        size=4096,
        mtime_ns=1000,
        canonical_doc_id="doc_shared_report",
    )
    cat.record_file(
        source_id="src_mac",
        relative_path="b/report.pdf",
        binary_sha256="sha_identical",
        size=4096,
        mtime_ns=1000,
        canonical_doc_id="doc_shared_report",
    )

    # Both references exist distinct in files
    file_a = cat.get_file("src_mac", "a/report.pdf")
    file_b = cat.get_file("src_mac", "b/report.pdf")
    assert file_a is not None and file_b is not None
    assert file_a.id != file_b.id

    # Evidence resolution resolves both paths
    evidence = cat.resolve_evidence("doc_shared_report")
    assert len(evidence) == 2
    paths = {e["relative_path"] for e in evidence}
    assert paths == {"a/report.pdf", "b/report.pdf"}
    uris = {e["source_uri"] for e in evidence}
    assert uris == {
        "macbook:/Users/luca/Docs/a/report.pdf",
        "macbook:/Users/luca/Docs/b/report.pdf",
    }

    # Case 2: Distinct content with same basename in another folder
    cat.upsert_document("doc_distinct_report", lightrag_doc_id="lr_distinct", lightrag_status="PROCESSED")
    cat.record_file(
        source_id="src_mac",
        relative_path="c/report.pdf",
        binary_sha256="sha_different_content",
        size=8192,
        mtime_ns=2000,
        canonical_doc_id="doc_distinct_report",
    )

    ev_distinct = cat.resolve_evidence("doc_distinct_report")
    assert len(ev_distinct) == 1
    assert ev_distinct[0]["relative_path"] == "c/report.pdf"
    assert ev_distinct[0]["binary_sha256"] == "sha_different_content"


# -----------------------------------------------------------------------------
# Exact Duplicate Clusters Tests (§8 A1)
# -----------------------------------------------------------------------------


def test_exact_duplicate_clusters_a1(memory_catalog: Catalog):
    cat = memory_catalog
    cat.add_source("src1", "Source 1", origin_host="host1", origin_folder="/dir1")
    cat.add_source("src2", "Source 2", origin_host="host2", origin_folder="/dir2")

    # Cluster of 3 identical files across 2 sources
    sha_dup = "sha_dup_cluster_123"
    cat.record_file("src1", "folder1/doc1.pdf", sha_dup, 100, 100)
    cat.record_file("src1", "folder2/doc1_copy.pdf", sha_dup, 100, 100)
    cat.record_file("src2", "archive/doc1_nas.pdf", sha_dup, 100, 100)

    # Unique file (must NOT be in clusters)
    cat.record_file("src1", "unique.pdf", "sha_unique_xyz", 200, 200)

    # Inactive duplicate pair
    sha_inactive = "sha_inactive_pair"
    cat.record_file("src1", "old1.txt", sha_inactive, 50, 50, active=False)
    cat.record_file("src1", "old2.txt", sha_inactive, 50, 50, active=False)

    # active_only=True: only the active cluster is returned
    clusters = cat.find_exact_duplicate_clusters(active_only=True)
    assert len(clusters) == 1
    assert clusters[0]["binary_sha256"] == sha_dup
    assert clusters[0]["count"] == 3
    assert len(clusters[0]["occurrences"]) == 3
    occ_paths = {o["relative_path"] for o in clusters[0]["occurrences"]}
    assert occ_paths == {"folder1/doc1.pdf", "folder2/doc1_copy.pdf", "archive/doc1_nas.pdf"}

    # active_only=False: both clusters returned
    all_clusters = cat.find_exact_duplicate_clusters(active_only=False)
    assert len(all_clusters) == 2


# -----------------------------------------------------------------------------
# Jobs Lifecycle & Idempotency Tests (§3, §4)
# -----------------------------------------------------------------------------


def test_job_lifecycle_and_idempotency(memory_catalog: Catalog):
    cat = memory_catalog
    cat.add_source("src1", "Source 1")

    # Create job with idempotency key
    job = cat.create_job(
        job_id="job-uuid-001",
        kb_id="kb-default",
        source_id="src1",
        stage="PREPARING",
        idempotency_key="idem-key-abc-123",
        scan_id="scan-1",
        counters={"added": 1, "unchanged": 5},
        warnings=["Non-fatal encoding warning"],
        resume_marker="marker_phase_1",
    )
    assert job.job_id == "job-uuid-001"
    assert job.stage == "PREPARING"
    assert job.idempotency_key == "idem-key-abc-123"
    assert job.counters["added"] == 1
    assert job.counters["unchanged"] == 5
    assert job.counters["deleted"] == 0
    assert job.warnings == ["Non-fatal encoding warning"]
    assert job.resume_marker == "marker_phase_1"

    # Lookup by idempotency key
    found = cat.get_job_by_idempotency_key("idem-key-abc-123")
    assert found is not None
    assert found.job_id == "job-uuid-001"

    # Duplicate idempotency key must raise IntegrityError (§4 idempotency)
    with pytest.raises(ValueError):
        cat.create_job(
            job_id="job-uuid-002",
            kb_id="kb-default",
            idempotency_key="idem-key-abc-123",
            stage="PREPARING",
        )

    # Invalid stage validation
    with pytest.raises(ValueError, match="Invalid job stage"):
        cat.create_job(job_id="job-bad", kb_id="kb-default", stage="INVALID_STAGE")

    # Transition through stages (§4)
    stages = ["TRANSFERRING", "VALIDATING", "PROCESSING", "COMPLETED"]
    for s in stages:
        updated = cat.update_job(
            "job-uuid-001",
            stage=s,
            counters={"added": 3, "modified": 1},
        )
        assert updated.stage == s
        assert updated.counters["added"] == 3
        assert updated.counters["modified"] == 1

    # Update error summary and review
    review_job = cat.update_job(
        "job-uuid-001",
        stage="NEEDS_REVIEW",
        error_summary="Deletes exceeded 20% guard threshold",
    )
    assert review_job.stage == "NEEDS_REVIEW"
    assert review_job.error_summary == "Deletes exceeded 20% guard threshold"

    # List jobs
    assert len(cat.list_jobs(kb_id="kb-default")) == 1
    assert len(cat.list_jobs(stage="NEEDS_REVIEW")) == 1
    assert len(cat.list_jobs(stage="COMPLETED")) == 0


# -----------------------------------------------------------------------------
# find_documents Pagination & Filtering Tests (§6)
# -----------------------------------------------------------------------------


def test_find_documents_pagination(memory_catalog: Catalog):
    cat = memory_catalog
    cat.add_source("src1", "Source 1")
    cat.add_source("src2", "Source 2")

    # Create 5 documents
    for i in range(1, 6):
        canon_id = f"doc_{i:03d}"
        status = "PROCESSED" if i <= 3 else "PENDING"
        cat.upsert_document(canon_id, lightrag_status=status)
        source = "src1" if i <= 4 else "src2"
        prefix = "notes" if i % 2 == 1 else "code"
        cat.record_file(source, f"{prefix}/file_{i}.md", f"sha_{i}", 100, 100, canonical_doc_id=canon_id)

    # Page 1 with page_size = 2
    res_p1 = cat.find_documents(page=1, page_size=2)
    assert res_p1["total"] == 5
    assert len(res_p1["items"]) == 2
    assert res_p1["page"] == 1
    assert res_p1["page_size"] == 2
    assert res_p1["total_pages"] == 3

    # Filter by status
    res_status = cat.find_documents(status="PROCESSED")
    assert res_status["total"] == 3

    # Filter by source_id
    res_src = cat.find_documents(source_id="src2")
    assert res_src["total"] == 1

    # Filter by path prefix
    res_prefix = cat.find_documents(path_prefix="notes/")
    assert res_prefix["total"] == 3


# -----------------------------------------------------------------------------
# Scan Reconciliation Tests (§4, §5)
# -----------------------------------------------------------------------------


def test_scan_reconciliation_unseen_files(memory_catalog: Catalog):
    cat = memory_catalog
    cat.add_source("src1", "Source 1")

    # Scan 1: Two files discovered
    cat.record_file("src1", "keep.txt", "sha1", 10, 10, last_seen_scan_id="scan-1")
    cat.record_file("src1", "delete_me.txt", "sha2", 20, 20, last_seen_scan_id="scan-1")

    # Scan 2: Only keep.txt is seen
    cat.record_file("src1", "keep.txt", "sha1", 10, 10, last_seen_scan_id="scan-2")

    # Mark unseen files inactive
    deactivated = cat.mark_files_inactive_not_seen("src1", "scan-2")
    assert len(deactivated) == 1
    assert deactivated[0].relative_path == "delete_me.txt"

    # Verify status in database
    assert cat.get_file("src1", "keep.txt").active is True
    assert cat.get_file("src1", "delete_me.txt").active is False


# -----------------------------------------------------------------------------
# Transaction & Atomicity Tests
# -----------------------------------------------------------------------------


def test_transaction_commit_and_rollback(disk_catalog: Catalog):
    cat = disk_catalog

    # Commit succeeds
    with cat.transaction():
        cat.add_source("src_tx", "Transaction Source")
    assert cat.get_source("src_tx") is not None

    # Rollback on exception
    with pytest.raises(RuntimeError):
        with cat.transaction():
            cat.add_source("src_fail", "Fail Source")
            raise RuntimeError("Forced abort")

    assert cat.get_source("src_fail") is None
