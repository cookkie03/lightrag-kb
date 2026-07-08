#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import requests

from kb_registry import ensure_kb_dirs, find_kb, global_env, load_registry

TEXT_EXT = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".adoc", ".asciidoc",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
    ".sh", ".zsh", ".bash", ".fish", ".ps1", ".bat",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".xml", ".html", ".htm", ".sql", ".csv", ".tsv", ".log", ".diff", ".patch", ".rtf",
}
PDF_EXT = {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
OFFICE_EXT = {
    ".docx", ".dotx", ".docm", ".dotm",
    ".pptx", ".potx", ".ppsx", ".pptm", ".potm", ".ppsm",
    ".xlsx", ".xlsm",
    ".odt", ".ott", ".fodt", ".ods", ".ots", ".fods", ".odp", ".otp", ".fodp",
    ".epub", ".eml", ".msg",
}
AUDIO_VIDEO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".avi", ".mov"}
ALL_EXT = TEXT_EXT | PDF_EXT | IMAGE_EXT | OFFICE_EXT | AUDIO_VIDEO_EXT

LOCALE_MAP = {
    "it": "it-IT",
    "en": "en-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "pt": "pt-PT",
    "nl": "nl-NL",
}
GARBLED_RANGES = (
    (0x2700, 0x27BF),
    (0x2580, 0x259F),
    (0xE000, 0xF8FF),
    (0x1F0A0, 0x1F0FF),
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_garbled(text: str, threshold: float = 0.15) -> bool:
    real_letters = sum(1 for char in text if char.isalpha())
    garbled = sum(1 for char in text if any(lo <= ord(char) <= hi for lo, hi in GARBLED_RANGES))
    total = real_letters + garbled
    if total < 50:
        return False
    return (garbled / total) > threshold


def _run_docling(py: str, code: str, timeout: int, label: str) -> str:
    result = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{label} fallito: {result.stderr[-500:]}")
    return result.stdout


def _is_mps_unsupported(error: Exception) -> bool:
    text = str(error)
    return "AcceleratorDeviceNotAvailableError" in text and "MPS is not supported" in text


def _log(prefix: str, rel: str, detail: str | None = None) -> None:
    if detail:
        print(f"{prefix} {rel} — {detail}")
    else:
        print(f"{prefix} {rel}")


def _docling_probe_text(path: Path, py: str, timeout: int) -> str:
    code = (
        "import sys\n"
        "from pathlib import Path as _P\n"
        "from docling.datamodel.base_models import InputFormat\n"
        "from docling.datamodel.pipeline_options import PdfPipelineOptions\n"
        "from docling.document_converter import DocumentConverter, PdfFormatOption\n"
        "opts = PdfPipelineOptions()\n"
        "opts.do_ocr = False\n"
        "opts.do_table_structure = False\n"
        "converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})\n"
        f"doc = converter.convert(_P({str(path)!r}), page_range=(1, 5)).document\n"
        "sys.stdout.write(doc.export_to_markdown())\n"
    )
    try:
        return _run_docling(py, code, timeout, f"probe Docling su {path.name}")
    except Exception:
        return ""


def render_docling_visual(path: Path, env: dict[str, str]) -> str:
    py = env["DOCLING_PYTHON"]
    langs = [item.strip() for item in env.get("DOCLING_LANG", "it,en").split(",") if item.strip()]
    locales = [LOCALE_MAP.get(item, item) for item in langs]
    timeout = int(env.get("DOCLING_TIMEOUT", "3600"))
    force_full_page = path.suffix.lower() in IMAGE_EXT or _looks_garbled(
        _docling_probe_text(path, py, min(timeout, 120))
    )
    def _code(accelerator_device: str) -> str:
        return (
            "import sys\n"
            "from pathlib import Path as _P\n"
            "from docling.datamodel.base_models import InputFormat\n"
            "from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions\n"
            "from docling.datamodel.pipeline_options import OcrMacOptions, PdfPipelineOptions, TableFormerMode, TableStructureOptions\n"
            "from docling.document_converter import DocumentConverter, PdfFormatOption\n"
            "opts = PdfPipelineOptions()\n"
            "opts.do_ocr = True\n"
            "opts.do_table_structure = True\n"
            "opts.table_structure_options = TableStructureOptions(do_cell_matching=True, mode=TableFormerMode.ACCURATE)\n"
            f"opts.ocr_options = OcrMacOptions(lang={locales!r}, recognition='accurate', force_full_page_ocr={force_full_page!r})\n"
            "opts.images_scale = 2.0\n"
            "opts.do_formula_enrichment = True\n"
            "opts.do_code_enrichment = True\n"
            f"opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.{accelerator_device}, num_threads=8)\n"
            "format_options = {InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}\n"
            "if hasattr(InputFormat, 'IMAGE'):\n"
            "    try:\n"
            "        from docling.document_converter import ImageFormatOption\n"
            "        format_options[InputFormat.IMAGE] = ImageFormatOption(pipeline_options=opts)\n"
            "    except Exception:\n"
            "        pass\n"
            "converter = DocumentConverter(format_options=format_options)\n"
            f"doc = converter.convert(_P({str(path)!r})).document\n"
            "sys.stdout.write(doc.export_to_markdown())\n"
        )

    try:
        return _run_docling(py, _code("MPS"), timeout, f"Docling visual su {path.name}")
    except Exception as exc:
        if not _is_mps_unsupported(exc):
            raise
        print(f"    ⚠ Docling su {path.name}: MPS non supportato da questo modello, fallback a CPU")
        return _run_docling(py, _code("CPU"), timeout, f"Docling visual (CPU fallback) su {path.name}")


def render_docling_native(path: Path, env: dict[str, str]) -> str:
    py = env["DOCLING_PYTHON"]
    timeout = int(env.get("DOCLING_TIMEOUT", "3600"))
    code = (
        "import sys\n"
        "from pathlib import Path as _P\n"
        "from docling.document_converter import DocumentConverter\n"
        f"doc = DocumentConverter().convert(_P({str(path)!r})).document\n"
        "sys.stdout.write(doc.export_to_markdown())\n"
    )
    return _run_docling(py, code, timeout, f"Docling native su {path.name}")


def render_docling_audio(path: Path, env: dict[str, str]) -> str:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg non trovato sul PATH. Installa con `brew install ffmpeg`.")
    py = env["DOCLING_PYTHON"]
    timeout = int(env.get("DOCLING_TIMEOUT", "3600"))
    language = next((item.strip() for item in env.get("DOCLING_LANG", "it,en").split(",") if item.strip()), "en")
    code = (
        "import sys\n"
        "from pathlib import Path as _P\n"
        "from docling.datamodel import asr_model_specs\n"
        "from docling.datamodel.base_models import InputFormat\n"
        "from docling.datamodel.pipeline_options import AsrPipelineOptions\n"
        "from docling.document_converter import AudioFormatOption, DocumentConverter\n"
        "from docling.pipeline.asr_pipeline import AsrPipeline\n"
        f"asr_options = asr_model_specs.WHISPER_TURBO_MLX.model_copy(update={{'language': {language!r}}})\n"
        "pipeline_options = AsrPipelineOptions(asr_options=asr_options)\n"
        "format_options = {InputFormat.AUDIO: AudioFormatOption(pipeline_cls=AsrPipeline, pipeline_options=pipeline_options)}\n"
        "if hasattr(InputFormat, 'VIDEO'):\n"
        "    try:\n"
        "        from docling.document_converter import VideoFormatOption\n"
        "        format_options[InputFormat.VIDEO] = VideoFormatOption(pipeline_cls=AsrPipeline, pipeline_options=pipeline_options)\n"
        "    except Exception:\n"
        "        pass\n"
        "converter = DocumentConverter(format_options=format_options)\n"
        f"doc = converter.convert(_P({str(path)!r})).document\n"
        "sys.stdout.write(doc.export_to_markdown())\n"
    )
    return _run_docling(py, code, timeout, f"Docling audio/video su {path.name}")


def _document_rows(data: object) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("documents", "items", "data", "rows", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def fetch_kb_docs(port: int, page_size: int = 50) -> dict[str, list[str]] | None:
    docs: dict[str, list[str]] = {}
    try:
        page = 1
        saw_any_page = False
        while True:
            response = requests.post(
                f"http://127.0.0.1:{port}/documents/paginated",
                json={
                    "status_filter": None,
                    "page": page,
                    "page_size": page_size,
                    "sort_field": "updated_at",
                    "sort_direction": "desc",
                },
                timeout=60,
            )
            if response.status_code == 404:
                raise RuntimeError("endpoint paginato non disponibile")
            response.raise_for_status()
            rows = _document_rows(response.json())
            if not rows:
                if saw_any_page:
                    break
                raise RuntimeError("risposta paginata vuota")
            saw_any_page = True
            for item in rows:
                file_path = item.get("file_path")
                doc_id = item.get("id")
                if file_path and doc_id:
                    docs.setdefault(file_path, []).append(doc_id)
            if len(rows) < page_size:
                break
            page += 1
        return docs
    except Exception:
        try:
            response = requests.get(f"http://127.0.0.1:{port}/documents", timeout=60)
            response.raise_for_status()
        except Exception as exc:
            print(f"  ⚠ impossibile leggere i documenti del KB ({exc}): mirror disattivato per questo run.")
            return None
        statuses = response.json().get("statuses", {})
        for bucket in statuses.values():
            for item in bucket or []:
                file_path = item.get("file_path")
                doc_id = item.get("id")
                if file_path and doc_id:
                    docs.setdefault(file_path, []).append(doc_id)
        return docs


def delete_docs(port: int, doc_ids: list[str]) -> bool:
    if not doc_ids:
        return True
    try:
        response = requests.delete(
            f"http://127.0.0.1:{port}/documents/delete_document",
            json={"doc_ids": doc_ids, "delete_file": False, "delete_llm_cache": True},
            timeout=120,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"    ⚠ cancellazione doc fallita ({exc}); proseguo.")
        return False


def insert_text(port: int, text: str, source: str, retries: int = 8) -> str:
    if not text.strip():
        return "empty"
    for attempt in range(1, retries + 2):
        response = requests.post(
            f"http://127.0.0.1:{port}/documents/text",
            json={"text": text, "file_source": source},
            timeout=300,
        )
        if response.status_code == 409:
            try:
                detail = response.json().get("detail", "")
            except Exception:
                detail = response.text or ""
            if "already contains" in detail or "already exists" in detail:
                return "exists"
            wait = min(10 * 2 ** (attempt - 1), 300)
            if attempt <= retries:
                print(f"    ⏳ pipeline occupata (409), riprovo in {wait}s ({attempt}/{retries})…")
                time.sleep(wait)
                continue
        response.raise_for_status()
        return "ok"
    raise RuntimeError("pipeline occupata: troppi 409 consecutivi")


def load_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_manifest(path: Path, manifest: dict[str, dict]) -> None:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def category_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        return "pdf"
    if ext in IMAGE_EXT:
        return "images"
    if ext in OFFICE_EXT:
        return "office"
    if ext in AUDIO_VIDEO_EXT:
        return "audio"
    return "text"


def artifact_path(derived_dir: Path, category: str, rel: Path) -> Path:
    return derived_dir / category / rel.parent / f"{rel.name}.md"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def iter_candidates(source_dir: Path, data_dir: Path, exclude_patterns: list[str]) -> Iterator[Path]:
    data_root = data_dir.resolve()

    def under_data_dir(path: Path) -> bool:
        try:
            path.resolve().relative_to(data_root)
            return True
        except (ValueError, OSError):
            return False

    def excluded(path: Path) -> bool:
        rel = path.relative_to(source_dir).as_posix()
        return any(fnmatch.fnmatch(rel, pattern) for pattern in exclude_patterns)

    seen_dirs: set[Path] = set()
    for root, dirs, files in os.walk(source_dir, topdown=True, followlinks=True):
        root_path = Path(root)
        try:
            root_resolved = root_path.resolve()
        except OSError:
            dirs[:] = []
            continue
        if root_resolved in seen_dirs:
            dirs[:] = []
            continue
        seen_dirs.add(root_resolved)

        kept_dirs = []
        for dirname in dirs:
            child = root_path / dirname
            if under_data_dir(child):
                continue
            try:
                child_resolved = child.resolve()
            except OSError:
                continue
            if child_resolved in seen_dirs:
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        for filename in files:
            path = root_path / filename
            if under_data_dir(path):
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if excluded(path):
                continue
            if path.suffix.lower() in ALL_EXT:
                yield path


def clean_force_run(derived_dir: Path) -> None:
    for subdir in ("text", "pdf", "images", "office", "audio"):
        target = derived_dir / subdir
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    for subdir in ("cache", "manifests"):
        target = derived_dir / subdir
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)


def read_text_file(path: Path) -> str:
    return path.read_text(errors="replace")


def render_content(path: Path, env: dict[str, str]) -> tuple[str, str | None]:
    ext = path.suffix.lower()
    category = category_for(path)
    if ext in TEXT_EXT:
        return read_text_file(path), None
    if ext in PDF_EXT or ext in IMAGE_EXT:
        return render_docling_visual(path, env), category
    if ext in OFFICE_EXT:
        return render_docling_native(path, env), category
    if ext in AUDIO_VIDEO_EXT:
        return render_docling_audio(path, env), category
    raise RuntimeError(f"Estensione non supportata: {path.suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync incrementale di una KB LightRAG con sorgente reale + derived Docling."
    )
    parser.add_argument("--kb", required=True)
    parser.add_argument("--force", action="store_true", help="rigenera cache, derivati e riallinea la KB")
    args = parser.parse_args()

    env = global_env()
    registry = load_registry(env)
    kb = find_kb(registry, args.kb)
    if not kb:
        sys.exit(f"KB '{args.kb}' non trovata.")

    ensure_kb_dirs(kb)
    source_dir = Path(kb["source_dir"])
    data_dir = Path(kb["data_dir"])
    derived_dir = Path(kb["derived_dir"])
    port = kb["port"]
    if not source_dir.is_dir():
        sys.exit(
            f"⚠ Cartella sorgente non disponibile: {source_dir}\n"
            f"  (non montata? cloud offline?) Nessuna modifica al KB."
        )
    try:
        if not requests.get(f"http://127.0.0.1:{port}/health", timeout=3).ok:
            raise RuntimeError("health check fallito")
    except Exception:
        sys.exit(
            f"Server '{args.kb}' non attivo o non healthy. "
            f"Esegui `ragcli stop {args.kb}`, poi `ragcli start {args.kb}`, quindi riprova."
        )

    if args.force:
        clean_force_run(derived_dir)
    manifest_path = derived_dir / "manifests" / "sync.json"
    manifest = {} if args.force else load_manifest(manifest_path)

    kb_docs = fetch_kb_docs(port)
    mirror = kb_docs is not None
    exclude_patterns = kb.get("exclude", [])
    candidate_count = sum(1 for _ in iter_candidates(source_dir, data_dir, exclude_patterns))
    present_rel: set[str] = set()

    added = updated = unchanged = removed = failed_count = 0
    next_manifest: dict[str, dict] = {}

    print(f"KB '{args.kb}': {candidate_count} file candidati dalla sorgente {source_dir}")
    for path in iter_candidates(source_dir, data_dir, exclude_patterns):
        rel = path.relative_to(source_dir).as_posix()
        present_rel.add(rel)
        old_entry = manifest.get(rel, {})
        current_hash = file_hash(path)
        in_kb = bool(kb_docs and rel in kb_docs)
        if not args.force and old_entry.get("sha256") == current_hash and in_kb:
            next_manifest[rel] = old_entry
            unchanged += 1
            _log("· invariato", rel)
            continue

        action = "updated" if old_entry else "added"
        if in_kb and kb_docs:
            _log("↻ rimuovo vecchia versione", rel)
            delete_docs(port, kb_docs.get(rel, []))

        try:
            category = category_for(path)
            if category == "text":
                _log("→ testo", rel)
            elif category == "pdf":
                _log("→ Docling PDF", rel)
            elif category == "images":
                _log("→ Docling immagine", rel)
            elif category == "office":
                _log("→ Docling documento", rel)
            elif category == "audio":
                _log("→ Docling audio/video", rel)
            content, artifact_category = render_content(path, env)
            artifact_rel: str | None = None
            if artifact_category is not None:
                out_path = artifact_path(derived_dir, artifact_category, path.relative_to(source_dir))
                ensure_parent(out_path)
                out_path.write_text(content)
                artifact_rel = out_path.relative_to(data_dir).as_posix()
                _log("  salvato derivato", rel, artifact_rel)
            old_artifact_rel = old_entry.get("artifact_rel")
            if old_artifact_rel and old_artifact_rel != artifact_rel:
                old_artifact = data_dir / old_artifact_rel
                if old_artifact.exists():
                    old_artifact.unlink()
                    _log("  rimosso derivato vecchio", rel, old_artifact_rel)
            result = insert_text(port, content, rel)
            if result == "empty":
                _log("· salto", rel, "contenuto vuoto")
                continue
            if action == "added":
                added += 1
                _log("✓ inserito", rel)
            else:
                updated += 1
                _log("✓ aggiornato", rel)
            next_manifest[rel] = {
                "sha256": current_hash,
                "category": category,
                "artifact_rel": artifact_rel,
                "source_key": rel,
            }
        except Exception as exc:
            failed_count += 1
            print(f"✗ errore su '{rel}': {exc}")

    if mirror and kb_docs is not None:
        orphans = {rel: ids for rel, ids in kb_docs.items() if rel not in present_rel}
        for rel, ids in orphans.items():
            _log("− rimuovo orfano", rel)
            if delete_docs(port, ids):
                removed += 1
            entry = manifest.get(rel)
            if entry and entry.get("artifact_rel"):
                artifact = data_dir / entry["artifact_rel"]
                if artifact.exists():
                    artifact.unlink()
                    _log("  eliminato derivato orfano", rel, entry["artifact_rel"])

    for rel, entry in manifest.items():
        if rel in next_manifest:
            continue
        artifact_rel = entry.get("artifact_rel")
        if artifact_rel:
            artifact = data_dir / artifact_rel
            if artifact.exists():
                artifact.unlink()

    save_manifest(manifest_path, next_manifest)
    print(
        f"\nFatto: {added} nuovi, {updated} aggiornati, {unchanged} invariati, "
        f"{removed} rimossi, {failed_count} falliti."
    )
    sys.exit(1 if failed_count else 0)


if __name__ == "__main__":
    main()
