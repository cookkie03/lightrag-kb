"""Unit and integration tests for mcp/kb_router.py according to §6 and §11 of the monocentric plan.

Tests:
1. FastMCP server creation and tool definitions verification (all §6 tools, no upload/code execution tools).
2. list_kbs tool: multi-KB inventory, state transitions (READY, OFFLINE, DEGRADED, OFFLINE (DEGRADED)),
   accurate source and document counts from Catalog.
3. Multi-KB routing & isolation: kb-alpha on port 9621 vs kb-beta on port 9622; no cross-KB leakage.
4. Explicit kb parameter enforcement: structured error KB_NOT_FOUND for unknown KB; strictly no fallback.
5. Structured errors on backend offline: BACKEND_OFFLINE with port and kb info; no unhandled crash.
6. ask tool: routes to correct KB, preserves BOTH response and references (§6), signals degraded status,
   normalizes strategy (balanced->mix, theme_focus->global, etc.), validates context parameters.
7. retrieve_context tool: routes to /query/data, returns structured chunks/entities/provenance.
8. find_documents tool: granular inventory using SQLite Catalog, pagination, status filtering, source filtering.
9. job_status tool: progress counters, stage, error summaries, retry/review flags, JOB_NOT_FOUND handling.
10. evidence tool: multi-source provenance resolution (canonical doc ID and lightrag doc ID),
    extracted JSON artifact chunk resolution, markdown artifact resolution, DOCUMENT_NOT_FOUND handling.
11. discover_duplicates tool: exact binary SHA256 clusters from Catalog (§8 A1);
    FEATURE_NOT_ENABLED for unimplemented modes (semantic, visual, audio).
12. compare_folders tool: FEATURE_NOT_ENABLED structured error (no hallucinated results).
13. FastMCP tool invocation via await mcp.call_tool(...).
14. Stdout JSON-RPC cleanliness: no stdout leakage during execution.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "mcp") not in sys.path:
    sys.path.insert(0, str(ROOT / "mcp"))

import kb_router
from kb_router import (
    KBRouter,
    STRATEGY_TO_MODE,
    _normalize_context,
    _normalize_strategy,
    build_mcp_server,
)
from storage.catalog import Catalog, generate_canonical_doc_id


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def mock_kb_environment(tmp_path: Path):
    """Create two isolated KBs with independent catalogs and source directories."""
    kb_root = tmp_path / "kb"
    kb_root.mkdir(parents=True, exist_ok=True)

    alpha_dir = kb_root / "kb-alpha"
    beta_dir = kb_root / "kb-beta"
    alpha_dir.mkdir(parents=True, exist_ok=True)
    beta_dir.mkdir(parents=True, exist_ok=True)

    # Derived artifacts directories
    alpha_derived = alpha_dir / "derived"
    alpha_derived.mkdir(parents=True, exist_ok=True)

    registry_data = {
        "kbs": [
            {
                "name": "kb-alpha",
                "port": 9621,
                "data_dir": str(alpha_dir),
                "source_dir": str(tmp_path / "src_alpha"),
                "derived_dir": str(alpha_derived),
                "rag_storage": str(alpha_dir / "rag_storage"),
                "exclude": [],
            },
            {
                "name": "kb-beta",
                "port": 9622,
                "data_dir": str(beta_dir),
                "source_dir": str(tmp_path / "src_beta"),
                "derived_dir": str(beta_dir / "derived"),
                "rag_storage": str(beta_dir / "rag_storage"),
                "exclude": [],
            },
        ]
    }

    session = requests.Session()
    router = KBRouter(registry=registry_data, kb_root=kb_root, session=session)
    mcp_app = build_mcp_server(router)

    return {
        "kb_root": kb_root,
        "alpha_dir": alpha_dir,
        "beta_dir": beta_dir,
        "alpha_derived": alpha_derived,
        "registry": registry_data,
        "session": session,
        "router": router,
        "mcp": mcp_app,
    }


# -----------------------------------------------------------------------------
# 1. MCP Tool Registration & Surface
# -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mcp_server_tools_registered(mock_kb_environment):
    mcp_app = mock_kb_environment["mcp"]
    tools = await mcp_app.list_tools()
    tool_names = {t.name for t in tools}

    expected_tools = {
        "list_kbs",
        "ask",
        "retrieve_context",
        "find_documents",
        "job_status",
        "evidence",
        "discover_duplicates",
        "compare_folders",
    }
    assert expected_tools.issubset(tool_names)

    # Negative assertion: forbidden tools according to §6
    forbidden_tools = {
        "upload_file",
        "upload_bytes",
        "execute_code",
        "execute_plan",
        "delete_file",
    }
    assert not (forbidden_tools & tool_names)


# -----------------------------------------------------------------------------
# 2. list_kbs Tool & Status Transitions
# -----------------------------------------------------------------------------


def test_list_kbs_offline_when_servers_down(mock_kb_environment, monkeypatch):
    router: KBRouter = mock_kb_environment["router"]
    monkeypatch.setattr(router, "check_server_health", lambda port: False)
    res_raw = router.list_kbs()
    res = json.loads(res_raw)

    assert res["total"] == 2
    names = [kb["name"] for kb in res["kbs"]]
    assert "kb-alpha" in names
    assert "kb-beta" in names

    # With no mock HTTP servers, status should be OFFLINE
    for kb in res["kbs"]:
        assert kb["status"] == "OFFLINE"
        assert kb["degraded"] is False


def test_list_kbs_ready_when_server_up(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health") as mock_health:
        # Port 9621 is healthy, port 9622 is down
        mock_health.side_effect = lambda port, **_: port == 9621

        res = json.loads(router.list_kbs())
        kb_alpha = next(k for k in res["kbs"] if k["name"] == "kb-alpha")
        kb_beta = next(k for k in res["kbs"] if k["name"] == "kb-beta")

        assert kb_alpha["status"] == "READY"
        assert kb_alpha["degraded"] is False
        assert kb_beta["status"] == "OFFLINE"
        assert kb_beta["degraded"] is False


def test_list_kbs_degraded_status_detection(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    # Insert a failed job into kb-alpha's catalog
    with router.get_catalog("kb-alpha") as cat:
        cat.create_job(
            job_id="job_failed_1",
            kb_id="kb-alpha",
            stage="FAILED_RETRYABLE",
            error_summary="Timeout during Docling conversion",
        )

    with patch.object(router, "check_server_health", return_value=True):
        res = json.loads(router.list_kbs())
        kb_alpha = next(k for k in res["kbs"] if k["name"] == "kb-alpha")
        kb_beta = next(k for k in res["kbs"] if k["name"] == "kb-beta")

        assert kb_alpha["status"] == "DEGRADED"
        assert kb_alpha["degraded"] is True
        assert "FAILED_RETRYABLE" in kb_alpha["degraded_reason"]

        assert kb_beta["status"] == "READY"
        assert kb_beta["degraded"] is False


def test_list_kbs_offline_and_degraded_combined(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with router.get_catalog("kb-alpha") as cat:
        cat.create_job(
            job_id="job_bad",
            kb_id="kb-alpha",
            stage="FAILED_FINAL",
            error_summary="Fatal corrupt artifact",
        )

    # Server down
    with patch.object(router, "check_server_health", return_value=False):
        res = json.loads(router.list_kbs())
        kb_alpha = next(k for k in res["kbs"] if k["name"] == "kb-alpha")
        assert kb_alpha["status"] == "OFFLINE (DEGRADED)"
        assert kb_alpha["degraded"] is True


def test_list_kbs_reports_source_and_document_counts(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with router.get_catalog("kb-alpha") as cat:
        cat.add_source(source_id="src_1", name="Alpha Source 1")
        cat.add_source(source_id="src_2", name="Alpha Source 2")
        cat.upsert_document(
            canonical_doc_id="doc_01",
            derived_hash="hash1",
            lightrag_status="PROCESSED",
        )
        cat.record_file(
            source_id="src_1",
            relative_path="file1.txt",
            binary_sha256="hash1",
            size=100,
            mtime_ns=1000,
            canonical_doc_id="doc_01",
        )

    res = json.loads(router.list_kbs())
    kb_alpha = next(k for k in res["kbs"] if k["name"] == "kb-alpha")
    assert kb_alpha["sources_count"] == 2
    assert kb_alpha["documents_count"] == 1


# -----------------------------------------------------------------------------
# 3. Explicit KB Parameter & No Fallback
# -----------------------------------------------------------------------------


def test_missing_or_unknown_kb_returns_structured_error(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]
    bad_kb = "non-existent-kb"

    tools_to_test = [
        lambda: router.ask(kb=bad_kb, question="What is X?"),
        lambda: router.retrieve_context(kb=bad_kb, question="What is X?"),
        lambda: router.find_documents(kb=bad_kb),
        lambda: router.job_status(kb=bad_kb, job_id="job-1"),
        lambda: router.evidence(kb=bad_kb, doc_id="doc-1"),
        lambda: router.discover_duplicates(kb=bad_kb),
        lambda: router.compare_folders(kb=bad_kb, left_source="s1", right_source="s2"),
    ]

    for tool_call in tools_to_test:
        out = json.loads(tool_call())
        assert out["error"] == "KB_NOT_FOUND"
        assert out["kb"] == bad_kb
        assert "not found" in out["message"].lower() or "non trovata" in out["message"].lower()


# -----------------------------------------------------------------------------
# 4. Backend Offline Handling
# -----------------------------------------------------------------------------


def test_backend_offline_structured_error(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health", return_value=False):
        res_ask = json.loads(router.ask(kb="kb-alpha", question="test question"))
        assert res_ask["error"] == "BACKEND_OFFLINE"
        assert res_ask["kb"] == "kb-alpha"
        assert res_ask["port"] == 9621

        res_ctx = json.loads(router.retrieve_context(kb="kb-alpha", question="test context"))
        assert res_ctx["error"] == "BACKEND_OFFLINE"
        assert res_ctx["kb"] == "kb-alpha"
        assert res_ctx["port"] == 9621


def test_backend_connection_exception_handled_cleanly(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health", return_value=True):
        with patch.object(router.session, "post", side_effect=requests.exceptions.ConnectionError("Connection refused")):
            res = json.loads(router.ask(kb="kb-alpha", question="query"))
            assert res["error"] == "BACKEND_OFFLINE"
            assert "Connection failed" in res["message"] or "Connessione fallita" in res["message"]


# -----------------------------------------------------------------------------
# 5. ask Tool: Routing, References Preservation, Strategies & Degradation
# -----------------------------------------------------------------------------


def test_ask_routes_to_correct_port(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health", return_value=True):
        with patch.object(router.session, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "response": "Alpha answer",
                "references": [{"source_id": "src-alpha", "content": "alpha snippet"}],
            }
            mock_post.return_value = mock_resp

            # Query kb-alpha
            router.ask(kb="kb-alpha", question="Question for alpha")
            assert mock_post.call_args[0][0] == "http://127.0.0.1:9621/query"

            # Query kb-beta
            router.ask(kb="kb-beta", question="Question for beta")
            assert mock_post.call_args[0][0] == "http://127.0.0.1:9622/query"


def test_ask_preserves_both_response_and_references(mock_kb_environment):
    """Verify §6 fix: upstream ask lost references by restricting to response."""
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health", return_value=True):
        with patch.object(router.session, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "response": "Synthesized knowledge",
                "references": [
                    {"doc_id": "doc_1", "text": "evidence 1"},
                    {"doc_id": "doc_2", "text": "evidence 2"},
                ],
                "query_time": 0.42,
            }
            mock_post.return_value = mock_resp

            res = json.loads(router.ask(kb="kb-alpha", question="Explain X"))
            assert res["response"] == "Synthesized knowledge"
            assert len(res["references"]) == 2
            assert res["references"][0]["doc_id"] == "doc_1"
            assert res["query_time"] == 0.42


def test_ask_plain_string_backend_response(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health", return_value=True):
        with patch.object(router.session, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.side_effect = json.JSONDecodeError("Expecting value", "", 0)
            mock_resp.text = "Direct text response from server"
            mock_post.return_value = mock_resp

            res = json.loads(router.ask(kb="kb-alpha", question="Explain X"))
            assert res["response"] == "Direct text response from server"
            assert res["references"] == []


def test_ask_normalizes_strategies(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health", return_value=True):
        with patch.object(router.session, "post") as mock_post:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {"response": "ok"}
            mock_post.return_value = mock_resp

            mappings = [
                ("balanced", "mix"),
                ("entity_focus", "local"),
                ("theme_focus", "global"),
                ("vector_only", "naive"),
                ("llm_only", "bypass"),
                ("hybrid", "hybrid"),
                ("mix", "mix"),
                ("local", "local"),
            ]
            for strat, expected_mode in mappings:
                router.ask(kb="kb-alpha", question="q", strategy=strat)
                sent_payload = mock_post.call_args[1]["json"]
                assert sent_payload["mode"] == expected_mode


def test_ask_invalid_strategy_returns_structured_error(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]
    res = json.loads(router.ask(kb="kb-alpha", question="q", strategy="invalid_strategy_name"))
    assert res["error"] == "INVALID_STRATEGY"
    assert "invalid_strategy_name" in res["message"]


def test_ask_context_normalization_and_validation(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health", return_value=True):
        with patch.object(router.session, "post") as mock_post:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {"response": "ok"}
            mock_post.return_value = mock_resp

            valid_ctx = {
                "top_k": 10,
                "rerank": True,
                "prompt_override": "Custom prompt",
            }
            router.ask(kb="kb-alpha", question="q", context=valid_ctx)
            sent_payload = mock_post.call_args[1]["json"]
            assert sent_payload["top_k"] == 10
            assert sent_payload["enable_rerank"] is True
            assert sent_payload["user_prompt"] == "Custom prompt"

            # Invalid context field
            invalid_res = json.loads(
                router.ask(kb="kb-alpha", question="q", context={"unsupported_key": 123})
            )
            assert invalid_res["error"] == "INVALID_CONTEXT"
            assert "unsupported_key" in invalid_res["message"]


def test_ask_signals_degraded_warning_explicitly(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with router.get_catalog("kb-alpha") as cat:
        cat.create_job(
            job_id="job_err",
            kb_id="kb-alpha",
            stage="NEEDS_REVIEW",
            error_summary="Conflict in extracted claims",
        )

    with patch.object(router, "check_server_health", return_value=True):
        with patch.object(router.session, "post") as mock_post:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {"response": "Alpha answer", "references": []}
            mock_post.return_value = mock_resp

            res = json.loads(router.ask(kb="kb-alpha", question="q"))
            assert res["degraded"] is True
            assert "DEGRADED" in res["warning"]


# -----------------------------------------------------------------------------
# 6. retrieve_context Tool
# -----------------------------------------------------------------------------


def test_retrieve_context_success(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with patch.object(router, "check_server_health", return_value=True):
        with patch.object(router.session, "post") as mock_post:
            mock_resp = MagicMock(status_code=200)
            raw_context_data = {
                "entities": [{"name": "Python", "description": "Programming language"}],
                "relationships": [{"src": "Python", "tgt": "Debian", "relation": "runs on"}],
                "chunks": [{"chunk_id": "c1", "content": "Python on Debian"}],
            }
            mock_resp.json.return_value = raw_context_data
            mock_post.return_value = mock_resp

            res = json.loads(router.retrieve_context(kb="kb-alpha", question="q"))
            assert mock_post.call_args[0][0] == "http://127.0.0.1:9621/query/data"
            assert len(res["entities"]) == 1
            assert len(res["relationships"]) == 1
            assert len(res["chunks"]) == 1


# -----------------------------------------------------------------------------
# 7. find_documents Tool (Granular Catalog Inventory)
# -----------------------------------------------------------------------------


def test_find_documents_pagination_and_filters(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with router.get_catalog("kb-alpha") as cat:
        cat.add_source(source_id="src_mac", name="MacBook")
        cat.add_source(source_id="src_nas", name="NAS")

        # Create 3 documents
        for i in range(1, 4):
            doc_id = f"doc_{i}"
            cat.upsert_document(
                canonical_doc_id=doc_id,
                derived_hash=f"hash_{i}",
                lightrag_status="PROCESSED" if i < 3 else "FAILED",
            )
            cat.record_file(
                source_id="src_mac" if i <= 2 else "src_nas",
                relative_path=f"folder_{i}/file_{i}.txt",
                binary_sha256=f"hash_{i}",
                size=100 * i,
                mtime_ns=1000 * i,
                canonical_doc_id=doc_id,
            )

    # Page 1, page_size 2
    res_p1 = json.loads(router.find_documents(kb="kb-alpha", page=1, page_size=2))
    assert res_p1["total"] == 3
    assert res_p1["total_pages"] == 2
    assert len(res_p1["items"]) == 2

    # Filter by status="FAILED"
    res_failed = json.loads(router.find_documents(kb="kb-alpha", status="FAILED"))
    assert res_failed["total"] == 1
    assert res_failed["items"][0]["canonical_doc_id"] == "doc_3"

    # Filter by source="src_mac"
    res_src = json.loads(router.find_documents(kb="kb-alpha", source="src_mac"))
    assert res_src["total"] == 2

    # Filter by path_prefix="folder_1"
    res_path = json.loads(router.find_documents(kb="kb-alpha", path_prefix="folder_1"))
    assert res_path["total"] == 1
    assert res_path["items"][0]["canonical_doc_id"] == "doc_1"


# -----------------------------------------------------------------------------
# 8. job_status Tool
# -----------------------------------------------------------------------------


def test_job_status_retrieval(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with router.get_catalog("kb-alpha") as cat:
        cat.create_job(
            job_id="job_sync_100",
            kb_id="kb-alpha",
            stage="PROCESSING",
            counters={"added": 12, "modified": 3, "unchanged": 50},
            warnings=["File unreadable.log skipped"],
        )

    res = json.loads(router.job_status(kb="kb-alpha", job_id="job_sync_100"))
    assert res["job_id"] == "job_sync_100"
    assert res["stage"] == "PROCESSING"
    assert res["progress"]["added"] == 12
    assert len(res["warnings"]) == 1
    assert res["retryable"] is False
    assert res["needs_review"] is False


def test_job_status_not_found(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]
    res = json.loads(router.job_status(kb="kb-alpha", job_id="non_existent_job"))
    assert res["error"] == "JOB_NOT_FOUND"
    assert res["job_id"] == "non_existent_job"


# -----------------------------------------------------------------------------
# 9. evidence Tool (Content, Provenance & Chunks)
# -----------------------------------------------------------------------------


def test_evidence_resolution_multi_source_and_json_artifact(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]
    derived_dir: Path = mock_kb_environment["alpha_derived"]

    # Create dummy Docling sidecar JSON artifact
    artifact_file = derived_dir / "doc_test.json"
    artifact_file.write_text(
        json.dumps({
            "text": "Full extracted document text.",
            "chunks": [
                {
                    "id": "chunk_01",
                    "text": "Chunk 1 content regarding financial metrics.",
                    "page": 4,
                    "table_cells": ["A1: Revenue", "B1: 100M"],
                },
                {
                    "id": "chunk_02",
                    "text": "Chunk 2 content regarding operational risks.",
                    "page": 5,
                    "timecode": "00:14:22",
                },
            ],
            "metadata": {"docling_version": "2.0"},
        })
    )

    with router.get_catalog("kb-alpha") as cat:
        cat.add_source(
            source_id="src_mac",
            name="MacBook",
            origin_host="macbook-air",
            origin_folder="/Users/luca/Docs",
        )
        cat.add_source(
            source_id="src_nas",
            name="NAS",
            origin_host="nas-box",
            origin_folder="/shares/data",
        )

        cat.upsert_document(
            canonical_doc_id="doc_financial_q3",
            derived_hash="hash_financial",
            extracted_artifact_path=str(artifact_file),
            lightrag_doc_id="lr_fin_q3",
            lightrag_status="PROCESSED",
        )

        # Occurrence on Mac
        cat.record_file(
            source_id="src_mac",
            relative_path="reports/q3.pdf",
            binary_sha256="bin_sha_q3",
            size=1024,
            mtime_ns=5000,
            canonical_doc_id="doc_financial_q3",
        )
        # Occurrence on NAS (renamed / mirrored)
        cat.record_file(
            source_id="src_nas",
            relative_path="archive/2026/q3_final.pdf",
            binary_sha256="bin_sha_q3",
            size=1024,
            mtime_ns=5000,
            canonical_doc_id="doc_financial_q3",
        )

    # 1. Resolve by canonical_doc_id with specific chunk_id
    res_raw = router.evidence(
        kb="kb-alpha", doc_id="doc_financial_q3", chunk_id="chunk_01"
    )
    res = json.loads(res_raw)

    assert res["canonical_doc_id"] == "doc_financial_q3"
    assert len(res["occurrences"]) == 2
    uris = [occ["source_uri"] for occ in res["occurrences"]]
    assert "macbook-air:/Users/luca/Docs/reports/q3.pdf" in uris
    assert "nas-box:/shares/data/archive/2026/q3_final.pdf" in uris

    # Verify chunk metadata resolution
    assert res["chunk"]["id"] == "chunk_01"
    assert "financial metrics" in res["chunk"]["text"]
    assert res["chunk"]["page"] == 4
    assert "A1: Revenue" in res["chunk"]["table_cells"]

    # 2. Resolve by LightRAG doc ID
    res_by_lr = json.loads(router.evidence(kb="kb-alpha", doc_id="lr_fin_q3"))
    assert res_by_lr["canonical_doc_id"] == "doc_financial_q3"
    assert res_by_lr["content_snippet"] == "Full extracted document text."


def test_evidence_markdown_artifact(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]
    derived_dir: Path = mock_kb_environment["alpha_derived"]

    md_file = derived_dir / "notes.md"
    md_file.write_text("# Meeting Notes\nDiscussion on SQLite and FastMCP architecture.")

    with router.get_catalog("kb-alpha") as cat:
        cat.add_source(source_id="src_mac", name="Mac")
        cat.upsert_document(
            canonical_doc_id="doc_md_01",
            extracted_artifact_path=str(md_file),
            lightrag_status="PROCESSED",
        )
        cat.record_file(
            source_id="src_mac",
            relative_path="notes.md",
            binary_sha256="bin_sha_notes",
            size=500,
            mtime_ns=3000,
            canonical_doc_id="doc_md_01",
        )

    res = json.loads(router.evidence(kb="kb-alpha", doc_id="doc_md_01"))
    assert res["content_snippet"] == "# Meeting Notes\nDiscussion on SQLite and FastMCP architecture."


def test_evidence_document_not_found(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]
    res = json.loads(router.evidence(kb="kb-alpha", doc_id="doc_missing"))
    assert res["error"] == "DOCUMENT_NOT_FOUND"
    assert res["doc_id"] == "doc_missing"


# -----------------------------------------------------------------------------
# 10. discover_duplicates Tool (A1 Exact SHA256 & Unimplemented Modes)
# -----------------------------------------------------------------------------


def test_discover_duplicates_exact_clusters(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    with router.get_catalog("kb-alpha") as cat:
        cat.add_source(source_id="src_a", name="Source A")
        cat.add_source(source_id="src_b", name="Source B")

        # Two identical files with identical binary SHA256 across sources
        shared_sha = "shared_sha256_abcdef1234567890"
        cat.upsert_document(canonical_doc_id="doc_shared", derived_hash=shared_sha)
        cat.record_file(
            source_id="src_a",
            relative_path="sub/duplicate_a.pdf",
            binary_sha256=shared_sha,
            size=2048,
            mtime_ns=8000,
            canonical_doc_id="doc_shared",
        )
        cat.record_file(
            source_id="src_b",
            relative_path="other/duplicate_b.pdf",
            binary_sha256=shared_sha,
            size=2048,
            mtime_ns=8000,
            canonical_doc_id="doc_shared",
        )

    res = json.loads(router.discover_duplicates(kb="kb-alpha", kind="exact"))
    assert res["kind"] == "exact"
    assert res["total_clusters"] == 1
    cluster = res["clusters"][0]
    assert cluster["binary_sha256"] == shared_sha
    assert len(cluster["occurrences"]) == 2


def test_discover_duplicates_unsupported_kind_returns_feature_not_enabled(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]

    unsupported_modes = ["semantic", "visual", "audio", "video"]
    for mode in unsupported_modes:
        res = json.loads(router.discover_duplicates(kb="kb-alpha", kind=mode))
        assert res["error"] == "FEATURE_NOT_ENABLED"
        assert f"discover_duplicates:{mode}" in res["feature"]
        assert "not currently enabled" in res["message"] or "non è attualmente abilitata" in res["message"]


# -----------------------------------------------------------------------------
# 11. compare_folders Tool (FEATURE_NOT_ENABLED per §6)
# -----------------------------------------------------------------------------


def test_compare_folders_returns_feature_not_enabled(mock_kb_environment):
    router: KBRouter = mock_kb_environment["router"]
    res = json.loads(
        router.compare_folders(
            kb="kb-alpha",
            left_source="src_mac",
            right_source="src_nas",
            mode="novelty",
        )
    )
    assert res["error"] == "FEATURE_NOT_ENABLED"
    assert res["feature"] == "compare_folders"
    assert "A6 knowledge novelty" in res["message"]


# -----------------------------------------------------------------------------
# 12. FastMCP Tool Execution via await mcp.call_tool(...)
# -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_fastmcp_async_call_tool_interface(mock_kb_environment):
    mcp_app = mock_kb_environment["mcp"]
    router: KBRouter = mock_kb_environment["router"]

    # 1. list_kbs through FastMCP
    res_list = await mcp_app.call_tool("list_kbs", {})
    # In fastmcp, result is wrapped in TextContent
    text = res_list.content[0].text
    parsed = json.loads(text)
    assert parsed["total"] == 2

    # 2. find_documents through FastMCP
    with router.get_catalog("kb-alpha") as cat:
        cat.add_source(source_id="src_main", name="Main")
        cat.upsert_document(canonical_doc_id="doc_async_test", derived_hash="dhash")
        cat.record_file(
            source_id="src_main",
            relative_path="file.txt",
            binary_sha256="dhash",
            size=120,
            mtime_ns=4000,
            canonical_doc_id="doc_async_test",
        )

    res_find = await mcp_app.call_tool(
        "find_documents", {"kb": "kb-alpha", "page": 1, "page_size": 10}
    )
    find_data = json.loads(res_find.content[0].text)
    assert find_data["total"] == 1
    assert find_data["items"][0]["canonical_doc_id"] == "doc_async_test"

    # 3. compare_folders through FastMCP
    res_cmp = await mcp_app.call_tool(
        "compare_folders",
        {"kb": "kb-alpha", "left_source": "s1", "right_source": "s2"},
    )
    cmp_data = json.loads(res_cmp.content[0].text)
    assert cmp_data["error"] == "FEATURE_NOT_ENABLED"


# -----------------------------------------------------------------------------
# 13. Stdout Cleanliness for SSH stdio JSON-RPC (§11 Test 7)
# -----------------------------------------------------------------------------


def test_stdout_cleanliness(mock_kb_environment, capsys):
    """Ensure no logging or diagnostics bleed onto stdout (reserved for JSON-RPC)."""
    router: KBRouter = mock_kb_environment["router"]

    # Execute multiple tools
    router.list_kbs()
    router.find_documents(kb="kb-alpha")
    router.discover_duplicates(kb="kb-alpha")
    router.compare_folders(kb="kb-alpha", left_source="a", right_source="b")

    captured = capsys.readouterr()
    assert captured.out == "", f"Polluted stdout detected: {captured.out!r}"
