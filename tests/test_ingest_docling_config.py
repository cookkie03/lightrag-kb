from pathlib import Path

import ingest
import pytest


def test_linux_docling_visual_uses_rapidocr_when_configured(monkeypatch):
    captured: dict[str, str] = {}

    def fake_run_docling(py, code, timeout, label):
        captured["code"] = code
        return "ok"

    monkeypatch.setattr(ingest, "_run_docling", fake_run_docling)
    monkeypatch.setattr(ingest, "_docling_probe_text", lambda *args, **kwargs: "")

    ingest.render_docling_visual(
        path=Path("/tmp/sample.pdf"),
        env={
            "DOCLING_PYTHON": "/tmp/python",
            "DOCLING_LANG": "it,en",
            "DOCLING_TIMEOUT": "3600",
            "DOCLING_PLATFORM": "linux",
            "DOCLING_OCR_ENGINE": "rapidocr",
            "DOCLING_ACCELERATOR": "cpu",
            "DOCLING_ENABLE_TABLES": "1",
            "DOCLING_ENABLE_CODE_ENRICHMENT": "1",
            "DOCLING_ENABLE_FORMULA_ENRICHMENT": "1",
            "DOCLING_IMAGE_SCALE": "2.0",
        },
    )

    assert "RapidOcrOptions" in captured["code"]
    assert "AcceleratorDevice.CPU" in captured["code"]
    assert "opts.do_table_structure = True" in captured["code"]
    assert "opts.images_scale = 2.0" in captured["code"]


def test_linux_docling_audio_uses_native_whisper(monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setattr(ingest.shutil, "which", lambda _: "/usr/bin/ffmpeg")

    def fake_run_docling(py, code, timeout, label):
        captured["code"] = code
        return "ok"

    monkeypatch.setattr(ingest, "_run_docling", fake_run_docling)

    ingest.render_docling_audio(
        Path("/tmp/sample.wav"),
        {
            "DOCLING_PYTHON": "/tmp/python",
            "DOCLING_TIMEOUT": "3600",
            "DOCLING_LANG": "it,en",
            "DOCLING_PLATFORM": "linux",
            "DOCLING_ASR_ENGINE": "whisper_turbo_native",
        },
    )

    assert "WHISPER_TURBO_NATIVE" in captured["code"]


def test_docling_visual_omits_table_specific_code_when_tables_disabled(monkeypatch):
    captured: dict[str, str] = {}

    def fake_run_docling(py, code, timeout, label):
        captured["code"] = code
        return "ok"

    monkeypatch.setattr(ingest, "_run_docling", fake_run_docling)
    monkeypatch.setattr(ingest, "_docling_probe_text", lambda *args, **kwargs: "")

    ingest.render_docling_visual(
        path=Path("/tmp/sample.pdf"),
        env={
            "DOCLING_PYTHON": "/tmp/python",
            "DOCLING_LANG": "it,en",
            "DOCLING_TIMEOUT": "3600",
            "DOCLING_PLATFORM": "linux",
            "DOCLING_OCR_ENGINE": "rapidocr",
            "DOCLING_ACCELERATOR": "cpu",
            "DOCLING_ENABLE_TABLES": "0",
        },
    )

    assert "opts.do_table_structure = False" in captured["code"]
    assert "TableFormerMode" not in captured["code"]
    assert "TableStructureOptions" not in captured["code"]
    assert "table_structure_options" not in captured["code"]


def test_docling_visual_defaults_to_linux_easyocr_and_cpu(monkeypatch):
    captured: dict[str, str] = {}

    def fake_run_docling(py, code, timeout, label):
        captured["code"] = code
        return "ok"

    monkeypatch.setattr(ingest, "_run_docling", fake_run_docling)
    monkeypatch.setattr(ingest, "_docling_probe_text", lambda *args, **kwargs: "")

    ingest.render_docling_visual(
        path=Path("/tmp/sample.pdf"),
        env={
            "DOCLING_PYTHON": "/tmp/python",
            "DOCLING_LANG": "it,en",
            "DOCLING_TIMEOUT": "3600",
            "DOCLING_PLATFORM": "linux",
        },
    )

    assert "EasyOcrOptions" in captured["code"]
    assert "AcceleratorDevice.CPU" in captured["code"]


def test_docling_accelerator_device_rejects_unsupported_explicit_value():
    with pytest.raises(RuntimeError, match="Unsupported DOCLING_ACCELERATOR"):
        ingest._docling_accelerator_device({"DOCLING_ACCELERATOR": "typo"})
