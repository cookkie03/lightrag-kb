"""Verify server admission rejects dangerous or incomplete sync requests."""

import hashlib

import pytest

from sync.server import validate_manifest
from storage.catalog import Catalog


def test_prepare_returns_source_snapshot_and_creates_job():
    from sync.server import prepare_sync

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/notes")
        catalog.record_file("source", "note.txt", "a" * 64, 10, 20)
        prepared = prepare_sync(catalog, "kb", "source", "full", "job")
        assert prepared["job_id"] == "job"
        assert prepared["scan_generation"] is None
        assert prepared["files"] == [{"path": "note.txt", "sha256": "a" * 64, "size": 10, "mtime_ns": 20}]
        assert catalog.get_job("job")["stage"] == "PREPARING"
        assert catalog.get_job("job")["source_id"] == "source"


def test_prepare_refuses_second_active_job():
    from sync.server import prepare_sync

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/notes")
        prepare_sync(catalog, "kb", "source", "full", "first")
        with pytest.raises(ValueError, match="active"):
            prepare_sync(catalog, "kb", "source", "partial", "second")
        assert catalog.get_job("second") is None


@pytest.mark.parametrize("job_id", ["../escape", "/absolute", ".", "", "nested/job"])
def test_prepare_refuses_unsafe_job_identifiers(job_id):
    from sync.server import prepare_sync

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/notes")
        with pytest.raises(ValueError):
            prepare_sync(catalog, "kb", "source", "full", job_id)
        assert catalog.list_jobs() == []


def test_prepare_refuses_unknown_source():
    from sync.server import prepare_sync

    with Catalog(":memory:") as catalog:
        with pytest.raises(ValueError):
            prepare_sync(catalog, "kb", "missing", "full", "job")
        assert catalog.get_job("job") is None


def test_admission_rejects_changed_source_root(tmp_path):
    from sync.server import admit_manifest

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/original")
        catalog.create_job("job", "kb", source_id="source")
        manifest = {"job_id": "job", "kb_id": "kb", "source_id": "source", "origin_host": "mac", "origin_folder": "/changed", "scan_generation": None, "mode": "full", "scan_complete": True, "scan_errors": [], "actions": [], "files": []}
        with pytest.raises(ValueError, match="origin"):
            admit_manifest(catalog, "job", manifest, tmp_path)


def test_admission_returns_worker_manifest_for_unchanged_snapshot(tmp_path):
    from sync.server import admit_manifest

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/original")
        catalog.create_job("job", "kb", source_id="source")
        manifest = {"job_id": "job", "kb_id": "kb", "source_id": "source", "origin_host": "mac", "origin_folder": "/original", "scan_generation": None, "mode": "full", "scan_complete": True, "scan_errors": [], "actions": [], "files": []}
        admitted = admit_manifest(catalog, "job", manifest, tmp_path)
        assert admitted["mode"] == "full_folder"
        assert admitted["scan_errors"] == 0
        assert admitted["actions"] == []
        catalog.update_source_scan("source", "new-generation")
        with pytest.raises(ValueError, match="generation"):
            admit_manifest(catalog, "job", manifest, tmp_path)


def test_admission_rejects_transferred_file_with_wrong_size(tmp_path):
    from sync.server import admit_manifest

    (tmp_path / "file.txt").write_text("payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/original")
        catalog.create_job("job", "kb", source_id="source")
        manifest = {"job_id": "job", "kb_id": "kb", "source_id": "source", "origin_host": "mac", "origin_folder": "/original", "scan_generation": None, "mode": "full", "scan_complete": True, "scan_errors": [], "actions": [{"action": "add", "path": "file.txt", "sha256": digest}], "files": [{"path": "file.txt", "sha256": digest, "size": 999, "mtime_ns": 1}]}
        with pytest.raises(ValueError, match="size"):
            admit_manifest(catalog, "job", manifest, tmp_path)


def test_admission_rejects_delete_not_supported_by_snapshot(tmp_path):
    from sync.server import admit_manifest

    with Catalog(":memory:") as catalog:
        catalog.add_source("source", "Source", origin_host="mac", origin_folder="/original")
        catalog.record_file("source", "keep.txt", "a" * 64, 1, 1)
        catalog.create_job("job", "kb", source_id="source")
        manifest = {"job_id": "job", "kb_id": "kb", "source_id": "source", "origin_host": "mac", "origin_folder": "/original", "scan_generation": None, "mode": "full", "scan_complete": True, "scan_errors": [], "actions": [{"action": "delete", "path": "keep.txt"}], "files": [{"path": "keep.txt", "sha256": "a" * 64, "size": 1, "mtime_ns": 1}]}
        with pytest.raises(ValueError, match="delta"):
            admit_manifest(catalog, "job", manifest, tmp_path)
        assert catalog.get_file("source", "keep.txt")["active"]


def test_partial_sync_cannot_request_deletion(tmp_path):
    manifest = {"mode": "partial", "scan_complete": True, "scan_errors": [], "actions": [{"action": "delete", "path": "file.txt"}]}
    with pytest.raises(ValueError):
        validate_manifest(manifest, tmp_path)


def test_upload_hash_must_match_staged_bytes(tmp_path):
    (tmp_path / "file.txt").write_text("payload")
    manifest = {"mode": "full", "scan_complete": True, "scan_errors": [], "actions": [{"action": "add", "path": "file.txt", "sha256": "0" * 64}]}
    with pytest.raises(ValueError):
        validate_manifest(manifest, tmp_path)
    manifest["actions"][0]["sha256"] = hashlib.sha256(b"payload").hexdigest()
    validate_manifest(manifest, tmp_path)


def test_manifest_rejects_escaping_path_with_trailing_newline(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("payload")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "link\n").symlink_to(outside)
    from pipeline.parsers import validate_safe_relative_path
    with pytest.raises(ValueError):
        validate_safe_relative_path("link\n", incoming, allow_symlinks=False)


def test_manifest_rejects_symlinked_incoming_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.txt").write_text("payload")
    incoming = tmp_path / "incoming"
    incoming.symlink_to(real, target_is_directory=True)
    manifest = {"mode": "full", "scan_complete": True, "scan_errors": [], "actions": [{"action": "add", "path": "file.txt", "sha256": hashlib.sha256(b"payload").hexdigest()}]}
    with pytest.raises(ValueError):
        validate_manifest(manifest, incoming)


def test_manifest_rejects_symlinked_parent_inside_staging(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.txt").write_text("payload")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    manifest = {"mode": "full", "scan_complete": True, "scan_errors": [], "actions": [{"action": "add", "path": "link/file.txt", "sha256": hashlib.sha256(b"payload").hexdigest()}]}
    with pytest.raises(ValueError):
        validate_manifest(manifest, tmp_path)


def test_manifest_cannot_escape_staging(tmp_path):
    manifest = {"mode": "full", "scan_complete": True, "scan_errors": [], "actions": [{"action": "add", "path": "../outside", "sha256": "0" * 64}]}
    with pytest.raises(ValueError):
        validate_manifest(manifest, tmp_path)
