import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "mcp") not in sys.path:
    sys.path.insert(0, str(ROOT / "mcp"))

from bin.url_importer import (
    sanitize_slug,
    format_media_markdown,
    import_media_url_to_kb,
)
from bin.ragcli import build_parser
from kb_router import KBRouter, build_mcp_server


def test_sanitize_slug():
    assert sanitize_slug("My Cool Video! 2026") == "my-cool-video-2026"
    assert sanitize_slug("   ") == "media-item"


def test_format_media_markdown():
    meta = {
        "title": "Test Title",
        "webpage_url": "https://youtube.com/watch?v=123",
        "uploader": "Test Channel",
        "duration": 120,
        "description": "Original description text",
        "upload_date": "20261114",
    }
    transcript = "This is the transcribed audio text."
    md = format_media_markdown(meta, transcript)
    assert "# Test Title" in md
    assert "https://youtube.com/watch?v=123" in md
    assert "Test Channel" in md
    assert "Original description text" in md
    assert "This is the transcribed audio text." in md


def test_import_media_url_to_kb(tmp_path, monkeypatch):
    source_dir = tmp_path / "kb_source"
    source_dir.mkdir()
    fake_kb = {
        "name": "test-kb",
        "source_dir": str(source_dir),
        "data_dir": str(source_dir / ".lightrag"),
        "derived_dir": str(source_dir / ".lightrag" / "derived"),
        "port": 9621,
    }

    monkeypatch.setattr("bin.url_importer.find_kb", lambda reg, name: fake_kb if name == "test-kb" else None)
    monkeypatch.setattr("bin.url_importer.load_registry", lambda env: [fake_kb])

    fake_ydl = MagicMock()
    def fake_extract_info(url, download=True):
        media_id = "test_vid_123"
        return {
            "id": media_id,
            "title": "Awesome Podcast Episode",
            "uploader": "TechCreator",
            "description": "Episode details and show notes.",
            "webpage_url": url,
            "duration": 300,
            "upload_date": "20261114",
        }

    fake_ydl.extract_info = fake_extract_info

    def fake_extract_stream(url, output_dir, ydl_client=None):
        dummy_audio = output_dir / "test_vid_123.mp3"
        dummy_audio.write_bytes(b"dummy audio")
        return {
            "id": "test_vid_123",
            "title": "Awesome Podcast Episode",
            "uploader": "TechCreator",
            "description": "Episode details and show notes.",
            "webpage_url": url,
            "duration": 300,
            "upload_date": "20261114",
            "audio_path": dummy_audio,
        }

    monkeypatch.setattr("bin.url_importer.extract_media_stream", fake_extract_stream)

    res = import_media_url_to_kb(
        kb_name="test-kb",
        url="https://youtube.com/watch?v=test_vid_123",
        subfolder="media",
        auto_index=False,
        transcriber=lambda path: "Transcribed speech from mock whisper.",
        ydl_client=fake_ydl,
    )

    assert res["status"] == "ok"
    assert res["title"] == "Awesome Podcast Episode"
    saved_file = Path(res["file_path"])
    assert saved_file.is_file()
    content = saved_file.read_text(encoding="utf-8")
    assert "Awesome Podcast Episode" in content
    assert "Transcribed speech from mock whisper." in content


@pytest.mark.anyio
async def test_mcp_ingest_url_tool_registered():
    router = KBRouter()
    mcp_app = build_mcp_server(router)
    tools = await mcp_app.list_tools()
    tool_names = {tool.name for tool in tools}
    assert "ingest_url" in tool_names

