"""Manage one KB's sources, file occurrences, documents and jobs as JSON."""

from __future__ import annotations

import copy
import math
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from storage.json_store import JsonStore

DEFAULT_KB_ROOT = Path(__file__).resolve().parent.parent / "kb"
VALID_SOURCE_MODES = {"full_folder", "selected_files"}
VALID_JOB_STAGES = {"PREPARING", "TRANSFERRING", "VALIDATING", "PROCESSING", "COMPLETED", "FAILED_RETRYABLE", "FAILED_FINAL", "NEEDS_REVIEW"}
DEFAULT_JOB_COUNTERS = dict.fromkeys(("added", "modified", "renamed", "deleted", "unchanged", "failed", "warnings"), 0)


def utc_now_iso():
    """Return a timezone-aware timestamp for catalog records."""
    return datetime.now(timezone.utc).isoformat()


def generate_canonical_doc_id(binary_sha256, revision=1):
    """Derive a synthetic source identifier without exposing the original filename."""
    prefix = binary_sha256.strip().lower()[:16] or "empty"
    return f"doc_{prefix}_r{revision}" if revision > 1 else f"doc_{prefix}"


@dataclass
class SourceRecord:
    """Describe a stable source and its latest complete scan."""
    id: str
    name: str
    origin_host: str = ""
    origin_folder: str = ""
    mode: str = "full_folder"
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    deletion_policy: str = "guarded"
    last_complete_scan_id: str | None = None
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self):
        """Return an independent JSON-compatible representation."""
        return asdict(self)


@dataclass
class DocumentRecord:
    """Describe a canonical indexed document and its active alias count."""
    canonical_doc_id: str
    derived_hash: str | None = None
    extracted_artifact_path: str | None = None
    lightrag_doc_id: str | None = None
    lightrag_status: str = "PENDING"
    parser_version: str | None = None
    created_at: str = ""
    updated_at: str = ""
    reference_count: int = 0

    def to_dict(self):
        """Return an independent JSON-compatible representation."""
        return asdict(self)


@dataclass
class FileRecord:
    """Describe one source-relative occurrence of a canonical document."""
    id: int | None = None
    source_id: str = ""
    relative_path: str = ""
    binary_sha256: str = ""
    size: int = 0
    mtime_ns: int = 0
    canonical_doc_id: str | None = None
    active: bool = True
    last_seen_scan_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self):
        """Return an independent JSON-compatible representation."""
        return asdict(self)


@dataclass
class JobRecord:
    """Describe the durable progress of an idempotent ingestion job."""
    job_id: str
    kb_id: str
    stage: str = "PREPARING"
    idempotency_key: str | None = None
    source_id: str | None = None
    scan_id: str | None = None
    counters: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_JOB_COUNTERS))
    warnings: list[str] = field(default_factory=list)
    error_summary: str | None = None
    resume_marker: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self):
        """Return an independent JSON-compatible representation."""
        return asdict(self)


class Catalog:
    """Provide atomic KB catalog operations over versioned JSON snapshots."""

    def __init__(self, path):
        """Open a JSON catalog without ever interpreting a legacy SQLite file as JSON."""
        self._temporary = tempfile.TemporaryDirectory() if str(path) == ":memory:" else None
        self._path = Path(self._temporary.name) / "catalog.json" if self._temporary else Path(path)
        if self._path.suffix == ".sqlite":
            raise ValueError("SQLite catalogs are not supported; migration is required")
        self._store = JsonStore(self._path)
        self._data = None

    @classmethod
    def open_for_kb(cls, kb_id, kb_root=None):
        """Open the KB snapshot, refusing to bypass unmigrated SQLite data."""
        if not kb_id or kb_id in {".", ".."} or Path(kb_id).name != kb_id or "\\" in kb_id:
            raise ValueError("invalid KB identifier")
        directory = Path(kb_root or DEFAULT_KB_ROOT) / kb_id
        if (directory / "catalog.sqlite").exists() and not (directory / "catalog.json").exists():
            raise RuntimeError("SQLite catalog requires an explicit non-destructive migration")
        return cls(directory / "catalog.json")

    @property
    def path(self):
        """Expose the snapshot path, or None for a temporary catalog."""
        return None if self._temporary else self._path

    @contextmanager
    def transaction(self):
        """Hold one exclusive transaction across multiple catalog operations."""
        if self._data is not None:
            yield self
            return
        with self._store.transaction() as data:
            self._data = data
            try:
                yield self
            finally:
                self._data = None

    @contextmanager
    def _snapshot(self):
        """Reuse the active transaction or open an independent atomic operation."""
        if self._data is not None:
            yield self._data
        else:
            with self._store.transaction() as data:
                yield data

    def close(self):
        """Release temporary resources without touching persistent catalogs."""
        if self._temporary:
            self._temporary.cleanup()
            self._temporary = None

    def __enter__(self):
        """Support scoped catalog use."""
        return self

    def __exit__(self, *args):
        """Release temporary resources at the end of a scope."""
        self.close()

    def add_source(self, source_id, name, origin_host="", origin_folder="", mode="full_folder", include_patterns=None, exclude_patterns=None, deletion_policy="guarded", status="active"):
        """Register a uniquely identified source in this KB."""
        if mode not in VALID_SOURCE_MODES:
            raise ValueError(f"Invalid source mode: {mode}")
        now = utc_now_iso()
        source = SourceRecord(source_id, name, origin_host, origin_folder, mode, include_patterns or [], exclude_patterns or [], deletion_policy, status=status, created_at=now, updated_at=now)
        with self._snapshot() as data:
            if source_id in data["sources"]:
                raise ValueError("source already exists")
            data["sources"][source_id] = source.to_dict()
        return source

    def get_source(self, source_id):
        """Return source metadata, or None if the source is unknown."""
        with self._snapshot() as data:
            value = data["sources"].get(source_id)
            return SourceRecord(**copy.deepcopy(value)) if value else None

    def list_sources(self, status=None):
        """List sources in stable identifier order with an optional status filter."""
        with self._snapshot() as data:
            return [SourceRecord(**copy.deepcopy(v)) for _, v in sorted(data["sources"].items()) if status is None or v["status"] == status]

    def update_source(self, source_id, **changes):
        """Update source metadata without changing its stable identifier."""
        allowed = {"name", "origin_host", "origin_folder", "mode", "include_patterns", "exclude_patterns", "deletion_policy", "status", "last_complete_scan_id"}
        if set(changes) - allowed:
            raise TypeError("unknown source fields")
        if changes.get("mode") is not None and changes["mode"] not in VALID_SOURCE_MODES:
            raise ValueError("invalid source mode")
        with self._snapshot() as data:
            source = data["sources"].get(source_id)
            if source is None:
                raise KeyError(source_id)
            source.update({k: copy.deepcopy(v) for k, v in changes.items() if v is not None})
            source["updated_at"] = utc_now_iso()
            return SourceRecord(**copy.deepcopy(source))

    def update_source_scan(self, source_id, scan_id):
        """Record the last successfully completed source scan."""
        return self.update_source(source_id, last_complete_scan_id=scan_id)

    def delete_source(self, source_id):
        """Remove a source and its file occurrences, leaving canonical documents intact."""
        with self._snapshot() as data:
            if source_id not in data["sources"]:
                return False
            del data["sources"][source_id]
            data["files"] = {k: v for k, v in data["files"].items() if v["source_id"] != source_id}
            return True

    def _document(self, data, value):
        """Reconstruct the active reference count from file occurrences."""
        if value is None:
            return None
        value = copy.deepcopy(value)
        value["reference_count"] = sum(f["active"] and f.get("canonical_doc_id") == value["canonical_doc_id"] for f in data["files"].values())
        return DocumentRecord(**value)

    def upsert_document(self, canonical_doc_id, derived_hash=None, extracted_artifact_path=None, lightrag_doc_id=None, lightrag_status="PENDING", parser_version=None):
        """Create or update a canonical document without losing existing metadata."""
        with self._snapshot() as data:
            now = utc_now_iso()
            doc = data["documents"].setdefault(canonical_doc_id, DocumentRecord(canonical_doc_id, created_at=now).to_dict())
            for key, value in (("derived_hash", derived_hash), ("extracted_artifact_path", extracted_artifact_path), ("lightrag_doc_id", lightrag_doc_id), ("parser_version", parser_version)):
                if value is not None:
                    doc[key] = value
            doc.update(lightrag_status=lightrag_status, updated_at=now)
            return self._document(data, doc)

    def get_document(self, canonical_doc_id):
        """Retrieve a canonical document with its current active reference count."""
        with self._snapshot() as data:
            return self._document(data, data["documents"].get(canonical_doc_id))

    def _find_document(self, field, value):
        """Find a document by an indexed attribute, deterministically."""
        with self._snapshot() as data:
            return next((self._document(data, doc) for _, doc in sorted(data["documents"].items()) if doc.get(field) == value), None)

    def get_document_by_lightrag_id(self, lightrag_doc_id):
        """Resolve an upstream document identifier."""
        return self._find_document("lightrag_doc_id", lightrag_doc_id)

    def get_document_by_derived_hash(self, derived_hash):
        """Resolve a derived-content hash to its canonical document."""
        return self._find_document("derived_hash", derived_hash)

    def update_document_status(self, canonical_doc_id, lightrag_status, lightrag_doc_id=None):
        """Record the upstream state without dropping a prior identifier."""
        with self._snapshot() as data:
            doc = data["documents"].get(canonical_doc_id)
            if doc is None:
                raise KeyError(canonical_doc_id)
            doc.update(lightrag_status=lightrag_status, updated_at=utc_now_iso())
            if lightrag_doc_id is not None:
                doc["lightrag_doc_id"] = lightrag_doc_id
            return self._document(data, doc)

    def get_document_reference_count(self, canonical_doc_id, active_only=True):
        """Count file occurrences referencing a document, optionally including inactive ones."""
        with self._snapshot() as data:
            return sum(f.get("canonical_doc_id") == canonical_doc_id and (f["active"] or not active_only) for f in data["files"].values())

    def delete_document(self, canonical_doc_id):
        """Delete a document only if no occurrence references it."""
        with self._snapshot() as data:
            if any(f.get("canonical_doc_id") == canonical_doc_id for f in data["files"].values()):
                raise ValueError("document is referenced")
            return data["documents"].pop(canonical_doc_id, None) is not None

    def delete_document_if_unreferenced(self, canonical_doc_id):
        """Atomically delete a document if it has no active references."""
        with self._snapshot() as data:
            if any(f.get("canonical_doc_id") == canonical_doc_id and f["active"] for f in data["files"].values()):
                return False
            if canonical_doc_id not in data["documents"]:
                return False
            # No active references remain; clear inactive links before removing the document.
            for occurrence in data["files"].values():
                if occurrence.get("canonical_doc_id") == canonical_doc_id:
                    occurrence["canonical_doc_id"] = None
            del data["documents"][canonical_doc_id]
            return True

    @staticmethod
    def _file_key(source_id, relative_path):
        """Encode the composite key without ambiguity for arbitrary path characters."""
        import json
        return json.dumps([source_id, relative_path], ensure_ascii=False)

    def record_file(self, source_id, relative_path, binary_sha256, size, mtime_ns, canonical_doc_id=None, active=True, last_seen_scan_id=None):
        """Create or update an occurrence while preserving its identity and references."""
        with self._snapshot() as data:
            if source_id not in data["sources"] or (canonical_doc_id is not None and canonical_doc_id not in data["documents"]):
                raise ValueError("unknown source or canonical document")
            key = self._file_key(source_id, relative_path)
            now = utc_now_iso()
            if key not in data["files"]:
                next_id = data.get("next_file_id", 1)
                data["next_file_id"] = next_id + 1
                data["files"][key] = FileRecord(id=next_id, source_id=source_id, relative_path=relative_path, created_at=now).to_dict()
            item = data["files"][key]
            item.update(binary_sha256=binary_sha256, size=size, mtime_ns=mtime_ns, active=active, updated_at=now)
            if canonical_doc_id is not None:
                item["canonical_doc_id"] = canonical_doc_id
            if last_seen_scan_id is not None:
                item["last_seen_scan_id"] = last_seen_scan_id
            return FileRecord(**copy.deepcopy(item))

    def get_file(self, source_id, relative_path):
        """Retrieve one source-relative occurrence."""
        with self._snapshot() as data:
            item = data["files"].get(self._file_key(source_id, relative_path))
            return FileRecord(**copy.deepcopy(item)) if item else None

    def list_files(self, source_id=None, active_only=True, limit=None, offset=None):
        """List occurrences in stable source and path order."""
        with self._snapshot() as data:
            items = sorted((f for f in data["files"].values() if (source_id is None or f["source_id"] == source_id) and (not active_only or f["active"])), key=lambda f: (f["source_id"], f["relative_path"]))
            if limit is not None:
                items = items[(offset or 0):(offset or 0) + limit]
            return [FileRecord(**copy.deepcopy(f)) for f in items]

    def find_files_by_hash(self, binary_sha256, active_only=True):
        """Find all matching original file occurrences."""
        return [f for f in self.list_files(active_only=active_only) if f.binary_sha256 == binary_sha256]

    def rename_file(self, source_id, old_relative_path, new_relative_path):
        """Rename an occurrence atomically without changing its document identity."""
        with self._snapshot() as data:
            old = self._file_key(source_id, old_relative_path)
            new = self._file_key(source_id, new_relative_path)
            if old not in data["files"]:
                raise FileNotFoundError(old_relative_path)
            if old != new and new in data["files"] and data["files"][new]["active"]:
                raise FileExistsError(new_relative_path)
            item = data["files"].pop(old)
            item.update(relative_path=new_relative_path, updated_at=utc_now_iso())
            data["files"][new] = item
            return FileRecord(**copy.deepcopy(item))

    def set_file_active(self, source_id, relative_path, active):
        """Activate or deactivate a known occurrence."""
        with self._snapshot() as data:
            item = data["files"].get(self._file_key(source_id, relative_path))
            if item is None:
                return False
            item.update(active=bool(active), updated_at=utc_now_iso())
            return True

    def mark_files_inactive_not_seen(self, source_id, current_scan_id):
        """Deactivate unseen files after the caller has established a complete scan."""
        with self._snapshot() as data:
            result = []
            for item in data["files"].values():
                if item["source_id"] == source_id and item["active"] and item["last_seen_scan_id"] != current_scan_id:
                    result.append(FileRecord(**copy.deepcopy(item)))
                    item.update(active=False, updated_at=utc_now_iso())
            return result

    def delete_file_record(self, source_id, relative_path):
        """Remove a catalog occurrence without modifying the source file."""
        with self._snapshot() as data:
            return data["files"].pop(self._file_key(source_id, relative_path), None) is not None

    def resolve_evidence(self, canonical_doc_id, active_only=True):
        """Resolve a canonical identifier to all original source occurrences."""
        with self._snapshot() as data:
            doc = data["documents"].get(canonical_doc_id)
            if not doc:
                return []
            result = []
            for f in sorted(data["files"].values(), key=lambda x: (x["source_id"], x["relative_path"])):
                if f.get("canonical_doc_id") != canonical_doc_id or (active_only and not f["active"]):
                    continue
                src = data["sources"][f["source_id"]]
                folder, rel, host = src["origin_folder"].rstrip("/"), f["relative_path"].lstrip("/"), src["origin_host"]
                uri = f"{host}:{folder}/{rel}" if host else f"{folder}/{rel}" if folder else rel
                result.append({"canonical_doc_id": canonical_doc_id, "lightrag_doc_id": doc["lightrag_doc_id"], "extracted_artifact_path": doc["extracted_artifact_path"], "source_id": src["id"], "source_name": src["name"], "origin_host": host, "origin_folder": src["origin_folder"], "relative_path": f["relative_path"], "binary_sha256": f["binary_sha256"], "size": f["size"], "mtime_ns": f["mtime_ns"], "active": f["active"], "source_uri": uri})
            return result

    def find_exact_duplicate_clusters(self, active_only=True):
        """Group identical original files with their source metadata."""
        with self._snapshot() as data:
            groups = {}
            # Each eligible occurrence enters exactly one digest group in stable path order.
            for item in sorted(data["files"].values(), key=lambda f: (f["source_id"], f["relative_path"])):
                if not item["binary_sha256"] or (active_only and not item["active"]):
                    continue
                source = data["sources"][item["source_id"]]
                occurrence = {key: item[key] for key in ("source_id", "relative_path", "canonical_doc_id", "size", "mtime_ns", "active")}
                occurrence.update(source_name=source["name"], origin_host=source["origin_host"], origin_folder=source["origin_folder"])
                groups.setdefault(item["binary_sha256"], []).append(occurrence)
            return [{"binary_sha256": key, "count": len(items), "occurrences": items} for key, items in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0])) if len(items) >= 2]

    def create_job(self, job_id, kb_id, stage="PREPARING", idempotency_key=None, source_id=None, scan_id=None, counters=None, warnings=None, error_summary=None, resume_marker=None):
        """Create a uniquely identified job with an optional unique idempotency key."""
        if stage not in VALID_JOB_STAGES:
            raise ValueError("Invalid job stage")
        with self._snapshot() as data:
            if job_id in data["jobs"] or (idempotency_key is not None and any(j["idempotency_key"] == idempotency_key for j in data["jobs"].values())):
                raise ValueError("duplicate job or idempotency key")
            now = utc_now_iso()
            job = JobRecord(job_id, kb_id, stage, idempotency_key, source_id, scan_id, counters={**DEFAULT_JOB_COUNTERS, **(counters or {})}, warnings=copy.deepcopy(warnings or []), error_summary=error_summary, resume_marker=resume_marker, created_at=now, updated_at=now)
            data["jobs"][job_id] = job.to_dict()
            return job

    def get_job(self, job_id):
        """Retrieve durable job progress by identifier."""
        with self._snapshot() as data:
            value = data["jobs"].get(job_id)
            return JobRecord(**copy.deepcopy(value)) if value else None

    def get_job_by_idempotency_key(self, idempotency_key):
        """Find a job previously submitted with an idempotency key."""
        with self._snapshot() as data:
            return next((JobRecord(**copy.deepcopy(j)) for j in data["jobs"].values() if j["idempotency_key"] == idempotency_key), None)

    def update_job(self, job_id, stage=None, counters=None, warnings=None, error_summary=None, resume_marker=None):
        """Atomically update job progress and merge supplied counters."""
        if stage is not None and stage not in VALID_JOB_STAGES:
            raise ValueError("Invalid job stage")
        with self._snapshot() as data:
            job = data["jobs"].get(job_id)
            if job is None:
                raise KeyError(job_id)
            for key, value in (("stage", stage), ("warnings", warnings), ("error_summary", error_summary), ("resume_marker", resume_marker)):
                if value is not None:
                    job[key] = copy.deepcopy(value)
            if counters is not None:
                job["counters"].update(counters)
            job["updated_at"] = utc_now_iso()
            return JobRecord(**copy.deepcopy(job))

    def list_jobs(self, kb_id=None, source_id=None, stage=None, limit=50):
        """List recent jobs with optional KB, source and stage filters."""
        with self._snapshot() as data:
            matching_jobs = []
            for job in data["jobs"].values():
                if kb_id is not None and job["kb_id"] != kb_id:
                    continue
                if source_id is not None and job["source_id"] != source_id:
                    continue
                if stage is not None and job["stage"] != stage:
                    continue
                matching_jobs.append(job)

            matching_jobs.sort(key=lambda job: job["created_at"], reverse=True)
            if limit is not None:
                matching_jobs = matching_jobs[:limit]

            return [JobRecord(**copy.deepcopy(job)) for job in matching_jobs]

    def increment_job_counter(self, job_id, counter_name, delta=1):
        """Increment a job counter under the same lock as the snapshot write."""
        with self._snapshot() as data:
            job = data["jobs"].get(job_id)
            if job is None:
                raise KeyError(job_id)
            job["counters"][counter_name] = job["counters"].get(counter_name, 0) + delta
            job["updated_at"] = utc_now_iso()
            return JobRecord(**copy.deepcopy(job))

    def find_documents(self, status=None, source_id=None, path_prefix=None, active_only=True, page=1, page_size=50):
        """Return a bounded page of documents with matching occurrences."""
        page, page_size = max(1, page), max(1, min(page_size, 500))
        with self._snapshot() as data:
            ids = {f["canonical_doc_id"] for f in data["files"].values() if f.get("canonical_doc_id") and (not active_only or f["active"]) and (source_id is None or f["source_id"] == source_id) and (path_prefix is None or f["relative_path"].startswith(path_prefix))}
            docs = [self._document(data, doc).to_dict() for key, doc in sorted(data["documents"].items()) if key in ids and (status is None or doc["lightrag_status"] == status)]
            total = len(docs)
            return {"items": docs[(page - 1) * page_size:page * page_size], "total": total, "page": page, "page_size": page_size, "total_pages": max(1, math.ceil(total / page_size))}
