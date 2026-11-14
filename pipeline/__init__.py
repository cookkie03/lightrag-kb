"""Pipeline package for LightRAG-KB.

Implements parsers, indexer, and ingestion worker according to §5 and §7 of:
`LightRAG-KB · Piano monolitico production-grade (Debian + MCP + one-shot sync)`
"""

from __future__ import annotations

from pipeline.indexer import (
    DocumentIndexingFailedError,
    DocumentIndexingTimeoutError,
    LightRAGClient,
    LightRAGConflictError,
    LightRAGError,
    ProtectedIndexer,
    RecoveryFailedError,
    TransientConflictError,
    UnverifiedDuplicateError,
)
from pipeline.ingestion_worker import (
    IngestionWorker,
    JobLockedError,
    SafetyGuardViolationError,
    create_staged_action,
    create_staged_job_manifest,
)
from pipeline.parsers import (
    compute_file_sha256,
    compute_text_sha256,
    create_parsed_document,
    get_file_category,
    parse_document,
    save_derived_artifacts,
)

__all__ = [
    "compute_file_sha256",
    "compute_text_sha256",
    "create_parsed_document",
    "create_staged_action",
    "create_staged_job_manifest",
    "get_file_category",
    "parse_document",
    "save_derived_artifacts",
    "LightRAGClient",
    "LightRAGError",
    "LightRAGConflictError",
    "TransientConflictError",
    "UnverifiedDuplicateError",
    "DocumentIndexingFailedError",
    "DocumentIndexingTimeoutError",
    "RecoveryFailedError",
    "ProtectedIndexer",
    "IngestionWorker",
    "JobLockedError",
    "SafetyGuardViolationError",
]