"""Validate incoming synchronization manifests and staged file integrity."""

import hashlib
from pathlib import Path

from pipeline.parsers import validate_safe_relative_path


def validate_manifest(manifest, incoming):
    """Reject incomplete delete requests, unsafe paths and mismatched uploads."""
    if not isinstance(manifest, dict) or manifest.get("mode") not in {"full", "partial"}:
        raise ValueError("invalid sync mode")
    if not isinstance(manifest.get("scan_complete"), bool) or not isinstance(manifest.get("scan_errors"), list):
        raise ValueError("invalid scan status")
    if not isinstance(manifest.get("actions"), list):
        raise ValueError("invalid actions")
    incoming = Path(incoming)
    seen = set()
    for action in manifest["actions"]:
        if not isinstance(action, dict) or action.get("action") not in {"add", "modify", "rename", "delete"}:
            raise ValueError("invalid action")
        name = action.get("path")
        if not isinstance(name, str):
            raise ValueError("invalid path")
        validate_safe_relative_path(name, incoming, allow_symlinks=False)
        if name in seen:
            raise ValueError("duplicate action path")
        seen.add(name)
        if action["action"] == "delete" and (manifest["mode"] != "full" or not manifest["scan_complete"] or manifest["scan_errors"]):
            raise ValueError("deletion requires a complete full scan")
        if action["action"] == "rename":
            old = action.get("old_path")
            if not isinstance(old, str):
                raise ValueError("rename requires old path")
            validate_safe_relative_path(old)
        if action["action"] in {"add", "modify"}:
            expected = action.get("sha256")
            if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                raise ValueError("invalid checksum")
            path = incoming / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("missing or unsafe staged file")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError("staged checksum mismatch")
