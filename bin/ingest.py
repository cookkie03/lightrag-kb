#!/usr/bin/env python3
"""ingest.py — OCR incrementale + inserimento in LightRAG per una KB.

Per ogni file della cartella sorgente:
  - testo/codice/dati  -> inserito direttamente
  - pdf/immagini       -> OCR via:
      - mineru-cloud  : API cloud mineru.net (veloce, richiede quota)
      - mineru        : mineru-api locale su :8000
      - glmocr        : glm-ocr via Ollama (tutto locale)
      - docling       : Docling (IBM), tutto locale, settato a massima qualità
    Con fallback automatico: se cloud fallisce → locale; se locale fallisce → errore.
  - office (docx/pptx/xlsx e varianti)
                       -> sempre via Docling (unico backend che li supporta),
                          indipendentemente dal backend OCR configurato per la KB.
  - audio/video        -> sempre via Docling ASR (Whisper MLX, nativo Apple Silicon),
                          indipendentemente dal backend OCR configurato per la KB.
  - OpenDocument (odt/ods/odp e varianti)
                       -> non supportati da Docling: segnalati con istruzioni di
                          conversione, mai saltati silenziosamente.
Inserimento via API REST del lightrag-server (POST /documents/text).
Cache incrementale su hash SHA-256: ri-OCR solo dei file cambiati.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests
import yaml

HOME = Path(__file__).resolve().parent.parent
CONFIG = HOME / "config" / "global.env"
REGISTRY = HOME / "registry.yaml"
KB_DIR = HOME / "kb"


def kb_data_dir(kb: dict) -> Path:
    """Dir dei dati della KB: registry `data_dir` se presente, altrimenti kb/<nome>/.
    Tenuta in sync con la stessa funzione in ragcli.py."""
    d = kb.get("data_dir")
    if d:
        return Path(d).expanduser()
    return KB_DIR / kb["name"]

TEXT_EXT = {
    # markup / doc
    ".md", ".markdown", ".txt", ".rst", ".org", ".tex",
    # code
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
    ".sh", ".zsh", ".bash", ".fish", ".ps1", ".bat",
    # data / config
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".env", ".xml", ".html", ".htm", ".sql",
    # misc text
    ".log", ".diff", ".patch", ".rtf",
}
OCR_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}

# Office (OOXML): nessun backend OCR li supporta tranne Docling -> sempre forzato,
# qualunque sia l'OCR_BACKEND configurato per la KB. Conversione diretta, senza OCR
# (Docling legge i formati nativamente: niente bitmap da scansionare).
OFFICE_EXT = {
    ".docx", ".dotx", ".docm", ".dotm",   # Word
    ".pptx", ".potx", ".ppsx", ".pptm", ".potm", ".ppsm",   # PowerPoint
    ".xlsx", ".xlsm",   # Excel
}

# Audio/video: sempre via Docling ASR (Whisper), qualunque sia l'OCR_BACKEND.
# I container video sono qui solo per estrarne la traccia audio (no analisi video).
AUDIO_VIDEO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".avi", ".mov"}

# OpenDocument: Docling non li supporta nativamente (nessun backend ODF). Non vengono
# ingestiti: l'utente viene avvisato con l'estensione Office equivalente da produrre
# (es. via LibreOffice headless), piuttosto che saltarli in silenzio.
ODF_TO_OFFICE = {
    ".odt": "docx", ".ott": "docx", ".fodt": "docx",
    ".odp": "pptx", ".otp": "pptx", ".fodp": "pptx",
    ".ods": "xlsx", ".ots": "xlsx", ".fods": "xlsx",
}

ALL_EXT = TEXT_EXT | OCR_EXT | OFFICE_EXT | AUDIO_VIDEO_EXT

MINERU_CLOUD_BASE = "https://mineru.net/api/v4"


# ---------------------------------------------------------------- helpers

def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- OCR: mineru cloud

def ocr_mineru_cloud(path: Path, g: dict) -> str:
    """3-step flow: presigned URL → PUT su OSS → batch task → poll → markdown."""
    token = g.get("MINERU_CLOUD_API_KEY", "")
    if not token:
        raise RuntimeError("MINERU_CLOUD_API_KEY non configurata")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    size = path.stat().st_size

    # step 1: ottieni URL pre-firmato
    r = requests.post(
        f"{MINERU_CLOUD_BASE}/file-urls/batch",
        headers=headers,
        json={"files": [{"name": path.name, "size": size}]},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"mineru cloud presign error: {d.get('msg')}")
    batch_id = d["data"]["batch_id"]
    upload_url = d["data"]["file_urls"][0]

    # step 2: upload file su OSS (PUT senza auth header)
    with open(path, "rb") as f:
        put_resp = requests.put(upload_url, data=f, timeout=300)
    put_resp.raise_for_status()

    # step 3: avvia task batch
    r = requests.post(
        f"{MINERU_CLOUD_BASE}/extract/task/batch",
        headers=headers,
        json={
            "batch_id": batch_id,
            "enable_formula": False,
            "language": "auto",
            "is_ocr": True,
            "enable_table": True,
            "files": [{"name": path.name, "url": upload_url, "data_id": path.name}],
        },
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"mineru cloud task error: {d.get('msg')}")
    result_batch_id = d["data"]["batch_id"]

    # step 4: poll fino a completamento (max 30 min)
    print(f"    ☁ mineru cloud: batch {result_batch_id[:8]}… in attesa", end="", flush=True)
    for _ in range(180):
        time.sleep(10)
        r = requests.get(
            f"{MINERU_CLOUD_BASE}/extract/task/batch",
            headers={"Authorization": f"Bearer {token}"},
            params={"batch_id": result_batch_id},
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError(f"mineru cloud poll error: {d.get('msg')}")
        tasks = d["data"].get("list", [])
        if not tasks:
            print(".", end="", flush=True)
            continue
        task = tasks[0]
        state = task.get("state", "")
        if state == "done":
            print(" ✓")
            # step 5: scarica il risultato zip
            zip_url = task.get("full_zip_url") or task.get("zip_url")
            if not zip_url:
                raise RuntimeError("mineru cloud: nessun zip_url nel risultato")
            zr = requests.get(zip_url, timeout=120)
            zr.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(zr.content)) as zf:
                md_files = [n for n in zf.namelist() if n.endswith(".md")]
                if not md_files:
                    raise RuntimeError("mineru cloud: nessun .md nello zip")
                return zf.read(md_files[0]).decode("utf-8", errors="replace")
        elif state in ("failed", "error"):
            raise RuntimeError(f"mineru cloud task fallito: {task.get('err_msg','?')}")
        print(".", end="", flush=True)
    raise RuntimeError("mineru cloud: timeout dopo 30 minuti")


# ---------------------------------------------------------------- OCR: mineru locale

def ocr_mineru_local(path: Path, g: dict, retries: int = 2) -> str:
    url = g.get("MINERU_API", "http://127.0.0.1:8000").rstrip("/") + "/file_parse"
    timeout = int(g.get("MINERU_TIMEOUT", "3600"))
    last_exc = None
    for attempt in range(1, retries + 2):
        try:
            with open(path, "rb") as f:
                resp = requests.post(
                    url,
                    files={"files": (path.name, f)},
                    data={"backend": "pipeline", "return_md": "true",
                          "return_content_list": "false", "return_images": "false"},
                    timeout=timeout,
                )
            resp.raise_for_status()
            results = resp.json().get("results", {})
            if not results:
                raise RuntimeError(f"mineru locale: nessun risultato per {path.name}")
            doc = next(iter(results.values()))
            return doc.get("md_content", "")
        except Exception as e:
            last_exc = e
            if attempt <= retries:
                print(f"    ⚠ tentativo {attempt} fallito ({e}), riprovo…")
    raise last_exc


# ---------------------------------------------------------------- OCR: glm-ocr

def ocr_glmocr(path: Path, g: dict) -> str:
    py = g.get("GLMOCR_PYTHON", "/Users/luca/.venv/bin/python")
    cfg = g.get("GLMOCR_CONFIG")
    code = (
        "import sys\n"
        "from glmocr import GlmOcr\n"
        f"with GlmOcr(config_path={cfg!r}) as ocr:\n"
        f"    r = ocr.parse({str(path)!r})\n"
        "    sys.stdout.write(r.markdown_result)\n"
    )
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"glm-ocr fallito su {path.name}: {r.stderr[-500:]}")
    return r.stdout


# ---------------------------------------------------------------- OCR: docling

_LOCALE_MAP = {
    "it": "it-IT", "en": "en-US", "fr": "fr-FR", "de": "de-DE", "es": "es-ES",
    "pt": "pt-PT", "nl": "nl-NL",
}


def ocr_docling(path: Path, g: dict) -> str:
    """Esegue Docling in un sottoprocesso (venv ML dedicato), settato a massima
    qualità: table structure in modalità ACCURATE, risoluzione immagine
    raddoppiata, formula/code enrichment abilitati. OCR applicato solo dove
    serve (pagine bitmap/scansionate), non forzato sul testo nativo.

    OCR engine: ocrmac (Vision framework nativo Apple, via Neural Engine) invece
    di EasyOCR — su M4 misurato ~3x più rapido a parità (o leggero miglioramento)
    di qualità del testo riconosciuto, senza costo aggiuntivo di RAM/VRAM.
    Accelerator espliciti su MPS per i modelli torch (layout, table structure).
    Vedi https://docling-project.github.io/docling/ per i riferimenti.
    """
    py = g.get("DOCLING_PYTHON", "/Users/luca/.venv/bin/python")
    langs = [l.strip() for l in g.get("DOCLING_LANG", "it,en").split(",") if l.strip()]
    locales = [_LOCALE_MAP.get(l, l) for l in langs]
    timeout = int(g.get("DOCLING_TIMEOUT", "3600"))
    code = (
        "import sys\n"
        "from docling.datamodel.base_models import InputFormat\n"
        "from docling.datamodel.pipeline_options import (\n"
        "    AcceleratorDevice, AcceleratorOptions, OcrMacOptions, PdfPipelineOptions,\n"
        "    TableFormerMode, TableStructureOptions,\n"
        ")\n"
        "from docling.document_converter import DocumentConverter, PdfFormatOption\n"
        "\n"
        "opts = PdfPipelineOptions()\n"
        "opts.do_ocr = True\n"
        "opts.do_table_structure = True\n"
        "opts.table_structure_options = TableStructureOptions(\n"
        "    do_cell_matching=True, mode=TableFormerMode.ACCURATE,\n"
        ")\n"
        f"opts.ocr_options = OcrMacOptions(lang={locales!r}, recognition='accurate')\n"
        "opts.images_scale = 2.0\n"
        "opts.do_formula_enrichment = True\n"
        "opts.do_code_enrichment = True\n"
        "opts.accelerator_options = AcceleratorOptions(\n"
        "    device=AcceleratorDevice.MPS, num_threads=8,\n"
        ")\n"
        "\n"
        "converter = DocumentConverter(\n"
        "    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}\n"
        ")\n"
        # Path esplicito, non stringa: convert() instrada le str come stream
        # risolto (perde la directory, mantiene solo il basename) e la pipeline
        # ASR la risolverebbe poi contro la cwd invece che contro il file reale.
        "from pathlib import Path as _P\n"
        f"doc = converter.convert(_P({str(path)!r})).document\n"
        "sys.stdout.write(doc.export_to_markdown())\n"
    )
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"docling fallito su {path.name}: {r.stderr[-500:]}")
    return r.stdout


# ---------------------------------------------------------------- Docling: office

def ocr_docling_office(path: Path, g: dict) -> str:
    """Office (docx/pptx/xlsx e varianti) via Docling: conversione diretta della
    struttura nativa del documento (testo, tabelle, layout slide), senza OCR —
    non ci sono bitmap da scansionare. Unico backend che li supporta: forzato a
    prescindere dall'OCR_BACKEND configurato per la KB."""
    py = g.get("DOCLING_PYTHON", "/Users/luca/.venv/bin/python")
    timeout = int(g.get("DOCLING_TIMEOUT", "3600"))
    code = (
        "import sys\n"
        "from docling.document_converter import DocumentConverter\n"
        "from pathlib import Path as _P\n"
        f"doc = DocumentConverter().convert(_P({str(path)!r})).document\n"
        "sys.stdout.write(doc.export_to_markdown())\n"
    )
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"docling (office) fallito su {path.name}: {r.stderr[-500:]}")
    return r.stdout


# ---------------------------------------------------------------- Docling: audio/video (ASR)

def ocr_docling_audio(path: Path, g: dict) -> str:
    """Audio/video via la pipeline ASR di Docling: trascrizione con Whisper in
    variante MLX (nativa Apple Silicon, nessuna dipendenza da openai-whisper),
    forzata esplicitamente — niente fallback silenzioso su whisper nativo se
    mlx-whisper non fosse installato, per evitare di girare su CPU senza saperlo.
    Richiede ffmpeg sul PATH (demuxing audio, anche dai container video).
    Lingua presa dal primo codice in DOCLING_LANG (stesso campo usato per l'OCR)."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg non trovato sul PATH: richiesto per l'OCR audio/video. "
            "Installa con 'brew install ffmpeg'."
        )
    py = g.get("DOCLING_PYTHON", "/Users/luca/.venv/bin/python")
    lang = next((l.strip() for l in g.get("DOCLING_LANG", "it,en").split(",") if l.strip()), "en")
    timeout = int(g.get("DOCLING_TIMEOUT", "3600"))
    code = (
        "import sys\n"
        "from docling.datamodel.asr_model_specs import WHISPER_TURBO_MLX\n"
        "from docling.datamodel.base_models import InputFormat\n"
        "from docling.datamodel.pipeline_options import AsrPipelineOptions\n"
        "from docling.document_converter import AudioFormatOption, DocumentConverter\n"
        "from docling.pipeline.asr_pipeline import AsrPipeline\n"
        "\n"
        f"asr_options = WHISPER_TURBO_MLX.model_copy(update={{'language': {lang!r}}})\n"
        "opts = AsrPipelineOptions(asr_options=asr_options)\n"
        "converter = DocumentConverter(format_options={\n"
        "    InputFormat.AUDIO: AudioFormatOption(pipeline_cls=AsrPipeline, pipeline_options=opts)\n"
        "})\n"
        # Path esplicito, non stringa: convert() instrada le str come stream
        # risolto (perde la directory, mantiene solo il basename) e la pipeline
        # ASR la risolverebbe poi contro la cwd invece che contro il file reale.
        "from pathlib import Path as _P\n"
        f"doc = converter.convert(_P({str(path)!r})).document\n"
        "sys.stdout.write(doc.export_to_markdown())\n"
    )
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"docling (audio/video ASR) fallito su {path.name}: {r.stderr[-500:]}")
    return r.stdout


# ---------------------------------------------------------------- OCR dispatcher con fallback

def ocr(path: Path, backend: str, g: dict) -> str:
    """Esegue OCR con fallback automatico cloud→locale in caso di errore."""
    if backend == "mineru-cloud":
        try:
            return ocr_mineru_cloud(path, g)
        except Exception as e:
            print(f"\n    ⚠ mineru cloud fallito ({e}), fallback su mineru locale…")
            return ocr_mineru_local(path, g)
    elif backend == "glmocr":
        return ocr_glmocr(path, g)
    elif backend == "docling":
        return ocr_docling(path, g)
    else:  # "mineru" (locale, default)
        return ocr_mineru_local(path, g)


# ---------------------------------------------------------------- LightRAG insert

def insert_text(port: int, text: str, source: str, retries: int = 8) -> str:
    """POST /documents/text. Ritorna 'ok' | 'exists' | 'empty'.

    LightRAG usa lo stesso 409 per DUE casi diversi e vanno distinti:
      - "Document storage already contains ..." -> il documento è GIÀ nella KB:
        non è un errore, si salta (rende l'ingest ri-eseguibile/idempotente).
      - pipeline occupata (scan/destructive in corso) -> si aspetta con backoff
        esponenziale (10s..5min, ~20 min totali)."""
    if not text.strip():
        return "empty"
    for attempt in range(1, retries + 2):
        resp = requests.post(
            f"http://127.0.0.1:{port}/documents/text",
            json={"text": text, "file_source": source},
            timeout=300,
        )
        if resp.status_code == 409:
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = resp.text or ""
            if "already contains" in detail or "already exists" in detail:
                return "exists"  # già presente: niente retry, si salta
            if attempt <= retries:  # occupata davvero: aspetta e riprova
                wait = min(10 * 2 ** (attempt - 1), 300)
                print(f"    ⏳ pipeline occupata (409), riprovo in {wait}s ({attempt}/{retries})…")
                time.sleep(wait)
                continue
        resp.raise_for_status()
        return "ok"
    raise RuntimeError("pipeline occupata: troppi 409 consecutivi")


# ---------------------------------------------------------------- mirror: stato KB

def fetch_kb_docs(port: int) -> dict | None:
    """Mappa file_path -> [doc_id] dei documenti attualmente nel KB.

    Interroga GET /documents (che ritorna {"statuses": {<stato>: [doc, ...]}}) e
    appiattisce tutti i bucket di stato. Ritorna None se la GET fallisce: in quel
    caso il chiamante degrada a sola-aggiunta (niente mirror, niente prune) per
    non rischiare cancellazioni su informazioni incomplete."""
    try:
        r = requests.get(f"http://127.0.0.1:{port}/documents", timeout=60)
        r.raise_for_status()
        statuses = r.json().get("statuses", {})
    except Exception as e:
        print(f"  ⚠ impossibile leggere i documenti del KB ({e}): mirror disattivato per questo run.")
        return None
    docs: dict[str, list[str]] = {}
    for bucket in statuses.values():
        for d in bucket or []:
            fp, did = d.get("file_path"), d.get("id")
            if fp and did:
                docs.setdefault(fp, []).append(did)
    return docs


def delete_docs(port: int, doc_ids: list[str]) -> bool:
    """Cancella documenti dal KB per doc_id (DELETE /documents/delete_document).
    delete_file resta False: non si toccano MAI i file sorgente. Ritorna True se ok."""
    if not doc_ids:
        return True
    try:
        r = requests.delete(
            f"http://127.0.0.1:{port}/documents/delete_document",
            json={"doc_ids": doc_ids, "delete_file": False, "delete_llm_cache": True},
            timeout=120,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"    ⚠ cancellazione doc fallita ({e}); proseguo.")
        return False


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="OCR incrementale + sync a specchio della KB col filesystem sorgente.")
    ap.add_argument("--kb", required=True)
    ap.add_argument("--force", action="store_true",
                    help="ignora la cache: ri-OCR e reinserisce tutti i file")
    ap.add_argument("--add", action="store_true",
                    help="solo-aggiunta: NON elimina nulla dal KB (niente prune degli orfani "
                         "né rimozione delle versioni vecchie dei file modificati)")
    args = ap.parse_args()

    g = load_env(CONFIG)
    reg = yaml.safe_load(REGISTRY.read_text()) or {"kbs": []}
    kb = next((k for k in reg["kbs"] if k["name"] == args.kb), None)
    if not kb:
        sys.exit(f"KB '{args.kb}' non trovata.")

    src = Path(kb["source_folder"])
    port = kb["port"]
    backend = kb.get("ocr_backend", g.get("OCR_BACKEND", "mineru"))
    kdir = kb_data_dir(kb)
    inputs = kdir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    cache_path = kdir / ".ocr_cache.json"
    cache = {} if args.force or not cache_path.exists() else json.loads(cache_path.read_text())

    # Guardia: sorgente inesistente/non montata -> stop, nessun danno al KB.
    if not src.is_dir():
        sys.exit(f"⚠ Cartella sorgente non disponibile: {src}\n"
                 f"  (non montata? cloud offline?) Nessuna modifica al KB.")

    # Escludi la data_dir della KB dalla scansione: se è dentro la sorgente
    # (es. <src>/.lightrag) rglob la attraverserebbe e re-indicizzerebbe i propri
    # output OCR (il filtro su p.name salta solo i dotfile, non le dot-cartelle).
    kdir_res = kdir.resolve()

    def _under_kdir(p: Path) -> bool:
        try:
            p.resolve().relative_to(kdir_res)
            return True
        except (ValueError, OSError):
            return False

    candidates = [p for p in src.rglob("*")
                  if p.is_file() and not p.name.startswith(".") and not _under_kdir(p)]
    files = [p for p in candidates if p.suffix.lower() in ALL_EXT]
    # ODF (odt/ods/odp...): Docling non li supporta -> segnalati, non saltati in silenzio.
    odf_files = [p for p in candidates if p.suffix.lower() in ODF_TO_OFFICE]
    # present_rel: tutti i file gestiti CHE COMPAIONO nel listing. I placeholder
    # cloud non scaricati compaiono comunque (is_file resta True) -> NON sono orfani.
    present_rel = {str(p.relative_to(src)) for p in files}

    if odf_files:
        print(f"⚠ {len(odf_files)} file in formato OpenDocument (non supportato da Docling):")
        for p in odf_files:
            target = ODF_TO_OFFICE[p.suffix.lower()]
            print(f"    · {p.relative_to(src)}  ->  convertilo in .{target}, es.: "
                  f"soffice --headless --convert-to {target} \"{p}\"")

    # Stato attuale del KB: serve a riconoscere modificati/orfani. None = GET fallita.
    kb_docs = fetch_kb_docs(port)
    mirror = kb_docs is not None and not args.add

    total = len(files)
    added = updated = unchanged = 0
    removed = 0
    pending: list[str] = []   # senza riscontro nel KB (vuoti/non disponibili) -> da ritentare
    failed: list[str] = []    # errori veri (OCR/insert)
    mode_lbl = ("" if mirror else "  [sola-aggiunta]" if args.add
                else "  [mirror off: KB non leggibile]")
    print(f"KB '{args.kb}': {total} file in {src} (OCR backend: {backend}){mode_lbl}")

    def save_cache():
        cache_path.write_text(json.dumps(cache, indent=2))

    for p in files:
        ext = p.suffix.lower()
        rel = str(p.relative_to(src))
        try:
            h = file_hash(p)
        except FileNotFoundError:
            print(f"  ⚠ file sparito (non sincronizzato?), salto: {rel}")
            pending.append(rel)
            cache.pop(rel, None)
            continue
        except (TimeoutError, OSError) as e:
            print(f"  ⚠ file non disponibile localmente (OneDrive/cloud non scaricato?), salto: {rel} ({e})")
            pending.append(rel)
            continue

        in_kb = kb_docs is not None and rel in kb_docs
        # INVARIATO: la cache concorda E (se conosciamo lo stato del KB) è davvero
        # nel KB. Se la cache dice "fatto" ma nel KB non c'è, NON saltiamo: reinseriamo.
        if not args.force and cache.get(rel) == h and (kb_docs is None or in_kb):
            unchanged += 1
            continue

        # MODIFICATO: esisteva una versione diversa (già nel KB, o hash cache diverso).
        is_modified = in_kb or (rel in cache and cache.get(rel) != h)
        # Rimuovi la versione precedente dal KB prima di reinserire (salvo --add).
        if in_kb and not args.add:
            if delete_docs(port, kb_docs.get(rel, [])):
                print(f"  ↻ aggiorno '{rel}': rimossa versione precedente dal KB")

        try:
            safe = rel.replace("/", "__")
            out = inputs / f"{safe}.{h[:12]}.md"   # cache OCR content-addressed
            # Compat con la vecchia cache OCR (<safe>.md senza hash): se la cache
            # conferma che il contenuto è invariato (cache[rel]==h), migrala al nome
            # content-addressed invece di ri-OCR. Va fatto PRIMA del cleanup, che
            # altrimenti la cancellerebbe.
            legacy = inputs / f"{safe}.md"
            if not out.exists() and legacy.exists() and cache.get(rel) == h:
                try:
                    legacy.replace(out)
                except OSError:
                    pass
            # cleanup: rimuovi vecchi output OCR di questo file con hash diverso
            prefix = safe + "."
            for old in inputs.iterdir():
                if old.name.startswith(prefix) and old.name.endswith(".md") and old != out:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            if ext in TEXT_EXT:
                md = p.read_text(errors="replace")
            elif out.exists():
                print(f"  [cache] OCR da inputs/ per {rel}")
                md = out.read_text(errors="replace")
            elif ext in OFFICE_EXT:
                # Forzato a Docling: unico backend che legge i formati Office nativi.
                print(f"  Docling(office) {rel}")
                md = ocr_docling_office(p, g)
                if md.strip():
                    out.write_text(md)
            elif ext in AUDIO_VIDEO_EXT:
                # Forzato a Docling ASR: unico backend con trascrizione audio/video.
                print(f"  Docling(ASR) {rel}")
                md = ocr_docling_audio(p, g)
                if md.strip():
                    out.write_text(md)
            else:
                label = {"mineru-cloud": "☁ cloud", "glmocr": "glm", "mineru": "locale", "docling": "docling"}
                print(f"  OCR({label.get(backend, backend)}) {rel}")
                md = ocr(p, backend, g)
                if md.strip():
                    out.write_text(md)   # salva solo OCR non vuoti (evita re-OCR stantio)

            res = insert_text(port, md, rel)
            if res == "empty":
                # Nessun contenuto nel KB: NON cachiamo, resta pending -> ritentato.
                pending.append(rel)
                cache.pop(rel, None)
                print(f"    ⚠ vuoto, salto (verrà ritentato al prossimo ingest)")
            elif res == "exists":
                cache[rel] = h
                save_cache()
                unchanged += 1
                print(f"    ↩ già nella KB")
            else:  # ok
                cache[rel] = h
                save_cache()
                if is_modified:
                    updated += 1
                    print(f"    ✓ aggiornato ({len(md)} char)")
                else:
                    added += 1
                    print(f"    ✓ inserito ({len(md)} char)")
        except Exception as e:
            failed.append(rel)
            print(f"    ✗ errore: {e}")

    # ---- PRUNE: rimuovi dal KB i documenti i cui file sono spariti dalla sorgente.
    if mirror:
        orphans = {fp: ids for fp, ids in kb_docs.items() if fp not in present_rel}
        if orphans:
            if not present_rel:
                # GUARDIA anti-wipe: sorgente vuota/non montata ma KB pieno -> niente prune.
                print(f"\n⚠ GUARDIA: nessun file leggibile nella sorgente ma il KB contiene "
                      f"{len(kb_docs)} documenti. Sorgente non montata/cloud offline? "
                      f"PRUNE ANNULLATO (nessuna cancellazione).")
            else:
                print(f"\nRimuovo {len(orphans)} documenti orfani dal KB (file spariti dalla sorgente):")
                for fp, ids in orphans.items():
                    if delete_docs(port, ids):
                        removed += 1
                        cache.pop(fp, None)
                        print(f"  - {fp}")
                save_cache()

    print(f"\nFatto: {added} nuovi, {updated} aggiornati, {unchanged} invariati, {removed} rimossi.")
    if pending:
        print(f"⏳ {len(pending)} senza riscontro nel KB (vuoti/non disponibili) — verranno ritentati:")
        for r in pending:
            print(f"    · {r}")
    if failed:
        print(f"✗ {len(failed)} falliti:")
        for r in failed:
            print(f"    · {r}")
    print(f"L'embedding/estrazione gira in background sul server. "
          f"Stato: http://127.0.0.1:{port} (WebUI)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
