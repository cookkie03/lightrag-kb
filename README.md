# KnowledgeOS (`knowledge-os`)

> **Local-first Semantic Knowledge Operating System & Agent Context Engine.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP Compliant](https://img.shields.io/badge/MCP-FastMCP-green.svg)](https://modelcontextprotocol.io/)
[![Architecture: Local-First](https://img.shields.io/badge/Architecture-Local--First-purple.svg)]()

`knowledge-os` is a local-first platform that turns unstructured documents, media, codebases, and conversations into a unified semantic knowledge mesh. Built for autonomous AI coding agents (Claude Code, Cursor, Codex) and human users, it manages multi-tenant knowledge bases with atomic persistence, deep multimodal ingestion, cross-KB routing, agent memory consolidation ("dreaming"), and zero-re-embedding renames.

---

## Architecture Overview

```text
                                 [ Autonomous Coding Agents ]
                           (Claude Code / Cursor / Codex / Obsidian)
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      │ Stdio / SSE Transport                        │ Tailscale Funnel (HTTPS)
                      ▼                                               ▼
          ┌───────────────────────┐                     ┌──────────────────────────┐
          │  Multi-KB MCP Router  │                     │   OAuth 2.0 / PKCE Shim  │
          │   (mcp/kb_router.py)  │                     │ (FastAPI + DCR + Tokens) │
          └───────────┬───────────┘                     └─────────────┬────────────┘
                      │                                               │ (Loopback)
                      ├───────────────────────┬───────────────────────┘
                      ▼                       ▼
          ┌───────────────────────┐ ┌───────────────────┐
          │   Agent Memory Core   │ │ Cross-KB Search   │
          │  (Recall / Dreaming)  │ │ & Evidence Graph  │
          └───────────┬───────────┘ └─────────┬─────────┘
                      │                       │
══════════════════════╪═══════════════════════╪═════════════════════════════════════════
   CONTROL PLANE      │                       │  `ragcli` CLI & Systemd Daemon
══════════════════════╪═══════════════════════╪═════════════════════════════════════════
                      ▼                       ▼
          ┌─────────────────────────────────────────────┐
          │        Catalog & Content-Addressable        │
          │      Transaction Engine (storage/)          │
          │  - POSIX locking (`fcntl.flock`)            │
          │  - Atomic writes (`mkstemp` + `os.replace`) │
          │  - Non-destructive alias renames            │
          └─────────────────────┬───────────────────────┘
                                │
        ┌───────────────────────┴───────────────────────┐
        ▼                                               ▼
┌───────────────────────────────┐               ┌───────────────────────────────┐
│     Multimodal Ingestion      │               │     Decoupled Graph RAG       │
│           Pipeline            │               │        Engine Adapter         │
│  - Docling (PDF, Tables, OCR) │               │  - LightRAG HTTP runtime      │
│  - Whisper ASR (Audio/Video)  │               │  - Local Ollama / OpenRouter  │
│  - Staged diff & sha256 cache │               │  - Verified 409 conflict sync │
└───────────────────────────────┘               └───────────────────────────────┘
```

---

## Key Capabilities

### 1. Unified Multi-KB MCP Interface
- **Agent Integration**: Exposes standard Model Context Protocol (MCP) endpoints for IDE agents (`claude-code`, `cursor`, `codex`).
- **Context & Evidence**: Provides tools for asking questions, retrieving structured context, locating primary sources, comparing directory trees, and tracking citation evidence.
- **Cross-KB Querying**: Federates retrieval across independent domain knowledge bases without cross-contaminating source domains.

### 2. High-Performance Multimodal Ingestion
- **Document & Table Parsing**: Deep parsing via IBM Docling (PDF, Office, ODF, EPUB, EML, MSG) with cell-level table structure preservation and formula extraction.
- **Vision & Audio**: Automated OCR (EasyOCR / Apple Vision) and Whisper speech-to-text pipeline with hardware acceleration (Apple Silicon MPS / Linux CUDA & CPU).
- **Staged Sync Engine**: Generates safe local file snapshots and transfer deltas. Protects against accidental bulk deletions (automatic review gate at >20% or >100 files).
- **Zero-Re-Embedding Renames**: Tracks documents via SHA-256 content hashes. Renaming or moving files updates catalog mappings atomically with **0 extra embedding tokens** spent.

### 3. Agent Memory & Autonomous "Dreaming"
- **Episodic & Procedural Memory**: Dedicated memory store for user preferences, architectural rules, and learned insights.
- **Autonomous Dreaming Daemon**: Scheduled background consolidation (`systemd.timer` / cron) that clusters raw session learnings, resolves contradictions, and prunes stale context.
- **Archive Importer**: Extracts memories and preferences directly from ChatGPT and Claude export archives.

### 4. Production-Grade System Engineering
- **Atomic Persistence**: Zero external DB dependencies (no SQLite/Postgres/Neo4j servers required). State lives in isolated JSON manifests guarded by POSIX file locks (`fcntl.flock`) and atomic staging replacement (`os.replace`).
- **Path Traversal Immunity**: Strict canonicalization and root confinement for all ingested paths.
- **Systemd Daemonization**: Complete Linux service templates for background server lifecycle, automated watchdogs, and background dreamers.
- **Remote Mobile Access**: Built-in FastAPI OAuth 2.0 authorization shim with Dynamic Client Registration (DCR) and PKCE, designed for secure exposure over Tailscale Funnel.

---

## Directory Structure

```text
knowledge-os/
├── bin/
│   ├── ragcli.py             # Unified Control Plane CLI
│   ├── kb_registry.py        # Multi-KB registry & metadata manager
│   ├── ingest.py             # Multimodal batch ingestion coordinator
│   ├── archive_importer.py   # ChatGPT/Claude archive memory extractor
│   ├── agent_hooks.py        # Context injection hooks for AI sessions
│   └── mcp_clients.py        # Automated MCP registration for IDE agents
├── pipeline/
│   ├── indexer.py            # Conflict-safe transactional indexing engine
│   ├── ingestion_worker.py   # Staged background ingestion & safety gates
│   ├── parsers.py            # Docling & Whisper multimodal extraction
│   └── dreaming.py           # Memory consolidation & synthesis engine
├── storage/
│   ├── catalog.py            # Content-addressable document occurrence catalog
│   └── json_store.py         # Thread/process-safe atomic JSON storage
├── mcp/
│   ├── kb_router.py          # Central Multi-KB FastMCP server
│   └── lightrag_mcp.py       # Dedicated per-KB legacy MCP server
├── services/
│   └── obsidian-mcp-oauth-shim/ # OAuth2 + PKCE proxy for remote access
├── systemd/                  # User systemd service templates & timers
├── config/                   # Global runtime and model configurations
└── tests/                    # Comprehensive unit and integration test suite
```

---

## Quickstart

### Prerequisites
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended)
- [Ollama](https://ollama.ai/) or OpenRouter API key

### Installation

```bash
# 1. Clone repository
git clone https://github.com/your-username/knowledge-os.git
cd knowledge-os

# 2. Run automated bootstrap
./setup.sh

# 3. Verify runtime health
ragcli doctor
```

The bootstrap script provisions `.venv`, installs all core dependencies (Docling, FastMCP, LightRAG API runtime), creates central CLI symlinks in `~/.local/bin`, and synchronizes default configuration.

---

## CLI Reference (`ragcli`)

### Knowledge Base Lifecycle

```bash
# Create and start a new knowledge base
ragcli add engineering ~/projects/docs --port 9621
ragcli start engineering

# Run incremental multimodal ingestion
ragcli ingest engineering

# Check status across all managed KBs
ragcli list
```

### Agent MCP Integration

Register the knowledge base with your active coding agent in one command:

```bash
# Automatically registers MCP configuration with the target client:
ragcli mcp add engineering --client claude-code
ragcli mcp add engineering --client cursor
ragcli mcp add engineering --client codex
```

### Agent Memory & Dreaming

```bash
# Store an explicit guideline or insight
ragcli memory remember "Always use fcntl.flock for state persistence" --category architecture

# Query semantic memory
ragcli memory recall "persistence requirements"

# Trigger memory synthesis and consolidation
ragcli memory dream

# Import memories from existing ChatGPT/Claude export dumps
ragcli import-archive ~/Downloads/conversations.json
```

---

## Multimodal Pipeline Configuration

Default extraction profiles are configured in `config/global.env`:

| Setting | Default | Description |
|---|---|---|
| `DOCLING_PLATFORM` | `auto` | Runtime detection (`linux`, `mac`, or `auto`) |
| `DOCLING_ACCELERATOR` | `auto` | Hardware acceleration (`mps`, `cuda`, `cpu`) |
| `DOCLING_OCR_ENGINE` | `easyocr` | OCR backend (`easyocr` on Linux, Apple Vision on macOS) |
| `DOCLING_ASR_ENGINE` | `whisper` | Audio transcription model (`whisper-turbo` / `mlx`) |
| `LLM_MODEL` | `qwen3.5:4b` | Default Ollama completion engine |
| `EMBEDDING_MODEL` | `qwen3-embedding:0.6b`| Default semantic vector embedding engine |

---

## Test Suite & Verification

The codebase includes test coverage across all critical failure paths (concurrency, path traversal, transient 409 conflict resolution, atomic rollbacks):

```bash
# Run test suite
pytest tests/
```

Key verified test suites:
- `tests/test_storage_catalog.py`: POSIX file locking, duplicate detection, atomic writes.
- `tests/test_pipeline_indexer.py`: Transactional checkpointing, retry loops, zero-re-embedding renames.
- `tests/test_sync_client.py`: Symlink safety, non-destructive diffing, delete protection gates.
- `tests/test_mcp_kb_router.py`: FastMCP endpoint contracts, evidence preservation, cross-KB retrieval.

---

## Engineering Design Philosophy

1. **Local-First & Private**: Data never leaves your infrastructure unless explicitly configured with external LLM endpoints.
2. **Defensive by Default**: All file modifications are atomic; deletions require explicit thresholds; paths are canonicalized against traversal attacks.
3. **Decoupled Architecture**: Underlying engines (RAG adapters, OCR runners, embedding models) are swappable components behind clean abstraction boundaries.
4. **Agent-Native**: First-class support for tool use, structured evidence retrieval, and durable episodic memory.
