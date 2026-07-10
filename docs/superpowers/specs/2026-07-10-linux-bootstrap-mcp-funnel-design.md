# Linux Bootstrap, MCP, and Funnel Design

## Goal

Rendere `lightrag-kb` operativo su questo PC Linux con una configurazione OS-agnostica, mantenendo gli stessi tool MCP, la massima compatibilita' dei formati di ingest supportati da Docling, un bootstrap CLI completo via `setup.sh`, e pubblicazione pubblica dello shim OAuth/MCP tramite Tailscale Funnel.

## Scope

Questa iterazione copre:

- bootstrap completo Linux tramite `setup.sh`
- configurazione centralizzata e OS-agnostica in `config/global.env`
- adattamento dell'ingest Docling per Linux con OCR/ASR configurabili
- mantenimento della superficie MCP attuale di LightRAG
- packaging Linux dello shim OAuth/MCP
- gestione servizi via `systemd --user`
- pubblicazione pubblica dello shim tramite `tailscale serve` + `tailscale funnel`
- comandi diagnostici e di manutenzione in `ragcli`

Non copre:

- custom domain esterno alla tailnet
- reverse proxy locale aggiuntivo
- refactor funzionali del protocollo OAuth o dei tool MCP
- supporto Windows nativo fuori da WSL

## Requirements

### Functional

1. `setup.sh` deve diventare il bootstrap principale per una nuova installazione Linux.
2. Il bootstrap deve installare il runtime condiviso, creare i symlink CLI nel `PATH`, inizializzare la config, materializzare i servizi `systemd --user`, abilitarli/riavviarli e configurare Tailscale Funnel per lo shim.
3. `ragcli` deve esporre almeno `doctor` e `service`.
4. `bin/ingest.py` deve selezionare OCR, ASR e accelerazione in modo esplicito a partire dalla configurazione centralizzata.
5. L'ingest deve preservare i formati gia' supportati dal repo e da Docling.
6. Lo shim deve continuare a esporre gli endpoint OAuth/MCP attuali.
7. La pubblicazione pubblica deve avvenire tramite dominio `*.ts.net` della tailnet, con HTTPS pubblico via Funnel.

### Non-functional

1. La repo deve restare OS-agnostica: i default devono poter funzionare sia su macOS sia su Linux, con override espliciti via config.
2. Il profilo Linux di default deve privilegiare affidabilita', qualita' OCR e compatibilita' dei formati rispetto a ottimizzazioni premature legate a GPU o driver.
3. I test devono coprire i seam pubblici concordati: CLI `ragcli`, superficie HTTP shim/MCP, e orchestrazione della pubblicazione.

## Current State

### CLI and runtime

- `setup.sh` oggi crea `.venv-engine`, installa LightRAG/Docling/FastMCP, mette `ragcli` e `lightrag-server` nel `PATH`, e copia `config/global.env.example` in `config/global.env`.
- `ragcli` gestisce KB, ingest e registrazione MCP, ma non governa servizi Linux ne' bootstrap operativo completo.

### Ingest and Docling

- `bin/ingest.py` e' attualmente orientato a un profilo macOS-first:
  - OCR visuale via `OcrMacOptions`
  - fallback CPU solo a livello di acceleratore, non come selezione esplicita del motore OCR
  - ASR via `WHISPER_TURBO_MLX`
- La configurazione globale centralizza gia' parte del runtime, ma non modella piattaforma, motore OCR/ASR, accelerazione o publish settings.

### MCP and shim

- `mcp/lightrag_mcp.py` espone tre tool: `ask`, `retrieve_context`, `find_documents`.
- `services/obsidian-mcp-oauth-shim` e' documentato e cablato su macOS/launchd, non ancora impacchettato come flusso Linux-first con `systemd --user`.

## Design Decisions

### 1. Bootstrap unificato in `setup.sh`

`setup.sh` diventa l'entrypoint principale per il bootstrap di una macchina Linux:

- crea/aggiorna il runtime condiviso `.venv-engine`
- installa dipendenze Python corrette per piattaforma
- crea i symlink CLI in `~/.local/bin`
- crea o aggiorna `config/global.env`
- materializza le unit `systemd --user`
- abilita o riavvia i servizi necessari
- configura `tailscale serve` e `tailscale funnel` per lo shim
- stampa URL pubblico finale e stato dei servizi

`ragcli install-services` e `ragcli publish shim` non saranno flussi utente primari. La loro logica verra' incorporata in helper richiamati da `setup.sh`, mantenendo `ragcli doctor` e `ragcli service ...` per diagnosi e manutenzione.

### 2. Configurazione centralizzata e OS-agnostica

La configurazione centrale resta in `config/global.env` con nuovi campi:

- `DEPLOY_PLATFORM=auto|linux|mac`
- `DOCLING_OCR_ENGINE=auto|easyocr|rapidocr|ocrmac|tesseract`
- `DOCLING_ASR_ENGINE=auto|whisper_turbo|whisper_turbo_native|whisper_turbo_mlx|whisper_s2t`
- `DOCLING_ACCELERATOR=auto|cpu|cuda|mps`
- `DOCLING_ENABLE_TABLES=1`
- `DOCLING_ENABLE_CODE_ENRICHMENT=1`
- `DOCLING_ENABLE_FORMULA_ENRICHMENT=1`
- `DOCLING_IMAGE_SCALE=2.0`
- `SHIM_BIND_HOST=127.0.0.1`
- `SHIM_BIND_PORT=8420`
- `TAILSCALE_FUNNEL_PORT=8443`
- `SHIM_PUBLIC_URL=https://your-node.your-tailnet.ts.net`

`bin/kb_registry.py` diventa il punto unico di risoluzione dei default e di normalizzazione della config.

### 3. Profilo Linux Docling

#### ASR

Su Linux il default raccomandato e' `whisper_turbo_native`, con `whisper_turbo` come alias auto-portabile. Questo rende il comportamento esplicito su Linux pur mantenendo una semantica coerente col default Docling moderno.

#### OCR

I motori candidati supportati dal design sono:

- `ocrmac` per macOS
- `easyocr` come baseline Linux compatibile
- `rapidocr` come opzione Linux orientata a performance/accelerazione
- `tesseract` solo se disponibile e supportato dalla versione Docling realmente installata

Il design non assume un engine `paddleocr` diretto nel codice del repo. La raccomandazione ricevuta su PaddleOCR viene trattata come segnale per verificare `rapidocr` e l'ecosistema OCR sottostante durante l'implementazione.

#### Table and layout preservation

La conservazione di tabelle, struttura e layout non deve dipendere dal motore OCR. Docling continua a gestire:

- layout analysis
- table structure extraction
- markdown strutturato
- formula/code enrichment

L'OCR fornisce il testo delle aree raster; la pipeline strutturale resta sempre abilitata nel profilo di massima qualita'.

#### Linux defaults

Per questo PC Linux:

- `DEPLOY_PLATFORM=linux`
- `DOCLING_ASR_ENGINE=whisper_turbo_native`
- `DOCLING_OCR_ENGINE=rapidocr` se il runtime e i test lo confermano
- fallback sicuro `DOCLING_OCR_ENGINE=easyocr`
- `DOCLING_ACCELERATOR=cpu` come baseline iniziale, con eventuale `cuda` solo se validata in test reali

### 4. Servizi Linux con `systemd --user`

I servizi vengono gestiti da `systemd --user`. Le unita' previste sono:

- `lightrag-server@.service` oppure equivalenti per KB
- servizio watchdog correlato
- servizio `obsidian-mcp-oauth-shim`
- eventuale servizio/helper per riconciliare la publish config Tailscale

`ragcli service` restera' il wrapper umano per:

- `status`
- `restart`
- `logs`

### 5. Pubblicazione pubblica tramite Tailscale Funnel

Approccio scelto: opzione 1, nessun reverse proxy locale aggiuntivo.

Flusso:

1. lo shim ascolta in locale su `127.0.0.1:8420`
2. `tailscale serve --bg 8443 http://127.0.0.1:8420`
3. `tailscale funnel --bg 8443`
4. URL pubblico servito dal dominio `*.ts.net` del nodo

Questo approccio minimizza i componenti, mantiene TLS e edge public exposure in Tailscale, e lascia il processo applicativo non esposto direttamente su interfacce pubbliche.

### 6. MCP server invariato come superficie pubblica

`mcp/lightrag_mcp.py` continua a esporre gli stessi tre tool:

- `ask`
- `retrieve_context`
- `find_documents`

L'intervento riguarda packaging, runtime Linux, registrazione CLI e bootstrap, non il contratto pubblico dei tool.

## User Experience

### First bootstrap

L'utente esegue:

```bash
./setup.sh
```

Effetti attesi:

- runtime pronto
- CLI nel `PATH`
- config Linux pronta o aggiornata
- servizi attivi
- shim pubblicato via Funnel
- URL pubblico mostrato a terminale

### Post-bootstrap checks

L'utente esegue:

```bash
ragcli doctor
ragcli service status
```

### Daily operations

L'utente continua a usare:

- `ragcli start`
- `ragcli stop`
- `ragcli ingest`
- `ragcli mcp add`
- `ragcli service restart`

## Files and Responsibilities

### Modify

- `setup.sh`
  - bootstrap completo Linux
- `config/global.env.example`
  - nuove chiavi OS-agnostiche
- `bin/kb_registry.py`
  - default centralizzati e normalizzazione config
- `bin/ingest.py`
  - risoluzione esplicita OCR/ASR/acceleratore
- `bin/ragcli.py`
  - nuovi comandi `doctor` e `service`
- `README.md`
  - documentazione aggiornata per Linux, systemd, Funnel, Docling
- `services/obsidian-mcp-oauth-shim/*`
  - packaging e doc Linux-first

### Create

- `docs/superpowers/plans/...`
  - piano implementativo successivo
- file unit `systemd --user` nel repo
- eventuali helper script per installare/aggiornare unit e publish config
- test CLI, test ingest config, test shim/publication orchestration

## Testing Strategy

Seam ufficiali concordati:

1. `ragcli` come CLI pubblica
2. superficie HTTP dello shim/MCP
3. integrazione end-to-end della pubblicazione

TDD plan ad alto livello:

- test CLI per `doctor`, `service`, helper richiamati da `setup.sh`
- test della risoluzione config per piattaforma, OCR, ASR e acceleratore
- test della costruzione delle unit `systemd`
- test della costruzione dei comandi/config `tailscale serve` e `tailscale funnel`
- test della superficie HTTP esistente dello shim, per verificare che il packaging Linux non rompa OAuth/MCP

I test unitari non dipenderanno da rete reale, Tailscale reale o servizi esterni reali. L'integrazione reale verra' verificata con comandi espliciti fuori dai test unitari.

## Risks and Mitigations

### OCR engine drift in Docling

Rischio: la versione Docling installata potrebbe non esporre tutti gli engine ipotizzati.

Mitigazione:

- verificare gli engine disponibili nel runtime installato
- degradare in modo esplicito verso `easyocr`
- fallire con errore leggibile se un engine configurato non e' disponibile

### Linux GPU variability

Rischio: CUDA/torch/RapidOCR potrebbe variare tra macchine Linux.

Mitigazione:

- baseline iniziale CPU-first
- accelerazione `cuda` solo come opt-in
- `doctor` deve esplicitare backend selezionato e prerequisiti mancanti

### Tailscale publish drift

Rischio: `serve`/`funnel` possono essere gia' configurati in modo differente sul nodo.

Mitigazione:

- setup idempotente
- `doctor` deve mostrare lo stato effettivo Tailscale
- i comandi devono essere riapplicabili senza rompere configurazioni coerenti

### Existing dirty worktree

Rischio: nel repo esistono gia' modifiche locali a `config/global.env.example`.

Mitigazione:

- l'implementazione non deve sovrascriverle alla cieca
- i cambiamenti verranno letti e integrati con attenzione

## Open Decisions Resolved

- Publish model: pubblico su Internet via Tailscale Funnel
- Reverse proxy locale: non introdotto in questa fase
- Service manager Linux: `systemd --user`
- Config centralization: nel file di configurazione globale esistente
- ASR Linux: `Whisper Turbo native`
- Formati ingest: preservati
- CLI maintenance commands: `doctor` e `service` mantenuti
- `setup.sh`: promosso a bootstrap operativo completo

## Success Criteria

La milestone e' soddisfatta quando:

1. `./setup.sh` porta una nuova installazione Linux in stato operativo senza richiedere sequenze manuali separate per servizi o publish dello shim.
2. `ragcli doctor` mostra chiaramente stato di runtime, config, servizi e publish.
3. `ragcli service` consente manutenzione ordinaria dei servizi Linux.
4. L'ingest usa config centralizzata e comportamento esplicito per OCR/ASR su Linux.
5. Lo shim risponde localmente e pubblicamente tramite URL `*.ts.net`.
6. L'MCP server mantiene gli stessi tre tool e continua a funzionare.
