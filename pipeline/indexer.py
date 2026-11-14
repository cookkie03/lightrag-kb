"""Indexer and LightRAG API adapter for LightRAG-KB.

Implements §5 of the monolithic plan:
- Protected create/update/rename/delete lifecycle.
- Content-stable synthetic canonical doc IDs separated from user filenames.
- Verification of effective `PROCESSED` status (no false successes, no premature completion).
- Strict 409 handling: never treat 409 as duplicate without server-verified proof;
  retry transient concurrency / pipeline busy conflicts.
- Single-writer lock per Knowledge Base (KBWriterLock) using POSIX advisory locking.
- Mandatory persistent checkpoints and crash recovery:
  - Checkpoint of previous derived content and mapping IDs is required before modifying index.
  - Old document is never deleted without a validated persistent checkpoint.
  - All mutations (catalog upsert, LightRAG insert, wait PROCESSED) are protected inside recovery try-block.
  - Checkpoint persisted to disk allows recovery even after an abrupt process crash.
- Retry-safe Deletions:
  - Deletes from LightRAG backend FIRST before deactivating the file occurrence in the catalog.
  - If backend deletion fails, catalog state remains active so retries are never lost.
  - Preserves shared canonical documents when other active aliases exist.
- Safe sandboxed paths and duplicate identity verification:
  - Validates relative paths against directory traversal and sandbox escaping.
  - Confirms duplicate identity against server state before reusing canonical documents.
- Zero-reembedding on file renames.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.parsers import (
    SafePathError,
    utc_now_iso,
    validate_safe_relative_path,
)
from storage.catalog import Catalog

logger = logging.getLogger("pipeline.indexer")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


class LightRAGError(Exception):
    """Base exception for LightRAG server communication errors."""


class LightRAGConflictError(LightRAGError):
    """Raised when LightRAG returns HTTP 409 Conflict."""


class TransientConflictError(LightRAGConflictError):
    """Raised when 409 represents transient concurrency or lock contention."""


class UnverifiedDuplicateError(LightRAGConflictError):
    """Raised when LightRAG claims duplicate/already exists but server verification fails.

    §5: 'non considerare 409 duplicato senza prova.'
    """


class DocumentIndexingFailedError(LightRAGError):
    """Raised when LightRAG sets a document status to FAILED."""


class DocumentIndexingTimeoutError(LightRAGError):
    """Raised when a document does not reach PROCESSED within timeout."""


class RecoverySucceededError(LightRAGError):
    """Raised when an update failed in LightRAG, but previous state was restored from checkpoint."""


class RecoveryFailedError(LightRAGError):
    """Raised when an update failed and recovery also failed, leaving the document DEGRADED."""


class CheckpointRequiredError(LightRAGError):
    """Raised when a mandatory checkpoint cannot be created or validated (§5)."""


class KBLockedError(LightRAGError):
    """Raised when the single-writer lock for the Knowledge Base cannot be acquired (§5)."""


def create_index_result(
    action: str,
    canonical_doc_id: str,
    status: str,
    lightrag_doc_id: str | None = None,
    reused: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an index result as a plain dict."""
    return {
        "action": action,
        "canonical_doc_id": canonical_doc_id,
        "status": status,
        "lightrag_doc_id": lightrag_doc_id,
        "reused": reused,
        "details": details,
    }


# -----------------------------------------------------------------------------
# Single Writer Lock per KB (§5)
# -----------------------------------------------------------------------------


class KBWriterLock:
    """Advisory single-writer lock per Knowledge Base (§5: 'un solo writer per KB').

    Uses POSIX advisory locking (fcntl.flock) on a persistent lock file.
    Supports re-entrant acquisition within the same process.
    """

    def __init__(self, lock_path: Path | str, kb_id: str = "") -> None:
        self.lock_path = Path(lock_path).resolve()
        self.kb_id = kb_id
        self._fd: int | None = None
        self._depth: int = 0

    def acquire(self, timeout: float = 0.0, poll_interval: float = 0.05) -> bool:
        if self._depth > 0:
            self._depth += 1
            return True

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        start_time = time.time()
        while True:
            try:
                fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                self._depth = 1
                return True
            except (BlockingIOError, OSError) as exc:
                if (time.time() - start_time) >= timeout:
                    raise KBLockedError(
                        f"Cannot acquire lock for KB '{self.kb_id}' "
                        f"(lockfile: {self.lock_path}): another writer is active."
                    ) from exc
                time.sleep(poll_interval)

    def release(self) -> None:
        if self._depth <= 0:
            return
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            finally:
                self._fd = None

    def __enter__(self) -> KBWriterLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


# -----------------------------------------------------------------------------
# LightRAG HTTP Client (§5)
# -----------------------------------------------------------------------------


class LightRAGClient:
    """HTTP client interacting with per-KB LightRAG server on localhost."""

    # Configure the per-KB endpoint and reusable HTTP session.
    def __init__(
        self,
        port: int,
        host: str = "127.0.0.1",
        session: requests.Session | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.port = port
        self.host = host
        self.base_url = f"http://{host}:{port}"
        self.session = session or requests.Session()
        self.timeout = timeout

    def health(self, timeout: float = 3.0) -> bool:
        """Check whether LightRAG backend is healthy."""
        try:
            resp = self.session.get(f"{self.base_url}/health", timeout=timeout)
            return resp.ok
        except Exception:
            return False

    def get_status_counts(self) -> dict[str, int]:
        """Fetch status count breakdown from /documents/status_counts."""
        resp = self.session.get(f"{self.base_url}/documents/status_counts", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_documents(
        self, status_filter: str | None = None, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """Fetch documents list via /documents/paginated with fallback to /documents."""
        docs: list[dict[str, Any]] = []
        try:
            page = 1
            # Accumulate complete pages until the server returns a short or empty page.
            while True:
                resp = self.session.post(
                    f"{self.base_url}/documents/paginated",
                    json={
                        "status_filter": status_filter,
                        "page": page,
                        "page_size": page_size,
                        "sort_field": "updated_at",
                        "sort_direction": "desc",
                    },
                    timeout=self.timeout,
                )
                if resp.status_code == 404:
                    raise RuntimeError("Paginated endpoint not available")
                resp.raise_for_status()
                data = resp.json()
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    for k in ("documents", "items", "data", "rows"):
                        if isinstance(data.get(k), list):
                            items = data[k]
                            break
                if not items:
                    break
                docs.extend(items)
                if len(items) < page_size:
                    break
                page += 1
            return docs
        except Exception:
            # Fallback to GET /documents
            resp = self.session.get(f"{self.base_url}/documents", timeout=self.timeout)
            resp.raise_for_status()
            raw = resp.json()
            statuses = raw.get("statuses", {})
            for bucket_name, bucket_items in statuses.items():
                if status_filter and bucket_name.upper() != status_filter.upper():
                    continue
                for item in bucket_items or []:
                    if isinstance(item, dict):
                        doc_item = dict(item)
                        doc_item.setdefault("status", bucket_name)
                        docs.append(doc_item)
            return docs

    def find_document_by_file_source(self, file_source: str) -> dict[str, Any] | None:
        """Find a document in LightRAG matching the specified file_source or doc ID."""
        all_docs = self.get_documents()
        for doc in all_docs:
            if (
                doc.get("file_path") == file_source
                or doc.get("file_source") == file_source
                or doc.get("id") == file_source
                or doc.get("doc_id") == file_source
            ):
                return doc
        return None

    def insert_text(
        self,
        text: str,
        file_source: str,
        retries: int = 5,
        backoff_base: float = 0.5,
    ) -> dict[str, Any]:
        """Insert text content into LightRAG with strict 409 validation (§5).

        Rules:
        - 200/201: Success.
        - 409 Transient (pipeline busy, lock contention): exponential backoff and retry.
        - 409 Duplicate claim ('already exists', 'already contains'):
          MUST verify document presence in LightRAG via find_document_by_file_source.
          If verified -> return duplicate_verified.
          If unverified -> raise UnverifiedDuplicateError. Never assume duplicate without proof!
        - Other errors: raise LightRAGError.
        """
        if not text.strip():
            return {"status": "empty", "file_source": file_source}

        # Retry bounded transient failures; duplicate claims require server verification.
        for attempt in range(1, retries + 2):
            try:
                resp = self.session.post(
                    f"{self.base_url}/documents/text",
                    json={"text": text, "file_source": file_source},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt <= retries:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                raise LightRAGError(f"Errore connessione verso LightRAG ({self.base_url}): {exc}") from exc

            if resp.status_code in (200, 201):
                try:
                    payload = resp.json()
                except Exception:
                    payload = {"message": resp.text}
                return {"status": "ok", "file_source": file_source, "response": payload}

            if resp.status_code == 409:
                try:
                    detail = str(resp.json().get("detail", "")).lower()
                except Exception:
                    detail = (resp.text or "").lower()

                # Case 1: Transient concurrency / pipeline busy
                is_transient = any(
                    marker in detail
                    for marker in (
                        "pipeline occupata",
                        "queue full",
                        "busy",
                        "lock",
                        "concurrency",
                        "in progress",
                    )
                )
                if is_transient:
                    if attempt <= retries:
                        sleep_s = backoff_base * (2 ** (attempt - 1))
                        logger.info(
                            "Pipeline occupata (409) su '%s'. Riprovo in %.1fs (%d/%d)...",
                            file_source,
                            sleep_s,
                            attempt,
                            retries,
                        )
                        time.sleep(sleep_s)
                        continue
                    raise TransientConflictError(
                        f"LightRAG pipeline busy (409) after {retries} attempts for '{file_source}': {detail}"
                    )

                # Case 2: Claim of existing duplicate
                is_duplicate_claim = any(
                    marker in detail
                    for marker in (
                        "already contains",
                        "already exists",
                        "duplicate",
                        "document exists",
                    )
                )
                if is_duplicate_claim:
                    # PROVE IT! Verify if doc actually exists in LightRAG
                    existing = self.find_document_by_file_source(file_source)
                    if existing is not None:
                        logger.info("409 duplicate verified for '%s' (id: %s)", file_source, existing.get("id"))
                        return {
                            "status": "duplicate_verified",
                            "file_source": file_source,
                            "existing_doc": existing,
                        }
                    # NOT PROVEN: Raise unverified error!
                    raise UnverifiedDuplicateError(
                        f"LightRAG returned 409 duplicate for '{file_source}', "
                        f"but server verification failed (document not found)."
                    )

                # Case 3: Other 409 conflicts
                if attempt <= retries:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                raise LightRAGConflictError(
                    f"Unresolved 409 Conflict for '{file_source}': {detail}"
                )

            # Non-409 error
            raise LightRAGError(
                f"LightRAG request failed with status {resp.status_code}: {resp.text}"
            )

        raise LightRAGError(f"Cannot insert '{file_source}' after {retries} attempts.")

    def delete_documents(
        self,
        doc_ids: list[str],
        retries: int = 3,
        backoff_base: float = 0.5,
    ) -> bool:
        """Delete documents from LightRAG server with retry on transient errors.

        Treats 200, 204, and 404 as success (idempotent deletion).
        """
        if not doc_ids:
            return True

        for attempt in range(1, retries + 2):
            try:
                resp = self.session.delete(
                    f"{self.base_url}/documents/delete_document",
                    json={"doc_ids": doc_ids, "delete_file": False, "delete_llm_cache": True},
                    timeout=self.timeout,
                )
                if resp.status_code in (200, 204, 404):
                    return True
                # Transient retry on 5xx or 409
                if resp.status_code in (500, 502, 503, 504, 409) and attempt <= retries:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return False
            except requests.RequestException:
                if attempt <= retries:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                return False

        return False

    def wait_for_processed(
        self,
        file_source: str,
        doc_id: str | None = None,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> dict[str, Any]:
        """Poll LightRAG until document reaches verified PROCESSED status (§5, §11).

        Raises:
            DocumentIndexingFailedError: If document status becomes FAILED.
            DocumentIndexingTimeoutError: If timeout expires before PROCESSED.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            doc = self.find_document_by_file_source(file_source)
            if doc is None and doc_id:
                all_docs = self.get_documents()
                doc = next((d for d in all_docs if d.get("id") == doc_id), None)

            if doc is not None:
                status = str(doc.get("status", "")).upper()
                if status == "PROCESSED":
                    return doc
                if status == "FAILED":
                    err = doc.get("error_message") or doc.get("detail") or "FAILED status in LightRAG"
                    raise DocumentIndexingFailedError(
                        f"Indexing failed in LightRAG for '{file_source}': {err}"
                    )

            time.sleep(poll_interval)

        raise DocumentIndexingTimeoutError(
            f"Timeout ({timeout}s) waiting for PROCESSED status for '{file_source}'"
        )


# -----------------------------------------------------------------------------
# Protected Indexer (§5)
# -----------------------------------------------------------------------------


class ProtectedIndexer:
    """Coordinates Catalog, LightRAGClient, and derived storage with recovery guards (§5)."""

    # Prepare derived storage, checkpoint storage and the per-KB writer lock.
    def __init__(
        self,
        catalog: Catalog,
        client: LightRAGClient,
        derived_dir: Path | str,
        writer_lock: KBWriterLock | None = None,
        kb_id: str = "",
    ) -> None:
        self.catalog = catalog
        self.client = client
        self.derived_dir = Path(derived_dir).resolve()
        self.derived_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir = self.derived_dir / ".checkpoints"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.kb_id = kb_id
        self.writer_lock = writer_lock or KBWriterLock(self.derived_dir / ".kb_writer.lock", kb_id=kb_id)

    # Map a checkpoint identifier to its on-disk record.
    def _checkpoint_path(self, checkpoint_id: str) -> Path:
        return self.checkpoints_dir / f"chk_{checkpoint_id}.json"

    # Replace the checkpoint atomically after updating its timestamp.
    def _save_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        checkpoint["updated_at"] = utc_now_iso()
        chk_path = self._checkpoint_path(checkpoint["checkpoint_id"])
        tmp_path = chk_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
        tmp_path.replace(chk_path)

    # Persist the recovery stage before the corresponding mutation.
    def _update_checkpoint_stage(self, checkpoint: dict[str, Any], stage: str) -> None:
        checkpoint["stage"] = stage
        self._save_checkpoint(checkpoint)

    # Best-effort cleanup after completion or successful recovery.
    def _remove_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        chk_path = self._checkpoint_path(checkpoint["checkpoint_id"])
        try:
            if chk_path.exists():
                chk_path.unlink()
        except OSError as exc:
            logger.warning("Failed to remove checkpoint file %s: %s", chk_path, exc)

    def recover_pending_checkpoints(self) -> list[str]:
        """Scan and recover any in-flight checkpoints left behind by an unhandled crash (§5).

        Returns list of recovered checkpoint IDs.
        """
        recovered: list[str] = []
        if not self.checkpoints_dir.exists():
            return recovered

        # Only stages that may have changed the index need restoration; READY is untouched.
        for chk_path in sorted(self.checkpoints_dir.glob("chk_*.json")):
            try:
                data = json.loads(chk_path.read_text(encoding="utf-8"))
                if data["stage"] in ("OLD_DELETED", "CATALOG_PENDING", "INSERTING_NEW"):
                    logger.warning(
                        "Detected incomplete checkpoint from previous crash: %s "
                        "(doc: %s, file: %s, stage: %s). Starting recovery...",
                        data["checkpoint_id"],
                        data["old_canonical_doc_id"],
                        data["relative_path"],
                        data["stage"],
                    )
                    self._execute_recovery(data, original_exc=None, raise_on_success=False)
                    recovered.append(data["checkpoint_id"])
                elif data["stage"] == "READY":
                    # Checkpoint created before index mutation; safe to discard
                    chk_path.unlink(missing_ok=True)
            except Exception as exc:
                logger.error("Failed to recover checkpoint %s: %s", chk_path, exc)
                raise

        return recovered

    def _execute_recovery(
        self,
        checkpoint: dict[str, Any],
        original_exc: Exception | None = None,
        raise_on_success: bool = True,
    ) -> None:
        """Execute recovery from persistent checkpoint restoring LightRAG and Catalog (§5)."""
        logger.warning(
            "Starting recovery for '%s' (canonical doc: %s) from checkpoint %s...",
            checkpoint["relative_path"],
            checkpoint["old_canonical_doc_id"],
            checkpoint["checkpoint_id"],
        )
        try:
            # 1. Restore previous derived text to LightRAG
            self.client.insert_text(checkpoint["old_text_content"], file_source=checkpoint["old_canonical_doc_id"])
            rec_doc = self.client.wait_for_processed(file_source=checkpoint["old_canonical_doc_id"])
            rec_lightrag_id = rec_doc.get("id")

            # 2. Restore catalog document status
            self.catalog.update_document_status(
                canonical_doc_id=checkpoint["old_canonical_doc_id"],
                lightrag_status="PROCESSED",
                lightrag_doc_id=rec_lightrag_id,
            )

            # 3. Restore catalog file record
            self.catalog.record_file(
                source_id=checkpoint["source_id"],
                relative_path=checkpoint["relative_path"],
                binary_sha256=checkpoint["old_binary_sha256"],
                size=checkpoint["old_size"],
                mtime_ns=checkpoint["old_mtime_ns"],
                canonical_doc_id=checkpoint["old_canonical_doc_id"],
                active=True,
            )

            # 4. Clean failed new document entry if different and unreferenced
            if checkpoint["new_canonical_doc_id"] != checkpoint["old_canonical_doc_id"]:
                self.catalog.delete_document_if_unreferenced(checkpoint["new_canonical_doc_id"])

            # 5. Remove completed checkpoint file
            self._remove_checkpoint(checkpoint)

            logger.info("Recovery succeeded for '%s': previous state restored.", checkpoint["relative_path"])
            if raise_on_success:
                raise RecoverySucceededError(
                    f"Update of '{checkpoint['relative_path']}' failed ({original_exc}), "
                    f"but checkpoint recovery successfully restored the previous state."
                ) from original_exc

        except RecoverySucceededError:
            raise
        except Exception as rec_exc:
            logger.critical("Recovery failed for '%s': %s", checkpoint["relative_path"], rec_exc)
            self.catalog.update_document_status(checkpoint["old_canonical_doc_id"], "FAILED")
            self._update_checkpoint_stage(checkpoint, "FAILED")
            cause = original_exc or rec_exc
            raise RecoveryFailedError(
                f"Update of '{checkpoint['relative_path']}' failed AND checkpoint recovery failed "
                f"({rec_exc}): KB is in DEGRADED state."
            ) from cause

    def index_new(
        self,
        parsed: dict[str, Any],
        source_id: str,
        relative_path: str,
        size: int,
        mtime_ns: int,
        artifact_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Index a new file occurrence (§5 Add).

        1. Check if binary_sha256 or derived_hash is already known and PROCESSED in catalog.
           If yes -> ALIAS REUSE! Verify document presence on LightRAG server, add occurrence
           to catalog, 0 new LightRAG inserts.
        2. If new -> upsert PENDING document in catalog, insert into LightRAG, wait
           for verified PROCESSED, then commit PROCESSED document and file record.
        """
        with self.writer_lock:
            validate_safe_relative_path(relative_path)

            # Step 1: Check for existing identical content
            existing_doc = self.catalog.get_document_by_derived_hash(parsed["derived_hash"])
            if existing_doc is None:
                # Check by binary SHA256
                matching_files = self.catalog.find_files_by_hash(parsed["binary_sha256"], active_only=True)
                if matching_files and matching_files[0].get("canonical_doc_id"):
                    candidate = self.catalog.get_document(matching_files[0]["canonical_doc_id"])
                    # Verify duplicate identity
                    if candidate and matching_files[0]["binary_sha256"] == parsed["binary_sha256"]:
                        existing_doc = candidate

            if existing_doc is not None and existing_doc["lightrag_status"] == "PROCESSED":
                # Verify that the document actually exists in LightRAG backend!
                # Do not assume duplicate without server verification (§5)
                server_doc = self.client.find_document_by_file_source(existing_doc["canonical_doc_id"])
                if server_doc is not None and str(server_doc.get("status", "")).upper() == "PROCESSED":
                    # Reuse canonical document! No new embeddings.
                    self.catalog.record_file(
                        source_id=source_id,
                        relative_path=relative_path,
                        binary_sha256=parsed["binary_sha256"],
                        size=size,
                        mtime_ns=mtime_ns,
                        canonical_doc_id=existing_doc["canonical_doc_id"],
                        active=True,
                    )
                    return create_index_result(
                        action="alias_reused",
                        canonical_doc_id=existing_doc["canonical_doc_id"],
                        status="PROCESSED",
                        lightrag_doc_id=existing_doc["lightrag_doc_id"],
                        reused=True,
                    )
                else:
                    logger.warning(
                        "Document '%s' present in catalog but missing or not PROCESSED in LightRAG. Reindexing...",
                        existing_doc["canonical_doc_id"],
                    )

            # Step 2: New canonical document
            canonical_id = parsed["canonical_doc_id"]
            artifact_str = str(artifact_path) if artifact_path else None

            self.catalog.upsert_document(
                canonical_doc_id=canonical_id,
                derived_hash=parsed["derived_hash"],
                extracted_artifact_path=artifact_str,
                lightrag_status="PENDING",
                parser_version=parsed["parser_version"],
            )

            try:
                insert_res = self.client.insert_text(
                    text=parsed["text_content"],
                    file_source=canonical_id,
                )

                # Wait for verified PROCESSED status
                doc_info = self.client.wait_for_processed(file_source=canonical_id)
                lightrag_doc_id = doc_info.get("id")

                # Commit to catalog
                self.catalog.update_document_status(
                    canonical_doc_id=canonical_id,
                    lightrag_status="PROCESSED",
                    lightrag_doc_id=lightrag_doc_id,
                )
                self.catalog.record_file(
                    source_id=source_id,
                    relative_path=relative_path,
                    binary_sha256=parsed["binary_sha256"],
                    size=size,
                    mtime_ns=mtime_ns,
                    canonical_doc_id=canonical_id,
                    active=True,
                )

                return create_index_result(
                    action="indexed_new",
                    canonical_doc_id=canonical_id,
                    status="PROCESSED",
                    lightrag_doc_id=lightrag_doc_id,
                    reused=False,
                    details=insert_res,
                )
            except Exception:
                # Mark document FAILED in catalog
                self.catalog.update_document_status(
                    canonical_doc_id=canonical_id,
                    lightrag_status="FAILED",
                )
                raise

    def update_modified(
        self,
        parsed: dict[str, Any],
        source_id: str,
        relative_path: str,
        size: int,
        mtime_ns: int,
        artifact_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Update a modified file occurrence with recovery and alias protection (§5 Modify).

        Rules:
        - If old canonical doc is SHARED (reference_count > 1):
          DO NOT delete old doc from LightRAG! Create a new canonical doc for this
          occurrence and update its reference; existing aliases remain untouched.
        - If old canonical doc is UNSHARED (reference_count == 1):
          Mandatory persistent checkpoint of previous derived content and mapping IDs
          BEFORE deleting from LightRAG.
          All mutating operations (catalog upsert, LightRAG insert, wait PROCESSED) are
          wrapped in the recovery try-block.
          If indexing fails, automatically execute recovery from the persistent checkpoint!
        """
        with self.writer_lock:
            validate_safe_relative_path(relative_path)

            old_file = self.catalog.get_file(source_id, relative_path)
            if old_file is None or not old_file.get("canonical_doc_id"):
                # File not previously tracked; route to index_new
                return self.index_new(parsed, source_id, relative_path, size, mtime_ns, artifact_path)

            old_canonical_id = old_file["canonical_doc_id"]
            old_ref_count = self.catalog.get_document_reference_count(old_canonical_id, active_only=True)
            artifact_str = str(artifact_path) if artifact_path else None

            # -----------------------------------------------------------------
            # Branch 1: Old canonical doc is shared by multiple files
            # -----------------------------------------------------------------
            if old_ref_count > 1:
                logger.info(
                    "Documento '%s' condiviso da %d alias: creo nuovo canonical doc senza toccare il vecchio.",
                    old_canonical_id,
                    old_ref_count,
                )
                new_canonical_id = parsed["canonical_doc_id"]

                # Check if new content matches another existing document
                existing_doc = self.catalog.get_document_by_derived_hash(parsed["derived_hash"])
                if existing_doc and existing_doc["lightrag_status"] == "PROCESSED":
                    server_doc = self.client.find_document_by_file_source(existing_doc["canonical_doc_id"])
                    if server_doc is not None and str(server_doc.get("status", "")).upper() == "PROCESSED":
                        self.catalog.record_file(
                            source_id=source_id,
                            relative_path=relative_path,
                            binary_sha256=parsed["binary_sha256"],
                            size=size,
                            mtime_ns=mtime_ns,
                            canonical_doc_id=existing_doc["canonical_doc_id"],
                            active=True,
                        )
                        return create_index_result(
                            action="updated_shared_reused",
                            canonical_doc_id=existing_doc["canonical_doc_id"],
                            status="PROCESSED",
                            lightrag_doc_id=existing_doc["lightrag_doc_id"],
                            reused=True,
                        )

                # Insert new document into LightRAG
                self.catalog.upsert_document(
                    canonical_doc_id=new_canonical_id,
                    derived_hash=parsed["derived_hash"],
                    extracted_artifact_path=artifact_str,
                    lightrag_status="PENDING",
                    parser_version=parsed["parser_version"],
                )

                try:
                    self.client.insert_text(parsed["text_content"], file_source=new_canonical_id)
                    doc_info = self.client.wait_for_processed(file_source=new_canonical_id)
                    lightrag_doc_id = doc_info.get("id")

                    self.catalog.update_document_status(
                        canonical_doc_id=new_canonical_id,
                        lightrag_status="PROCESSED",
                        lightrag_doc_id=lightrag_doc_id,
                    )
                    self.catalog.record_file(
                        source_id=source_id,
                        relative_path=relative_path,
                        binary_sha256=parsed["binary_sha256"],
                        size=size,
                        mtime_ns=mtime_ns,
                        canonical_doc_id=new_canonical_id,
                        active=True,
                    )
                    return create_index_result(
                        action="updated_shared_new",
                        canonical_doc_id=new_canonical_id,
                        status="PROCESSED",
                        lightrag_doc_id=lightrag_doc_id,
                    )
                except Exception:
                    self.catalog.update_document_status(new_canonical_id, "FAILED")
                    raise

            # -----------------------------------------------------------------
            # Branch 2: Old canonical doc is unshared (only referenced by this file)
            # -----------------------------------------------------------------
            old_doc = self.catalog.get_document(old_canonical_id)
            if old_doc is None:
                raise CheckpointRequiredError(
                    f"Canonical document '{old_canonical_id}' not found in catalog for '{relative_path}'."
                )

            # Mandatory checkpoint validation (§5):
            # Checkpoint of previous derived content and mapping doc IDs is REQUIRED.
            # Old doc must NOT be deleted without a valid persistent checkpoint.
            if not old_doc.get("extracted_artifact_path"):
                raise CheckpointRequiredError(
                    f"Mandatory checkpoint failed for '{relative_path}': "
                    f"no extracted_artifact_path in catalog for '{old_canonical_id}'."
                )

            old_art = Path(old_doc["extracted_artifact_path"])
            if not old_art.is_file():
                raise CheckpointRequiredError(
                    f"Mandatory checkpoint failed for '{relative_path}': "
                    f"previous derived artifact file '{old_art}' does not exist."
                )

            checkpoint_content = old_art.read_text(encoding="utf-8", errors="replace")
            if not checkpoint_content.strip():
                raise CheckpointRequiredError(
                    f"Mandatory checkpoint failed for '{relative_path}': "
                    f"previous derived artifact '{old_art}' is empty."
                )

            if not parsed["text_content"] or not parsed["text_content"].strip():
                raise ValueError(
                    f"New document content for '{relative_path}' empty or invalid."
                )

            checkpoint_lightrag_id = old_doc.get("lightrag_doc_id")
            target_old_doc_id = checkpoint_lightrag_id or old_canonical_id
            new_canonical_id = parsed["canonical_doc_id"]
            checkpoint_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

            # Create persistent checkpoint on disk BEFORE deleting old doc from LightRAG!
            checkpoint = {
                "checkpoint_id": checkpoint_id,
                "stage": "READY",
                "source_id": source_id,
                "relative_path": relative_path,
                "old_canonical_doc_id": old_canonical_id,
                "old_lightrag_doc_id": checkpoint_lightrag_id,
                "old_text_content": checkpoint_content,
                "old_derived_hash": old_doc.get("derived_hash"),
                "old_artifact_path": old_doc.get("extracted_artifact_path"),
                "old_binary_sha256": old_file["binary_sha256"],
                "old_size": old_file["size"],
                "old_mtime_ns": old_file["mtime_ns"],
                "new_canonical_doc_id": new_canonical_id,
                "new_derived_hash": parsed["derived_hash"],
                "new_artifact_path": artifact_str,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
            self._save_checkpoint(checkpoint)

            # Entire mutation sequence is INSIDE try to guarantee recovery
            try:
                # 1. Delete old doc from LightRAG
                self._update_checkpoint_stage(checkpoint, "OLD_DELETED")
                del_ok = self.client.delete_documents([target_old_doc_id])
                if not del_ok:
                    raise LightRAGError(
                        f"Deletion of old version '{target_old_doc_id}' from LightRAG failed."
                    )

                # 2. Upsert PENDING in catalog (INSIDE try)
                self._update_checkpoint_stage(checkpoint, "CATALOG_PENDING")
                self.catalog.upsert_document(
                    canonical_doc_id=new_canonical_id,
                    derived_hash=parsed["derived_hash"],
                    extracted_artifact_path=artifact_str,
                    lightrag_status="PENDING",
                    parser_version=parsed["parser_version"],
                )

                # 3. Insert new text in LightRAG
                self._update_checkpoint_stage(checkpoint, "INSERTING_NEW")
                self.client.insert_text(parsed["text_content"], file_source=new_canonical_id)
                doc_info = self.client.wait_for_processed(file_source=new_canonical_id)
                lightrag_doc_id = doc_info.get("id")

                # 4. Commit to catalog
                self.catalog.update_document_status(
                    canonical_doc_id=new_canonical_id,
                    lightrag_status="PROCESSED",
                    lightrag_doc_id=lightrag_doc_id,
                )
                self.catalog.record_file(
                    source_id=source_id,
                    relative_path=relative_path,
                    binary_sha256=parsed["binary_sha256"],
                    size=size,
                    mtime_ns=mtime_ns,
                    canonical_doc_id=new_canonical_id,
                    active=True,
                )

                # If canonical doc ID changed, delete old unreferenced document record
                if new_canonical_id != old_canonical_id:
                    self.catalog.delete_document_if_unreferenced(old_canonical_id)

                # Mark checkpoint completed / delete checkpoint file
                self._remove_checkpoint(checkpoint)

                return create_index_result(
                    action="updated_unshared",
                    canonical_doc_id=new_canonical_id,
                    status="PROCESSED",
                    lightrag_doc_id=lightrag_doc_id,
                )
            except Exception as update_exc:
                logger.warning(
                    "Update failed for '%s': starting recovery from persisted checkpoint...",
                    relative_path,
                )
                self._execute_recovery(checkpoint, original_exc=update_exc, raise_on_success=True)

    def delete_file(self, source_id: str, relative_path: str) -> dict[str, Any]:
        """Delete a file occurrence with retry-safe alias protection (§5 Delete).

        Rules:
        - If file is already inactive: checks if lingering unreferenced canonical doc needs cleanup.
        - If file has active aliases (>1): deactivates file in catalog, keeps doc in LightRAG.
        - If file is sole active reference (==1): deletes from LightRAG FIRST;
          only after LightRAG confirms deletion is the file deactivated in catalog.
          If LightRAG delete fails, catalog remains active, ensuring full retry-safety.
        """
        with self.writer_lock:
            validate_safe_relative_path(relative_path)

            file_rec = self.catalog.get_file(source_id, relative_path)
            if file_rec is None:
                return {"action": "not_found", "relative_path": relative_path}

            if not file_rec["active"]:
                # Already inactive in catalog. Check if canonical doc is lingering unreferenced
                canonical_id = file_rec.get("canonical_doc_id")
                if canonical_id:
                    active_refs = self.catalog.get_document_reference_count(canonical_id, active_only=True)
                    if active_refs == 0:
                        doc_rec = self.catalog.get_document(canonical_id)
                        if doc_rec is not None:
                            target_id = doc_rec.get("lightrag_doc_id") or canonical_id
                            self.client.delete_documents([target_id])
                            self.catalog.delete_document_if_unreferenced(canonical_id)
                return {"action": "already_inactive", "relative_path": relative_path}

            canonical_id = file_rec.get("canonical_doc_id")
            if not canonical_id:
                self.catalog.set_file_active(source_id, relative_path, False)
                return {"action": "deactivated_no_doc", "relative_path": relative_path}

            active_refs = self.catalog.get_document_reference_count(canonical_id, active_only=True)

            if active_refs > 1:
                # Shared doc: other files still reference it.
                # Do NOT delete from LightRAG. Deactivate file occurrence only.
                self.catalog.set_file_active(source_id, relative_path, False)
                logger.info(
                    "File '%s' deactivated, but canonical doc '%s' retained in LightRAG (still has %d active aliases).",
                    relative_path,
                    canonical_id,
                    active_refs - 1,
                )
                return {
                    "action": "alias_deactivated_doc_retained",
                    "canonical_doc_id": canonical_id,
                    "remaining_aliases": active_refs - 1,
                    "deleted_from_lightrag": False,
                }

            # active_refs == 1: Sole reference. Must delete from LightRAG first!
            doc_rec = self.catalog.get_document(canonical_id)
            target_id = (doc_rec.get("lightrag_doc_id") if doc_rec else None) or canonical_id

            # Delete from LightRAG backend BEFORE deactivating file in catalog
            del_ok = self.client.delete_documents([target_id])
            if not del_ok:
                raise LightRAGError(
                    f"Deletion from LightRAG failed for doc_id '{target_id}' (file: '{relative_path}'). "
                    f"Catalog was NOT deactivated to allow retry."
                )

            # Deletion confirmed from LightRAG: now safe to deactivate in catalog
            self.catalog.set_file_active(source_id, relative_path, False)
            self.catalog.delete_document_if_unreferenced(canonical_id)

            logger.info("File '%s' removed and canonical doc '%s' deleted from LightRAG.", relative_path, canonical_id)
            return {
                "action": "doc_deleted",
                "canonical_doc_id": canonical_id,
                "deleted_from_lightrag": True,
            }

    def rename_file(
        self,
        source_id: str,
        old_relative_path: str,
        new_relative_path: str,
    ) -> dict[str, Any]:
        """Rename a file occurrence preserving its canonical document mapping (§5 Rename).

        Guarantees 0 new parsing and 0 new embedding calls.
        """
        with self.writer_lock:
            validate_safe_relative_path(old_relative_path)
            validate_safe_relative_path(new_relative_path)

            renamed = self.catalog.rename_file(source_id, old_relative_path, new_relative_path)
            logger.info(
                "Rename '%s' -> '%s' (canonical doc: %s): 0 re-embeddings.",
                old_relative_path,
                new_relative_path,
                renamed["canonical_doc_id"],
            )
            return {
                "action": "renamed",
                "source_id": source_id,
                "old_relative_path": old_relative_path,
                "new_relative_path": new_relative_path,
                "canonical_doc_id": renamed["canonical_doc_id"],
            }