"""Tests for pipeline/parsers.py (§7 of monolithic plan).

Tests:
- File category detection (text, pdf, images, office, audio, unsupported).
- Binary SHA-256 and text SHA-256 computation.
- Content-stable canonical doc ID generation.
- Plain text / markdown / code parsing with encoding fallback and NUL-byte stripping.
- Linux Docling configuration:
  - Enforces Linux OCR engines (RapidOCR / EasyOCR / Tesseract).
  - Explicitly rejects macOS-only OcrMacOptions on Linux.
  - Enforces CPU accelerator (or CUDA), explicitly rejects Apple MPS.
  - Enforces Linux ASR (whisper_turbo_native), explicitly rejects WHISPER_TURBO_MLX.
- Garbled text detection and automatic force_full_page_ocr trigger for scanned/corrupted PDFs.
- Native Office document parsing.
- Audio parsing with ffmpeg validation.
- Persistent derived artifacts: Markdown extraction and structured JSON sidecar persistence.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.parsers import (
    GARBLED_RANGES,
    LinuxConfigurationError,
    ParsedDocument,
    ParserError,
    UnsupportedFormatError,
    build_docling_audio_code,
    build_docling_native_code,
    build_docling_visual_code,
    compute_file_sha256,
    compute_text_sha256,
    generate_canonical_doc_id,
    get_file_category,
    get_linux_docling_config,
    looks_garbled,
    parse_audio_document,
    parse_document,
    parse_native_document,
    parse_text_file,
    parse_visual_document,
    save_derived_artifacts,
)


# -----------------------------------------------------------------------------
# Classification & Hashing Tests
# -----------------------------------------------------------------------------


def test_get_file_category():
    assert get_file_category("notes.md") == "text"
    assert get_file_category("main.py") == "text"
    assert get_file_category("config.yaml") == "text"
    assert get_file_category("document.pdf") == "pdf"
    assert get_file_category("image.png") == "images"
    assert get_file_category("photo.jpeg") == "images"
    assert get_file_category("spreadsheet.xlsx") == "office"
    assert get_file_category("slides.pptx") == "office"
    assert get_file_category("report.docx") == "office"
    assert get_file_category("book.epub") == "office"
    assert get_file_category("audio.wav") == "audio"
    assert get_file_category("voice.mp3") == "audio"
    assert get_file_category("archive.zip") == "unsupported"
    assert get_file_category("binary.exe") == "unsupported"


def test_hashing_and_canonical_id(tmp_path: Path):
    f = tmp_path / "hello.txt"
    f.write_text("Hello LightRAG", encoding="utf-8")

    f_hash = compute_file_sha256(f)
    t_hash = compute_text_sha256("Hello LightRAG")
    assert f_hash == t_hash
    assert len(f_hash) == 64

    # Canonical doc ID
    doc_id = generate_canonical_doc_id(f_hash)
    assert doc_id == f"doc_{f_hash[:16]}"

    # With revision
    doc_id_rev2 = generate_canonical_doc_id(f_hash, revision=2)
    assert doc_id_rev2 == f"doc_{f_hash[:16]}_r2"


# -----------------------------------------------------------------------------
# Plain Text & Code Parsing Tests
# -----------------------------------------------------------------------------


def test_parse_text_file_utf8(tmp_path: Path):
    p = tmp_path / "note.md"
    p.write_text("# Title\n\nThis is a sample markdown note with emoji: 🚀\n", encoding="utf-8")

    parsed = parse_text_file(p, source_uri="macbook:/notes/note.md")
    assert parsed.category == "text"
    assert parsed.parser_name == "text_plain"
    assert "# Title" in parsed.text_content
    assert parsed.metadata["encoding"] == "utf-8"
    assert parsed.metadata["line_count"] == 3
    assert parsed.sidecar["source_uri"] == "macbook:/notes/note.md"
    assert parsed.sidecar["stats"]["char_count"] == len(parsed.text_content)


def test_parse_text_file_latin1_fallback(tmp_path: Path):
    p = tmp_path / "latin1.txt"
    # Write text encoded in latin-1 (ISO-8859-1) containing characters invalid in UTF-8
    p.write_bytes("Parliamo d'affari: caffè e qualità.".encode("latin-1"))

    parsed = parse_text_file(p)
    assert parsed.category == "text"
    assert "caffè" in parsed.text_content
    assert parsed.metadata["encoding"] in ("cp1252", "latin-1")


def test_parse_text_file_strips_nul_bytes(tmp_path: Path):
    p = tmp_path / "nul_bytes.txt"
    p.write_bytes(b"Clean\x00Text\x00Without\x00Nul\n")

    parsed = parse_text_file(p)
    assert "\x00" not in parsed.text_content
    assert "CleanTextWithoutNul" in parsed.text_content


# -----------------------------------------------------------------------------
# Linux Docling Configuration & Safety Tests (§7)
# -----------------------------------------------------------------------------


def test_linux_docling_config_defaults():
    cfg = get_linux_docling_config({"DOCLING_PLATFORM": "linux"})
    assert cfg["platform"] == "linux"
    assert cfg["ocr_engine"] == "rapidocr"
    assert cfg["accelerator"] == "CPU"
    assert cfg["asr_engine"] == "whisper_turbo_native"
    assert cfg["enable_tables"] is True
    assert cfg["enable_code"] is True
    assert cfg["enable_formula"] is True


def test_linux_docling_rejects_mac_ocr_options():
    with pytest.raises(LinuxConfigurationError, match="OcrMacOptions non è supportato"):
        get_linux_docling_config({
            "DOCLING_PLATFORM": "linux",
            "DOCLING_OCR_ENGINE": "ocrmac",
        })


def test_linux_docling_rejects_mps_accelerator():
    with pytest.raises(LinuxConfigurationError, match="Acceleratore MPS non disponibile"):
        get_linux_docling_config({
            "DOCLING_PLATFORM": "linux",
            "DOCLING_ACCELERATOR": "MPS",
        })


def test_linux_docling_rejects_whisper_turbo_mlx():
    with pytest.raises(LinuxConfigurationError, match="WHISPER_TURBO_MLX è esclusivo per macOS"):
        get_linux_docling_config({
            "DOCLING_PLATFORM": "linux",
            "DOCLING_ASR_ENGINE": "whisper_turbo_mlx",
        })


def test_build_docling_visual_code_linux_rapidocr(tmp_path: Path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    cfg = get_linux_docling_config({
        "DOCLING_PLATFORM": "linux",
        "DOCLING_OCR_ENGINE": "rapidocr",
        "DOCLING_ACCELERATOR": "CPU",
        "DOCLING_ENABLE_TABLES": "1",
    })

    code = build_docling_visual_code(pdf, cfg, force_full_page=False)
    assert "RapidOcrOptions" in code
    assert "OcrMacOptions" not in code
    assert "AcceleratorDevice.CPU" in code
    assert "AcceleratorDevice.MPS" not in code
    assert "opts.do_table_structure = True" in code
    assert "TableFormerMode" in code


def test_build_docling_visual_code_disables_tables_when_flag_off(tmp_path: Path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    cfg = get_linux_docling_config({
        "DOCLING_PLATFORM": "linux",
        "DOCLING_OCR_ENGINE": "easyocr",
        "DOCLING_ACCELERATOR": "CPU",
        "DOCLING_ENABLE_TABLES": "0",
    })

    code = build_docling_visual_code(pdf, cfg, force_full_page=True)
    assert "EasyOcrOptions" in code
    assert "opts.do_table_structure = False" in code
    assert "TableFormerMode" not in code


# -----------------------------------------------------------------------------
# Garbled Text Detection & OCR Trigger Tests (§7)
# -----------------------------------------------------------------------------


def test_looks_garbled_detection():
    # Normal text
    clean_text = "Questo è un paragrafo in italiano contenente testo leggibile di normale lunghezza con parole chiare."
    assert not looks_garbled(clean_text)

    # Garbled text filled with Unicode private use or box characters
    garbled_chars = "".join(chr(0xE000 + (i % 500)) for i in range(100))
    garbled_sample = "Testo " + garbled_chars + " confuso"
    assert looks_garbled(garbled_sample)


def test_parse_visual_document_with_garbled_probe(tmp_path: Path):
    pdf = tmp_path / "scanned.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy scanned content")

    executed_scripts: list[str] = []

    def fake_runner(python_bin, code, timeout, label):
        executed_scripts.append(code)
        if "probe" in label:
            # Return garbled text from probe
            return "A" + "".join(chr(0xE000 + i) for i in range(80))
        # Return converted markdown
        return "# OCR Extracted Page\n\nClean text recovered after full page OCR.\n"

    parsed = parse_visual_document(
        pdf,
        env={"DOCLING_PLATFORM": "linux", "DOCLING_OCR_ENGINE": "rapidocr"},
        runner=fake_runner,
    )

    assert parsed.category == "pdf"
    assert parsed.metadata["force_full_page_ocr"] is True
    assert "# OCR Extracted Page" in parsed.text_content
    # Ensure probe was called and then full OCR script was executed with force_full_page_ocr=True
    assert len(executed_scripts) == 2
    assert "force_full_page_ocr=True" in executed_scripts[1]


# -----------------------------------------------------------------------------
# Office & Audio Parsing Tests (§7)
# -----------------------------------------------------------------------------


def test_parse_native_document(tmp_path: Path):
    docx = tmp_path / "memo.docx"
    docx.write_bytes(b"PK\x03\x04 dummy docx")

    def fake_runner(python_bin, code, timeout, label):
        assert "DocumentConverter().convert" in code
        return "# Office Document\n\nParagraph 1 from Word document.\n"

    parsed = parse_native_document(
        docx,
        env={"DOCLING_PLATFORM": "linux"},
        runner=fake_runner,
    )
    assert parsed.category == "office"
    assert parsed.parser_name == "docling_native"
    assert "Paragraph 1" in parsed.text_content


def test_parse_audio_document_requires_ffmpeg(tmp_path: Path, monkeypatch):
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF dummy wav")

    monkeypatch.setattr("shutil.which", lambda prog: None)
    with pytest.raises(ParserError, match="ffmpeg non trovato"):
        parse_audio_document(wav, env={"DOCLING_PLATFORM": "linux"})


def test_parse_audio_document_linux_whisper(tmp_path: Path, monkeypatch):
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF dummy wav")

    monkeypatch.setattr("shutil.which", lambda prog: "/usr/bin/ffmpeg")

    executed: list[str] = []

    def fake_runner(python_bin, code, timeout, label):
        executed.append(code)
        return "# Transcript\n\n[00:01] Parliamo di architettura software.\n"

    parsed = parse_audio_document(
        wav,
        env={"DOCLING_PLATFORM": "linux", "DOCLING_ASR_ENGINE": "whisper_turbo_native"},
        runner=fake_runner,
    )
    assert parsed.category == "audio"
    assert "WHISPER_TURBO_NATIVE" in executed[0]
    assert "WHISPER_TURBO_MLX" not in executed[0]
    assert "[00:01]" in parsed.text_content


def test_parse_document_dispatcher(tmp_path: Path):
    txt = tmp_path / "code.py"
    txt.write_text("print('hello world')", encoding="utf-8")

    parsed = parse_document(txt)
    assert parsed.category == "text"
    assert "hello world" in parsed.text_content

    # Unsupported format
    unsupported = tmp_path / "file.xyz123"
    unsupported.write_bytes(b"unknown")
    with pytest.raises(UnsupportedFormatError):
        parse_document(unsupported)


# -----------------------------------------------------------------------------
# Derived Artifact & Sidecar Persistence (§7)
# -----------------------------------------------------------------------------


def test_save_derived_artifacts_persistence(tmp_path: Path):
    derived_dir = tmp_path / "derived"
    src_file = tmp_path / "report.md"
    src_file.write_text("# Important Report\n\nDetailed findings.", encoding="utf-8")

    parsed = parse_document(src_file, source_uri="nas:/vol1/report.md")

    md_path, sidecar_path = save_derived_artifacts(
        parsed=parsed,
        derived_dir=derived_dir,
        relative_path="projects/report.md",
        source_uri="nas:/vol1/report.md",
    )

    # 1. Verify markdown output path and content
    assert md_path.exists()
    assert md_path == derived_dir / "text" / "projects" / "report.md.md"
    assert md_path.read_text(encoding="utf-8") == parsed.text_content

    # 2. Verify relative sidecar path and content
    assert sidecar_path.exists()
    assert sidecar_path == derived_dir / "text" / "projects" / "report.md.sidecar.json"
    sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))

    assert sidecar_data["canonical_doc_id"] == parsed.canonical_doc_id
    assert sidecar_data["binary_sha256"] == parsed.binary_sha256
    assert sidecar_data["derived_hash"] == parsed.derived_hash
    assert sidecar_data["category"] == "text"
    assert sidecar_data["source_uri"] == "nas:/vol1/report.md"
    assert sidecar_data["markdown_artifact_path"] == str(md_path)
    assert sidecar_data["stats"]["char_count"] == len(parsed.text_content)

    # 3. Verify canonical sidecar index
    canonical_sidecar = derived_dir / "sidecars" / f"{parsed.canonical_doc_id}.json"
    assert canonical_sidecar.exists()
    canonical_data = json.loads(canonical_sidecar.read_text(encoding="utf-8"))
    assert canonical_data["canonical_doc_id"] == parsed.canonical_doc_id
