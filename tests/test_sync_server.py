"""Verify server admission rejects dangerous or incomplete sync requests."""

import hashlib

import pytest

from sync.server import validate_manifest


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


def test_manifest_cannot_escape_staging(tmp_path):
    manifest = {"mode": "full", "scan_complete": True, "scan_errors": [], "actions": [{"action": "add", "path": "../outside", "sha256": "0" * 64}]}
    with pytest.raises(ValueError):
        validate_manifest(manifest, tmp_path)
