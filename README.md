# LightRAG-KB

CLI minimale per gestire knowledge base LightRAG locali con:

- WebUI come superficie principale per gli umani
- MCP query-only per coding agent
- un solo runtime Python condiviso per `ragcli`, `lightrag-server`, LightRAG e Docling
- ingest incrementale dalla cartella sorgente reale

## Modello operativo

Per ogni KB:

- `source_dir` contiene i file originali da indicizzare
- `source_dir/.lightrag/rag_storage/` contiene lo storage interno LightRAG
- `source_dir/.lightrag/derived/` contiene gli artefatti Docling prodotti da noi

Layout standard:

```text
<source_dir>/.lightrag/
  .env
  derived/
    text/
    pdf/
    images/
    office/
    audio/
    cache/
    manifests/
  rag_storage/
```

Il server parte sempre con:

```bash
lightrag-server --input-dir <source_dir> --working-dir <source_dir>/.lightrag/rag_storage --port <port>
```

`ragcli start` è l’unico comando della CLI che avvia il server. `ingest` e MCP non fanno auto-start.

## Setup

Dopo il clone:

```bash
./setup.sh
```

Lo script:

1. crea `.venv-engine`
2. installa lì `lightrag-hku[api]`, `ollama`, `docling[asr,ocrmac,easyocr]`, `fastmcp`, `requests`, `pyyaml`
3. crea i symlink:
   - `~/.local/bin/ragcli`
   - `~/.local/bin/lightrag-server`
4. crea `config/global.env` da `config/global.env.example` se mancante

## CLI pubblica

```bash
ragcli add <name> <source_dir> [--port]
ragcli list
ragcli ingest <name> [--force]
ragcli exclude add <name> <pattern>
ragcli exclude rm <name> <pattern>
ragcli start <name>
ragcli stop <name>
ragcli remove <name>
ragcli mcp add <name> --client <client|all>
ragcli mcp rm <name> --client <client|all>
ragcli move <name>
```

Esempio rapido:

```bash
ragcli add progetto ~/code/mio-progetto
ragcli start progetto
ragcli ingest progetto
ragcli mcp add progetto --client codex
```

WebUI: `http://127.0.0.1:<porta>`

`ragcli remove <name>`:

- richiede server fermo
- rimuove la KB dal registry
- elimina `data_dir` completa, quindi `.lightrag/`, `derived/`, `rag_storage/`, `.env`, log e cache
- prova anche a de-registrare l'MCP dai client supportati
- preserva integralmente `source_dir` e i file originali

## Ingest

`ragcli ingest <name>`:

- sincronizza prima la `.env` locale della KB con la configurazione globale
- scopre i candidati dalla `source_dir`
- esegue render e insert come fasi separate
- per testo/codice usa il contenuto originale
- per PDF e immagini usa Docling con profilo high-quality per macOS
- per Office, ODF, EPUB, EML, MSG usa il parsing nativo Docling
- per audio/video usa Docling ASR con `WHISPER_TURBO_MLX`
- salva i derivati in `derived/...`
- usa un manifest phase-aware per saltare rerender inutili e ritentare solo gli insert mancati
- prova gli insert in un ordine più stabile: testo, office, PDF/immagini, audio/video
- mantiene sync incrementale con prune dei file rimossi

`ragcli ingest <name> --force`:

- rigenera i derivati
- ricostruisce manifest e cache locali
- riallinea forzatamente il KB con la sorgente

## Docling su macOS

Il profilo usato nel repo è allineato alla documentazione ufficiale Docling:

- `OcrMacOptions(recognition="accurate")`
- `force_full_page_ocr=True` solo quando il probe del testo nativo risulta degradato, oppure per le immagini
- `do_table_structure=True`
- `TableFormerMode.ACCURATE`
- `do_cell_matching=True`
- `images_scale=2.0`
- `do_formula_enrichment=True`
- `do_code_enrichment=True`
- `AcceleratorDevice.MPS`
- ASR `WHISPER_TURBO_MLX` su Apple Silicon

Riferimenti ufficiali:

- [Installation](https://docling-project.github.io/docling/getting_started/installation/)
- [Advanced options](https://docling-project.github.io/docling/usage/advanced_options/)
- [Supported formats](https://docling-project.github.io/docling/usage/supported_formats/)
- [Processing audio and video](https://docling-project.github.io/docling/usage/processing_audio_media/)
- [GPU support](https://docling-project.github.io/docling/usage/gpu/)

Nota: la docs aggiornata indica che ODF (`.odt`, `.ods`, `.odp`) è supportato da Docling, quindi non viene più trattato come formato da convertire a priori.

## MCP

`mcp/lightrag_mcp.py` espone solo 3 tool:

- `ask`
- `retrieve_context`
- `find_documents`

Se il server è fermo, l’MCP non prova ad avviarlo e restituisce un messaggio esplicito che rimanda a `ragcli start <name>`.

## Config globale

`config/global.env` controlla il runtime condiviso e i parametri globali di LightRAG/Docling.

Variabili principali:

- `ENGINE_PYTHON`
- `LIGHTRAG_SERVER_BIN`
- `DOCLING_PYTHON`
- `DOCLING_LANG`
- `DOCLING_TIMEOUT`
- `LLM_PROVIDER`
- `OLLAMA_HOST`
- `LLM_MODEL`
- `EMBEDDING_MODEL`
- `BASE_PORT`
