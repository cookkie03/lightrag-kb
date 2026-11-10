"""Validate incoming synchronization manifests and staged file integrity."""

import hashlib
from pathlib import Path

from pipeline.parsers import validate_safe_relative_path
from sync.client import diff


# Serialize job creation with the source snapshot and active-job check.
def prepare_sync(catalog, kb_id, source_id, mode, job_id):
    """Create one source-bound job and return the authoritative comparison snapshot."""
    if mode not in {"full", "partial"} or not all(isinstance(value, str) and value and value not in {".", ".."} and Path(value).name == value and "\\" not in value for value in (kb_id, source_id, job_id)):
        raise ValueError("invalid sync identifiers or mode")
    with catalog.transaction():
        source = catalog.get_source(source_id)
        if not source or source.status != "active" or not source.origin_host or not source.origin_folder:
            raise ValueError("unknown or unconfigured source")
        if any(job.stage in {"PREPARING", "TRANSFERRING", "VALIDATING", "PROCESSING"} for job in catalog.list_jobs(kb_id=kb_id, limit=None)):
            raise ValueError("another job is active")
        files = catalog.list_files(source_id=source_id)
        catalog.create_job(job_id, kb_id, source_id=source_id)
        return {"job_id": job_id, "kb": kb_id, "source": source_id, "scan_generation": source.last_complete_scan_id, "origin_host": source.origin_host, "origin_folder": source.origin_folder, "deletion_policy": source.deletion_policy, "files": [{"path": f.relative_path, "sha256": f.binary_sha256, "size": f.size, "mtime_ns": f.mtime_ns} for f in files]}


def validate_manifest(manifest, incoming):
    """Reject incomplete delete requests, unsafe paths and mismatched uploads."""
    if not isinstance(manifest, dict) or manifest.get("mode") not in {"full", "partial"}:
        raise ValueError("invalid sync mode")
    if not isinstance(manifest.get("scan_complete"), bool) or not isinstance(manifest.get("scan_errors"), list):
        raise ValueError("invalid scan status")
    if not isinstance(manifest.get("actions"), list):
        raise ValueError("invalid actions")
    incoming = Path(incoming)
    if incoming.is_symlink():
        raise ValueError("unsafe incoming directory")
    # Each destination occurs once; deletes require a trustworthy full scan,
    # and uploaded content must match its declared checksum.
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


def admit_manifest(catalog, job_id, manifest, incoming):
    """Bind a validated full snapshot and its delta to a registered source and job."""
    from pipeline.ingestion_worker import StagedAction, StagedJobManifest

    job = catalog.get_job(job_id)
    if not isinstance(manifest, dict) or not job or (manifest.get("job_id"), manifest.get("kb_id"), manifest.get("source_id")) != (job.job_id, job.kb_id, job.source_id):
        raise ValueError("job identity mismatch")
    source = catalog.get_source(job.source_id)
    if not source or source.status != "active":
        raise ValueError("unknown or inactive source")
    if not source.origin_host or not source.origin_folder or (manifest.get("origin_host"), manifest.get("origin_folder")) != (source.origin_host, source.origin_folder):
        raise ValueError("source origin mismatch")
    if "scan_generation" not in manifest or manifest["scan_generation"] != source.last_complete_scan_id:
        raise ValueError("stale scan generation")
    validate_manifest(manifest, incoming)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("missing snapshot")
    # Validate every snapshot entry before using paths as keys in the delta.
    seen = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("invalid snapshot path")
        validate_safe_relative_path(item["path"])
        digest = item.get("sha256")
        if item["path"] in seen or not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid snapshot entry")
        if type(item.get("size")) is not int or item["size"] < 0 or type(item.get("mtime_ns")) is not int or item["mtime_ns"] < 0:
            raise ValueError("invalid snapshot metadata")
        seen.add(item["path"])
    # Recompute the delta from the catalog rather than trusting client actions.
    previous = [{"path": f.relative_path, "sha256": f.binary_sha256} for f in catalog.list_files(source_id=source.id)]
    current = {item["path"]: item for item in files}
    plan = diff(previous, files, manifest["mode"] == "full" and manifest["scan_complete"] and not manifest["scan_errors"])
    expected = {(kind, path, None) for kind in ("add", "modify", "delete") for path in plan[kind]}
    expected.update(("rename", item["new"], item["old"]) for item in plan["rename"])
    actual = {(a["action"], a["path"], a.get("old_path")) for a in manifest["actions"]}
    if actual != expected:
        raise ValueError("snapshot delta mismatch")
    for action in manifest["actions"]:
        if action["action"] in {"add", "modify"}:
            item = current[action["path"]]
            if action["sha256"] != item["sha256"]:
                raise ValueError("snapshot checksum mismatch")
            if (Path(incoming) / action["path"]).stat().st_size != item["size"]:
                raise ValueError("snapshot size mismatch")
    return StagedJobManifest(job_id, job.kb_id, source.id, mode="full_folder" if manifest["mode"] == "full" else "selected_files", scan_complete=manifest["scan_complete"], scan_errors=len(manifest["scan_errors"]), actions=[StagedAction(a["action"], a["path"], old_relative_path=a.get("old_path"), binary_sha256=current.get(a["path"], {}).get("sha256", ""), size=current.get(a["path"], {}).get("size", 0), mtime_ns=current.get(a["path"], {}).get("mtime_ns", 0)) for a in manifest["actions"]])
