"""Parser module for LightRAG-KB Linux ingestion pipeline.

Implements §7 of the monolithic plan:
- Baseline support: Markdown/txt/code, PDF, Office/ODF/EPUB/email via Docling,
  OCR of PDF/images, audio/spoken content via Linux ASR.
- Linux-first engines: RapidOCR / EasyOCR / Tesseract on CPU, Whisper Linux
  via whisper_turbo_native / faster-whisper.
- Strict elimination of macOS-specific options (OcrMacOptions, MPS accelerator,
  WHISPER_TURBO_MLX) in Debian codepaths.
- Persistent derived artifacts: Markdown extractions and structured JSON sidecars
  containing page metadata, timecodes, hashes, and original source URIs.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TEXT_EXT: set[str] = {
    ".md", ".markdown", ".txt", ".rst", ".org", ".tex", ".adoc", ".asciidoc",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
    ".sh", ".zsh", ".bash", ".fish", ".ps1", ".bat",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".xml", ".html", ".htm", ".sql", ".csv", ".tsv", ".log", ".diff", ".patch", ".rtf",
}

PDF_EXT: set[str] = {".pdf"}

IMAGE_EXT: set[str] = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}

OFFICE_EXT: set[str] = {
    ".docx", ".dotx", ".docm", ".dotm",
    ".pptx", ".potx", ".ppsx", ".pptm", ".potm", ".ppsm",
    ".xlsx", ".xlsm",
    ".odt", ".ott", ".fodt", ".ods", ".ots", ".fods", ".odp", ".otp", ".fodp",
    ".epub", ".eml", ".msg",
}

AUDIO_VIDEO_EXT: set[str] = {
    ".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".avi", ".mov"
}

ALL_SUPPORTED_EXT: set[str] = TEXT_EXT | PDF_EXT | IMAGE_EXT | OFFICE_EXT | AUDIO_VIDEO_EXT

GARBLED_RANGES = (
    (0x2700, 0x27BF),
    (0x2580, 0x259F),
    (0xE000, 0xF8FF),
    (0x1F0A0, 0x1F0FF),
)

LOCALE_MAP: dict[str, str] = {
    "it": "it-IT",
    "en": "en-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "pt": "pt-PT",
    "nl": "nl-NL",
}


class ParserError(Exception):
    """Base exception for parsing errors."""


class SafePathError(ParserError, ValueError):
    """Raised when a file path violates sandbox boundaries or safety rules (§11 #10)."""


class UnsupportedFormatError(ParserError):
    """Raised when an unhandled file format is encountered."""


class LinuxConfigurationError(ParserError):
    """Raised when Mac-only options are requested in Linux mode."""


def validate_safe_relative_path(
    path: str | Path,
    base_dir: Path | str | None = None,
    allow_symlinks: bool = True,
) -> str:
    """Validate that path is a safe relative path within the allowed sandbox (§11 #10).

    Rules:
    - Path must not be empty.
    - Path must not contain NUL bytes.
    - Path must not be an absolute path (e.g. /etc/passwd or C:\\...).
    - Path must not contain directory traversal ('..').
    - If base_dir is provided:
      The resolved path must be relative to base_dir.resolve().
      If the path exists and is a symlink: its target must resolve inside base_dir.resolve().
    """
    raw = str(path).strip()
    if not raw:
        raise SafePathError("Il percorso non può essere vuoto.")
    if "\0" in raw:
        raise SafePathError(f"Il percorso contiene byte NUL non ammessi: {repr(raw)}")

    p = Path(raw)
    if p.is_absolute() or raw.startswith("/") or raw.startswith("\\") or (len(raw) > 1 and raw[1] == ":"):
        raise SafePathError(f"Percorso assoluto non consentito nella sandbox: '{raw}'")

    parts = p.parts
    if ".." in parts:
        raise SafePathError(f"Directory traversal ('..') non consentito: '{raw}'")

    normalized = os.path.normpath(raw)
    if (
        normalized == "."
        or normalized == ".."
        or normalized.startswith(".." + os.sep)
        or normalized.startswith(".." + "/")
    ):
        raise SafePathError(f"Percorso non valido o traversal rilevato: '{raw}'")

    if base_dir is not None:
        base = Path(base_dir).resolve()
        candidate = base / normalized
        try:
            resolved_candidate = candidate.resolve()
        except Exception as e:
            raise SafePathError(f"Impossibile risolvere il percorso '{raw}': {e}") from e

        # Ensure inside base
        try:
            resolved_candidate.relative_to(base)
        except ValueError:
            raise SafePathError(f"Percorso '{raw}' tenta di uscire dalla sandbox '{base}'")

        if candidate.is_symlink():
            if not allow_symlinks:
                raise SafePathError(f"Symlink non consentito: '{raw}'")
            try:
                target_resolved = Path(os.path.realpath(candidate)).resolve()
                target_resolved.relative_to(base)
            except ValueError:
                raise SafePathError(f"Symlink '{raw}' punta all'esterno della sandbox '{base}'")

    return normalized


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def compute_file_sha256(path: Path | str) -> str:
    """Compute binary SHA-256 hash of a file."""
    p = Path(path)
    digest = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def compute_text_sha256(text: str) -> str:
    """Compute SHA-256 hash of text encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def generate_canonical_doc_id(binary_sha256: str, revision: int = 1) -> str:
    """Generate content-stable synthetic canonical doc ID (§3)."""
    clean_hash = binary_sha256.strip().lower()
    prefix = clean_hash[:16] if clean_hash else "empty"
    if revision > 1:
        return f"doc_{prefix}_r{revision}"
    return f"doc_{prefix}"


def get_file_category(path: Path | str) -> str:
    """Classify file extension into standard category."""
    ext = Path(path).suffix.lower()
    if ext in PDF_EXT:
        return "pdf"
    if ext in IMAGE_EXT:
        return "images"
    if ext in OFFICE_EXT:
        return "office"
    if ext in AUDIO_VIDEO_EXT:
        return "audio"
    if ext in TEXT_EXT:
        return "text"
    return "unsupported"


def looks_garbled(text: str, threshold: float = 0.15) -> bool:
    """Check if extracted text contains an abnormal density of garbled Unicode characters."""
    real_letters = sum(1 for char in text if char.isalpha())
    garbled = sum(1 for char in text if any(lo <= ord(char) <= hi for lo, hi in GARBLED_RANGES))
    total = real_letters + garbled
    if total < 50:
        return False
    return (garbled / total) > threshold


@dataclass
class ParsedDocument:
    """Container for parsed text content, derived hashes, and structured sidecar metadata."""

    canonical_doc_id: str
    category: str
    text_content: str
    binary_sha256: str
    derived_hash: str
    parser_name: str
    parser_version: str
    metadata: dict[str, Any] = field(default_factory=dict)
    sidecar: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_linux_docling_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Resolve and validate Docling configuration for Debian/Linux environment (§7).

    Eliminates macOS-specific configurations and defaults to CPU with Linux OCR/ASR.
    """
    env = env or {}
    platform = env.get("DOCLING_PLATFORM", "linux").strip().lower()
    if platform in ("darwin", "macos", "mac"):
        platform = "mac"
    else:
        platform = "linux"

    ocr_engine = env.get("DOCLING_OCR_ENGINE", "auto").strip().lower()
    if ocr_engine == "auto":
        ocr_engine = "rapidocr" if platform == "linux" else "ocrmac"
    elif platform == "linux" and ocr_engine == "ocrmac":
        raise LinuxConfigurationError(
            "OcrMacOptions non è supportato su Linux/Debian. Configura 'rapidocr' o 'easyocr'."
        )

    accelerator = env.get("DOCLING_ACCELERATOR", "auto").strip().upper()
    if accelerator == "AUTO":
        accelerator = "CPU"
    elif platform == "linux" and accelerator == "MPS":
        raise LinuxConfigurationError(
            "Acceleratore MPS non disponibile su Linux/Debian. Usa 'CPU' o 'CUDA'."
        )

    asr_engine = env.get("DOCLING_ASR_ENGINE", "auto").strip().lower()
    if asr_engine == "auto":
        asr_engine = "whisper_turbo_native"
    elif platform == "linux" and asr_engine == "whisper_turbo_mlx":
        raise LinuxConfigurationError(
            "WHISPER_TURBO_MLX è esclusivo per macOS (Apple Silicon). "
            "Su Linux/Debian usa 'whisper_turbo_native' o 'faster_whisper'."
        )

    langs = [item.strip() for item in env.get("DOCLING_LANG", "it,en").split(",") if item.strip()]
    locales = [LOCALE_MAP.get(item, item) for item in langs]

    def _bool_flag(key: str, default: str = "1") -> bool:
        return env.get(key, default).strip().lower() not in {"0", "false", "no", "off"}

    return {
        "platform": platform,
        "ocr_engine": ocr_engine,
        "accelerator": accelerator,
        "asr_engine": asr_engine,
        "python_bin": env.get("DOCLING_PYTHON", sys.executable),
        "timeout": int(env.get("DOCLING_TIMEOUT", "3600")),
        "langs": langs,
        "locales": locales,
        "enable_tables": _bool_flag("DOCLING_ENABLE_TABLES", "1"),
        "enable_code": _bool_flag("DOCLING_ENABLE_CODE_ENRICHMENT", "1"),
        "enable_formula": _bool_flag("DOCLING_ENABLE_FORMULA_ENRICHMENT", "1"),
        "image_scale": float(env.get("DOCLING_IMAGE_SCALE", "2.0")),
    }


def run_docling_subprocess(
    python_bin: str,
    code: str,
    timeout: int = 3600,
    label: str = "Docling execution",
) -> str:
    """Execute Docling script inside isolated Python process to avoid memory leaks or crash propagation."""
    result = subprocess.run(
        [python_bin, "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        err_msg = (result.stderr or result.stdout)[-600:].strip()
        raise ParserError(f"{label} fallito (code {result.returncode}): {err_msg}")
    return result.stdout


def build_docling_visual_code(
    path: Path, config: dict[str, Any], force_full_page: bool
) -> str:
    """Build Python code string for Docling visual extraction (PDF & Images) on Linux."""
    ocr_engine = config["ocr_engine"]
    langs = config["langs"]
    locales = config["locales"]
    accelerator = config["accelerator"]
    do_tables = config["enable_tables"]
    do_formula = config["enable_formula"]
    do_code = config["enable_code"]
    image_scale = config["image_scale"]

    if ocr_engine == "rapidocr":
        ocr_import = "RapidOcrOptions"
        ocr_expr = f"RapidOcrOptions(lang={langs!r}, force_full_page_ocr={force_full_page!r})"
    elif ocr_engine == "easyocr":
        ocr_import = "EasyOcrOptions"
        ocr_expr = f"EasyOcrOptions(lang={langs!r}, force_full_page_ocr={force_full_page!r})"
    elif ocr_engine == "tesseract":
        ocr_import = "TesseractOcrOptions"
        ocr_expr = f"TesseractOcrOptions(lang={langs!r}, force_full_page_ocr={force_full_page!r})"
    elif ocr_engine == "ocrmac":
        ocr_import = "OcrMacOptions"
        ocr_expr = f"OcrMacOptions(lang={locales!r}, force_full_page_ocr={force_full_page!r})"
    else:
        raise LinuxConfigurationError(f"Motore OCR non supportato: {ocr_engine}")

    pipeline_imports = f"{ocr_import}, PdfPipelineOptions"
    table_setup = ""
    if do_tables:
        pipeline_imports += ", TableFormerMode, TableStructureOptions"
        table_setup = (
            "opts.table_structure_options = "
            "TableStructureOptions(do_cell_matching=True, mode=TableFormerMode.ACCURATE)\n"
        )

    code = (
        "import sys\n"
        "from pathlib import Path as _P\n"
        "from docling.datamodel.base_models import InputFormat\n"
        "from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions\n"
        f"from docling.datamodel.pipeline_options import {pipeline_imports}\n"
        "from docling.document_converter import DocumentConverter, PdfFormatOption\n"
        "opts = PdfPipelineOptions()\n"
        "opts.do_ocr = True\n"
        f"opts.do_table_structure = {do_tables!r}\n"
        f"{table_setup}"
        f"opts.ocr_options = {ocr_expr}\n"
        f"opts.images_scale = {image_scale!r}\n"
        f"opts.do_formula_enrichment = {do_formula!r}\n"
        f"opts.do_code_enrichment = {do_code!r}\n"
        f"opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.{accelerator}, num_threads=8)\n"
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
    return code


def build_docling_native_code(path: Path) -> str:
    """Build Python code string for Docling native office conversion."""
    return (
        "import sys\n"
        "from pathlib import Path as _P\n"
        "from docling.document_converter import DocumentConverter\n"
        f"doc = DocumentConverter().convert(_P({str(path)!r})).document\n"
        "sys.stdout.write(doc.export_to_markdown())\n"
    )


def build_docling_audio_code(path: Path, config: dict[str, Any]) -> str:
    """Build Python code string for Docling audio/video extraction using Linux ASR."""
    language = config["langs"][0] if config["langs"] else "en"
    mapping = {
        "whisper_turbo": "WHISPER_TURBO",
        "whisper_turbo_native": "WHISPER_TURBO_NATIVE",
        "whisper_s2t": "WHISPER_SMALL_S2T",
        "whisper_turbo_mlx": "WHISPER_TURBO_MLX",
    }
    symbol = mapping.get(config["asr_engine"], "WHISPER_TURBO_NATIVE")
    return (
        "import sys\n"
        "from pathlib import Path as _P\n"
        "from docling.datamodel import asr_model_specs\n"
        "from docling.datamodel.base_models import InputFormat\n"
        "from docling.datamodel.pipeline_options import AsrPipelineOptions\n"
        "from docling.document_converter import AudioFormatOption, DocumentConverter\n"
        "from docling.pipeline.asr_pipeline import AsrPipeline\n"
        f"asr_options = asr_model_specs.{symbol}.model_copy(update={{'language': {language!r}}})\n"
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


# -----------------------------------------------------------------------------
# Parser Implementations (§7)
# -----------------------------------------------------------------------------


def parse_text_file(
    path: Path,
    binary_sha256: str | None = None,
    source_uri: str = "",
) -> ParsedDocument:
    """Parse a plain text, markdown, or code document."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File non trovato: {path}")

    b_sha = binary_sha256 or compute_file_sha256(p)
    raw_bytes = p.read_bytes()

    # Strip NUL bytes before sending extracted text to downstream consumers.
    cleaned_bytes = raw_bytes.replace(b"\x00", b"")

    encoding_used = "utf-8"
    try:
        text = cleaned_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = cleaned_bytes.decode("cp1252")
            encoding_used = "cp1252"
        except UnicodeDecodeError:
            text = cleaned_bytes.decode("latin-1", errors="replace")
            encoding_used = "latin-1"

    d_hash = compute_text_sha256(text)
    canonical_id = generate_canonical_doc_id(b_sha)

    lines = text.splitlines()
    word_count = len(text.split())

    metadata = {
        "file_name": p.name,
        "extension": p.suffix.lower(),
        "encoding": encoding_used,
        "size_bytes": len(raw_bytes),
        "line_count": len(lines),
        "word_count": word_count,
        "char_count": len(text),
    }

    sidecar = {
        "schema_version": "1.0",
        "canonical_doc_id": canonical_id,
        "binary_sha256": b_sha,
        "derived_hash": d_hash,
        "category": "text",
        "source_uri": source_uri or str(p),
        "created_at": utc_now_iso(),
        "stats": {
            "char_count": len(text),
            "word_count": word_count,
            "line_count": len(lines),
            "page_count": 1,
        },
        "metadata": metadata,
    }

    return ParsedDocument(
        canonical_doc_id=canonical_id,
        category="text",
        text_content=text,
        binary_sha256=b_sha,
        derived_hash=d_hash,
        parser_name="text_plain",
        parser_version="text-v1",
        metadata=metadata,
        sidecar=sidecar,
    )


def parse_visual_document(
    path: Path,
    env: dict[str, str] | None = None,
    runner: Callable[[str, str, int, str], str] | None = None,
    binary_sha256: str | None = None,
    source_uri: str = "",
) -> ParsedDocument:
    """Parse PDF or image document using Linux Docling with OCR fallback (§7)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File non trovato: {path}")

    b_sha = binary_sha256 or compute_file_sha256(p)
    category = "pdf" if p.suffix.lower() in PDF_EXT else "images"
    config = get_linux_docling_config(env)
    exec_runner = runner or run_docling_subprocess

    # Probe text for PDFs to check for garbled characters
    force_full_page = category == "images"
    probe_sample = ""
    if category == "pdf":
        probe_code = (
            "import sys\n"
            "from pathlib import Path as _P\n"
            "from docling.datamodel.base_models import InputFormat\n"
            "from docling.datamodel.pipeline_options import PdfPipelineOptions\n"
            "from docling.document_converter import DocumentConverter, PdfFormatOption\n"
            "opts = PdfPipelineOptions()\n"
            "opts.do_ocr = False\n"
            "opts.do_table_structure = False\n"
            "converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})\n"
            f"doc = converter.convert(_P({str(p)!r}), page_range=(1, 5)).document\n"
            "sys.stdout.write(doc.export_to_markdown())\n"
        )
        try:
            probe_sample = exec_runner(
                config["python_bin"],
                probe_code,
                min(config["timeout"], 120),
                f"probe visual su {p.name}",
            )
            force_full_page = looks_garbled(probe_sample)
        except Exception:
            force_full_page = True

    code = build_docling_visual_code(p, config, force_full_page=force_full_page)
    markdown_content = exec_runner(
        config["python_bin"],
        code,
        config["timeout"],
        f"Docling visual su {p.name}",
    )

    d_hash = compute_text_sha256(markdown_content)
    canonical_id = generate_canonical_doc_id(b_sha)

    lines = markdown_content.splitlines()
    word_count = len(markdown_content.split())

    metadata = {
        "file_name": p.name,
        "extension": p.suffix.lower(),
        "ocr_engine": config["ocr_engine"],
        "accelerator": config["accelerator"],
        "force_full_page_ocr": force_full_page,
        "line_count": len(lines),
        "word_count": word_count,
        "char_count": len(markdown_content),
    }

    sidecar = {
        "schema_version": "1.0",
        "canonical_doc_id": canonical_id,
        "binary_sha256": b_sha,
        "derived_hash": d_hash,
        "category": category,
        "source_uri": source_uri or str(p),
        "created_at": utc_now_iso(),
        "stats": {
            "char_count": len(markdown_content),
            "word_count": word_count,
            "line_count": len(lines),
        },
        "metadata": metadata,
    }

    return ParsedDocument(
        canonical_doc_id=canonical_id,
        category=category,
        text_content=markdown_content,
        binary_sha256=b_sha,
        derived_hash=d_hash,
        parser_name="docling_visual",
        parser_version=f"docling-visual-{config['ocr_engine']}-{config['accelerator'].lower()}",
        metadata=metadata,
        sidecar=sidecar,
    )


def parse_native_document(
    path: Path,
    env: dict[str, str] | None = None,
    runner: Callable[[str, str, int, str], str] | None = None,
    binary_sha256: str | None = None,
    source_uri: str = "",
) -> ParsedDocument:
    """Parse Office / ODF / EPUB / email documents using Docling native converter (§7)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File non trovato: {path}")

    b_sha = binary_sha256 or compute_file_sha256(p)
    config = get_linux_docling_config(env)
    exec_runner = runner or run_docling_subprocess

    code = build_docling_native_code(p)
    markdown_content = exec_runner(
        config["python_bin"],
        code,
        config["timeout"],
        f"Docling native su {p.name}",
    )

    d_hash = compute_text_sha256(markdown_content)
    canonical_id = generate_canonical_doc_id(b_sha)

    lines = markdown_content.splitlines()
    word_count = len(markdown_content.split())

    metadata = {
        "file_name": p.name,
        "extension": p.suffix.lower(),
        "line_count": len(lines),
        "word_count": word_count,
        "char_count": len(markdown_content),
    }

    sidecar = {
        "schema_version": "1.0",
        "canonical_doc_id": canonical_id,
        "binary_sha256": b_sha,
        "derived_hash": d_hash,
        "category": "office",
        "source_uri": source_uri or str(p),
        "created_at": utc_now_iso(),
        "stats": {
            "char_count": len(markdown_content),
            "word_count": word_count,
            "line_count": len(lines),
        },
        "metadata": metadata,
    }

    return ParsedDocument(
        canonical_doc_id=canonical_id,
        category="office",
        text_content=markdown_content,
        binary_sha256=b_sha,
        derived_hash=d_hash,
        parser_name="docling_native",
        parser_version="docling-native-v1",
        metadata=metadata,
        sidecar=sidecar,
    )


def parse_audio_document(
    path: Path,
    env: dict[str, str] | None = None,
    runner: Callable[[str, str, int, str], str] | None = None,
    binary_sha256: str | None = None,
    source_uri: str = "",
) -> ParsedDocument:
    """Parse audio or spoken video file using Linux ASR (whisper_turbo_native / faster-whisper) (§7)."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"File non trovato: {path}")

    if shutil.which("ffmpeg") is None:
        raise ParserError(
            "ffmpeg non trovato sul PATH di Debian. Installalo per consentire la trascrizione ASR."
        )

    b_sha = binary_sha256 or compute_file_sha256(p)
    config = get_linux_docling_config(env)
    exec_runner = runner or run_docling_subprocess

    code = build_docling_audio_code(p, config)
    markdown_content = exec_runner(
        config["python_bin"],
        code,
        config["timeout"],
        f"Docling audio su {p.name}",
    )

    d_hash = compute_text_sha256(markdown_content)
    canonical_id = generate_canonical_doc_id(b_sha)

    lines = markdown_content.splitlines()
    word_count = len(markdown_content.split())

    metadata = {
        "file_name": p.name,
        "extension": p.suffix.lower(),
        "asr_engine": config["asr_engine"],
        "line_count": len(lines),
        "word_count": word_count,
        "char_count": len(markdown_content),
    }

    sidecar = {
        "schema_version": "1.0",
        "canonical_doc_id": canonical_id,
        "binary_sha256": b_sha,
        "derived_hash": d_hash,
        "category": "audio",
        "source_uri": source_uri or str(p),
        "created_at": utc_now_iso(),
        "stats": {
            "char_count": len(markdown_content),
            "word_count": word_count,
            "line_count": len(lines),
        },
        "metadata": metadata,
    }

    return ParsedDocument(
        canonical_doc_id=canonical_id,
        category="audio",
        text_content=markdown_content,
        binary_sha256=b_sha,
        derived_hash=d_hash,
        parser_name="docling_audio",
        parser_version=f"docling-asr-{config['asr_engine']}",
        metadata=metadata,
        sidecar=sidecar,
    )


def parse_document(
    path: Path | str,
    env: dict[str, str] | None = None,
    runner: Callable[[str, str, int, str], str] | None = None,
    binary_sha256: str | None = None,
    source_uri: str = "",
) -> ParsedDocument:
    """Unified entry point to parse any supported document according to §7."""
    p = Path(path)
    cat = get_file_category(p)

    if cat == "text":
        return parse_text_file(p, binary_sha256=binary_sha256, source_uri=source_uri)
    if cat in ("pdf", "images"):
        return parse_visual_document(
            p, env=env, runner=runner, binary_sha256=binary_sha256, source_uri=source_uri
        )
    if cat == "office":
        return parse_native_document(
            p, env=env, runner=runner, binary_sha256=binary_sha256, source_uri=source_uri
        )
    if cat == "audio":
        return parse_audio_document(
            p, env=env, runner=runner, binary_sha256=binary_sha256, source_uri=source_uri
        )

    raise UnsupportedFormatError(f"Estensione o formato non supportato: {p.suffix}")


# -----------------------------------------------------------------------------
# Derived Artifact Persistence (§7)
# -----------------------------------------------------------------------------


def save_derived_artifacts(
    parsed: ParsedDocument,
    derived_dir: Path | str,
    relative_path: Path | str,
    source_uri: str = "",
) -> tuple[Path, Path]:
    """Persist extracted Markdown and structured JSON sidecar to Debian storage (§7, §11).

    Enforces safe sandboxed paths to prevent escaping the derived directory.

    Returns:
        tuple[Path, Path]: (markdown_artifact_path, sidecar_artifact_path)
    """
    root = Path(derived_dir).resolve()
    safe_rel_str = validate_safe_relative_path(relative_path, base_dir=root)
    rel = Path(safe_rel_str)

    # 1. Markdown artifact path: <derived_dir>/<category>/<relative_path>.md
    md_out_path = root / parsed.category / f"{rel.as_posix()}.md"
    if not md_out_path.resolve().is_relative_to(root):
        raise SafePathError(f"Destinazione markdown non sicura: '{md_out_path}'")
    md_out_path.parent.mkdir(parents=True, exist_ok=True)
    md_out_path.write_text(parsed.text_content, encoding="utf-8")

    # 2. Sidecar artifact path: <derived_dir>/<category>/<relative_path>.sidecar.json
    sidecar_out_path = root / parsed.category / f"{rel.as_posix()}.sidecar.json"
    if not sidecar_out_path.resolve().is_relative_to(root):
        raise SafePathError(f"Destinazione sidecar non sicura: '{sidecar_out_path}'")
    sidecar_out_path.parent.mkdir(parents=True, exist_ok=True)

    # Also persist canonical sidecar index: <derived_dir>/sidecars/<canonical_doc_id>.json
    canonical_sidecar_dir = root / "sidecars"
    canonical_sidecar_dir.mkdir(parents=True, exist_ok=True)
    canonical_sidecar_path = canonical_sidecar_dir / f"{parsed.canonical_doc_id}.json"

    sidecar_data = dict(parsed.sidecar)
    if source_uri:
        sidecar_data["source_uri"] = source_uri
    sidecar_data["markdown_artifact_path"] = str(md_out_path)
    sidecar_data["saved_at"] = utc_now_iso()

    json_content = json.dumps(sidecar_data, indent=2, ensure_ascii=False, sort_keys=True)
    sidecar_out_path.write_text(json_content, encoding="utf-8")
    canonical_sidecar_path.write_text(json_content, encoding="utf-8")

    return md_out_path, sidecar_out_path
