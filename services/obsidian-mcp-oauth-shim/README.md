# obsidian-mcp-oauth-shim

Piccolo authorization server OAuth 2.1 (DCR + PKCE) che espone un endpoint
MCP LightRAG già pubblicato in locale come Custom Connector remoto per
claude.ai, tramite Tailscale Funnel.

Lo shim resta necessario perché claude.ai parla con custom connector remoti via
OAuth. Questo servizio mantiene discovery, DCR, authorize e token endpoint, poi
fa da reverse proxy verso il backend `lightrag_mcp` locale.

## Componenti

- `app/main.py` — endpoint FastAPI (discovery, DCR, authorize, token, proxy `/mcp`)
- `app/db.py` — persistenza SQLite (`shim.db`): client registrati, authorization code, refresh token
- `app/security.py` — JWT (access token), PKCE S256, hashing
- `app/config.py` — carica `.env`, genera `JWT_SECRET` e `CONSENT_PASSCODE` al primo avvio se assenti
- `manage.py` — CLI di amministrazione
- `com.luca.obsidianmcpoauthshim.plist` (in `~/Library/LaunchAgents/`) — servizio launchd

## Configurazione (`.env`)

File in `<repo>/services/obsidian-mcp-oauth-shim/.env` (permessi `600`). Campi principali:

| Chiave | Significato |
|---|---|
| `ISSUER_URL` | URL pubblico dello shim (hostname Tailscale Funnel) |
| `SHIM_BIND_HOST` | Bind locale del processo uvicorn, normalmente `127.0.0.1` |
| `SHIM_BIND_PORT` | Porta locale del processo uvicorn |
| `LIGHTRAG_MCP_URL` | Endpoint locale del backend `lightrag_mcp` (default `http://127.0.0.1:8743/mcp`) |
| `LIGHTRAG_MCP_VERIFY_SSL` | Verifica TLS del backend MCP locale; tipicamente `false` se il backend è su HTTP loopback |
| `JWT_SECRET` | Auto-generato al primo avvio, firma gli access token |
| `CONSENT_PASSCODE` | Auto-generato al primo avvio, richiesto nella pagina di consenso `/authorize` |

Il passcode di consenso attuale è in `.env` sotto `CONSENT_PASSCODE`. Ti servirà
ogni volta che approvi un nuovo client (es. la prima connessione di claude.ai).

Il backend `lightrag_mcp` deve essere già raggiungibile in locale su
`LIGHTRAG_MCP_URL`; lo shim non implementa MCP, si occupa solo dello strato
OAuth e del proxy.

## Linux service model

Su Linux lo shim gira sotto `systemd --user`, in bind locale su
`127.0.0.1:${SHIM_BIND_PORT}`. Il bootstrap del repo installa l'unità e poi
pubblica verso internet con:

```bash
tailscale serve --bg 8443 http://127.0.0.1:8420
tailscale funnel --bg 8443
```

Controlli utili:

```bash
ragcli doctor
ragcli service status
ragcli service logs obsidian-mcp-oauth-shim
```

## Avviare / fermare il servizio

Su Linux:

```bash
systemctl --user status obsidian-mcp-oauth-shim.service
systemctl --user restart obsidian-mcp-oauth-shim.service
journalctl --user -u obsidian-mcp-oauth-shim.service -n 100 --no-pager
```

Su macOS:

Gira come LaunchAgent (`com.luca.obsidianmcpoauthshim`), già installato e avviato:

```bash
# stato
launchctl list | grep obsidianmcpoauthshim

# riavvio (es. dopo aver modificato .env)
launchctl kickstart -k gui/$(id -u)/com.luca.obsidianmcpoauthshim

# stop
launchctl bootout gui/$(id -u)/com.luca.obsidianmcpoauthshim

# restart da zero
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.luca.obsidianmcpoauthshim.plist
```

Log applicativi: `~/Library/Logs/obsidian-mcp-oauth-shim/stdout.log` e `stderr.log`.

Il servizio ascolta solo su `127.0.0.1:8420`; l'esposizione pubblica è a carico
di Tailscale Funnel (vedi sotto), non del servizio stesso.

## Avvio da `~/Documents`

**Nota storica**: il progetto è stato temporaneamente in `~/Documents/Scripts/obsidian-mcp-oauth-shim/`
e ha avuto **due episodi separati** (2026-07-02 e 2026-07-03) di `launchd` che
smette di riuscire a fare lo spawn del processo Python: `launchctl list` mostra
PID `-`, `last exit code = 78 (EX_CONFIG)`, **zero log del processo Python**
(non parte proprio). Il secondo episodio si è verificato da solo durante la
notte, senza che nessuno spostasse/copiasse nulla — quindi non è (solo) un
problema di `mv`/`cp` che porta con sé extended attribute residui: **c'è
qualcosa di intermittente specifico di `~/Documents`** (ipotesi non
confermate: Time Machine, Spotlight, rivalutazione periodica di ACL) che
blocca `launchd` in modo silenzioso e imprevedibile. Il progetto è stato
riportato in `~/obsidian-mcp-oauth-shim/` (fuori da `Documents`), dove non si
è mai verificato. Per mantenerlo nel repository lightrag, il LaunchAgent
esegue invece il piccolo launcher esterno in `~/.local/bin/`; questo è l'unico
file non di sistema lasciato fuori dal repository e serve esclusivamente ad
aggirare quel limite di `launchd`.

**Se in futuro serve comunque spostarlo**: evita `~/Documents` e le sue
sottocartelle per qualunque servizio Python/venv gestito da `launchd`. Va bene
invece per script `bash` semplici (es. `obsidian-watchdog.sh`, che gira senza
problemi da `~/Documents/Scripts/`) — il problema sembra specifico
all'esecuzione di interpreti/venv, non agli script di testo.

Se proprio serve rilocare questo progetto, non fare mai `mv`/`cp` di `.venv/`:
ricrealo sempre da zero nella nuova posizione:

```bash
cd ~/Documents/lightrag-kb/services/obsidian-mcp-oauth-shim
python3 -m venv .venv
../../.venv/bin/pip install --upgrade pip
../../.venv/bin/pip install -r requirements.txt
# poi aggiorna i path nel plist e bootstrap
```

## Tailscale Funnel

Verificato: Tailscale standalone (system extension, bundle `io.tailscale.ipn.macos`),
Funnel abilitato sul tailnet. Hostname pubblico:
`https://lucas-macbook-air.tail234659.ts.net`.

Comandi già eseguiti per esporre lo shim (porta 8420 → 443 pubblica):

```bash
tailscale funnel --bg 8420      # avvia in background, sopravvive a logout/riavvio Tailscale
tailscale funnel status         # verifica configurazione attiva
tailscale funnel --https=443 off   # per disattivare l'esposizione pubblica
```

**Nota**: Funnel espone lo shim (non il backend MCP locale diretto) a chiunque su
internet conosca l'URL. La superficie protetta è: passcode sulla pagina di
consenso + PKCE obbligatorio + JWT firmato. Non condividere `CONSENT_PASSCODE`
né il contenuto di `.env`.

## Rigenerare i secret

```bash
cd ~/Documents/lightrag-kb/services/obsidian-mcp-oauth-shim
../../.venv/bin/python manage.py rotate-jwt-secret   # invalida tutti gli access token in circolazione
../../.venv/bin/python manage.py rotate-passcode     # nuovo passcode per /authorize
```

Dopo la rotazione, riavvia il servizio (vedi sopra).

## Revocare un client registrato

```bash
cd ~/Documents/lightrag-kb/services/obsidian-mcp-oauth-shim
../../.venv/bin/python manage.py list-clients
../../.venv/bin/python manage.py revoke-client <client_id>
```

## URL da inserire in claude.ai

Impostazioni → Connectors → Add custom connector → **Remote MCP server URL**:

```
https://lucas-macbook-air.tail234659.ts.net/mcp
```

Claude.ai eseguirà da solo la discovery OAuth, la registrazione dinamica del
client e ti mostrerà la pagina di consenso dove inserire il `CONSENT_PASSCODE`.
