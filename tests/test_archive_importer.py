import io
import json
import tempfile
import zipfile
from pathlib import Path

import pytest
from bin.archive_importer import (
    ArchiveSecurityError,
    detect_and_parse_json,
    extract_user_preferences,
    import_archive_to_kb,
    safe_extract_zip,
)


def create_zip_in_memory(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(name, data)
    return buf.getvalue()


def test_safe_extract_zip_normal():
    data = create_zip_in_memory({
        "folder/test.txt": "hello world",
        "doc.json": '{"k": 1}',
    })
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "test.zip"
        zip_path.write_bytes(data)
        out_dir = Path(tmpdir) / "extracted"

        extracted = safe_extract_zip(zip_path, out_dir)
        assert len(extracted) == 2
        assert (out_dir / "folder" / "test.txt").read_text() == "hello world"
        assert (out_dir / "doc.json").read_text() == '{"k": 1}'


def test_safe_extract_zip_traversal_blocked():
    data = create_zip_in_memory({
        "../escape.txt": "evil payload",
    })
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "evil.zip"
        zip_path.write_bytes(data)
        out_dir = Path(tmpdir) / "extracted"

        with pytest.raises(ArchiveSecurityError, match="Relative traversal|Zip-Slip"):
            safe_extract_zip(zip_path, out_dir)


def test_safe_extract_zip_absolute_path_blocked():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/etc/passwd", "root:x:0:0")
    data = buf.getvalue()

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "evil.zip"
        zip_path.write_bytes(data)
        out_dir = Path(tmpdir) / "extracted"

        with pytest.raises(ArchiveSecurityError, match="Absolute path|Zip-Slip"):
            safe_extract_zip(zip_path, out_dir)


def test_parse_chatgpt_json():
    sample_chatgpt = [{
        "id": "conv-123",
        "title": "Configurazione Tailwind",
        "create_time": 1700000000.0,
        "update_time": 1700000100.0,
        "mapping": {
            "node-1": {
                "id": "node-1",
                "parent": None,
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["Ricordati che preferisco sempre Python minimale e commenti Redis"]},
                    "create_time": 1700000005.0
                }
            },
            "node-2": {
                "id": "node-2",
                "parent": "node-1",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Certamente, useremo Python minimale."]},
                    "create_time": 1700000010.0
                }
            }
        },
        "current_node": "node-2"
    }]

    parsed = detect_and_parse_json(json.dumps(sample_chatgpt))
    assert len(parsed) == 1
    conv = parsed[0]
    assert conv.id == "conv-123"
    assert conv.title == "Configurazione Tailwind"
    assert len(conv.messages) == 2
    assert conv.messages[0].role == "user"
    assert "Ricordati che" in conv.messages[0].content

    # Preference extractor
    prefs = extract_user_preferences(parsed)
    assert len(prefs) >= 1
    assert "preferisco sempre Python minimale" in prefs[0]["content"]


def test_parse_claude_json():
    sample_claude = [{
        "uuid": "claude-uuid-1",
        "name": "Refactoring OAuth",
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:30:00Z",
        "chat_messages": [
            {
                "sender": "human",
                "text": "Nelle mie macchine uso debian 12 con systemd per i demoni.",
                "created_at": "2026-11-01T10:05:00Z"
            },
            {
                "sender": "assistant",
                "text": "Perfetto, creeremo i service unit systemd per Debian 12.",
                "created_at": "2026-11-01T10:06:00Z"
            }
        ]
    }]

    parsed = detect_and_parse_json(json.dumps(sample_claude))
    assert len(parsed) == 1
    conv = parsed[0]
    assert conv.id == "claude-uuid-1"
    assert len(conv.messages) == 2

    prefs = extract_user_preferences(parsed)
    assert len(prefs) == 1
    assert "debian 12" in prefs[0]["content"]


def test_import_archive_to_kb_full_pipeline():
    sample_claude = [{
        "uuid": "c-1",
        "name": "Design Session",
        "created_at": "2026-11-01T10:00:00Z",
        "chat_messages": [
            {"sender": "human", "text": "Ricordati che sul mio server usiamo la porta 9621."}
        ]
    }]
    zip_bytes = create_zip_in_memory({
        "conversations.json": json.dumps(sample_claude)
    })

    with tempfile.TemporaryDirectory() as tmpdir:
        zip_file = Path(tmpdir) / "export.zip"
        zip_file.write_bytes(zip_bytes)
        source_dir = Path(tmpdir) / "kb_source"

        result = import_archive_to_kb(zip_file, source_dir)
        assert result["conversations_imported"] == 1
        assert result["preferences_extracted"] == 1

        # Check files created on disk
        imported_files = list((source_dir / "imported").rglob("*.md"))
        assert len(imported_files) == 1
        pref_files = list((source_dir / "memories" / "preferences").rglob("*.md"))
        assert len(pref_files) == 1
        assert "porta 9621" in pref_files[0].read_text()


def test_parse_notion_export_json():
    notion_data = {
        "title": "Notion Meeting Notes",
        "results": [
            {
                "type": "heading_1",
                "heading_1": {"rich_text": [{"plain_text": "Meeting Agenda"}]}
            },
            {
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"plain_text": "Decision on database schema"}]}
            }
        ]
    }
    parsed = detect_and_parse_json(json.dumps(notion_data))
    assert len(parsed) == 1
    conv = parsed[0]
    assert conv.source == "notion"
    content = conv.messages[0].content
    assert "# Meeting Agenda" in content
    assert "- Decision on database schema" in content


def test_parse_generic_json_export():
    generic_data = {
        "project": "LightRAG-KB",
        "settings": {"mode": "production", "workers": 4},
        "tags": ["rag", "llm", "offline"]
    }
    parsed = detect_and_parse_json(json.dumps(generic_data))
    assert len(parsed) == 1
    content = parsed[0].messages[0].content
    assert "LightRAG-KB" in content
    assert "production" in content
    assert "rag" in content
