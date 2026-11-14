"""URL and Media Importer for LightRAG-KB.

Downloads media and audio streams from YouTube, Soundcloud, Instagram, TikTok,
and other platforms supported by yt-dlp, transcribes audio using Whisper ASR,
and imports the structured transcript into the Knowledge Base.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

from bin.kb_registry import find_kb, global_env, load_registry
from pipeline.parsers import parse_audio_document


class MediaImportError(Exception):
    """Raised when media extraction, download, or transcription fails."""
    pass


# Sanitize a string into a safe filename slug.
# Strips non-alphanumeric characters to prevent path traversal or filesystem incompatibilities.
def sanitize_slug(text: str, max_length: int = 80) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    slug = re.sub(r"[-\s]+", "-", cleaned)
    return slug[:max_length] if slug else "media-item"


# Extract audio and metadata from a media URL using yt-dlp.
# Downloads audio stream to temporary directory and returns metadata dict with local file path.
def extract_media_stream(
    url: str,
    output_dir: Path,
    ydl_client: Any = None,
) -> dict[str, Any]:
    if yt_dlp is None and ydl_client is None:
        raise MediaImportError("yt-dlp is not installed in the current environment.")

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(output_dir / "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    client = ydl_client or yt_dlp.YoutubeDL(ydl_opts)
    try:
        info = client.extract_info(url, download=True)
        if not info:
            raise MediaImportError(f"No media info returned for URL: {url}")
    except Exception as exc:
        raise MediaImportError(f"yt-dlp download failed for {url}: {exc}") from exc

    media_id = str(info.get("id", "media"))
    title = str(info.get("title") or "Untitled Media")
    uploader = str(info.get("uploader") or info.get("channel") or "Unknown")
    description = str(info.get("description") or "").strip()
    webpage_url = str(info.get("webpage_url") or url)
    duration = info.get("duration", 0)
    upload_date = str(info.get("upload_date") or "")

    # Locate generated audio file
    candidate_files = list(output_dir.glob(f"{media_id}.*"))
    if not candidate_files:
        raise MediaImportError(f"Audio file was not generated for media id: {media_id}")
    audio_path = candidate_files[0]

    return {
        "id": media_id,
        "title": title,
        "uploader": uploader,
        "description": description,
        "webpage_url": webpage_url,
        "duration": duration,
        "upload_date": upload_date,
        "audio_path": audio_path,
    }


# Format extracted media transcript and metadata into Markdown document with YAML frontmatter.
# Standardizes external content into clean text ready for LightRAG chunking and knowledge graph extraction.
def format_media_markdown(
    metadata: dict[str, Any],
    transcript: str,
) -> str:
    frontmatter = [
        "---",
        f"title: {json.dumps(metadata.get('title', 'Untitled'))}",
        f"source_url: {json.dumps(metadata.get('webpage_url', ''))}",
        f"author: {json.dumps(metadata.get('uploader', 'Unknown'))}",
        f"upload_date: {json.dumps(metadata.get('upload_date', ''))}",
        f"duration_seconds: {metadata.get('duration', 0)}",
        "type: media_transcript",
        "transcription_engine: whisper",
        "---",
        "",
        f"# {metadata.get('title', 'Untitled')}",
        "",
        f"- **Source:** {metadata.get('webpage_url', '')}",
        f"- **Author/Channel:** {metadata.get('uploader', 'Unknown')}",
        f"- **Duration:** {metadata.get('duration', 0)}s",
        "",
    ]
    desc = metadata.get("description", "").strip()
    if desc:
        frontmatter.extend([
            "## Description",
            desc,
            "",
        ])
    frontmatter.extend([
        "## Transcript",
        transcript.strip(),
        "",
    ])
    return "\n".join(frontmatter)


# Download, transcribe, and save a media URL into a Knowledge Base source folder.
# Optionally indexes the resulting document into the running LightRAG server instance.
def import_media_url_to_kb(
    kb_name: str,
    url: str,
    subfolder: str = "media",
    auto_index: bool = True,
    custom_title: str | None = None,
    transcriber: Callable[[Path], str] | None = None,
    ydl_client: Any = None,
) -> dict[str, Any]:
    env = global_env()
    registry = load_registry(env)
    kb = find_kb(registry, kb_name)
    if not kb:
        raise MediaImportError(f"KB '{kb_name}' not found in registry.")

    source_dir = Path(kb["source_dir"]).resolve()
    target_dir = source_dir / subfolder
    target_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rag_media_") as tmpdir:
        tmp_path = Path(tmpdir)
        info = extract_media_stream(url, tmp_path, ydl_client=ydl_client)
        if custom_title:
            info["title"] = custom_title

        audio_file = info["audio_path"]
        if transcriber:
            transcript = transcriber(audio_file)
        else:
            parsed = parse_audio_document(audio_file, env=env)
            transcript = parsed.get("content", "")

        markdown_doc = format_media_markdown(info, transcript)

        slug = sanitize_slug(info["title"])
        dest_file = target_dir / f"{slug}.md"
        counter = 1
        while dest_file.exists():
            dest_file = target_dir / f"{slug}-{counter}.md"
            counter += 1

        # Write final markdown atomically
        fd, temp_dest = tempfile.mkstemp(dir=str(target_dir), prefix=".media_import_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(markdown_doc)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_dest, str(dest_file))
        finally:
            if os.path.exists(temp_dest):
                os.unlink(temp_dest)

    indexed = False
    index_error = None
    if auto_index:
        try:
            import requests
            port = kb.get("port")
            if port:
                rel_path = dest_file.relative_to(source_dir).as_posix()
                res = requests.post(
                    f"http://127.0.0.1:{port}/documents/text",
                    json={"text": markdown_doc, "description": rel_path},
                    timeout=30,
                )
                if res.ok:
                    indexed = True
                else:
                    index_error = f"Server returned {res.status_code}: {res.text}"
        except Exception as exc:
            index_error = str(exc)

    return {
        "status": "ok",
        "kb": kb_name,
        "title": info["title"],
        "url": url,
        "file_path": str(dest_file),
        "relative_path": dest_file.relative_to(source_dir).as_posix(),
        "indexed": indexed,
        "index_error": index_error,
    }
