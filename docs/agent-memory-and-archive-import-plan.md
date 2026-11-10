# Piano di Implementazione: Agent Memory, Hooks, Import Archivi & "Dreaming" Memory Lifecycle

## Contesto & Visione
L'obiettivo è rendere **LightRAG-KB** un hub completo e amichevole per la memoria persistente degli agenti di coding (Claude Code, Antigravity, Cursor, Codex, ecc.), con tre pilastri chiave:
1. **Memoria come Knowledge Base standard**: Nessun database esotico o sottosistema parallelo. Una memoria per agenti è a tutti gli effetti una KB LightRAG (es. `agent-memory` o la KB di un singolo progetto), dove i fatti, le preferenze e le decisioni vengono memorizzati come Markdown strutturato con YAML frontmatter, indicizzati sia a grafo che a vettori.
2. **Hook per Coding Agent**:
   - *Pre-prompt Injection*: Hook che intercetta i prompt o l'avvio della sessione e inietta contesto e preferenze rilevanti.
   - *Post-session Capture*: Hook di fine sessione che cattura decisioni, soluzioni e fatti emersi e li salva.
3. **Import Facile & Sicuro di Archivi (ZIP/JSON)**:
   - Import di esportazioni ChatGPT (`conversations.json`), Claude (`conversations.json`), Google Takeout/Gemini e archivi generici.
   - Protezione da Zip-Slip, decompressione controllata, parsing e conversione in Markdown strutturato.
   - *User Profile Extractor*: Estrazione automatica di regole, fatti e preferenze utente dalle chat storiche.
4. **Ciclo di Vita della Memoria & "Dreaming" (Consolidamento, Dedup & Revisioni)**:
   - Gestione delle decisioni superate/deprecate (status: active, superseded, deprecated, con puntatori `superseded_by`).
   - Meccanismo di **Dreaming / Consolidation** (`ragcli memory dream <kb>`): fase offline o on-demand ispirata al consolidamento del sonno biologico che analizza la memoria, fa deduplicazione di fatti simili, risolve contraddizioni/decisioni obsolete, accorpa memorie episodiche in regole procedurali/fattuali sintetiche e ripulisce il grafo/catalogo.

---

## Architettura & Componenti

```
+-----------------------------------------------------------------------------------------+
|                                    Coding Agents                                        |
|   Claude Code (.claude/)       Antigravity (.gemini/)       Cursor / Codex              |
+-----------------------------------------------------------------------------------------+
       |                                                               |
       | Hook: UserPromptSubmit                                        | Hook: SessionEnd / Stop
       v                                                               v
+-----------------------------+                               +---------------------------+
| bin/ragcli memory inject    |                               | bin/ragcli memory record  |
| - query KB LightRAG         |                               | - estrae decisioni/fatti  |
| - inietta snippet/regole    |                               | - scrive markdown + YAML  |
+-----------------------------+                               +---------------------------+
       \                                                             /
        \                                                           /
         v                                                         v
+-----------------------------------------------------------------------------------------+
|                              FastMCP Router (mcp/kb_router.py)                          |
| Tools per Agent:                                                                        |
| - remember(kb, content, type, tags, title, supersedes_id)                               |
| - search_memory(kb, query, type, active_only)                                           |
| - deprecate_memory(kb, memory_id, reason, superseded_by)                                |
| - ask, retrieve_context, find_documents, evidence (esistenti)                           |
+-----------------------------------------------------------------------------------------+
                                           |
                                           v
+-----------------------------------------------------------------------------------------+
|                          LightRAG-KB (Storage & Ingestion)                              |
| Cartella KB: <source_dir>/                                                              |
| ├── memories/                                                                           |
| │   ├── preferences/   (regole operative e stile utente)                                |
| │   ├── decisions/     (ADR, scelte architetturali con versioning/superseded_by)        |
| │   ├── episodic/      (riassunti sessioni di coding)                                   |
| │   └── facts/         (verità di progetto, config, credenziali/endpoint)               |
| ├── imported/                                                                           |
| │   ├── chatgpt/       (conversazioni formattate da zip import)                         |
| │   └── claude/        ...                                                              |
| └── .lightrag/                                                                          |
|     ├── rag_storage/   (Vettori + Grafo entità/relazioni LightRAG)                      |
|     └── catalog.json   (Storage JSON atomico con lock e versioning)                     |
+-----------------------------------------------------------------------------------------+
                                           ^
                     +---------------------+---------------------+
                     |                                           |
+-----------------------------------------+   +-------------------------------------------+
|       Archive Importer & Extractor      |   |        "Dreaming" & Consolidation         |
|       bin/archive_importer.py           |   |           pipeline/dreaming.py            |
| - Safe ZIP unpack (Zip-Slip guard)      |   | - Trova decisioni in conflitto/obsolete   |
| - Normalizzatori ChatGPT/Claude/Gemini  |   | - Dedup semantico & cluster di memorie    |
| - User Profile & Instructions Extractor |   | - Compressione episodica -> fattuale      |
| - CLI: ragcli import-archive <kb> <zip> |   | - Aggiorna frontmatter (status: superseded)|
+-----------------------------------------+   +-------------------------------------------+
```

---

## 1. Struttura dei File di Memoria & Schemi

Ogni memoria è un file Markdown con YAML frontmatter standard all'interno di `<source_dir>/memories/<tipo>/<slug>.md`:

```markdown
---
id: mem_20261110_abc123
type: decision # [preference, decision, episodic, fact]
status: active # [active, deprecated, superseded]
title: "Scelta architettura OAuth Shim"
tags: [auth, tailscale, oauth]
created_at: "2026-11-10T14:30:00Z"
updated_at: "2026-11-10T14:30:00Z"
superseded_by: null # o "mem_20261110_def456"
confidence: 1.0
source: "claude-code:session_xyz"
---
# Scelta architettura OAuth Shim

Abbiamo deciso di non esporre la porta 8420 direttamente su internet ma di passare
esclusivamente tramite Tailscale Funnel con TLS terminato su 443.
```

Questo formato garantisce:
- **Trasparenza**: Modificabile e leggibile a mano dall'utente in qualsiasi momento.
- **Supporto nativo Docling/LightRAG**: LightRAG indicizza il testo e associa relazioni alle entità senza bisogno di parser dedicati.
- **Tracciabilità delle revisioni**: Se una decisione cambia, la vecchia riceve `status: superseded` con puntatore all'ID della nuova.

---

## 2. Estensione MCP (`mcp/kb_router.py`)

Aggiunta di tool con permessi di scrittura e consultazione specifica per la memoria su qualsiasi KB:

1. **`remember(kb: str, content: str, memory_type: str = "fact", title: str = "", tags: list[str] = [], supersedes_id: str = None) -> dict`**:
   - Crea il file Markdown in `<source_dir>/memories/<memory_type>/<slug>.md`.
   - Se `supersedes_id` è valorizzato, aggiorna automaticamente la memoria precedente impostando `status: superseded` e `superseded_by: <nuovo_id>`.
   - Se il server LightRAG è attivo, invoca l'inserimento incrementale real-time (o scrive su disco e notifica il catalogo).
2. **`search_memory(kb: str, query: str, memory_type: str = None, include_deprecated: bool = False, top_k: int = 5) -> list[dict]`**:
   - Query mirata con filtraggio per `status: active` di default, evitando che gli agenti ricevano decisioni ormai invalidate.
3. **`deprecate_memory(kb: str, memory_id: str, reason: str, superseded_by: str = None) -> dict`**:
   - Aggiorna il frontmatter della memoria contrassegnandola come superata o non più valida, registrando il motivo.

---

## 3. Hook per Coding Agent (`bin/agent_hooks.py` & `bin/ragcli.py`)

Integrazione pronta per Claude Code, Cursor, Antigravity e Codex.

1. **Pre-prompt Injection (`ragcli memory inject --kb <kb> --prompt <testo>`)**:
   - Viene invocato dall'hook `UserPromptSubmit` (o hook CLI corrispondente).
   - Esegue una ricerca rapida su LightRAG / memorie attive per identificare preferenze e vincoli attinenti al prompt.
   - Emette un blocco contestuale sintetico da iniettare nel contesto del modello:
     ```text
     <project_memory kb="agent-memory">
     - [PREFERENCE] Usa commenti stile Redis e codice Python minimale (AGENTS.md).
     - [ACTIVE DECISION] Non usare SQLite per catalog, solo storage/json_store.py.
     </project_memory>
     ```
2. **Post-session Capture (`ragcli memory capture --kb <kb> --transcript <path>`)**:
   - Viene invocato da `SessionEnd` o al termine di una sessione.
   - Isola tool use di modifica file, messaggi finali dell'agente e decisioni prese.
   - Crea una nota episodica e salva i fatti salienti tramite `remember()`.
3. **Comando di installazione hook**:
   - `ragcli hook install --client claude-code [--kb agent-memory]`
   - Configura automaticamente `.claude/settings.json` nel progetto corrente o a livello globale.

---

## 4. Archive Importer & User Profile Extractor (`bin/archive_importer.py`)

Comando CLI: `ragcli import-archive <kb_name> <path_to_zip> [--format auto|chatgpt|claude|gemini|generic]`

1. **Sicurezza e Isolamento Zero-Trust**:
   - Prevenzione `Zip-Slip`: ogni entry verificata con `os.path.commonpath([target_dir, extracted_path]) == target_dir`.
   - Rifiuto categorico di symlink, hardlink, file di device e percorsi con `..`.
   - Limite di decompressione (max 200MB per file, max 2GB totale, ratio < 100:1) per prevenire zip bomb.
   - Decompressione in cartella sandbox temporanea sicura (`$CLAUDE_JOB_DIR/tmp` o cartella isolata).
2. **Normalizzatori dei formati di conversazione**:
   - **ChatGPT (`conversations.json`)**: Parsing dell'albero dei messaggi, estrazione dei nodi attivi, conversione di ciascuna chat in un documento Markdown pulito con timestamp, modello e ruolo (`# Chat: <title>`).
   - **Claude Export (`conversations.json`)**: Estrazione cronologica dei turni Human/Assistant e generazione di thread Markdown.
   - **Google Takeout / Gemini**: Parsing dei file JSON/HTML e normalizzazione in Markdown.
3. **User Profile & Instructions Extractor**:
   - Durante l'importazione, uno scanner individua prompt contenenti istruzioni stabili ("ricordati sempre di...", "il mio ruolo è...", "preferisco il codice in...", "nelle mie macchine uso debian...").
   - Genera automaticamente file di preferenze in `<source_dir>/memories/preferences/` pronti per l'ingestione.
4. **Ingest Automatico**:
   - I file Markdown estratti e normalizzati vengono posizionati in `<source_dir>/imported/<provider>_<data>/`.
   - Chiama in automatico l'ingest LightRAG (`bin/ingest.py`) incrementale.

---

## 5. "Dreaming": Consolidamento della Memoria, Dedup & Risoluzione Conflitti

Modulo: `pipeline/dreaming.py`  
Comando: `ragcli memory dream <kb_name> [--dry-run]`

Ispirato al consolidamento della memoria durante la fase REM del sonno:
1. **Deduplicazione Semantica & Clustering**:
   - Calcola similarità tra le memorie (usando il modello di embedding locale configurato in `global.env`).
   - Raggruppa frammenti simili scritti in sessioni diverse (es. 5 note separate sullo stesso argomento).
2. **Rilevamento Contraddizioni & Decisioni Superate**:
   - Analizza coppie di decisioni nello stesso cluster semantico.
   - Quando una decisione successiva contraddice una precedente (es. "usa SQLite" vs "sostituisci SQLite con JSON store"), marca automaticamente la vecchia come `status: superseded` con puntatore `superseded_by: <nuovo_id>`.
3. **Compressione Episodica -> Procedurale/Fattuale**:
   - Riassume 20 log di sessione episodici vecchi in un singolo documento riassuntivo ("Milestone architetturale: Mese X"), spostando i log dettagliati in archivio e liberando spazio nel grafo di conoscenza per query più rapide e pertinenti.
4. **Integrazione con LightRAG**:
   - Esegue il riallineamento del catalogo (`storage/catalog.py`), aggiorna i vettori/entità nel backend LightRAG e rimuove o archivia le entità orfane.

---

## 6. Comandi CLI Rapidi (`ragcli memory ...`)

Aggiunta del sottocomando `ragcli memory` a `bin/ragcli.py`:
- `ragcli memory remember <kb> "<testo>" [--type fact|preference|decision] [--title "..."]`: Salva al volo una memoria da terminale.
- `ragcli memory recall <kb> "<query>"`: Ricerca rapida da terminale tra le memorie attive.
- `ragcli memory dream <kb> [--dry-run]`: Lancia il ciclo di consolidamento e dedup.
- `ragcli memory list <kb> [--type ...] [--status active|superseded]`: Visualizza inventario memorie.
- `ragcli memory deprecate <kb> <memory_id> --reason "..."`: Invalida manualmente una memoria.

---

## File Critici Coinvolti

1. **`pipeline/dreaming.py`** (Nuovo): Logica di consolidamento sonno/dreaming (dedup semantico, conflict detection, compaction episodica).
2. **`bin/archive_importer.py`** (Nuovo): Parser e convertitore sicuro per ZIP/JSON (ChatGPT, Claude, Takeout) + User Profile Extractor.
3. **`bin/agent_hooks.py`** (Nuovo): Script di supporto per hook pre-prompt (injection) e post-session (capture).
4. **`mcp/kb_router.py`** (Modifica): Esposizione dei tool MCP `remember`, `search_memory`, `deprecate_memory`.
5. **`bin/ragcli.py`** (Modifica): Sottocomandi CLI `memory`, `import-archive`, `hook`.
6. **`bin/mcp_clients.py`** (Modifica): Supporto hook e aggiornamento configurazioni per Claude Code, Cursor, Codex, Antigravity.

---

## Piano di Verifica & Test

1. **Test Unitari & Sicurezza**:
   - Test safe-unzip in `tests/test_archive_importer.py` verificando il blocco di Zip-Slip (`../../etc/passwd`), percorsi malevoli e file compressi anomali.
   - Test dei normalizzatori JSON su export reali/mock di ChatGPT e Claude.
   - Test di `storage/json_store.py` e catalogo con lo schema di memoria con `superseded_by`.
2. **Test Ciclo di Vita & Dreaming**:
   - Creare 2 decisioni contrastanti in una KB di test.
   - Eseguire `ragcli memory dream test-kb` e verificare che la prima risulti `status: superseded` con il puntatore corretto.
   - Verificare che `search_memory` restituisca solo le decisioni `active` di default.
3. **Test MCP End-to-End**:
   - Avviare `mcp/kb_router.py` su stdio, chiamare `remember` e validare la creazione del file Markdown con frontmatter corretto.
   - Chiamare `search_memory` e verificare la corrispondenza semantica.
4. **Test Hook**:
   - Eseguire `ragcli memory inject --prompt "come gestiamo lo storage?"` e validare l'output formattato per il prompt dell'agente.
