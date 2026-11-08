# LightRAG-KB · Piano monolitico production-grade (Debian + MCP + one-shot sync)

> **SPECIFICA ESECUTIVA V2.0 — 6 novembre 2026.** Questo è il **solo documento normativo** da seguire per implementare [cookkie03/lightrag-kb](https://github.com/cookkie03/lightrag-kb) in modo autonomo. Le v1.0–v1.4 sono superate e non vanno eseguite. La [pagina di ricerca](https://app.notion.com/p/Motore-headless-multimodale-Discovery-deduplicazione-e-confronto-filesystem-4102e441b0e5813799fbd97df894868d?pvs=21) conserva analisi e alternative. **Stato:** specifica pronta per l'implementazione; il software non è ancora implementato o collaudato su Debian. Quando upstream presenta una differenza tra documentazione e release fissata nel lock, implementare un adapter compatibile, dimostrare il comportamento con test e registrare la decisione, non improvvisare modifiche distruttive su dati reali.
> 

# 1. Obiettivo, vincoli e definizione di completamento

Realizzare **LightRAG-KB, un motore headless per coding agent** che conserva codice, configurazioni, indici e ogni contenuto derivato **esclusivamente su un mini PC Debian**. Gli originali rimangono sul MacBook, NAS o altri computer; i coding agent girano sul MacBook. L'agente aggiorna una KB tramite **una sola istruzione shell locale**, senza montare dischi sul Debian e senza installare un secondo motore. Un **solo MCP remoto** permette di selezionare KB, ricercare, interrogare, verificare citazioni e analizzare documenti.

**Decisioni non negoziabili:**

- **Più KB indipendenti**, ognuna con molte cartelle sorgente. Un solo MCP router: parametro KB esplicito su tutti i tool, nessun fallback o cross-KB automatico.
- Debian contiene una singola installazione del repository/runtime e i dati persistenti di **tutte** le KB, mantenuti insieme sotto una root operativa. Per isolamento possono esistere più processi LightRAG con working_dir/porte distinti.
- **Nessun mount permanente**, nessun polling obbligatorio di Mac/NAS, nessuna copia permanente indiscriminata degli originali. Il coding agent fa push degli originali selezionati in staging; il server ne salva OCR, trascrizioni, chunk, riferimenti, hash e feature, indicizza e poi elimina i file temporanei a ingest **verificato**.
- File nuovi, modificati, rinominati ed eliminati gestiti automaticamente in un full-folder sync. Una *selezione di file* inviata parzialmente non può mai implicare delete.
- Full gratuito/open source; implementazione minimale, evitare nuovi servizi obbligatori non giustificati. LLM/embedding locali o endpoint già disponibili configurabili.
- **Prodotto production-grade** significa: funzionamento dimostrato da test, nessuna perdita silenziosa, backup/restore verificato e guida operativa. Non significa zero downtime, né assenza totale di guasti. Commit intermedi frequenti, PR e merge incrementali su master/main per avanzamento visibile. Eliminare tempestivamente il codice obsoleto appena sostituito. Python minimale con il minor numero di istruzioni e commenti stile Redis.

## Definizione di Done

Un coding agent sul Mac esegue un singolo comando di sync e riceve un report JSON; sync successiva senza modifiche trasferisce e indicizza **zero** file; rename cambia il riferimento senza nuove elaborazioni; delete dopo scansione completa e autorizzata rimuove il riferimento corretto; documenti già indicizzati rimangono interrogabili mentre Mac è offline; una conversione o un update fallito lascia contenuti precedenti recuperabili. Un unico MCP accede a KB-A e KB-B senza contaminazione, con citazioni. Funzioni avanzate espongono duplicati e knowledge difference con report verificabili. Tutti i test bloccanti al §11 devono passare prima del deploy reale.

# 2. Architettura minima

```text
MacBook: Claude Code / Codex / Hermes + SSH, rsync, Python 3
  originali: /Users/.../Projects, /Users/.../Second-Brain
  query: 1 MCP configurato con ssh -T verso il router stdio Debian
  ingestion: 1 comando locale che scarica script client da Debian e lo esegue
          | SSH + Tailscale (file transfer e richieste)
          v
Debian: /srv/lightrag-kb/
  app/                     checkout Git del progetto
  .venv/                   runtime Python con versioni bloccate
  config/                  registry, profiles, secrets (protetti)
  kb/kb-id/                catalogo JSON, derived, storage LightRAG, reports, logs
  staging/job-id/        ORIGINALI TEMPORANEI (quota, cleanup)
  backup/                  copie consistenti configurabili
  client/[kb-sync.py](http://kb-sync.py)        script effimero distribuito al Mac
  mcp/kb_[router.py](http://router.py)         un solo server MCP stdio
  LightRAG backend KB-A on 127.0.0.1:9621, KB-B on :9622, ...
```

Il processo MCP router su Debian legge il registry e instrada la chiamata a un backend **per KB**; la documentazione upstream attualmente descrive un workspace per processo e porte/working_dir distinte. Non basarsi su una selezione workspace dinamica per request non verificata. Il router non esegue shell arbitrarie fornite dall'LLM e non deve avere privilegi per cancellare file originali.

**Runtime:** un solo ambiente condiviso Python e un catalogo JSON per KB, senza SQLite; tutti i dati persistenti risiedono sotto `kb/<kb-id>/`. Verificare i percorsi degli indici vettoriali e grafici nella release upstream fissata. Non imporre PostgreSQL, Neo4j, Redis, Celery, Docker o proxy HTTP come prerequisiti. Gestione processi con systemd user/service appropriato, ACL Tailscale e SSH key; binding dei LightRAG HTTP server sempre su [localhost](http://localhost) Debian.

# 3. Modello dati minimo

Nel file `kb/<kb-id>/catalog.json`, oggetto JSON versionato contenente insieme le seguenti collezioni:

- `sources`: id stabile, nome, origin_host, origin_folder, modalità `full_folder` o `selected_files`, include/exclude, deletion_policy, last_complete_scan_id, status.
- `files`: (source_id, relative_path) univoco, binary_sha256, size, mtime_ns, canonical_doc_id, active, last_seen_scan_id, updated_at. Separare nome originale e identificativo LightRAG.
- `documents`: canonical_doc_id, derived_hash, extracted artifact path, LightRAG doc ID e status, parser/embedding/model config version, reference_count ricostruibile dal catalogo.
- `jobs`: idempotency_key, kb_id, source_id, scan_id, stage, counters, warnings, error summary, created/updated, resume marker.

**Persistenza:** snapshot JSON versionato per KB; lock interprocesso, validazione, temporaneo nella stessa directory, flush e fsync, `os.replace` e fsync della directory. Backup coordinati e recovery LightRAG. Testare crash, concorrenza, corruzione e restore; partizionare solo dopo benchmark.

**Perché serve:** LightRAG può considerare duplicati **nomi uguali anche in folder differenti** e contenuti testuali uguali anche con filename differenti. Il catalogo conserva **ogni occorrenza originale e il suo source URI** anche quando indicizziamo un unico documento canonico. Usare un file_source **sintetico, univoco e stabile per contenuto/revisione**, separato dal basename originale; risolvere nelle evidenze l'ID verso tutte le occorrenze e i loro percorsi. Se l'upstream rifiuta un contenuto uguale, collegare l'occorrenza al documento già indicizzato **senza** segnare l'upload come guasto. Verificare via integration test la risposta 409 e gli stati FAILED duplicate: non equiparare qualsiasi 409 a “identico”.

**Niente storico completo di tutte le revisioni** nella v2.0. Si conserva soltanto il derivato dell'ultima versione funzionante (e il backup necessario durante l'update), poi si rilascia lo spazio quando il nuovo è confermato e non referenziato.

# 4. Interfaccia esatta Mac ↔ Debian (contratto v1)

**Zero installazione permanente obbligatoria sul Mac:** il codice del client è conservato, revisionato e distribuito esclusivamente dal repository Debian; può essere invocato dalla shell dell'agente attraverso SSH. Il Mac ha Python 3, rsync e OpenSSH; lo script effimero esegue le operazioni sul Mac, non sul server.

**Una invocazione rappresentativa da implementare (il comando shell, non un CLI preesistente):**

```bash
ssh -T debian-tailnet 'cat /srv/lightrag-kb/client/[kb-sync.py](http://kb-sync.py)' <br>  | python3 - sync <br>      --kb second-brain <br>      --source mac-second-brain <br>      --folder "$HOME/Second-Brain" <br>      --remote debian-tailnet <br>      --delete-policy guarded <br>      --wait <br>      --json
```

L'accesso SSH al server implica fiducia nel codice che si sta eseguendo: limitare la consegna del client ad account/host autorizzati, evitare pipe da siti terzi; aggiungere verifica SHA256/versione dello script quando si passa da prototipo a installazione persistente. **Nessun parametro password in CLI/MCP/log**: usare SSH config, chiavi, agent e profili. Il nome `source` e la KB sono identificativi stabili registrati al primo sync, mai automaticamente riassegnati a un'altra folder senza migrazione esplicita. Il parametro `folder` è obbligatorio; `--remote` e `--source` possono derivare dal profilo/registry dopo il primo setup. L'agente può usare la stessa procedura da un host con accesso ai file NAS; **non** passare al MCP un path Mac sperando che Debian lo legga.

**Il client implementa esattamente i comandi `sync` e `push`:**

- `sync`: snapshot **completo** e autorevole della cartella, con supporto opzionale alle delete se la scan riesce integralmente.
- `push`: riceve una lista esplicita di file, aggiunge/aggiorna **senza mai cancellare** i mancanti.
- `--wait` attende `COMPLETED` o fallimento e restituisce exit code non-zero su errore; `--no-wait` risponde solo `job_id` per job lunghi. **Output JSON breve** di una riga su stdout; tutti i log diagnostici su stderr.

**Protocollo del client (operazioni via ssh con subcommands non interattivi):**

1. `ragcli sync prepare --kb ID --source ID --mode full|partial --json`: su Debian restituisce `job_id`, `scan_generation` precedente, policy e informazioni per il diff. Il client non invia al modello linguistico una lista di migliaia di file.
2. Client enumera la cartella locale, rispettando esclusioni, symlink policy e file on-demand. Produce un manifest **UTF-8 JSONL** oppure JSON array streaming, con `path` relativo, size, mtime_ns, sha256 (per candidate changed), type, scan_complete, scan_errors. Nomi con newline/Unicode sono correttamente escapati; no shell expansion dei nomi. Il manifest deve essere completo anche quando zero file risultano cambiati.
3. Client recupera in file temporaneo (non in output LLM) il manifest di riferimento del server e confronta `source_id + path + size + mtime_ns`; hash dei file nuovi/cambiati, e verifica hash degli invariati periodicamente. Rename: abbina old/new path con **stesso sha256** e percorso vecchio scomparso solo dopo scan complete; se ambiguo trattare come nuovo + rimozione, mai inventare rename.
4. Trasferisce solo nuovi/cambiati via **rsync over SSH**, protetto da elenco file NULL-delimited e `--files-from`/`--from0` dove disponibile; paths confinati a `staging/job-id/incoming`. Il protocollo diff evita duplicazioni; rsync gestisce efficientemente il trasferimento. Se non è compatibile con il rsync macOS disponibile, il client usa un fallback sicuro documentato con `rsync` o `sftp`, senza dover aggiornare il sistema dell'utente. **Non usare `rsync --delete` sui dati delle KB.**
5. Scrive manifest e richieste di `add/modify/rename/delete` nello staging; chiama `ragcli sync commit JOB --json` via SSH; lato server valida paths e hash ricevuti, snapshot e conteggi, idempotenza, source ownership, spazio disponibile, e applica i filtri di sicurezza.
6. L'ingestion worker Debian processa asincronamente il job, salvi i contenuti derivati e attende stato `PROCESSED` verificato, aggiorna catalogo e segnala outcome. `ragcli job status JOB --json` e `ragcli job wait JOB --json`; il client con `--wait` espone un unico risultato all'agente.
7. **Cleanup:** cancella soltanto i file staged che hanno esito verificato o il job completato. Errori lasciano staging per retry con TTL/quota configurabili; deve essere sempre possibile ripetere la sync dalla sorgente. I derived vengono conservati.

**API e shape JSON stabilite (v1):**

```json
{"ok":true,"job_id":"job-123","kb":"second-brain","source":"mac-second-brain","state":"COMPLETED","added":2,"modified":1,"renamed":1,"deleted":0,"unchanged":94,"failed":0,"warnings":0}
```

- Stati job: `PREPARING`, `TRANSFERRING`, `VALIDATING`, `PROCESSING`, `COMPLETED`, `FAILED_RETRYABLE`, `FAILED_FINAL`, `NEEDS_REVIEW`.
- Un batch **non** è `COMPLETED` finché gli elementi sottoposti a ingest non sono `PROCESSED` o *riusati esplicitamente* da documento canonico già verificato.
- Job idempotente: stessa richiesta ripetuta non duplica revisioni, contenuti o grafi. Crash e retry riusano manifest + checksum. Una sola sync attiva per source (lock).
- `push` e ogni job con scansione incompleta hanno delete count = 0.
- Lo schema CLI rimane verificabile da machine-readable tests; stderr contiene dettagli, stdout soltanto JSON. Per un utente umano si può aggiungere `--human`, ma il contratto agent è prioritario.

# 5. Decisione definitiva: create/update/rename/delete

### Add

Controllare se binary SHA256 o derived hash è già noto. Se sì, riusare canonical doc/derivati; aggiungere una nuova *occurrence* al catalogo per ogni path originale. Se è nuovo, parser→derived persistente→LightRAG→verifica PROCESSED→catalog commit→cleanup. In caso di fallimento, report e retry, **nessun falso successo**.

### Modify

Se hash originale cambia: creare/validare i nuovi derived **prima** di modificare l'indice. La documentazione LightRAG non garantisce una sostituzione atomica e può rifiutare due documenti con lo stesso basename. Scegliere la strategia MVP **semplice e verificabile**: un solo writer per KB, checkpoint del derived precedente e del mapping doc IDs; cancellazione dell'entry vecchia e reinserimento con `file_source` sintetico univoco, **attesa effettiva PROCESSED**, quindi commit dei riferimenti nel catalogo. Se fallisce, eseguire immediatamente recovery dalla copia persistita del **vecchio derived**; se recovery non riesce, KB/job `DEGRADED/FAILED_RETRYABLE`, niente dichiarazioni di successo e allarme nel MCP. È ammessa una **finestra di ricerca incompleta** durante il reindex, documentata; non si promette atomicità o zero downtime. Non modificare/eliminare mai l'originale sul Mac.

Prima di implementare la cancellazione del vecchio canonical doc, controllare se altri file/alias lo referenziano. Se il documento è condiviso, fare un nuovo canonical doc per il contenuto aggiornato e collegarvi la singola occorrenza; non alterare gli altri alias.

**Ottimizzazione possibile soltanto se un test upstream la conferma:** insert nuova revisione con `file_source` sintetico distinto e poi delete vecchia, con sospensione temporanea di query durante lo switch; se supportata senza contaminazione di graph, usare questa variante. Non bloccare il deploy sulla pretesa di transactional versioning che LightRAG non offre.

### Rename/Move

Se old path è assente nel **full snapshot completo** e new path con stesso sha256 è presente: aggiornare in catalogo URI/relative_path e risoluzione delle citazioni. **Zero nuovo parsing/embedding**. Per omonimi o copie indistinguibili, non creare falsi collegamenti: treat as add + conditional delete. Se il source_id cambia, occorre una migrazione esplicita della sorgente: non inferire automaticamente una traversata tra KB differenti.

### Delete

Delezioni automatiche **solo** con `sync` full e policy `guarded` già abilitata per la source. Il client certifica `scan_complete=true` e 0 errori di enumerazione/trasferimento; la folder root e source identity devono coincidere con l'ultima completa. Se cartella offline, vuota in modo sospetto, root cambiata, upload parziale, checksum error, grande quantità di delete oltre limite (default: >20% o >100 file in un job; configurabile per source), impostare `NEEDS_REVIEW` e **non cancellare**. Per piccoli delta da scansione completa valida, rimuovere nel catalogo l'occorrenza e, **solo se** nessun'altra occorrenza referenzia il canonical doc, chiedere a LightRAG di eliminare quel documento. Se delete backend fallisce, job segnala errore e mantiene uno stato riconciliabile; non fingere che il documento sia sparito. Non esiste un sistema complesso di tombstone storico: solo stato active/inactive nel catalogo, un report audit e backup.

L'eliminazione di una KB intera rimane comando amministrativo separato e richiede dry-run, backup e conferma, **mai** parte di sync.

### Condizioni globali

La mancanza di un file in un **push parziale non vale come delete**. Il Mac offline non è un evento di sync valido. Nessuna operazione deve dedurre che una cartella vuota per timeout o errore significhi cancellazione di tutti i contenuti. Tutti i cambiamenti indicizzati sono serializzati in modo che il catalogo possa riparare lo stato dopo un crash.

# 6. Interfaccia MCP e riuso del codice

**Riusare i tool già implementati**, rendendoli multi-KB in un unico router FastMCP:

- `list_kbs()` → nomi/IDs e stato.
- `ask(kb,question,strategy,context)` → risposta **e references**; l'attuale `ask` perde alcuni metadata se restringe al campo response.
- `retrieve_context(kb,question,strategy,context)` → original chunks/entità/relazioni, source IDs.
- `find_documents(kb,status,page,page_size,source?)` → inventario granulare con filtri su source/path, usando catalogo.
- `job_status(kb,job_id)` → progress e retry/review, JSON breve.
- `evidence(kb,doc_id,chunk_id?)` → extracted content e path originale, pagine/linee/celle/timecode quando disponibili.
- `discover_duplicates(kb,kind,scope?)` e `compare_folders(kb,left_source,right_source,mode)`: disponibili quando il modulo avanzato corrispondente è pronto, altrimenti errore strutturato `FEATURE_NOT_ENABLED`, non risultati inventati.

**Non aggiungere** MCP di upload per singoli byte, hash, OCR, begin/end transfer, execute arbitrary code, o un MCP distinto per KB. Sono fasi interne dello script sync e dell'engine. **Un solo MCP configurato sul Mac** con un comando stdio SSH, ad es.:

```bash
ssh -T debian-tailnet /srv/lightrag-kb/.venv/bin/python <br>  /srv/lightrag-kb/app/mcp/kb_[router.py](http://router.py)
```

L'MCP espone tool definitions stabili e output piccoli/paginati. Quando una KB è in stato DEGRADED, il risultato lo segnala in modo esplicito. Report completi rimangono nei dati Debian, MCP restituisce summary, ID e pagine, non migliaia di file nell'LLM.

# 7. Parsing e contenuti che persistono

- **Baseline obbligatoria**: Markdown/txt/code, PDF, Office/ODF/EPUB/email supportati da Docling, OCR di PDF/immagini, audio parlato tramite ASR Linux. Salvare testo/JSON strutturato, page metadata dove possibile, chunks/embedding e source provenance. Scegliere backend OCR Linux mantenuto (RapidOCR/Tesseract/EasyOCR) e Whisper Linux via faster-whisper; eliminare dipendenze `OcrMacOptions`, MPS e `WHISPER_TURBO_MLX` nei codepath Debian. Separare pipeline estrazione da LightRAG indexer; non lanciare due parser sullo stesso file per default.
- **RAG-Anything**: analizzare upstream Docling/LightRAG integration; preferire plugin/API mantenuti se superano fixture di citazione e riproducibilità, senza duplicare codice custom. L'integrazione non è requisito se Docling basta per tutte le modalità base.
- Derived persistiti Debian: per ogni documento, estrazione Markdown + Docling JSON/sidecar, per audio transcript e timestamp, per immagini OCR, descrizioni, per ogni unità collegamento a original source URIs. Gli originali in staging sono **temporanei**.
- OCR/ASR e modello VLM/embedding devono poter essere eseguiti **su Debian CPU**, eventualmente con provider OpenAI-compatible configurato; non richiedere Mac MPS per funzionare. L'opzione gratuita può essere lenta ma non deve richiedere credenziali a pagamento.

# 8. Funzionalità avanzate: criteri, inclusione e contratti

**Criterio unico di inclusione:** introdurre una feature avanzata **solo se** apporta una capacità misurabile che la base non ha; non perché un GitHub è famoso. Le feature avanzate approvate per realizzazione autonoma sono le seguenti. Implementarle in ordine e **senza trasformare ogni libreria in servizio obbligatorio**.

| Feature avanzata | Gap reale della base | Implementazione/strumento di riferimento | Gate |
| --- | --- | --- | --- |
| **A1. Duplicati esatti globali** | La base cataloga per source, ma non espone cluster trasversali tra tutte le cartelle della KB | Raggruppamento del catalogo JSON per binary SHA256 con gruppi di 2+ occurrence e source paths; nessun nuovo tool esterno | Cluster completo anche senza file iniziale, nessun falso positivo. |
| **A2. Documenti quasi duplicati / stessa conoscenza parafrasata** | Retrieval top-k non enumera tutti i file simili o gruppi senza seed | [SemHash](https://github.com/MinishLab/semhash) su derived/chunk embedding, clustering e confronto; [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) per testo/metadata se utile | Cluster + overlap evidence; un fatto unico deve restare visibile. |
| **A3. Immagini simili** | OCR e didascalie non trovano foto ricompressa, ridimensionata o cropped | Hash percettivi/embedding immagine; usare [Czkawka](https://github.com/qarmin/czkawka) come riferimento e benchmark (senza originali permanenti). Calcolare feature prima del cleanup | Foto ricompressa/cropped raggruppata, immagini differenti non erroneamente uguali. |
| **A4. Musica e duplicati audio** | ASR indicizza il parlato ma non rileva lo stesso brano in codec diversi | [Chromaprint](https://github.com/acoustid/chromaprint), signature audio persistenti; Czkawka per confronto | WAV/MP3/FLAC stesso brano gruppati, file non correlati distinti. |
| **A5. Video contenuto** | Trascrizione audio non descrive scene visive e non rileva video ricodificati | ffmpeg/scene detection + keyframe captions/visual signatures; [RAG-Anything](https://github.com/HKUDS/RAG-Anything) come riferimento | 2 scene/timestamp e similitudine ricodificati verificabili. |
| **A6. Knowledge novelty / confronto cartelle** | Anche embedding simili non dicono quali fatti siano assenti da altra cartella | Estrazione claim dai derived, candidate matching SemHash, verifica LLM con evidenze originali, report `novel/overlap/contradiction/uncertain` | Tutti i fatti gold unici e contraddittori del corpus test emergono con source refs, incertezza dichiarata. |

**Decisioni negative definitive per il primo ciclo:** [Graphiti](https://github.com/getzep/graphiti) **NON obbligatorio**: temporal graph aggiunge complessità e non è necessario per costruire un compare claims con evidenze; [Microsoft GraphRAG](https://github.com/microsoft/graphrag) e [RAGFlow](https://github.com/infiniflow/ragflow) restano alternative da benchmark, non parti dello stack. `execute_plan` MCP con codice arbitrario **NON** incluso: coding agents hanno già shell e tool composabili. RAG-Anything è un'alternativa/integrazione di parser, non un terzo backend permanente da avviare sempre.

**Ordine e fallback delle avanzate:** A1–A4 realizzabili da feature persistenti; A5 e A6 più costose, ma approvate poiché introducono video visually-aware e semantic anti-join, che la base non offre. Se un modello o libreria non soddisfa benchmark sulla macchina reale, mantenere feature dietro feature flag e marcare `EXPERIMENTAL/UNSUPPORTED`, mai inventare successo. Una funzione non completata blocca la release **full-featured**, ma non la release `core` che superi §11. L'agente deve completarle o documentare un blocco tecnico riproducibile, non eliminarle silenziosamente.

# 9. File-by-file implementation map

## Decisioni architetturali approvate

- CLI e MCP sono interfacce sottili con elenchi propri di comandi/tool; chiamano direttamente gli stessi moduli funzionali. Validazione, sicurezza, logica e gestione degli errori non si duplicano. Non introdurre un ulteriore livello `core/operations.py`.
- `storage/registry.py` gestisce l'elenco globale e la configurazione delle KB; `storage/catalog.py` gestisce le sorgenti, occorrenze, documenti e job di una singola KB; `storage/json_store.py` condivide solo la persistenza JSON sicura. Separare dal registry l'eventuale gestione dei processi.
- I dati applicativi sono memorizzati in JSON, non SQLite. Preservare i dati preesistenti; qualsiasi migrazione richiede inventario, backup e verifica non distruttiva. I formati interni della release LightRAG devono essere verificati separatamente.
- `sync/client.py` è il client eseguibile sul Mac senza installare il motore; `sync/server.py` prepara e valida sul Debian staging, manifest e commit. Il worker in `pipeline/` elabora i job validati. La directory operativa di staging resta necessaria.
- `pipeline/parsers.py` gestisce i formati e produce derivati; `analysis/` implementa la logica condivisa delle analisi esposte da CLI e MCP. Creare i singoli moduli di analisi solo insieme alla rispettiva implementazione e ai test, senza file segnaposto.
- Ogni file, funzione e classe descrive il proprio scopo; documentare invarianti e motivazioni quando rilevanti, evitando commenti ridondanti o obsoleti. Organizzare i file per responsabilità, senza conservare pipeline o router duplicati.
- Sviluppare in un workspace isolato e integrare progressivamente commit verificati su `master`, preservando le modifiche preesistenti. Non fare push senza autorizzazione. Eliminare il codice che non serve al prodotto, dopo aver verificato dipendenze, compatibilità e contenuto delle modifiche preesistenti; non eliminare dati operativi o segreti come parte della pulizia.

| File/nuovo modulo | Modifica richiesta |
| --- | --- |
| `bin/[ragcli.py](http://ragcli.py)` | Preservare comandi esistenti come alias; `kb create/list/start/stop/status/remove`, `source add/list`, `sync prepare/commit`, `job status/wait`, `doctor`; output `--json`; storage **sempre** Debian. |
| `storage/registry.py` | Registry JSON globale: KB multiple con molte sorgenti, configurazione path Debian e source ID stabile; niente `source_dir/.lightrag`. |
| `pipeline/ingestion_worker.py` | Ingest dei manifest validati; nuovi/modificati/rename/delete, safety guards, derived persistenti, retry e progress. Gli eventuali alias precedenti non duplicano la pipeline. |
| `bin/mcp_[clients.py](http://clients.py)` | Registrare **un** MCP SSH-stdio remoto per Claude Code/Codex; niente registrazioni per singola KB. |
| `mcp/kb_[router.py](http://router.py)` (nuovo) | Router per KB, riuso ask/retrieve_context/find_documents, list_kbs/job_status/evidence; feature MCP A1–A6 dopo implementazione. |
| `sync/client.py` | Script effimero per Mac: snapshot source, delta, transfer rsync, manifest, job wait, output JSON. |
| `sync/server.py` | Preparazione staging, validazione di manifest e file, controlli di sicurezza e accettazione idempotente dei job sul Debian. |
| `storage/json_store.py` | Lock interprocesso e snapshot JSON atomici condivisi, senza logica del dominio KB. |
| `storage/[catalog.py](http://catalog.py)` (nuovo) | Catalogo JSON versionato source/file/doc/job, mapping aliases, lock, scrittura atomica e migrazioni minime. |
| `pipeline/[parsers.py](http://parsers.py)` (nuovo) | Docling/OCR/ASR Linux, JSON sidecar, checksum, metadata source. |
| `pipeline/[indexer.py](http://indexer.py)` (nuovo) | API adapter LightRAG, doc tracking, duplicate responses, protected update/recovery. |
| `analysis/[duplicates.py](http://duplicates.py)`, `analysis/[media.py](http://media.py)`, `analysis/[claims.py](http://claims.py)` (nuovi) | A1–A6, cluster e report evidence. |
| `config/global.env.example`, `[setup.sh](http://setup.sh)` | Config Linux, `uv.lock`, installazione stabile, service systemd e README Mac/SSH. |
| `tests/`, `.github/workflows/ci.yml` | Unit/integration/E2E e golden data; criteri §11. |

L'agente può riorganizzare nomi tecnici interni solo se mantiene i contratti esterni e i test. Non eliminare improvvisamente comandi preesistenti: creare alias o migration path e documentarli.

# 10. Ordine di esecuzione autonoma

**Stage 0 — Baseline e isolamento**

- Crea branch dedicato `feat/debian-kb-v2`, pinned dependencies con `pyproject.toml` + `uv.lock`; screenshot/backup di sample state e test iniziali. Prima di toccare dati reali usare fixture sintetici.
- Installa in sandbox Debian o CI e verifica versione effettiva upstream LightRAG, endpoint/document status e parser. **Non** sostituire un endpoint sulla base di un'ipotesi; implementa adapter e test.

**Stage 1 — Core multi-KB**

- Isola il working dir per KB, catalogo semplice, lifecycle, router MCP; preserva tre tool esistenti; test KB A/B senza leakage e 2 sources in KB A.

**Stage 2 — Sync one-shot**

- Implementa client effimero Mac, `sync`/ `push`, script SSH+rsync con manifest, job status e --wait, no deletes a scan incompleta; test E2E di upload/ripetizione. Implementa safety update, rename, delete e recovery prima di accettare repository reali.

**Stage 3 — Parser/derived**

- Docling/ASR Linux, checkpoint derived e cleanup temporanei sicuro, evidenze; test PDF/Office/OCR e offline source.

**Stage 4 — Advanced**

- A1 global SHA clusters, A2 SemHash doc near duplication, A3 visual, A4 Chromaprint, A5 video, A6 knowledge novelty/contradictions, in quest'ordine. Feature flags + benchmark, report strutturati e documentazione tool MCP.

**Stage 5 — Release**

- Backup and restore drill, systemd, security review (path traversal, SSH auth, secrets), CI gates, update README con comandi verificati, prova end-to-end **MacBook reale → Debian reale** prima di uso produzione, release tags.
- **Autonomia:** il coding agent non deve fermarsi per dettagli implementativi minori: sceglie alternative tra librerie mantenute se test passano. Su blocchi strutturali (es. upstream incompatibile, mancata raggiungibilità fisica del Debian, impossibilità login SSH), registra failing check e continua con implementazione/CI in sandbox; non dichiarare deployed quando non lo è. Non eseguire cancellazioni su file veri dell'utente né migrazioni distruttive senza backup esplicito.

# 11. Test obbligatori e release gates

**Core-release tests (tutti obbligatori):**

1. `2 KB` isolate e 2+ source nella stessa KB; MCP remoto unico interroga entrambe e mantiene provenance.
2. Primo sync folder file new, seconda sync identica ⇒ **0 upload/embedding**, solo 1 file changed ⇒ 1 reingest.
3. Rename senza modifica hash ⇒ nuovo percorso nelle evidenze e **0 reembedding**.
4. Delete su snapshot completo sano, source nota e policy abilitata ⇒ solo l'alias corretto; delete su push parziale, host offline, permission error, source root cambiata, rsync failure ⇒ **0 delete**.
5. Collisione `a/report.pdf` e `b/report.pdf`, contenuti distinti e identici, senza perdita dei due riferimenti.
6. Fault injection: fallimento di parsing e HTTP 409/500, crash dopo delete old e prima di insert new ⇒ ultimo derived recuperabile + retry, mai `COMPLETED` falso.
7. MCP router via SSH stdio: stdout pulito JSON-RPC, host/backend offline errori strutturati, no KB fallback.
8. Mac spento dopo sync ⇒ query su derived ancora riuscita; staging cleaned soltanto dopo verified PROCESSED.
9. Restore integrale KB + catalogo JSON/indici vettoriali/grafo/derived in una sandbox con citazioni funzionanti.
10. File con spazi, Unicode, newline, symlink fuori root e payload malevoli non provocano escaping o delete fuori sandbox.

**Advanced tests (obbligatori per release full-featured):**

- A1 cluster SHA esatti globali; A2 vicinanza semantica senza perdere fatto unico; A3 foto ricompressa e crop; A4 stesso brano WAV/MP3/FLAC; A5 video con 2 scene e timecode; A6 fatti unici e valori contraddittori da PDF/DOCX con original evidence. Misurare precision/recall e citazioni su corpus sintetico con gold labels. Non promuovere un candidato a “eliminabile” tramite sola similarità.
- Metodi: pytest per unit/contract/integration, Hypothesis stateful per serie di add-modify-rename-delete-crash, E2E reale SSH/rsync per release; no chiavi cloud a pagamento nella CI; fixture libere da dati personali.

**Gates:**

- `CORE_READY`: Stage 0–3 + dieci core tests verdi, backup restore e runbook. **Non** descrivere come full-featured.
- `FULL_READY`: Stage 4 A1–A6, benchmark advanced verdi e Stage 5 completato. Un feature flag `unsupported` impedisce `FULL_READY`, ma non impedisce `CORE_READY`.
- `DEPLOYED`: prove di installazione e test *su host Debian e Mac reali*. Se non si dispone di accesso remoto alle macchine, il massimo stato dichiarabile è `IMPLEMENTED_NOT_DEPLOYED`.

# 12. Riferimenti GitHub e documentazione

- [Repository di partenza](https://github.com/cookkie03/lightrag-kb)
- [LightRAG: API Server](https://github.com/HKUDS/LightRAG/blob/main/docs/LightRAG-API-Server.md) e [File Processing / duplicate rules](https://github.com/HKUDS/LightRAG/blob/main/docs/FileProcessingPipeline.md)
- [FastMCP](https://github.com/PrefectHQ/fastmcp) e [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [rsync](https://github.com/RsyncProject/rsync), [rclone](https://github.com/rclone/rclone), [Docling](https://github.com/docling-project/docling), [RAG-Anything](https://github.com/HKUDS/RAG-Anything)
- [SemHash](https://github.com/MinishLab/semhash), [Czkawka](https://github.com/qarmin/czkawka), [Chromaprint](https://github.com/acoustid/chromaprint), [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz), [Graphiti](https://github.com/getzep/graphiti)
- [pytest](https://github.com/pytest-dev/pytest), [Hypothesis](https://github.com/HypothesisWorks/hypothesis)

**Release policy:** implementare e testare prima della diffusione; niente cancellazioni irreversibili su dati non di test; aggiornare questa specifica soltanto con un change log esplicito, non accumulare nuove v1.x in testa alla pagina.

# 13. Registro modifiche (Change Log)

- **8 novembre 2026 — Revisione architetturale v2.1:**
  1. **Catalogo JSON per KB**: Sostituzione SQLite con catalog.json in kb/<kb-id>/, lock esclusivo e atomic replace. Dati nella cartella omonima.
  2. **Minime istruzioni di codice**: Implementazione con minor numero possibile di istruzioni e comandi Python piu semplici e lineari.
  3. **Commenti stile Redis**: Commentare funzioni, classi, decoratori e cicli complessi (scopo, invarianti, ragioni), sempre sincronizzati.
  4. **Commit intermedi frequenti, PR e merge**: Commit atomici, PR e merge su master/main man mano che i test passano per visibilita continua.
  5. **Rimozione codice obsoleto**: Eliminare tempestivamente ogni codice o file obsoleto appena viene sostituito.
