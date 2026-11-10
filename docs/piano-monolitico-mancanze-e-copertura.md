# LightRAG-KB · Analisi Copertura Piano Monolitico e Gap Residui

Documento normativo di riferimento:
`docs/LightRAG-KB · Piano monolitico production-grade (D 4102e441b0e58152bb7fe911b782321c.md` (Specifica V2.0 / V2.1)

Data rilevazione: Novembre 2026
Stato complessivo stimato:
- **Core Ingestion, Storage & MCP**: ~70% implementato
- **Specifica completa (incluse feature avanzate e gate operativi)**: ~57% implementato
- **Gap residuo totale**: ~43%

---

## 1. Funzionalità e Moduli Ancora Mancanti

### A. Moduli di Analisi Avanzata (§8 e §9)
La cartella `analysis/` non è ancora stata creata. Mancano:
1. **A2. Documenti quasi duplicati / stessa conoscenza parafrasata (`analysis/duplicates.py`)**:
   - Integrazione SemHash su embedding chunk/derivati e clustering.
   - RapidFuzz per confronto testuale e sovrapposizione evidenze.
   - Attualmente `KBRouter.discover_duplicates()` per kind non exact restituisce lo stub `FEATURE_NOT_ENABLED`.
2. **A3. Immagini simili (`analysis/media.py`)**:
   - Hash percettivi (pHash / dHash) ed embedding immagini prima del cleanup di staging.
   - Riconoscimento immagini ricompresse, ridimensionate o ritagliate.
3. **A4. Musica e duplicati audio (`analysis/media.py`)**:
   - Fingerprint audio (Chromaprint / fpcalc) per raggruppare lo stesso brano in formati diversi (WAV/MP3/FLAC).
   - L'attuale pipeline ASR copre solo il parlato (Whisper), non la signature musicale.
4. **A5. Video visivo e analisi scene (`analysis/media.py`)**:
   - Segmentazione scene tramite ffmpeg, estrazione keyframe, visual captioning.
5. **A6. Knowledge novelty & Confronto cartelle (`analysis/claims.py`)**:
   - Estrazione asserzioni (claim extraction) dai derivati.
   - Matching e anti-join semantico tra sorgenti con report `novel/overlap/contradiction/uncertain`.
   - Attualmente `KBRouter.compare_folders()` restituisce lo stub `FEATURE_NOT_ENABLED`.

---

### B. Client di Trasporto Remoto Mac ↔ Debian (§4)
Sebbene `sync/client.py` implementi il calcolo locale dello snapshot, la generazione del manifest deterministico e la deduzione sicura dei rename:
- Manca lo script/wrapper effimero completo richiamabile via SSH (`push` e `sync --wait`).
- Manca l'invocazione di `rsync` over SSH con elenchi NULL-delimited (`--from0`) verso `staging/<job_id>/incoming`.
- Manca l'output vincolato JSON a riga singola su stdout con log confinati su stderr.

---

### C. Procedure Operative e Release Gate (§11)
- **Backup e Restore integrale drill (§11 #9)**:
  - Esiste solo il salvataggio di checkpoint transitori durante le modifiche e il backup `.bak` dei file client MCP.
  - Manca uno script/procedura automatizzata per backup atomico e restore integrale di catalogo JSON + archivi LightRAG + derived artifacts.
- **Collaudo su macchine reali (§11 Gate `DEPLOYED`)**:
  - Non sono ancora state eseguite prove E2E su Debian e Mac fisici reali tramite connessione SSH/Tailscale.
  - Lo stato attuale verificabile da repo è `IMPLEMENTED_NOT_DEPLOYED`.

---

## 2. Sintesi delle Componenti Già Implementate

- **Catalogo & Storage (§3)**: `storage/catalog.py`, `storage/json_store.py` (JSON versionato, lock atomico, transazioni sicure, mapping canonical doc).
- **CRUD & Safety Guards (§5)**: `pipeline/indexer.py`, `pipeline/ingestion_worker.py` (lock single-writer, recupero da checkpoint, protezione alias, blocco delete sospette).
- **FastMCP Router (§6)**: `mcp/kb_router.py` (tool core multi-KB `list_kbs`, `ask`, `retrieve_context`, `find_documents`, `job_status`, `evidence`, output stdout pulito).
- **Parser Linux (§7)**: `pipeline/parsers.py` (Docling OCR nativo Linux, Office converter, faster-whisper ASR, persistenza sidecar JSON e derivati).
- **Deduplica esatta A1 (§8)**: `storage/catalog.py:find_exact_duplicate_clusters` e `KBRouter.discover_duplicates(kind="exact")`.
