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
    StagedAction,
    StagedJobManifest,
)
from pipeline.parsers import (
    ParsedDocument,
    compute_file_sha256,
    compute_text_sha256,
    get_file_category,
    parse_document,
    save_derived_artifacts,
)

__all__ = [
    "compute_file_sha256",
    "compute_text_sha256",
    "get_file_category",
    "parse_document",
    "save_derived_artifacts",
    "ParsedDocument",
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
    "StagedAction",
    "StagedJobManifest",
]
