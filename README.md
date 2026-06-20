# LightRAG-KB — RAG locale multi-knowledge-base

Tool per creare più knowledge base RAG locali con [LightRAG](https://github.com/hkuds/lightrag),
integrate con Ollama, con OCR di PDF/immagini, ingest di Office/audio/video via Docling, e un MCP
server per ogni KB collegabile a Claude Code.

## Setup
```bash
cp config/global.env.example config/global.env   # poi inserisci le tue API key (OpenRouter, MinerU cloud)
```
`config/global.env` e gli `.env` per-KB (`kb/<nome>/.env`, generati da `ragcli`) sono in `.gitignore`
perché contengono segreti/percorsi locali: non vanno committati. Allo stesso modo i dati delle KB
(`kb/<nome>/inputs/`, `kb/<nome>/rag_storage/`) sono esclusi perché contengono documenti personali.

## Componenti
- **LightRAG** installato isolato come uv tool (`uv tool install "lightrag-hku[api]" --with ollama`).
- **`bin/ragcli`** — CLI di gestione (in PATH via `~/.local/bin/ragcli`).
- **`config/global.env`** — config centrale (modelli, OCR, porte). Modificala qui.
- **`config/registry.yaml`** — elenco KB (editabile a mano).
- **`mcp/lightrag_mcp.py`** — MCP server per-KB (tool: `query`, `insert_text`, `kb_status`).
- **`~/Documents/Scripts/lightrag-toggle.command`** — accende/spegne tutte le KB abilitate (doppio click).

Ogni KB vive in `kb/<nome>/`: `rag_storage/` (grafo+vettori), `inputs/` (markdown OCR), `.env`, `.ocr_cache.json`.

## Workflow tipico
```bash
ragcli create miakb ~/percorso/cartella               # crea KB (provider: ollama, default)
ragcli create miakb ~/percorso/cartella --provider openrouter  # oppure con OpenRouter
ragcli start miakb                                    # avvia il server LightRAG
ragcli ingest miakb                                   # OCR + embedding della cartella
ragcli mcp-add miakb                                  # registra l'MCP in Claude Code
```
WebUI della KB: `http://127.0.0.1:<porta>` (documenti, query, grafo della conoscenza).

## Comandi
| Comando | Cosa fa |
|---|---|
| `ragcli create <nome> <cartella> [--port N] [--ocr mineru\|mineru-cloud\|glmocr\|docling] [--provider ollama\|openrouter] [--llm-model M] [--lang L]` | nuova KB |
| `ragcli list` | elenco KB + stato |
| `ragcli ingest <nome> [--force]` | OCR incrementale + embedding (`--force` = re-OCR tutto) |
| `ragcli start\|stop\|restart <nome\|all>` | gestione server |
| `ragcli mcp-add <nome> [--print-only]` | registra/mostra MCP per Claude Code |
| `ragcli regen <nome\|all>` | rigenera l'.env dopo aver cambiato `global.env` |
| `ragcli status` | riepilogo server + MCP |

## OCR
Backend selezionabile per KB con `--ocr` in fase di `create` (default: `OCR_BACKEND` in `global.env`).

- **mineru** (default): usa `mineru-api` locale su :8000 — accendilo col `mineru-toggle.command`. Endpoint `/file_parse`.
- **mineru-cloud**: API cloud [mineru.net](https://mineru.net) (veloce, richiede token/quota); fallback automatico su `mineru` locale in caso di errore.
- **glmocr**: usa la skill `glm-ocr` via Ollama, tutto locale (nessun servizio). Per-KB con `--ocr glmocr`.
- **docling**: usa [Docling](https://docling-project.github.io/docling/) (IBM), tutto locale (nessun servizio), settato a **massima qualità** (vedi sotto). Per-KB con `--ocr docling`.

Ingest incrementale: re-OCR solo dei file modificati (hash in `.ocr_cache.json`). I markdown OCR-izzati
sono salvati in `kb/<nome>/inputs/` per ispezione.

### Docling — impostazioni massima qualità
Eseguito in sottoprocesso nel venv ML condiviso (`DOCLING_PYTHON`, di default lo stesso di `glmocr`).
Pipeline configurata in `bin/ingest.py::ocr_docling` secondo la
[documentazione ufficiale](https://docling-project.github.io/docling/usage/) per privilegiare la qualità sulla velocità:

| Opzione Docling | Valore | Effetto |
|---|---|---|
| `do_ocr` | `True` | abilita l'OCR su PDF/immagini scansionate |
| `ocr_options` | `EasyOcrOptions(lang=[...])` | OCR applicato solo su pagine/bitmap effettivamente scansionati; il testo nativo del PDF viene usato direttamente (più rapido, nessuna perdita di qualità) |
| `do_table_structure` | `True` | estrazione/ricostruzione struttura tabelle |
| `table_structure_options.mode` | `TableFormerMode.ACCURATE` | modalità tabelle più precisa (vs `FAST`) |
| `table_structure_options.do_cell_matching` | `True` | allinea le celle rilevate al contenuto testuale |
| `images_scale` | `2.0` | rendering pagina a risoluzione doppia → OCR più preciso su scansioni |
| `do_formula_enrichment` | `True` | riconoscimento formule matematiche → LaTeX |
| `do_code_enrichment` | `True` | riconoscimento dedicato dei blocchi di codice |

Config in `global.env`:
- `DOCLING_PYTHON` — python del venv con `docling` + `easyocr` installati (default `/Users/luca/.venv/bin/python`)
- `DOCLING_LANG` — lingue OCR EasyOCR, comma-separated (default `it,en`)
- `DOCLING_TIMEOUT` — timeout sottoprocesso in secondi (default `3600`)

Installazione (nel venv condiviso): `uv pip install --python /Users/luca/.venv/bin/python docling easyocr`.

### Office, audio/video — sempre via Docling
Indipendentemente dall'`OCR_BACKEND` scelto per la KB (mineru/mineru-cloud/glmocr non li
supportano), questi formati sono sempre instradati a Docling:

- **Office (OOXML)**: `.docx`/`.dotx`/`.docm`/`.dotm`, `.pptx`/`.potx`/`.ppsx`/`.pptm`/`.potm`/`.ppsm`,
  `.xlsx`/`.xlsm`. Conversione diretta della struttura nativa (testo, tabelle, layout slide) — nessun OCR,
  non ci sono bitmap da scansionare.
- **Audio/video**: `.wav`, `.mp3`, `.m4a`, `.aac`, `.ogg`, `.flac`, `.mp4`, `.avi`, `.mov`. Trascrizione
  via la pipeline ASR di Docling con **Whisper MLX** (`WHISPER_TURBO_MLX`, nativo Apple Silicon — nessuna
  dipendenza da `openai-whisper`/CPU). Lingua presa dal primo codice in `DOCLING_LANG`. Per i container
  video viene estratta solo la traccia audio. Richiede `ffmpeg` sul PATH (`brew install ffmpeg`).

**OpenDocument non supportato** — `.odt`/`.ods`/`.odp` (e varianti `.ott`/`.ots`/`.otp`/`.fodt`/`.fods`/`.fodp`)
non hanno un backend Docling. `ragcli ingest` li rileva e stampa l'avviso con il comando di conversione
da eseguire (LibreOffice headless), invece di saltarli in silenzio:
```
soffice --headless --convert-to docx "documento.odt"
```

Installazione modello Whisper MLX (nel venv condiviso): `uv pip install --python /Users/luca/.venv/bin/python mlx-whisper`
(il modello viene poi scaricato automaticamente al primo utilizzo).

## Provider e modelli

**Ollama (default)** — tutto locale, impostato in `config/global.env`:
- LLM: `gemma4:e2b` (`LLM_MODEL`)
- Embedding: `nomic-embed-text-v2-moe` dim 768 (`EMBEDDING_MODEL` / `EMBEDDING_DIM`)

**OpenRouter** — cloud, richiede API key in `OPENROUTER_API_KEY`:
- LLM: `openrouter/owl-alpha` (`OPENROUTER_LLM_MODEL`)
- Embedding: `nvidia/llama-nemotron-embed-vl-1b-v2:free` dim 2048 (`OPENROUTER_EMBEDDING_MODEL` / `OPENROUTER_EMBEDDING_DIM`)

Per usare OpenRouter: imposta `OPENROUTER_API_KEY` in `global.env`, poi `ragcli create <nome> <cartella> --provider openrouter`.
Per cambiare provider di una KB esistente: modifica `provider:` in `config/registry.yaml` e fai `ragcli regen <nome>`.

## Riferimento completo — `config/global.env`
Tutte le variabili lette da `ragcli` per generare l'`.env` di ogni KB (`write_kb_env` in `bin/ragcli.py`)
e per l'OCR in `bin/ingest.py`. Dopo una modifica: `ragcli regen <nome|all>`.

| Variabile | Default | Cosa controlla |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | provider di default per nuove KB (`ollama` \| `openrouter`), override per-KB con `--provider` |
| `OLLAMA_HOST` | `http://localhost:11434` | endpoint Ollama locale |
| `LLM_MODEL` | `gemma4:e2b` | modello LLM Ollama per estrazione entità/relazioni, override per-KB con `--llm-model` |
| `OLLAMA_LLM_NUM_CTX` | `32768` | context window del LLM Ollama |
| `EMBEDDING_MODEL` | `nomic-embed-text-v2-moe:latest` | modello embedding Ollama |
| `EMBEDDING_DIM` | `768` | dimensione vettori embedding (deve corrispondere al modello) |
| `OLLAMA_EMBEDDING_NUM_CTX` | `8192` | context window dell'embedder Ollama |
| `EMBEDDING_USE_BASE64` | `true` (ollama) / `false` (openrouter) | encoding embedding richiesto da LightRAG |
| `OPENROUTER_API_KEY` | — | API key OpenRouter (richiesta se `--provider openrouter`) |
| `OPENROUTER_HOST` | `https://openrouter.ai/api/v1` | endpoint OpenRouter (OpenAI-compatible) |
| `OPENROUTER_LLM_MODEL` | `openrouter/owl-alpha` | modello LLM via OpenRouter |
| `OPENROUTER_EMBEDDING_MODEL` | `nvidia/llama-nemotron-embed-vl-1b-v2:free` | modello embedding via OpenRouter |
| `OPENROUTER_EMBEDDING_DIM` | `2048` | dimensione vettori embedding OpenRouter |
| `OCR_BACKEND` | `docling` | backend OCR di default (`mineru`\|`mineru-cloud`\|`glmocr`\|`docling`), override per-KB con `--ocr` |
| `MINERU_API` | `http://127.0.0.1:8000` | endpoint mineru-api locale |
| `MINERU_TIMEOUT` | `3600` | timeout richiesta mineru locale (secondi) |
| `MINERU_CLOUD_API_KEY` | — | token API mineru.net (per backend `mineru-cloud`) |
| `GLMOCR_CONFIG` | `~/.claude/skills/glm-ocr/config.yaml` | config della skill glm-ocr |
| `GLMOCR_PYTHON` | `/Users/luca/.venv/bin/python` | python del venv con `glmocr` installato |
| `DOCLING_PYTHON` | `/Users/luca/.venv/bin/python` | python del venv con `docling`+`easyocr` installati |
| `DOCLING_LANG` | `it,en` | lingue OCR EasyOCR per Docling, comma-separated |
| `DOCLING_TIMEOUT` | `3600` | timeout sottoprocesso Docling (secondi) |
| `RERANK_BINDING` | `null` | rerank (disabilitato, vedi Note) |
| `SUMMARY_LANGUAGE` | `Italian` | lingua del knowledge graph generato da LightRAG, override per-KB con `--lang` |
| `MAX_GLEANING` | `2` | passaggi extra di estrazione entità/relazioni (più alto = grafo più completo, più LLM in ingest) |
| `CHUNK_SIZE` | `1200` | dimensione chunk per l'indicizzazione |
| `CHUNK_OVERLAP_SIZE` | `150` | sovrapposizione tra chunk consecutivi (continuità di contesto) |
| `COSINE_THRESHOLD` | `0.25` | soglia similarità coseno nel retrieval (più alta = meno rumore, default LightRAG 0.2) |
| `BASE_PORT` | `9621` | porta di partenza per nuove KB (incrementata automaticamente, override con `--port`) |

## Note
- Rerank disabilitato: LightRAG supporta rerank solo via cohere/jina/aliyun, non via Ollama.
- L'ingest richiede il server della KB attivo (ragcli lo avvia da solo se serve).
- Dopo `ingest`, l'estrazione del grafo gira in background sul server; segui l'avanzamento nella WebUI.
