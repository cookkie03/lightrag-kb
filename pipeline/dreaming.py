"""Memory management and Dreaming (sleep consolidation) pipeline.

Provides structured Markdown frontmatter storage for agent memories
(preferences, decisions, episodic logs, facts) with status lifecycle
(active, superseded, deprecated), semantic dedup, contradiction resolution,
and episodic compaction.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_MEMORY_TYPES = {"preference", "decision", "episodic", "fact"}
VALID_MEMORY_STATUSES = {"active", "superseded", "deprecated"}


def utc_now_iso() -> str:
    """Return timezone-aware UTC ISO timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryRecord:
    """Structured representation of an agent memory."""
    id: str
    type: str  # preference, decision, episodic, fact
    status: str = "active"  # active, superseded, deprecated
    title: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    superseded_by: str | None = None
    deprecation_reason: str | None = None
    confidence: float = 1.0
    source: str = ""
    content: str = ""
    file_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Parse YAML frontmatter and markdown body from file content.
def parse_memory_file(file_path: Path | str) -> MemoryRecord | None:
    path = Path(file_path)
    if not path.is_file():
        return None

    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        return None

    raw_frontmatter = parts[1]
    body = parts[2].strip()

    meta: dict[str, Any] = {}
    for line in raw_frontmatter.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        # Handle simple types (json-encoded strings, numbers, lists)
        if val.startswith("[") and val.endswith("]"):
            try:
                meta[key] = json.loads(val)
            except Exception:
                # Fallback parse comma separated
                inner = val[1:-1].strip()
                meta[key] = [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
        elif val.lower() == "null" or val == "":
            meta[key] = None
        elif val.lower() == "true":
            meta[key] = True
        elif val.lower() == "false":
            meta[key] = False
        else:
            try:
                meta[key] = json.loads(val)
            except Exception:
                meta[key] = val.strip("'\"")

    mem_id = meta.get("id") or path.stem
    mem_type = meta.get("type", "fact")
    if mem_type not in VALID_MEMORY_TYPES:
        mem_type = "fact"

    mem_status = meta.get("status", "active")
    if mem_status not in VALID_MEMORY_STATUSES:
        mem_status = "active"

    return MemoryRecord(
        id=mem_id,
        type=mem_type,
        status=mem_status,
        title=meta.get("title", ""),
        tags=list(meta.get("tags") or []),
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        superseded_by=meta.get("superseded_by"),
        deprecation_reason=meta.get("deprecation_reason"),
        confidence=float(meta.get("confidence", 1.0)),
        source=meta.get("source", ""),
        content=body,
        file_path=str(path.resolve()),
    )


# Serialize a MemoryRecord into YAML frontmatter and markdown body.
def serialize_memory_file(mem: MemoryRecord) -> str:
    frontmatter = [
        "---",
        f"id: {json.dumps(mem.id)}",
        f"type: {json.dumps(mem.type)}",
        f"status: {json.dumps(mem.status)}",
        f"title: {json.dumps(mem.title)}",
        f"tags: {json.dumps(mem.tags)}",
        f"created_at: {json.dumps(mem.created_at)}",
        f"updated_at: {json.dumps(mem.updated_at)}",
        f"superseded_by: {json.dumps(mem.superseded_by) if mem.superseded_by else 'null'}",
    ]
    if mem.deprecation_reason:
        frontmatter.append(f"deprecation_reason: {json.dumps(mem.deprecation_reason)}")
    frontmatter.append(f"confidence: {mem.confidence}")
    frontmatter.append(f"source: {json.dumps(mem.source)}")
    frontmatter.append("---")
    frontmatter.append("")

    header = f"# {mem.title}" if mem.title else ""
    body = mem.content.strip()
    if header and not body.startswith("#"):
        body = f"{header}\n\n{body}"

    return "\n".join(frontmatter) + "\n" + body + "\n"


# Load all memory markdown files for a given knowledge base source directory.
def load_kb_memories(source_dir: Path | str) -> list[MemoryRecord]:
    source_path = Path(source_dir).resolve()
    memories_dir = source_path / "memories"
    if not memories_dir.is_dir():
        return []

    records: list[MemoryRecord] = []
    for md_file in memories_dir.rglob("*.md"):
        mem = parse_memory_file(md_file)
        if mem:
            records.append(mem)
    return records


# Write or update a memory record on disk in the appropriate subdirectory.
def write_memory_record(source_dir: Path | str, mem: MemoryRecord) -> Path:
    source_path = Path(source_dir).resolve()
    target_dir = source_path / "memories" / mem.type
    target_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^\w\s-]", "", mem.title.lower().strip())
    slug = re.sub(r"[\s_-]+", "-", slug)[:40].strip("-")
    filename = f"{mem.id}_{slug}.md" if slug else f"{mem.id}.md"

    target_file = Path(mem.file_path) if mem.file_path else (target_dir / filename)
    target_file.parent.mkdir(parents=True, exist_ok=True)
    content = serialize_memory_file(mem)
    target_file.write_text(content, encoding="utf-8")
    mem.file_path = str(target_file.resolve())
    return target_file


# Record a new memory, handling revision / superseding when requested.
def remember_memory(
    source_dir: Path | str,
    content: str,
    memory_type: str = "fact",
    title: str = "",
    tags: list[str] | None = None,
    supersedes_id: str | None = None,
    source: str = "agent",
) -> MemoryRecord:
    if memory_type not in VALID_MEMORY_TYPES:
        raise ValueError(f"Invalid memory type: {memory_type}. Must be one of {VALID_MEMORY_TYPES}")

    now = utc_now_iso()
    prefix = memory_type[:4]
    random_suffix = os.urandom(3).hex()
    timestamp_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    new_id = f"mem_{prefix}_{timestamp_tag}_{random_suffix}"

    # If this supersedes an existing memory, find and update the old one first
    if supersedes_id:
        existing = load_kb_memories(source_dir)
        for old_mem in existing:
            if old_mem.id == supersedes_id:
                old_mem.status = "superseded"
                old_mem.superseded_by = new_id
                old_mem.updated_at = now
                write_memory_record(source_dir, old_mem)
                break

    new_mem = MemoryRecord(
        id=new_id,
        type=memory_type,
        status="active",
        title=title or (content.strip().splitlines()[0][:50] if content else "Untitled"),
        tags=tags or [],
        created_at=now,
        updated_at=now,
        superseded_by=None,
        confidence=1.0,
        source=source,
        content=content.strip(),
    )
    write_memory_record(source_dir, new_mem)
    return new_mem


# Deprecate an existing memory by ID with an optional reason or successor.
def deprecate_memory_by_id(
    source_dir: Path | str,
    memory_id: str,
    reason: str = "",
    superseded_by: str | None = None,
) -> MemoryRecord | None:
    now = utc_now_iso()
    memories = load_kb_memories(source_dir)
    for mem in memories:
        if mem.id == memory_id:
            mem.status = "superseded" if superseded_by else "deprecated"
            mem.deprecation_reason = reason
            if superseded_by:
                mem.superseded_by = superseded_by
            mem.updated_at = now
            write_memory_record(source_dir, mem)
            return mem
    return None


# Filter memories matching text query and criteria.
def query_memories(
    source_dir: Path | str,
    query: str,
    memory_type: str | None = None,
    active_only: bool = True,
    top_k: int = 10,
) -> list[MemoryRecord]:
    memories = load_kb_memories(source_dir)
    query_lower = query.lower().strip()
    words = [w for w in re.split(r"\W+", query_lower) if len(w) > 2]

    scored: list[tuple[float, MemoryRecord]] = []
    for mem in memories:
        if active_only and mem.status != "active":
            continue
        if memory_type and mem.type != memory_type:
            continue

        haystack = f"{mem.title} {mem.content} {' '.join(mem.tags)}".lower()
        score = 0.0
        if query_lower and query_lower in haystack:
            score += 10.0
        for w in words:
            if w in haystack:
                score += 1.5

        if score > 0 or not query_lower:
            scored.append((score, mem))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item[1] for item in scored[:top_k]]


# Text similarity calculation between two memories for clustering and dedup.
def memory_similarity(mem_a: MemoryRecord, mem_b: MemoryRecord) -> float:
    text_a = f"{mem_a.title}\n{mem_a.content}".lower().strip()
    text_b = f"{mem_b.title}\n{mem_b.content}".lower().strip()
    ratio = difflib.SequenceMatcher(None, text_a, text_b).quick_ratio()
    return ratio


# Detect contradictory or replacing decisions within the same topic or subject.
CONTRADICTION_KEYWORDS = [
    "sostituito", "deprecato", "invece di", "non più", "migrato a", "abbandonato",
    "superseded", "instead of", "no longer", "migrated to", "deprecated"
]


# Analyze memories and detect outdated or conflicting decisions.
def find_conflicting_decisions(memories: list[MemoryRecord]) -> list[tuple[MemoryRecord, MemoryRecord, str]]:
    conflicts: list[tuple[MemoryRecord, MemoryRecord, str]] = []
    decisions = [m for m in memories if m.type == "decision" and m.status == "active"]

    # Compare chronologically: newer decision might override an older one
    decisions.sort(key=lambda m: m.created_at or "")

    for i in range(len(decisions)):
        for j in range(i + 1, len(decisions)):
            older = decisions[i]
            newer = decisions[j]

            sim = memory_similarity(older, newer)
            newer_text = newer.content.lower()

            # Check if newer mentions an explicit migration or override
            overrides = any(kw in newer_text for kw in CONTRADICTION_KEYWORDS)
            shared_tags = set(older.tags) & set(newer.tags)

            if (sim > 0.65) or (overrides and (shared_tags or sim > 0.4)):
                reason = f"More recent decision ({newer.id}) overrides or conflicts with {older.id} (sim: {sim:.2f})"
                conflicts.append((older, newer, reason))

    return conflicts


# Compact multiple old episodic session logs into a consolidated milestone log.
def compact_episodic_memories(
    source_dir: Path | str,
    memories: list[MemoryRecord],
    dry_run: bool = False,
) -> dict[str, Any]:
    source_path = Path(source_dir).resolve()
    episodics = [m for m in memories if m.type == "episodic" and m.status == "active"]
    if len(episodics) < 4:
        return {"compacted_count": 0, "milestones_created": 0}

    # Group older episodics (e.g., everything older than the 2 most recent)
    episodics.sort(key=lambda m: m.created_at or "")
    to_compact = episodics[:-2]

    now = utc_now_iso()
    summary_lines = [
        "# Milestone di Consolidamento Sessioni Storiche",
        f"Consolidate {len(to_compact)} sessioni episodiche in un'unica sintesi storica.",
        "",
        "## Sessioni archiviate:",
    ]
    for mem in to_compact:
        summary_lines.append(f"- **{mem.title}** ({mem.created_at}): {mem.content[:150].strip()}...")

    milestone_id = f"mem_fact_milestone_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    milestone_record = MemoryRecord(
        id=milestone_id,
        type="fact",
        status="active",
        title="Consolidamento Sessioni Storiche",
        tags=["consolidation", "milestone", "history"],
        created_at=now,
        updated_at=now,
        source="dreaming:compaction",
        content="\n".join(summary_lines),
    )

    if not dry_run:
        write_memory_record(source_path, milestone_record)
        for old_ep in to_compact:
            old_ep.status = "superseded"
            old_ep.superseded_by = milestone_id
            old_ep.updated_at = now
            write_memory_record(source_path, old_ep)

    return {
        "compacted_count": len(to_compact),
        "milestones_created": 1,
        "milestone_id": milestone_id,
    }


# Execute the Dreaming (consolidation) cycle on a KB source directory.
def run_dreaming_cycle(source_dir: Path | str, dry_run: bool = False) -> dict[str, Any]:
    source_path = Path(source_dir).resolve()
    memories = load_kb_memories(source_path)

    # 1. Resolve conflicting or superseded decisions
    conflicts = find_conflicting_decisions(memories)
    superseded_records: list[dict[str, str]] = []

    for older, newer, reason in conflicts:
        superseded_records.append({
            "older_id": older.id,
            "newer_id": newer.id,
            "reason": reason,
        })
        if not dry_run:
            older.status = "superseded"
            older.superseded_by = newer.id
            older.updated_at = utc_now_iso()
            older.deprecation_reason = reason
            write_memory_record(source_path, older)

    # 2. Episodic log compaction
    compaction_res = compact_episodic_memories(source_path, memories, dry_run=dry_run)

    return {
        "total_memories": len(memories),
        "superseded_decisions": len(superseded_records),
        "details_superseded": superseded_records,
        "episodic_compaction": compaction_res,
        "dry_run": dry_run,
    }
