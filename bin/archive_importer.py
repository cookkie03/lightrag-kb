"""Safe archive importer and normalizer for ChatGPT, Claude, and Gemini exports.

Extracts archives with Zip-Slip protection, bounded size/ratio enforcement,
and normalizes message trees into clean Markdown documents with frontmatter.
Also extracts stable user preferences and instructions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MAX_FILE_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB per file limit
MAX_TOTAL_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB total extracted limit
MAX_COMPRESSION_RATIO = 100.0  # Zip-bomb protection: uncompressed/compressed ratio


class ArchiveSecurityError(RuntimeError):
    """Raised when an archive violates safety constraints (Zip-Slip, bomb, symlink)."""
    pass


# Check destination containment to block directory traversal / Zip-Slip attacks.
def _is_safe_extraction_path(target_dir: Path, dest_path: Path) -> bool:
    try:
        resolved_target = target_dir.resolve()
        resolved_dest = dest_path.resolve()
        return os.path.commonpath([str(resolved_target), str(resolved_dest)]) == str(resolved_target)
    except (ValueError, OSError):
        return False


# Unpack a zip archive strictly inside target_dir with decompression and path guards.
def safe_extract_zip(zip_path: Path | str, target_dir: Path | str) -> list[Path]:
    zip_path = Path(zip_path).resolve()
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    if not zip_path.is_file():
        raise FileNotFoundError(f"Archive file not found: {zip_path}")

    extracted_files: list[Path] = []
    total_bytes = 0

    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            # Block symlinks and directory entries masquerading as files.
            # Upper 16 bits of external_attr contain UNIX file mode: 0o120000 = S_IFLNK.
            is_symlink = (info.external_attr >> 16) & 0o120000 == 0o120000
            if is_symlink:
                raise ArchiveSecurityError(f"Symlinks are forbidden in archive: {info.filename}")

            filename = info.filename
            if ".." in filename.split("/") or ".." in filename.split("\\"):
                raise ArchiveSecurityError(f"Relative traversal ('..') detected in entry: {filename}")

            if filename.startswith("/") or filename.startswith("\\"):
                raise ArchiveSecurityError(f"Absolute path entry detected in archive: {filename}")

            # Verify size bounds and compression ratio.
            if info.file_size > MAX_FILE_SIZE_BYTES:
                raise ArchiveSecurityError(
                    f"File {filename} exceeds max size limit ({info.file_size} > {MAX_FILE_SIZE_BYTES})"
                )

            total_bytes += info.file_size
            if total_bytes > MAX_TOTAL_EXTRACTED_BYTES:
                raise ArchiveSecurityError(
                    f"Total extracted size exceeds limit ({total_bytes} > {MAX_TOTAL_EXTRACTED_BYTES})"
                )

            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_COMPRESSION_RATIO and info.file_size > 1024 * 1024:
                    raise ArchiveSecurityError(f"Compression ratio too high ({ratio:.1f}) for entry: {filename}")

            dest = target_dir / filename
            if not _is_safe_extraction_path(target_dir, dest):
                raise ArchiveSecurityError(f"Zip-Slip attempt detected: {filename}")

            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted_files.append(dest)

    return extracted_files


@dataclass
class NormalizedMessage:
    role: str
    content: str
    timestamp: str | None = None
    model: str | None = None


@dataclass
class NormalizedConversation:
    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[NormalizedMessage] = field(default_factory=list)
    source: str = "archive"


# Sanitize title to create a safe filesystem filename.
def slugify(text: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", text.strip().lower())
    slug = re.sub(r"[\s_-]+", "-", cleaned)[:60].strip("-")
    return slug or "conversation"


# Convert normalized conversation into a Markdown document with YAML frontmatter.
def conversation_to_markdown(conv: NormalizedConversation) -> str:
    frontmatter = [
        "---",
        f"id: {json.dumps(conv.id)}",
        f"title: {json.dumps(conv.title)}",
        f"source: {json.dumps(conv.source)}",
        f"created_at: {json.dumps(conv.created_at)}",
        f"updated_at: {json.dumps(conv.updated_at)}",
        "type: episodic",
        "status: active",
        "---",
        f"# {conv.title or 'Conversazione'}",
        "",
    ]
    lines = list(frontmatter)

    for msg in conv.messages:
        role_label = msg.role.capitalize()
        header = f"### {role_label}"
        meta = []
        if msg.timestamp:
            meta.append(msg.timestamp)
        if msg.model:
            meta.append(f"Model: {msg.model}")
        if meta:
            header += f" ({', '.join(meta)})"
        lines.append(header)
        lines.append("")
        lines.append(msg.content.strip())
        lines.append("")

    return "\n".join(lines).strip() + "\n"


# Parse ChatGPT conversations.json message tree and extract active linear thread.
def parse_chatgpt_conversations(json_data: list[dict[str, Any]]) -> list[NormalizedConversation]:
    conversations: list[NormalizedConversation] = []

    for item in json_data:
        cid = item.get("id") or item.get("conversation_id") or "unknown"
        title = item.get("title") or "Chat senza titolo"
        c_time = item.get("create_time")
        u_time = item.get("update_time")
        created_at = datetime.fromtimestamp(c_time, tz=timezone.utc).isoformat() if c_time else ""
        updated_at = datetime.fromtimestamp(u_time, tz=timezone.utc).isoformat() if u_time else created_at

        # ChatGPT message tree: mapping with current_node tracing backwards to root.
        mapping = item.get("mapping", {})
        current_node_id = item.get("current_node")

        active_nodes: list[dict[str, Any]] = []
        curr = current_node_id
        while curr and curr in mapping:
            node = mapping[curr]
            active_nodes.append(node)
            curr = node.get("parent")
        active_nodes.reverse()

        messages: list[NormalizedMessage] = []
        for node in active_nodes:
            msg = node.get("message")
            if not msg:
                continue
            author = msg.get("author", {})
            role = author.get("role", "unknown")
            if role in {"system"}:
                continue
            content_data = msg.get("content", {})
            parts = content_data.get("parts", [])
            text_parts = []
            for p in parts:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict) and "text" in p:
                    text_parts.append(p["text"])
            content_str = "\n".join(text_parts).strip()
            if not content_str:
                continue

            m_time = msg.get("create_time")
            ts = datetime.fromtimestamp(m_time, tz=timezone.utc).isoformat() if m_time else None
            metadata = msg.get("metadata", {})
            model_slug = metadata.get("model_slug")

            messages.append(NormalizedMessage(
                role=role,
                content=content_str,
                timestamp=ts,
                model=model_slug,
            ))

        if messages:
            conversations.append(NormalizedConversation(
                id=cid,
                title=title,
                created_at=created_at,
                updated_at=updated_at,
                messages=messages,
                source="chatgpt",
            ))

    return conversations


# Parse Claude export conversations.json list into NormalizedConversation items.
def parse_claude_conversations(json_data: list[dict[str, Any]]) -> list[NormalizedConversation]:
    conversations: list[NormalizedConversation] = []

    for item in json_data:
        cid = item.get("uuid") or item.get("id") or "unknown"
        title = item.get("name") or item.get("title") or "Chat Claude"
        created_at = item.get("created_at") or ""
        updated_at = item.get("updated_at") or created_at

        chat_messages = item.get("chat_messages", [])
        messages: list[NormalizedMessage] = []

        for msg in chat_messages:
            sender = msg.get("sender") or ("human" if msg.get("role") == "user" else "assistant")
            role = "user" if sender == "human" else "assistant"
            content = msg.get("text", "")
            if not content and "content" in msg:
                c_field = msg["content"]
                if isinstance(c_field, list):
                    content = "\n".join(b.get("text", "") for b in c_field if isinstance(b, dict)).strip()
                elif isinstance(c_field, str):
                    content = c_field

            if not content.strip():
                continue

            ts = msg.get("created_at")
            messages.append(NormalizedMessage(
                role=role,
                content=content.strip(),
                timestamp=ts,
                model=None,
            ))

        if messages:
            conversations.append(NormalizedConversation(
                id=cid,
                title=title,
                created_at=created_at,
                updated_at=updated_at,
                messages=messages,
                source="claude",
            ))

    return conversations


# Detect whether json structure matches ChatGPT, Claude, or generic export.
def detect_and_parse_json(content: str) -> list[NormalizedConversation]:
    data = json.loads(content)
    if isinstance(data, dict):
        if "conversations" in data and isinstance(data["conversations"], list):
            data = data["conversations"]
        else:
            data = [data]

    if not data or not isinstance(data, list):
        return []

    first = data[0]
    if not isinstance(first, dict):
        return []

    # Distinguish ChatGPT (mapping tree) vs Claude (chat_messages list)
    if "mapping" in first or "current_node" in first:
        return parse_chatgpt_conversations(data)
    elif "chat_messages" in first or "uuid" in first:
        return parse_claude_conversations(data)
    elif "messages" in first and isinstance(first["messages"], list):
        # Generic format
        return parse_claude_conversations(data)

    return []


# Heuristic patterns identifying stable user instructions, preferences, and environment facts.
USER_INSTRUCTION_PATTERNS = [
    re.compile(r"(?:ricordati|ricorda|tieni a mente|non dimenticare)\s+che\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:preferisco|prediligo|la mia preferenza è)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:nelle mie macchine|sul mio server|nel mio ambiente|uso solitamente)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:il mio ruolo è|lavoro come|sono uno?)\s+(.+)", re.IGNORECASE),
    re.compile(r"(?:always remember|please remember|my preference is|i always use)\s+(.+)", re.IGNORECASE),
]


# Extract preference candidates from user turns across conversations.
def extract_user_preferences(conversations: list[NormalizedConversation]) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()

    for conv in conversations:
        for msg in conv.messages:
            if msg.role not in {"user", "human"}:
                continue
            text = msg.content
            matched_for_message = False
            for pat in USER_INSTRUCTION_PATTERNS:
                if matched_for_message:
                    break
                for match in pat.finditer(text):
                    snippet = match.group(0).strip()
                    # Keep snippet bounded and avoid duplicates
                    if len(snippet) < 15 or len(snippet) > 600:
                        continue
                    key = snippet.lower()
                    if key in seen_hashes:
                        continue
                    seen_hashes.add(key)
                    extracted.append({
                        "content": snippet,
                        "source_conv_id": conv.id,
                        "source_conv_title": conv.title,
                        "timestamp": msg.timestamp or conv.created_at,
                    })
                    matched_for_message = True
                    break

    return extracted


# Process an archive file or directory and write normalized markdown + preferences into target_dir.
def import_archive_to_kb(
    archive_path: Path | str,
    output_source_dir: Path | str,
    provider: str = "auto",
) -> dict[str, Any]:
    archive_path = Path(archive_path).resolve()
    output_source_dir = Path(output_source_dir).resolve()
    imported_dir = output_source_dir / "imported"
    preferences_dir = output_source_dir / "memories" / "preferences"

    imported_dir.mkdir(parents=True, exist_ok=True)
    preferences_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rag_import_") as tmpdir:
        tmp_path = Path(tmpdir)
        if archive_path.is_file() and archive_path.suffix.lower() == ".zip":
            safe_extract_zip(archive_path, tmp_path)
        elif archive_path.is_dir():
            # Directory input: copy safe files
            for root, _, files in os.walk(archive_path):
                for f in files:
                    src_f = Path(root) / f
                    rel = src_f.relative_to(archive_path)
                    dest_f = tmp_path / rel
                    dest_f.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_f, dest_f)
        else:
            raise ValueError(f"Unsupported archive format: {archive_path}")

        # Search for conversation JSON files in extracted directory
        all_convs: list[NormalizedConversation] = []
        for json_candidate in tmp_path.rglob("*.json"):
            try:
                content = json_candidate.read_text(encoding="utf-8", errors="replace")
                convs = detect_and_parse_json(content)
                if convs:
                    all_convs.extend(convs)
            except Exception:
                continue

        # Export conversations into markdown documents
        written_convs = 0
        timestamp_prefix = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        batch_folder = imported_dir / f"import_{timestamp_prefix}"
        batch_folder.mkdir(parents=True, exist_ok=True)

        for conv in all_convs:
            md_content = conversation_to_markdown(conv)
            filename = f"{slugify(conv.title)}_{conv.id[:8]}.md"
            (batch_folder / filename).write_text(md_content, encoding="utf-8")
            written_convs += 1

        # Extract user preferences and save to memories/preferences/
        preferences = extract_user_preferences(all_convs)
        written_prefs = 0
        for idx, pref in enumerate(preferences):
            pref_id = f"pref_{timestamp_prefix}_{idx:03d}"
            pref_content = pref["content"]
            md = [
                "---",
                f"id: {pref_id}",
                "type: preference",
                "status: active",
                f"title: {json.dumps(pref_content[:40])}",
                f"created_at: {json.dumps(datetime.now(timezone.utc).isoformat())}",
                f"source: {json.dumps('import:' + pref['source_conv_title'])}",
                "---",
                f"# Preferenza Utente Estratta",
                "",
                pref_content,
                "",
            ]
            pref_file = preferences_dir / f"{pref_id}.md"
            pref_file.write_text("\n".join(md), encoding="utf-8")
            written_prefs += 1

        return {
            "conversations_imported": written_convs,
            "preferences_extracted": written_prefs,
            "destination_folder": str(batch_folder),
        }
