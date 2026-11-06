"""Catalog database interface for LightRAG-KB (SQLite per KB).

Implements the minimum data model defined in §3 of:
`LightRAG-KB · Piano monolitico production-grade (Debian + MCP + one-shot sync)`

Tables:
- `sources`: Stable source metadata, origin host/folder, mode, patterns, policy, scan state.
- `documents`: Canonical documents, derived hashes, extracted artifacts, LightRAG doc IDs.
- `files`: File occurrences (source_id, relative_path unique), binary hashes, mtime, canonical doc mapping.
- `jobs`: Job lifecycle, idempotency keys, stages, counters, warnings, error summaries, resume markers.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Sequence

DEFAULT_KB_ROOT = Path(__file__).resolve().parent.parent / "kb"

VALID_SOURCE_MODES = {"full_folder", "selected_files"}

VALID_JOB_STAGES = {
    "PREPARING",
    "TRANSFERRING",
    "VALIDATING",
    "PROCESSING",
    "COMPLETED",
    "FAILED_RETRYABLE",
    "FAILED_FINAL",
    "NEEDS_REVIEW",
}

DEFAULT_JOB_COUNTERS: dict[str, int] = {
    "added": 0,
    "modified": 0,
    "renamed": 0,
    "deleted": 0,
    "unchanged": 0,
    "failed": 0,
    "warnings": 0,
}

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    origin_host TEXT NOT NULL DEFAULT '',
    origin_folder TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'full_folder' CHECK(mode IN ('full_folder', 'selected_files')),
    include_patterns TEXT NOT NULL DEFAULT '[]',
    exclude_patterns TEXT NOT NULL DEFAULT '[]',
    deletion_policy TEXT NOT NULL DEFAULT 'guarded',
    last_complete_scan_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    canonical_doc_id TEXT PRIMARY KEY,
    derived_hash TEXT,
    extracted_artifact_path TEXT,
    lightrag_doc_id TEXT,
    lightrag_status TEXT NOT NULL DEFAULT 'PENDING',
    parser_version TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    binary_sha256 TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    canonical_doc_id TEXT REFERENCES documents(canonical_doc_id) ON DELETE SET NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    last_seen_scan_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, relative_path)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    kb_id TEXT NOT NULL,
    source_id TEXT REFERENCES sources(id) ON DELETE SET NULL,
    scan_id TEXT,
    stage TEXT NOT NULL CHECK(stage IN (
        'PREPARING', 'TRANSFERRING', 'VALIDATING',
        'PROCESSING', 'COMPLETED', 'FAILED_RETRYABLE',
        'FAILED_FINAL', 'NEEDS_REVIEW'
    )),
    counters TEXT NOT NULL DEFAULT '{}',
    warnings TEXT NOT NULL DEFAULT '[]',
    error_summary TEXT,
    resume_marker TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_source_path ON files(source_id, relative_path);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(binary_sha256);
CREATE INDEX IF NOT EXISTS idx_files_canonical_doc ON files(canonical_doc_id);
CREATE INDEX IF NOT EXISTS idx_files_active ON files(active);
CREATE INDEX IF NOT EXISTS idx_documents_lightrag_id ON documents(lightrag_doc_id);
CREATE INDEX IF NOT EXISTS idx_documents_derived_hash ON documents(derived_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_kb_stage ON jobs(kb_id, stage);
CREATE INDEX IF NOT EXISTS idx_jobs_idempotency ON jobs(idempotency_key);
"""


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def generate_canonical_doc_id(binary_sha256: str, revision: int = 1) -> str:
    """Generate a synthetic, unique, content-stable canonical doc ID (§3).

    Separates original user filenames from the identifier submitted to LightRAG,
    preventing basename collisions across folders or revisions.
    """
    clean_hash = binary_sha256.strip().lower()
    prefix = clean_hash[:16] if clean_hash else "empty"
    if revision > 1:
        return f"doc_{prefix}_r{revision}"
    return f"doc_{prefix}"


@dataclass
class SourceRecord:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DocumentRecord:
    canonical_doc_id: str
    derived_hash: str | None = None
    extracted_artifact_path: str | None = None
    lightrag_doc_id: str | None = None
    lightrag_status: str = "PENDING"
    parser_version: str | None = None
    created_at: str = ""
    updated_at: str = ""
    reference_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileRecord:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JobRecord:
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Catalog:
    """SQLite Catalog management for a Knowledge Base (§3)."""

    def __init__(self, db_path: Path | str) -> None:
        if str(db_path) == ":memory:":
            self._path: Path | None = None
            self._conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=30.0)
        else:
            self._path = Path(db_path).resolve()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=30.0)

        self._in_transaction = False
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._migrate()

    def _configure_connection(self) -> None:
        self._conn.execute("PRAGMA foreign_keys = ON;")
        if self._path is not None:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute("PRAGMA synchronous = NORMAL;")

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        if not self._in_transaction:
            self._conn.commit()
        return cur

    def _migrate(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        cur = self._conn.execute("SELECT MAX(version) FROM schema_migrations")
        row = cur.fetchone()
        current_version = row[0] if (row and row[0] is not None) else 0

        if current_version < 1:
            self._conn.executescript(SCHEMA_V1)
            self._conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
                (utc_now_iso(),),
            )
            self._conn.execute("PRAGMA user_version = 1")
            self._conn.commit()

    @classmethod
    def open_for_kb(cls, kb_id: str, kb_root: Path | str | None = None) -> Catalog:
        """Open or initialize catalog.sqlite for the given KB ID under kb_root (§3)."""
        root = Path(kb_root) if kb_root is not None else DEFAULT_KB_ROOT
        db_path = root / kb_id / "catalog.sqlite"
        return cls(db_path)

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for an atomic database transaction."""
        if self._in_transaction:
            yield self._conn
            return

        self._in_transaction = True
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._in_transaction = False

    def close(self) -> None:
        """Close SQLite database connection."""
        self._conn.close()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # -------------------------------------------------------------------------
    # Sources (§3)
    # -------------------------------------------------------------------------

    def add_source(
        self,
        source_id: str,
        name: str,
        origin_host: str = "",
        origin_folder: str = "",
        mode: str = "full_folder",
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        deletion_policy: str = "guarded",
        status: str = "active",
    ) -> SourceRecord:
        """Register a new source directory or selection for this KB."""
        if mode not in VALID_SOURCE_MODES:
            raise ValueError(f"Invalid source mode: {mode}. Must be one of {VALID_SOURCE_MODES}")

        now = utc_now_iso()
        inc_json = json.dumps(include_patterns or [])
        exc_json = json.dumps(exclude_patterns or [])

        self._execute(
            """
            INSERT INTO sources (
                id, name, origin_host, origin_folder, mode,
                include_patterns, exclude_patterns, deletion_policy,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                name,
                origin_host,
                origin_folder,
                mode,
                inc_json,
                exc_json,
                deletion_policy,
                status,
                now,
                now,
            ),
        )

        source = self.get_source(source_id)
        if source is None:
            raise RuntimeError(f"Failed to create source {source_id}")
        return source

    def get_source(self, source_id: str) -> SourceRecord | None:
        """Retrieve source metadata by ID."""
        cur = self._conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,))
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_source(row)

    def list_sources(self, status: str | None = None) -> list[SourceRecord]:
        """List all sources, optionally filtered by status."""
        if status is not None:
            cur = self._conn.execute(
                "SELECT * FROM sources WHERE status = ? ORDER BY id ASC", (status,)
            )
        else:
            cur = self._conn.execute("SELECT * FROM sources ORDER BY id ASC")
        return [self._row_to_source(row) for row in cur.fetchall()]

    def update_source(
        self,
        source_id: str,
        name: str | None = None,
        origin_host: str | None = None,
        origin_folder: str | None = None,
        mode: str | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        deletion_policy: str | None = None,
        status: str | None = None,
        last_complete_scan_id: str | None = None,
    ) -> SourceRecord:
        """Update source properties and touch updated_at."""
        existing = self.get_source(source_id)
        if existing is None:
            raise KeyError(f"Source not found: {source_id}")

        if mode is not None and mode not in VALID_SOURCE_MODES:
            raise ValueError(f"Invalid source mode: {mode}. Must be one of {VALID_SOURCE_MODES}")

        fields: list[str] = []
        params: list[Any] = []

        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if origin_host is not None:
            fields.append("origin_host = ?")
            params.append(origin_host)
        if origin_folder is not None:
            fields.append("origin_folder = ?")
            params.append(origin_folder)
        if mode is not None:
            fields.append("mode = ?")
            params.append(mode)
        if include_patterns is not None:
            fields.append("include_patterns = ?")
            params.append(json.dumps(include_patterns))
        if exclude_patterns is not None:
            fields.append("exclude_patterns = ?")
            params.append(json.dumps(exclude_patterns))
        if deletion_policy is not None:
            fields.append("deletion_policy = ?")
            params.append(deletion_policy)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if last_complete_scan_id is not None:
            fields.append("last_complete_scan_id = ?")
            params.append(last_complete_scan_id)

        fields.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(source_id)

        sql = f"UPDATE sources SET {', '.join(fields)} WHERE id = ?"
        self._execute(sql, params)

        updated = self.get_source(source_id)
        assert updated is not None
        return updated

    def update_source_scan(self, source_id: str, scan_id: str) -> SourceRecord:
        """Set last_complete_scan_id for the given source."""
        return self.update_source(source_id, last_complete_scan_id=scan_id)

    def delete_source(self, source_id: str) -> bool:
        """Delete a source and cascade its file records."""
        cur = self._execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Documents (§3)
    # -------------------------------------------------------------------------

    def upsert_document(
        self,
        canonical_doc_id: str,
        derived_hash: str | None = None,
        extracted_artifact_path: str | None = None,
        lightrag_doc_id: str | None = None,
        lightrag_status: str = "PENDING",
        parser_version: str | None = None,
    ) -> DocumentRecord:
        """Insert or update a canonical document."""
        now = utc_now_iso()
        self._execute(
            """
            INSERT INTO documents (
                canonical_doc_id, derived_hash, extracted_artifact_path,
                lightrag_doc_id, lightrag_status, parser_version,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_doc_id) DO UPDATE SET
                derived_hash = COALESCE(excluded.derived_hash, documents.derived_hash),
                extracted_artifact_path = COALESCE(excluded.extracted_artifact_path, documents.extracted_artifact_path),
                lightrag_doc_id = COALESCE(excluded.lightrag_doc_id, documents.lightrag_doc_id),
                lightrag_status = excluded.lightrag_status,
                parser_version = COALESCE(excluded.parser_version, documents.parser_version),
                updated_at = excluded.updated_at
            """,
            (
                canonical_doc_id,
                derived_hash,
                extracted_artifact_path,
                lightrag_doc_id,
                lightrag_status,
                parser_version,
                now,
                now,
            ),
        )

        doc = self.get_document(canonical_doc_id)
        if doc is None:
            raise RuntimeError(f"Failed to upsert document {canonical_doc_id}")
        return doc

    def get_document(self, canonical_doc_id: str) -> DocumentRecord | None:
        """Retrieve a canonical document along with its active reference count (§3)."""
        cur = self._conn.execute(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM files f WHERE f.canonical_doc_id = d.canonical_doc_id AND f.active = 1) AS reference_count
            FROM documents d
            WHERE d.canonical_doc_id = ?
            """,
            (canonical_doc_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_document(row)

    def get_document_by_lightrag_id(self, lightrag_doc_id: str) -> DocumentRecord | None:
        """Find canonical document by LightRAG's internal document ID."""
        cur = self._conn.execute(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM files f WHERE f.canonical_doc_id = d.canonical_doc_id AND f.active = 1) AS reference_count
            FROM documents d
            WHERE d.lightrag_doc_id = ?
            """,
            (lightrag_doc_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_document(row)

    def get_document_by_derived_hash(self, derived_hash: str) -> DocumentRecord | None:
        """Find canonical document by derived content hash."""
        cur = self._conn.execute(
            """
            SELECT d.*,
                   (SELECT COUNT(*) FROM files f WHERE f.canonical_doc_id = d.canonical_doc_id AND f.active = 1) AS reference_count
            FROM documents d
            WHERE d.derived_hash = ?
            """,
            (derived_hash,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_document(row)

    def update_document_status(
        self,
        canonical_doc_id: str,
        lightrag_status: str,
        lightrag_doc_id: str | None = None,
    ) -> DocumentRecord:
        """Update LightRAG indexing status for a document."""
        now = utc_now_iso()
        if lightrag_doc_id is not None:
            self._execute(
                """
                UPDATE documents
                SET lightrag_status = ?, lightrag_doc_id = ?, updated_at = ?
                WHERE canonical_doc_id = ?
                """,
                (lightrag_status, lightrag_doc_id, now, canonical_doc_id),
            )
        else:
            self._execute(
                """
                UPDATE documents
                SET lightrag_status = ?, updated_at = ?
                WHERE canonical_doc_id = ?
                """,
                (lightrag_status, now, canonical_doc_id),
            )

        doc = self.get_document(canonical_doc_id)
        if doc is None:
            raise KeyError(f"Document not found: {canonical_doc_id}")
        return doc

    def get_document_reference_count(
        self, canonical_doc_id: str, active_only: bool = True
    ) -> int:
        """Reconstruct reference count from file occurrences (§3)."""
        if active_only:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM files WHERE canonical_doc_id = ? AND active = 1",
                (canonical_doc_id,),
            )
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM files WHERE canonical_doc_id = ?",
                (canonical_doc_id,),
            )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    def delete_document(self, canonical_doc_id: str) -> bool:
        """Unconditionally delete a document record."""
        cur = self._execute(
            "DELETE FROM documents WHERE canonical_doc_id = ?", (canonical_doc_id,)
        )
        return cur.rowcount > 0

    def delete_document_if_unreferenced(self, canonical_doc_id: str) -> bool:
        """Delete document only if no active file occurrences reference it (§3, §5).

        Returns True if deleted, False if other files still refer to this canonical doc.
        """
        ref_count = self.get_document_reference_count(canonical_doc_id, active_only=True)
        if ref_count > 0:
            return False
        return self.delete_document(canonical_doc_id)

    # -------------------------------------------------------------------------
    # Files & Occurrences (§3)
    # -------------------------------------------------------------------------

    def record_file(
        self,
        source_id: str,
        relative_path: str,
        binary_sha256: str,
        size: int,
        mtime_ns: int,
        canonical_doc_id: str | None = None,
        active: bool = True,
        last_seen_scan_id: str | None = None,
    ) -> FileRecord:
        """Record or update a file occurrence under (source_id, relative_path).

        Separates original path from LightRAG canonical_doc_id (§3).
        """
        now = utc_now_iso()
        active_int = 1 if active else 0

        self._execute(
            """
            INSERT INTO files (
                source_id, relative_path, binary_sha256, size, mtime_ns,
                canonical_doc_id, active, last_seen_scan_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, relative_path) DO UPDATE SET
                binary_sha256 = excluded.binary_sha256,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                canonical_doc_id = COALESCE(excluded.canonical_doc_id, files.canonical_doc_id),
                active = excluded.active,
                last_seen_scan_id = COALESCE(excluded.last_seen_scan_id, files.last_seen_scan_id),
                updated_at = excluded.updated_at
            """,
            (
                source_id,
                relative_path,
                binary_sha256,
                size,
                mtime_ns,
                canonical_doc_id,
                active_int,
                last_seen_scan_id,
                now,
                now,
            ),
        )

        f = self.get_file(source_id, relative_path)
        if f is None:
            raise RuntimeError(f"Failed to record file {source_id}:{relative_path}")
        return f

    def get_file(self, source_id: str, relative_path: str) -> FileRecord | None:
        """Retrieve a specific file occurrence."""
        cur = self._conn.execute(
            "SELECT * FROM files WHERE source_id = ? AND relative_path = ?",
            (source_id, relative_path),
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_file(row)

    def list_files(
        self,
        source_id: str | None = None,
        active_only: bool = True,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[FileRecord]:
        """List files with optional filtering on source and active status."""
        clauses: list[str] = []
        params: list[Any] = []

        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if active_only:
            clauses.append("active = 1")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        pagination_sql = ""
        if limit is not None:
            pagination_sql += f" LIMIT {int(limit)}"
            if offset is not None:
                pagination_sql += f" OFFSET {int(offset)}"

        sql = f"SELECT * FROM files {where_sql} ORDER BY source_id ASC, relative_path ASC{pagination_sql}"
        cur = self._conn.execute(sql, params)
        return [self._row_to_file(row) for row in cur.fetchall()]

    def find_files_by_hash(
        self, binary_sha256: str, active_only: bool = True
    ) -> list[FileRecord]:
        """Find all file occurrences matching a binary SHA256 checksum."""
        if active_only:
            cur = self._conn.execute(
                "SELECT * FROM files WHERE binary_sha256 = ? AND active = 1 ORDER BY source_id, relative_path",
                (binary_sha256,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM files WHERE binary_sha256 = ? ORDER BY source_id, relative_path",
                (binary_sha256,),
            )
        return [self._row_to_file(row) for row in cur.fetchall()]

    def rename_file(
        self,
        source_id: str,
        old_relative_path: str,
        new_relative_path: str,
    ) -> FileRecord:
        """Rename an existing file occurrence preserving its canonical document mapping (§5).

        Avoids re-embedding when content is identical.
        """
        existing = self.get_file(source_id, old_relative_path)
        if existing is None:
            raise FileNotFoundError(f"Source file not found: {source_id}:{old_relative_path}")

        target = self.get_file(source_id, new_relative_path)
        if target is not None:
            if target.active:
                raise FileExistsError(f"Target file already exists and is active: {source_id}:{new_relative_path}")
            # If target exists but is inactive, remove the inactive record first
            self._execute(
                "DELETE FROM files WHERE source_id = ? AND relative_path = ?",
                (source_id, new_relative_path),
            )

        now = utc_now_iso()
        self._execute(
            """
            UPDATE files
            SET relative_path = ?, updated_at = ?
            WHERE source_id = ? AND relative_path = ?
            """,
            (new_relative_path, now, source_id, old_relative_path),
        )

        renamed = self.get_file(source_id, new_relative_path)
        assert renamed is not None
        return renamed

    def set_file_active(self, source_id: str, relative_path: str, active: bool) -> bool:
        """Set active flag on a file occurrence (soft delete / reactivate)."""
        now = utc_now_iso()
        active_int = 1 if active else 0
        cur = self._execute(
            """
            UPDATE files
            SET active = ?, updated_at = ?
            WHERE source_id = ? AND relative_path = ?
            """,
            (active_int, now, source_id, relative_path),
        )
        return cur.rowcount > 0

    def mark_files_inactive_not_seen(
        self, source_id: str, current_scan_id: str
    ) -> list[FileRecord]:
        """Deactivate files belonging to a source that were not seen in current scan (§4, §5)."""
        cur = self._conn.execute(
            """
            SELECT * FROM files
            WHERE source_id = ? AND active = 1 AND (last_seen_scan_id IS NULL OR last_seen_scan_id != ?)
            """,
            (source_id, current_scan_id),
        )
        unseen_rows = cur.fetchall()
        if not unseen_rows:
            return []

        now = utc_now_iso()
        self._execute(
            """
            UPDATE files
            SET active = 0, updated_at = ?
            WHERE source_id = ? AND active = 1 AND (last_seen_scan_id IS NULL OR last_seen_scan_id != ?)
            """,
            (now, source_id, current_scan_id),
        )

        return [self._row_to_file(r) for r in unseen_rows]

    def delete_file_record(self, source_id: str, relative_path: str) -> bool:
        """Permanently remove a file record from catalog."""
        cur = self._execute(
            "DELETE FROM files WHERE source_id = ? AND relative_path = ?",
            (source_id, relative_path),
        )
        return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Evidence Resolution & Duplicates (§3, §6, §8 A1)
    # -------------------------------------------------------------------------

    def resolve_evidence(
        self, canonical_doc_id: str, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """Resolve a synthetic canonical_doc_id to all original file occurrences and URIs (§3, §6)."""
        active_clause = "AND f.active = 1" if active_only else ""
        cur = self._conn.execute(
            f"""
            SELECT
                f.relative_path,
                f.binary_sha256,
                f.size,
                f.mtime_ns,
                f.active,
                s.id AS source_id,
                s.name AS source_name,
                s.origin_host,
                s.origin_folder,
                d.canonical_doc_id,
                d.lightrag_doc_id,
                d.extracted_artifact_path
            FROM files f
            JOIN sources s ON f.source_id = s.id
            JOIN documents d ON f.canonical_doc_id = d.canonical_doc_id
            WHERE f.canonical_doc_id = ? {active_clause}
            ORDER BY s.id, f.relative_path
            """,
            (canonical_doc_id,),
        )

        results: list[dict[str, Any]] = []
        for row in cur.fetchall():
            host = row["origin_host"]
            folder = row["origin_folder"].rstrip("/")
            rel = row["relative_path"].lstrip("/")
            if host:
                source_uri = f"{host}:{folder}/{rel}"
            elif folder:
                source_uri = f"{folder}/{rel}"
            else:
                source_uri = rel

            results.append(
                {
                    "canonical_doc_id": row["canonical_doc_id"],
                    "source_id": row["source_id"],
                    "source_name": row["source_name"],
                    "origin_host": host,
                    "origin_folder": row["origin_folder"],
                    "relative_path": row["relative_path"],
                    "source_uri": source_uri,
                    "binary_sha256": row["binary_sha256"],
                    "size": row["size"],
                    "mtime_ns": row["mtime_ns"],
                    "active": bool(row["active"]),
                    "lightrag_doc_id": row["lightrag_doc_id"],
                    "extracted_artifact_path": row["extracted_artifact_path"],
                }
            )
        return results

    def find_exact_duplicate_clusters(
        self, active_only: bool = True
    ) -> list[dict[str, Any]]:
        """Cluster files across all KB sources sharing the exact same binary_sha256 (§8 A1).

        Returns clusters with 2+ occurrences.
        """
        active_filter = "AND active = 1" if active_only else ""
        cur = self._conn.execute(
            f"""
            SELECT binary_sha256, COUNT(*) AS count
            FROM files
            WHERE binary_sha256 != '' {active_filter}
            GROUP BY binary_sha256
            HAVING COUNT(*) >= 2
            ORDER BY count DESC, binary_sha256 ASC
            """
        )
        clusters_meta = cur.fetchall()
        if not clusters_meta:
            return []

        results: list[dict[str, Any]] = []
        for meta in clusters_meta:
            sha = meta["binary_sha256"]
            count = meta["count"]

            f_active = "AND f.active = 1" if active_only else ""
            file_cur = self._conn.execute(
                f"""
                SELECT f.*, s.name AS source_name, s.origin_host, s.origin_folder
                FROM files f
                JOIN sources s ON f.source_id = s.id
                WHERE f.binary_sha256 = ? {f_active}
                ORDER BY f.source_id, f.relative_path
                """,
                (sha,),
            )
            occurrences = []
            for f_row in file_cur.fetchall():
                occurrences.append(
                    {
                        "source_id": f_row["source_id"],
                        "source_name": f_row["source_name"],
                        "origin_host": f_row["origin_host"],
                        "origin_folder": f_row["origin_folder"],
                        "relative_path": f_row["relative_path"],
                        "canonical_doc_id": f_row["canonical_doc_id"],
                        "size": f_row["size"],
                        "mtime_ns": f_row["mtime_ns"],
                        "active": bool(f_row["active"]),
                    }
                )

            results.append(
                {
                    "binary_sha256": sha,
                    "count": count,
                    "occurrences": occurrences,
                }
            )

        return results

    # -------------------------------------------------------------------------
    # Jobs & Progress (§3, §4)
    # -------------------------------------------------------------------------

    def create_job(
        self,
        job_id: str,
        kb_id: str,
        stage: str = "PREPARING",
        idempotency_key: str | None = None,
        source_id: str | None = None,
        scan_id: str | None = None,
        counters: dict[str, int] | None = None,
        warnings: list[str] | None = None,
        error_summary: str | None = None,
        resume_marker: str | None = None,
    ) -> JobRecord:
        """Create a sync/ingest job record tracking progress and idempotency."""
        if stage not in VALID_JOB_STAGES:
            raise ValueError(f"Invalid job stage: {stage}. Must be one of {VALID_JOB_STAGES}")

        now = utc_now_iso()
        final_counters = dict(DEFAULT_JOB_COUNTERS)
        if counters:
            final_counters.update(counters)

        counters_json = json.dumps(final_counters)
        warnings_json = json.dumps(warnings or [])

        self._execute(
            """
            INSERT INTO jobs (
                job_id, idempotency_key, kb_id, source_id, scan_id,
                stage, counters, warnings, error_summary, resume_marker,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                idempotency_key,
                kb_id,
                source_id,
                scan_id,
                stage,
                counters_json,
                warnings_json,
                error_summary,
                resume_marker,
                now,
                now,
            ),
        )

        job = self.get_job(job_id)
        if job is None:
            raise RuntimeError(f"Failed to create job {job_id}")
        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        """Retrieve a job by ID."""
        cur = self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_job(row)

    def get_job_by_idempotency_key(self, idempotency_key: str) -> JobRecord | None:
        """Find a job by its unique idempotency key (§4)."""
        cur = self._conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return self._row_to_job(row)

    def update_job(
        self,
        job_id: str,
        stage: str | None = None,
        counters: dict[str, int] | None = None,
        warnings: list[str] | None = None,
        error_summary: str | None = None,
        resume_marker: str | None = None,
    ) -> JobRecord:
        """Update job stage, counters, or status."""
        current = self.get_job(job_id)
        if current is None:
            raise KeyError(f"Job not found: {job_id}")

        if stage is not None and stage not in VALID_JOB_STAGES:
            raise ValueError(f"Invalid job stage: {stage}. Must be one of {VALID_JOB_STAGES}")

        fields: list[str] = []
        params: list[Any] = []

        if stage is not None:
            fields.append("stage = ?")
            params.append(stage)

        if counters is not None:
            updated_counters = dict(current.counters)
            updated_counters.update(counters)
            fields.append("counters = ?")
            params.append(json.dumps(updated_counters))

        if warnings is not None:
            fields.append("warnings = ?")
            params.append(json.dumps(warnings))

        if error_summary is not None:
            fields.append("error_summary = ?")
            params.append(error_summary)

        if resume_marker is not None:
            fields.append("resume_marker = ?")
            params.append(resume_marker)

        fields.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(job_id)

        sql = f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = ?"
        self._execute(sql, params)

        updated = self.get_job(job_id)
        assert updated is not None
        return updated

    def list_jobs(
        self,
        kb_id: str | None = None,
        source_id: str | None = None,
        stage: str | None = None,
        limit: int = 50,
    ) -> list[JobRecord]:
        """List jobs filtered by kb_id, source_id, and/or stage."""
        clauses: list[str] = []
        params: list[Any] = []

        if kb_id is not None:
            clauses.append("kb_id = ?")
            params.append(kb_id)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if stage is not None:
            clauses.append("stage = ?")
            params.append(stage)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM jobs {where_sql} ORDER BY created_at DESC LIMIT {int(limit)}"
        cur = self._conn.execute(sql, params)
        return [self._row_to_job(r) for r in cur.fetchall()]

    # -------------------------------------------------------------------------
    # Inventory / Pagination (§6 find_documents)
    # -------------------------------------------------------------------------

    def find_documents(
        self,
        status: str | None = None,
        source_id: str | None = None,
        path_prefix: str | None = None,
        active_only: bool = True,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """Query granular inventory with source/path filters and pagination (§6)."""
        page = max(1, page)
        page_size = max(1, min(page_size, 500))
        offset = (page - 1) * page_size

        join_conditions = ["f.canonical_doc_id = d.canonical_doc_id"]
        if active_only:
            join_conditions.append("f.active = 1")
        if source_id is not None:
            join_conditions.append("f.source_id = ?")
        if path_prefix is not None:
            join_conditions.append("f.relative_path LIKE ?")

        where_conditions: list[str] = []
        where_params: list[Any] = []

        if status is not None:
            where_conditions.append("d.lightrag_status = ?")
            where_params.append(status)

        join_params: list[Any] = []
        if source_id is not None:
            join_params.append(source_id)
        if path_prefix is not None:
            join_params.append(f"{path_prefix}%")

        from_clause = f"""
            FROM documents d
            JOIN files f ON {' AND '.join(join_conditions)}
        """

        full_params = join_params + where_params
        where_clause = f"WHERE {' AND '.join(where_conditions)}" if where_conditions else ""

        # Total distinct canonical documents matching filters
        count_sql = f"SELECT COUNT(DISTINCT d.canonical_doc_id) {from_clause} {where_clause}"
        cur = self._conn.execute(count_sql, full_params)
        total = int(cur.fetchone()[0])
        total_pages = max(1, math.ceil(total / page_size)) if total > 0 else 1

        select_sql = f"""
            SELECT DISTINCT
                d.canonical_doc_id,
                d.derived_hash,
                d.extracted_artifact_path,
                d.lightrag_doc_id,
                d.lightrag_status,
                d.parser_version,
                d.created_at,
                d.updated_at,
                (SELECT COUNT(*) FROM files cf WHERE cf.canonical_doc_id = d.canonical_doc_id AND cf.active = 1) AS reference_count
            {from_clause}
            {where_clause}
            ORDER BY d.canonical_doc_id ASC
            LIMIT {page_size} OFFSET {offset}
        """
        cur = self._conn.execute(select_sql, full_params)
        items = [self._row_to_document(r).to_dict() for r in cur.fetchall()]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    # -------------------------------------------------------------------------
    # Row conversion helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _row_to_source(row: sqlite3.Row) -> SourceRecord:
        inc = json.loads(row["include_patterns"]) if row["include_patterns"] else []
        exc = json.loads(row["exclude_patterns"]) if row["exclude_patterns"] else []
        return SourceRecord(
            id=row["id"],
            name=row["name"],
            origin_host=row["origin_host"] or "",
            origin_folder=row["origin_folder"] or "",
            mode=row["mode"] or "full_folder",
            include_patterns=inc,
            exclude_patterns=exc,
            deletion_policy=row["deletion_policy"] or "guarded",
            last_complete_scan_id=row["last_complete_scan_id"],
            status=row["status"] or "active",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> DocumentRecord:
        ref_count = row["reference_count"] if "reference_count" in row.keys() else 0
        return DocumentRecord(
            canonical_doc_id=row["canonical_doc_id"],
            derived_hash=row["derived_hash"],
            extracted_artifact_path=row["extracted_artifact_path"],
            lightrag_doc_id=row["lightrag_doc_id"],
            lightrag_status=row["lightrag_status"],
            parser_version=row["parser_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            reference_count=int(ref_count or 0),
        )

    @staticmethod
    def _row_to_file(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=row["id"],
            source_id=row["source_id"],
            relative_path=row["relative_path"],
            binary_sha256=row["binary_sha256"],
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            canonical_doc_id=row["canonical_doc_id"],
            active=bool(row["active"]),
            last_seen_scan_id=row["last_seen_scan_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        raw_counters = json.loads(row["counters"]) if row["counters"] else {}
        merged_counters = dict(DEFAULT_JOB_COUNTERS)
        merged_counters.update(raw_counters)
        warnings = json.loads(row["warnings"]) if row["warnings"] else []
        return JobRecord(
            job_id=row["job_id"],
            kb_id=row["kb_id"],
            stage=row["stage"],
            idempotency_key=row["idempotency_key"],
            source_id=row["source_id"],
            scan_id=row["scan_id"],
            counters=merged_counters,
            warnings=warnings,
            error_summary=row["error_summary"],
            resume_marker=row["resume_marker"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
