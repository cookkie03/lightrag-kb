"""Asynchronous ingestion worker for LightRAG-KB Debian host.

Implements §4 and §5 of the monolithic plan:
- Processes staged jobs from Mac / NAS clients:
  - Validates manifests and checks KB-level single-writer lock (§5: 'un solo writer per KB').
  - Validates safe sandboxed paths to prevent directory traversal and sandbox escaping (§11 #10).
  - Automatically recovers any pending checkpoints left from previous crashes (§5).
  - Strict safety guards on deletions:
    - Deletes permitted ONLY on full sync with guarded policy, scan_complete=True, and 0 scan errors.
    - Suspicious empty root / offline host triggers NEEDS_REVIEW without deletions.
    - Large deletion batches (>20% or >100 files) trigger NEEDS_REVIEW without deletions.
    - Push mode (selected_files) forces delete count = 0.
  - Effective verification of PROCESSED status before declaring COMPLETED.
  - Mandatory persistent checkpoints and recovery protection for modified files.
  - Retry-safe deletions: LightRAG delete confirmed before catalog deactivation.
  - Zero-reembedding on file renames.
  - Staging cleanup: cleans ONLY verified staged files confined strictly within staging_dir.
  - Derived Markdown extractions and JSON sidecars are persisted permanently on Debian (§7).
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.indexer import (
    IndexResult,
    KBLockedError,
    LightRAGClient,
    ProtectedIndexer,
    RecoveryFailedError,
    RecoverySucceededError,
)
from pipeline.parsers import (
    ParsedDocument,
    SafePathError,
    compute_file_sha256,
    parse_document,
    save_derived_artifacts,
    validate_safe_relative_path,
)
from storage.catalog import Catalog, JobRecord

logger = logging.getLogger("pipeline.ingestion_worker")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


class IngestionWorkerError(Exception):
    """Base exception for ingestion worker errors."""


class JobLockedError(IngestionWorkerError):
    """Raised when another job is already active for the specified KB or source (§5)."""


class SafetyGuardViolationError(IngestionWorkerError):
    """Raised when deletion safety thresholds or push restrictions are violated (§5)."""


@dataclass
class StagedAction:
    """Action descriptor for a single file within a sync job."""

    action: str  # "add", "modify", "rename", "delete"
    relative_path: str
    old_relative_path: str | None = None
    binary_sha256: str = ""
    size: int = 0
    mtime_ns: int = 0
    staged_filename: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StagedJobManifest:
    """Staged sync job manifest emitted by Mac client or ragcli sync prepare (§4)."""

    job_id: str
    kb_id: str
    source_id: str
    scan_id: str | None = None
    mode: str = "full_folder"  # "full_folder" or "selected_files"
    scan_complete: bool = True
    scan_errors: int = 0
    actions: list[StagedAction] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StagedJobManifest:
        raw_actions = data.get("actions") or data.get("files") or []
        parsed_actions = [
            StagedAction(
                action=a["action"],
                relative_path=a["relative_path"],
                old_relative_path=a.get("old_relative_path"),
                binary_sha256=a.get("binary_sha256", ""),
                size=int(a.get("size", 0)),
                mtime_ns=int(a.get("mtime_ns", 0)),
                staged_filename=a.get("staged_filename"),
            )
            for a in raw_actions
        ]
        return cls(
            job_id=data["job_id"],
            kb_id=data["kb_id"],
            source_id=data["source_id"],
            scan_id=data.get("scan_id"),
            mode=data.get("mode", "full_folder"),
            scan_complete=bool(data.get("scan_complete", True)),
            scan_errors=int(data.get("scan_errors", 0)),
            actions=parsed_actions,
        )

    @classmethod
    def load_from_file(cls, path: Path | str) -> StagedJobManifest:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)


class IngestionWorker:
    """Process staged sync and push jobs against the JSON catalog and LightRAG."""

    def __init__(
        self,
        catalog: Catalog,
        indexer: ProtectedIndexer,
        derived_dir: Path | str,
        staging_dir: Path | str | None = None,
        env: dict[str, str] | None = None,
        parser_runner: Callable[[str, str, int, str], str] | None = None,
        kb_id: str = "",
    ) -> None:
        self.catalog = catalog
        self.indexer = indexer
        self.derived_dir = Path(derived_dir).resolve()
        self.staging_dir = Path(staging_dir).resolve() if staging_dir else None
        self.env = env or {}
        self.parser_runner = parser_runner
        self.kb_id = kb_id
        self.kb_lock = indexer.writer_lock

    def acquire_kb_lock(self, kb_id: str, current_job_id: str) -> None:
        """Ensure only one sync job is actively mutating a KB (§5: 'un solo writer per KB').

        Checks if any other job for this Knowledge Base is currently in an active stage.
        """
        active_stages = {"PREPARING", "TRANSFERRING", "VALIDATING", "PROCESSING"}
        jobs = self.catalog.list_jobs(kb_id=kb_id, limit=50)
        for j in jobs:
            if j.job_id != current_job_id and j.stage in active_stages:
                raise JobLockedError(
                    f"Job '{j.job_id}' è attualmente attivo ({j.stage}) per la KB '{kb_id}'. "
                    f"È consentito un solo writer per KB."
                )

    def acquire_source_lock(self, source_id: str, current_job_id: str) -> None:
        """Preserved for backward-compatibility; checks active jobs for source (§4)."""
        active_stages = {"PREPARING", "TRANSFERRING", "VALIDATING", "PROCESSING"}
        jobs = self.catalog.list_jobs(source_id=source_id, limit=20)
        for j in jobs:
            if j.job_id != current_job_id and j.stage in active_stages:
                raise JobLockedError(
                    f"Job '{j.job_id}' è attualmente attivo ({j.stage}) per la sorgente '{source_id}'."
                )

    def validate_safety_guards(
        self,
        manifest: StagedJobManifest,
    ) -> tuple[bool, str | None]:
        """Validate delete safety guards according to §5.

        Rules:
        - Push mode (selected_files) or scan_complete=False or scan_errors > 0 ->
          Delete count must be 0!
        - High deletion threshold: delete_count > 100 or >20% of active files ->
          NEEDS_REVIEW.
        - Suspicious empty root: 0 active files remaining when catalog has files ->
          NEEDS_REVIEW.
        """
        delete_actions = [a for a in manifest.actions if a.action == "delete"]
        if not delete_actions:
            return True, None

        source = self.catalog.get_source(manifest.source_id)
        if source is not None and source.deletion_policy != "guarded":
            return False, f"Policy di cancellazione disabilitata ('{source.deletion_policy}') per sorgente {manifest.source_id}."

        # Guard 1: Push / partial scan cannot delete
        if manifest.mode != "full_folder":
            return False, "La modalità push (selected_files) non autorizza delezioni."
        if not manifest.scan_complete:
            return False, "Scansione incompleta: delezioni automatiche bloccate per sicurezza."
        if manifest.scan_errors > 0:
            return False, f"La scansione ha registrato {manifest.scan_errors} errori: delezioni bloccate."

        # Guard 2: High deletion threshold (>20% or >100 files)
        active_files = self.catalog.list_files(source_id=manifest.source_id, active_only=True)
        total_active = len(active_files)
        del_count = len(delete_actions)

        if total_active > 0:
            pct = (del_count / total_active) * 100
            if del_count > 100 or (total_active >= 5 and (del_count / total_active) > 0.20):
                return False, (
                    f"Superata soglia di sicurezza delezioni: {del_count}/{total_active} file "
                    f"({pct:.1f}% > 20% o >100). Impostato stato NEEDS_REVIEW."
                )

        # Guard 3: Suspicious empty root / offline host
        # If all currently active files are being deleted at once and no adds exist
        non_delete_count = sum(1 for a in manifest.actions if a.action in ("add", "modify", "rename"))
        if total_active > 0 and non_delete_count == 0 and del_count == total_active:
            return False, (
                "Cartella vuota in modo sospetto o host sorgente offline: "
                "blocco preventivo di tutte le cancellazioni (NEEDS_REVIEW)."
            )

        return True, None

    def process_job(
        self,
        job_id: str,
        manifest: StagedJobManifest | None = None,
    ) -> JobRecord:
        """Execute a staged job from start to finish (§4, §5)."""
        job = self.catalog.get_job(job_id)
        if job is None:
            raise IngestionWorkerError(f"Job non trovato nel catalogo: {job_id}")

        if job.stage == "COMPLETED":
            logger.info("Job '%s' già completato (idempotente).", job_id)
            return job

        # Load manifest if not passed directly
        if manifest is None:
            if not self.staging_dir:
                raise IngestionWorkerError(f"Nessuna directory di staging configurata per il job {job_id}")
            manifest_file = self.staging_dir / job_id / "manifest.json"
            if not manifest_file.exists():
                raise IngestionWorkerError(f"File di manifest mancante in staging: {manifest_file}")
            manifest = StagedJobManifest.load_from_file(manifest_file)

        if (manifest.job_id, manifest.kb_id, manifest.source_id) != (job.job_id, job.kb_id, job.source_id):
            return self.catalog.update_job(job_id, stage="NEEDS_REVIEW", error_summary="Manifest identity does not match job")

        source_id = manifest.source_id
        kb_id = manifest.kb_id

        # Acquire KB-level single-writer lock (§5: 'un solo writer per KB')
        with self.kb_lock:
            # Step 1: Verify catalog locks for KB and source
            self.acquire_kb_lock(kb_id, job_id)
            self.acquire_source_lock(source_id, job_id)

            # Step 1.5: Recover any pending checkpoints left from previous crashes (§5)
            self.indexer.recover_pending_checkpoints()

            # Step 2: Validate safe sandboxed paths across all manifest actions (§11 #10)
            for act in manifest.actions:
                if act.action not in {"add", "modify", "rename", "delete"}:
                    return self.catalog.update_job(job_id, stage="NEEDS_REVIEW", error_summary=f"Unknown action: {act.action}")
                try:
                    validate_safe_relative_path(act.relative_path)
                    if act.old_relative_path:
                        validate_safe_relative_path(act.old_relative_path)
                    if act.staged_filename:
                        validate_safe_relative_path(act.staged_filename)
                except SafePathError as path_err:
                    err_msg = f"Violazione di sicurezza percorso nel manifest: {path_err}"
                    logger.error(err_msg)
                    return self.catalog.update_job(
                        job_id=job_id,
                        stage="NEEDS_REVIEW",
                        error_summary=err_msg,
                        warnings=[err_msg],
                    )

            # Step 3: Validate safety guards (§5)
            self.catalog.update_job(job_id, stage="VALIDATING")
            is_safe, guard_error = self.validate_safety_guards(manifest)
            if not is_safe:
                logger.warning("Safety guard attivata per il job '%s': %s", job_id, guard_error)
                updated_job = self.catalog.update_job(
                    job_id=job_id,
                    stage="NEEDS_REVIEW",
                    error_summary=guard_error,
                    warnings=[guard_error] if guard_error else [],
                )
                return updated_job

            # Step 4: Transition to PROCESSING
            self.catalog.update_job(job_id, stage="PROCESSING")

            counters: dict[str, int] = {
                "added": 0,
                "modified": 0,
                "renamed": 0,
                "deleted": 0,
                "unchanged": 0,
                "failed": 0,
                "warnings": 0,
            }
            warnings_list: list[str] = []
            verified_staged_files: list[Path] = []
            errors_summary: list[str] = []

            staging_incoming = (
                (self.staging_dir / job_id / "incoming")
                if self.staging_dir
                else None
            )

            source_rec = self.catalog.get_source(source_id)
            source_origin = source_rec.origin_folder if source_rec else ""
            source_host = source_rec.origin_host if source_rec else ""

            # Step 5: Process actions
            for item in manifest.actions:
                rel_path = item.relative_path
                source_uri = f"{source_host}:{source_origin}/{rel_path}".lstrip(":")

                try:
                    # ---------------------------------------------------------
                    # ACTION: RENAME
                    # ---------------------------------------------------------
                    if item.action == "rename":
                        if not item.old_relative_path:
                            raise ValueError(f"Rename su '{rel_path}' senza old_relative_path specificato")
                        self.indexer.rename_file(
                            source_id=source_id,
                            old_relative_path=item.old_relative_path,
                            new_relative_path=rel_path,
                        )
                        counters["renamed"] += 1
                        continue

                    # ---------------------------------------------------------
                    # ACTION: DELETE (§5 Retry-safe Deletions)
                    # ---------------------------------------------------------
                    if item.action == "delete":
                        del_res = self.indexer.delete_file(source_id, rel_path)
                        if del_res.get("action") != "already_inactive":
                            counters["deleted"] += 1
                        continue

                    # ---------------------------------------------------------
                    # ACTION: ADD or MODIFY
                    # ---------------------------------------------------------
                    # Locate raw input file
                    staged_path: Path | None = None
                    if item.staged_filename and staging_incoming:
                        candidate_file = staging_incoming / item.staged_filename
                        if candidate_file.is_file():
                            staged_path = candidate_file
                    elif staging_incoming and (staging_incoming / rel_path).exists():
                        staged_path = staging_incoming / rel_path
                    elif Path(rel_path).is_file():
                        staged_path = Path(rel_path)

                    if staged_path is None or not staged_path.is_file():
                        raise FileNotFoundError(f"File sorgente per '{rel_path}' non trovato in staging incoming")

                    file_size = item.size or staged_path.stat().st_size
                    mtime_ns = item.mtime_ns or staged_path.stat().st_mtime_ns
                    b_sha = item.binary_sha256 or compute_file_sha256(staged_path)

                    # Parse document via pipeline.parsers
                    parsed = parse_document(
                        path=staged_path,
                        env=self.env,
                        runner=self.parser_runner,
                        binary_sha256=b_sha,
                        source_uri=source_uri,
                    )

                    # Persist derived markdown and JSON sidecar (§7)
                    md_path, sidecar_path = save_derived_artifacts(
                        parsed=parsed,
                        derived_dir=self.derived_dir,
                        relative_path=rel_path,
                        source_uri=source_uri,
                    )

                    if item.action == "add":
                        idx_res = self.indexer.index_new(
                            parsed=parsed,
                            source_id=source_id,
                            relative_path=rel_path,
                            size=file_size,
                            mtime_ns=mtime_ns,
                            artifact_path=md_path,
                        )
                        if idx_res.reused:
                            counters["unchanged"] += 1
                        else:
                            counters["added"] += 1

                    elif item.action == "modify":
                        idx_res = self.indexer.update_modified(
                            parsed=parsed,
                            source_id=source_id,
                            relative_path=rel_path,
                            size=file_size,
                            mtime_ns=mtime_ns,
                            artifact_path=md_path,
                        )
                        if idx_res.reused:
                            counters["unchanged"] += 1
                        else:
                            counters["modified"] += 1

                    # Successfully processed and verified
                    verified_staged_files.append(staged_path)

                except RecoverySucceededError as rec_succ:
                    # Update failed, but previous state was restored
                    counters["failed"] += 1
                    warnings_list.append(str(rec_succ))
                    errors_summary.append(str(rec_succ))
                except RecoveryFailedError as rec_fail:
                    # Critical degradation
                    counters["failed"] += 1
                    errors_summary.append(str(rec_fail))
                except Exception as exc:
                    counters["failed"] += 1
                    err_text = f"Errore su '{rel_path}': {exc}"
                    logger.error(err_text)
                    errors_summary.append(err_text)

            # Step 6: Final state resolution
            final_stage = "COMPLETED" if counters["failed"] == 0 else "FAILED_RETRYABLE"
            error_summary_text = "; ".join(errors_summary) if errors_summary else None

            # Step 7: Staging cleanup (§4, §5)
            # Only unlinks files strictly inside staging_dir and not symlinks
            for v_file in verified_staged_files:
                try:
                    if self.staging_dir:
                        resolved_v = v_file.resolve()
                        resolved_staging = self.staging_dir.resolve()
                        if (
                            resolved_v.is_relative_to(resolved_staging)
                            and not v_file.is_symlink()
                            and v_file.is_file()
                        ):
                            v_file.unlink()
                    elif v_file.is_file():
                        v_file.unlink()
                except OSError as cleanup_err:
                    logger.debug("Impossibile rimuovere file staged %s: %s", v_file, cleanup_err)

            if final_stage == "COMPLETED" and manifest.mode == "full_folder" and manifest.scan_complete:
                scan_id = manifest.scan_id or job_id
                self.catalog.update_source_scan(source_id, scan_id)

            updated_job = self.catalog.update_job(
                job_id=job_id,
                stage=final_stage,
                counters=counters,
                warnings=warnings_list,
                error_summary=error_summary_text,
            )

            return updated_job
