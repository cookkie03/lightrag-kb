#!/usr/bin/env python3
from __future__ import annotations

import argparse
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

from kb_registry import (
    dir_is_excluded,
    ensure_kb_dirs,
    find_kb,
    global_env,
    load_registry,
    normalize_exclude_specs_for_kb,
    path_is_excluded,
)

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


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _manifest_entry(rel: str, path: Path, current_hash: str, category: str, old_entry: dict | None) -> dict:
    entry = {
        "source_key": old_entry.get("source_key") if old_entry else rel,
        "sha256": current_hash,
        "category": category,
        "artifact_rel": old_entry.get("artifact_rel") if old_entry else None,
        "render_status": "pending",
        "rendered_sha256": None,
        "insert_status": "pending",
        "last_error": None,
    }
    if old_entry:
        entry["render_status"] = old_entry.get("render_status") or ("done" if category == "text" or old_entry.get("artifact_rel") else "pending")
        entry["rendered_sha256"] = old_entry.get("rendered_sha256") or (old_entry.get("sha256") if entry["render_status"] == "done" else None)
        entry["insert_status"] = old_entry.get("insert_status") or ("done" if old_entry.get("sha256") == current_hash and entry["render_status"] == "done" else "pending")
        entry["last_error"] = old_entry.get("last_error")
    if entry["source_key"] != rel:
        entry["source_key"] = rel
    if entry["category"] != category:
        entry["category"] = category
    if path.suffix.lower() in TEXT_EXT and not entry["artifact_rel"]:
        entry["artifact_rel"] = None
    return entry


def _complete_insert_status(status: str | None) -> bool:
    return status == "done"


def _needs_render(entry: dict, source_changed: bool, force: bool, artifact_exists: bool) -> bool:
    if force or source_changed:
        return True
    if entry.get("render_status") != "done":
        return True
    if entry.get("category") != "text" and not artifact_exists:
        return True
    return False


def _needs_insert(entry: dict, source_changed: bool, force: bool, mirror_has_doc: bool | None) -> bool:
    if force or source_changed:
        return True
    if not _complete_insert_status(entry.get("insert_status")):
        return True
    if mirror_has_doc is False:
        return True
    return False


def _insert_priority(category: str) -> tuple[int, str]:
    return {
        "text": (0, category),
        "office": (1, category),
        "pdf": (2, category),
        "images": (2, category),
        "audio": (3, category),
    }.get(category, (4, category))


def _insert_content(item: dict, data_dir: Path) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    category = item["category"]
    if category == "text":
        return read_text_file(item["path"])
    artifact_rel = item["entry"].get("artifact_rel")
    if not artifact_rel:
        raise RuntimeError("manca artifact_rel per il contenuto derivato")
    artifact = data_dir / artifact_rel
    if not artifact.exists():
        raise RuntimeError(f"artifatto derivato mancante: {artifact_rel}")
    return artifact.read_text(errors="replace")


def _classify_insert_failure(exc: Exception) -> str:
    text = str(exc).lower()
    if "pipeline occupata" in text or "409" in text or "timed out" in text or "timeout" in text or "connection" in text:
        return "transient"
    return "failed"


def _phase_header(name: str, count: int) -> None:
    print(f"\n[{name}] {count} file")


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
                print(f"    ⏳ pipeline occupata (409) su {source}, riprovo in {wait}s ({attempt}/{retries})…")
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


def iter_candidates(source_dir: Path, data_dir: Path, exclude_specs: list[str]) -> Iterator[Path]:
    data_root = data_dir.resolve()

    def under_data_dir(path: Path) -> bool:
        try:
            path.resolve().relative_to(data_root)
            return True
        except (ValueError, OSError):
            return False

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
            if dir_is_excluded(child.relative_to(source_dir).as_posix(), exclude_specs):
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
            if path_is_excluded(path.relative_to(source_dir).as_posix(), exclude_specs):
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


def _render_phase(
    plan: list[dict],
    env: dict[str, str],
    source_dir: Path,
    data_dir: Path,
    derived_dir: Path,
    manifest: dict[str, dict],
) -> int:
    render_failed = 0
    render_queue = [item for item in plan if item["needs_render"]]
    if not render_queue:
        return 0
    _phase_header("render", len(render_queue))
    for item in render_queue:
        rel = item["rel"]
        path = item["path"]
        entry = item["entry"]
        category = item["category"]
        if item["source_changed"] or item["force"]:
            entry["insert_status"] = "pending"
            entry["last_error"] = None
        try:
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
            entry["rendered_sha256"] = text_hash(content)
            item["content"] = content
            item["rendered_sha256"] = entry["rendered_sha256"]
            artifact_rel = entry.get("artifact_rel")
            if artifact_category is not None:
                out_path = artifact_path(derived_dir, artifact_category, path.relative_to(source_dir))
                ensure_parent(out_path)
                out_path.write_text(content)
                artifact_rel = out_path.relative_to(data_dir).as_posix()
                _log("  salvato derivato", rel, artifact_rel)
                old_artifact_rel = entry.get("artifact_rel")
                if old_artifact_rel and old_artifact_rel != artifact_rel:
                    old_artifact = data_dir / old_artifact_rel
                    if old_artifact.exists():
                        old_artifact.unlink()
                        _log("  rimosso derivato vecchio", rel, old_artifact_rel)
            elif category == "text":
                artifact_rel = None
            entry.update(
                {
                    "sha256": item["current_hash"],
                    "category": category,
                    "artifact_rel": artifact_rel,
                    "render_status": "done",
                    "last_error": None,
                }
            )
            _log("✓ render", rel)
        except Exception as exc:
            render_failed += 1
            entry.update(
                {
                    "sha256": item["current_hash"],
                    "category": category,
                    "render_status": "failed",
                    "last_error": str(exc),
                }
            )
            print(f"✗ render fallito '{rel}': {exc}")
    save_manifest(derived_dir / "manifests" / "sync.json", manifest)
    return render_failed


def _insert_phase(
    plan: list[dict],
    port: int,
    data_dir: Path,
    kb_docs: dict[str, list[str]] | None,
    derived_dir: Path,
    manifest: dict[str, dict],
) -> tuple[int, int, list[dict]]:
    inserted_added = 0
    inserted_updated = 0
    transient: list[dict] = []
    insert_queue = [item for item in plan if item["needs_insert"] and item["entry"].get("render_status") == "done"]
    if not insert_queue:
        return inserted_added, inserted_updated, transient
    insert_queue.sort(key=lambda item: (_insert_priority(item["category"]), item["rel"]))
    _phase_header("insert", len(insert_queue))
    for item in insert_queue:
        rel = item["rel"]
        entry = item["entry"]
        source_changed = item["source_changed"]
        try:
            if (source_changed or item["force"]) and kb_docs is not None and rel in kb_docs:
                _log("↻ rimuovo vecchia versione", rel)
                delete_docs(port, kb_docs.get(rel, []))
            content = _insert_content(item, data_dir)
            result = insert_text(port, content, rel)
            if result == "empty":
                entry["insert_status"] = "done"
                entry["last_error"] = None
                _log("· salto", rel, "contenuto vuoto")
                if item["is_new"]:
                    inserted_added += 1
                else:
                    inserted_updated += 1
            elif result == "exists":
                entry["insert_status"] = "done"
                entry["last_error"] = None
                _log("· già presente", rel)
                if item["is_new"]:
                    inserted_added += 1
                else:
                    inserted_updated += 1
            else:
                entry["insert_status"] = "done"
                entry["last_error"] = None
                if item["is_new"]:
                    inserted_added += 1
                    _log("✓ inserito", rel)
                else:
                    inserted_updated += 1
                    _log("✓ aggiornato", rel)
        except Exception as exc:
            status = _classify_insert_failure(exc)
            entry["insert_status"] = status
            entry["last_error"] = str(exc)
            if status == "transient":
                transient.append(item)
                print(f"↻ insert in attesa '{rel}': {exc}")
            else:
                print(f"✗ insert fallito '{rel}': {exc}")
    save_manifest(derived_dir / "manifests" / "sync.json", manifest)
    return inserted_added, inserted_updated, transient


def _retry_transient_inserts(
    transient: list[dict],
    port: int,
    data_dir: Path,
    kb_docs: dict[str, list[str]] | None,
    derived_dir: Path,
    manifest: dict[str, dict],
) -> tuple[int, int]:
    if not transient:
        return 0, 0
    print("\n[insert:retry] passaggio finale sui transient")
    time.sleep(5)
    added = updated = 0
    retry_queue = sorted(transient, key=lambda item: (_insert_priority(item["category"]), item["rel"]))
    for item in retry_queue:
        rel = item["rel"]
        entry = item["entry"]
        try:
            if (item["source_changed"] or item["force"]) and kb_docs is not None and rel in kb_docs:
                _log("↻ rimuovo vecchia versione", rel)
                delete_docs(port, kb_docs.get(rel, []))
            content = _insert_content(item, data_dir)
            result = insert_text(port, content, rel)
            if result in {"empty", "exists"}:
                entry["insert_status"] = "done"
                entry["last_error"] = None
                _log("· pronto", rel, "già stabile")
                if item["is_new"]:
                    added += 1
                else:
                    updated += 1
            else:
                entry["insert_status"] = "done"
                entry["last_error"] = None
                if item["is_new"]:
                    added += 1
                    _log("✓ inserito", rel)
                else:
                    updated += 1
                    _log("✓ aggiornato", rel)
        except Exception as exc:
            entry["insert_status"] = "failed"
            entry["last_error"] = str(exc)
            print(f"✗ retry fallito '{rel}': {exc}")
    save_manifest(derived_dir / "manifests" / "sync.json", manifest)
    return added, updated


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
    exclude_patterns, _ = normalize_exclude_specs_for_kb(kb.get("exclude", []), source_dir)
    candidates = sorted(
        list(iter_candidates(source_dir, data_dir, exclude_patterns)),
        key=lambda path: path.relative_to(source_dir).as_posix(),
    )
    present_rel: set[str] = set()

    added = updated = unchanged = removed = 0
    render_failed = insert_failed = 0
    next_manifest: dict[str, dict] = {}
    plan: list[dict] = []

    print(f"KB '{args.kb}': {len(candidates)} file candidati dalla sorgente {source_dir}")
    for path in candidates:
        rel = path.relative_to(source_dir).as_posix()
        present_rel.add(rel)
        old_entry = manifest.get(rel, {})
        current_hash = file_hash(path)
        category = category_for(path)
        entry = _manifest_entry(rel, path, current_hash, category, old_entry if not args.force else None)
        source_changed = bool(
            args.force
            or old_entry.get("sha256") != current_hash
            or old_entry.get("category") != category
        )
        artifact_rel = entry.get("artifact_rel")
        artifact_exists = bool(artifact_rel) and (data_dir / artifact_rel).exists()
        mirror_has_doc = None if kb_docs is None else rel in kb_docs
        render_needed = _needs_render(entry, source_changed, args.force, artifact_exists)
        insert_needed = _needs_insert(entry, source_changed, args.force, mirror_has_doc)
        if (
            not args.force
            and not source_changed
            and _complete_insert_status(entry.get("insert_status"))
            and entry.get("render_status") == "done"
            and (category == "text" or artifact_exists)
            and mirror_has_doc is not False
        ):
            next_manifest[rel] = entry
            unchanged += 1
            _log("· invariato", rel)
            continue

        if render_needed:
            _log("↻ render", rel, category)
        elif insert_needed:
            _log("↻ insert", rel, category)
        next_manifest[rel] = entry
        plan.append(
            {
                "path": path,
                "rel": rel,
                "current_hash": current_hash,
                "category": category,
                "entry": entry,
                "source_changed": source_changed,
                "needs_render": render_needed,
                "needs_insert": insert_needed,
                "is_new": not bool(old_entry),
                "force": args.force,
            }
        )

    render_failed = _render_phase(plan, env, source_dir, data_dir, derived_dir, next_manifest)
    inserted_added, inserted_updated, transient = _insert_phase(plan, port, data_dir, kb_docs, derived_dir, next_manifest)
    retry_added, retry_updated = _retry_transient_inserts(transient, port, data_dir, kb_docs, derived_dir, next_manifest)
    added += inserted_added + retry_added
    updated += inserted_updated + retry_updated
    insert_failed = sum(1 for item in plan if item["needs_insert"] and item["entry"].get("insert_status") == "failed")

    if kb_docs is not None:
        orphans = {rel: ids for rel, ids in kb_docs.items() if rel not in present_rel}
        if orphans:
            _phase_header("prune", len(orphans))
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
        f"{removed} rimossi, {render_failed} render falliti, {insert_failed} insert falliti."
    )
    sys.exit(1 if (render_failed or insert_failed) else 0)


if __name__ == "__main__":
    main()
