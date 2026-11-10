#!/usr/bin/env bash
# Watcher indipendente per l'ingest secondbrain: logga progresso/memoria su file
# e si ferma da solo quando pending=0. Pensato per girare detached (setsid+nohup+disown).
LOG=/home/luca/SecondBrain/.lightrag/ingest-watch.log
prev_done=""

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"; }

log "=== watcher avviato (pid $$) ==="
while true; do
  s=$(curl -sS -m 5 http://127.0.0.1:9623/documents/status_counts 2>&1)
  if [ $? -ne 0 ] || [ -z "$s" ]; then
    log "SERVER UNREACHABLE — controlla se e' crashato"
    sleep 60
    continue
  fi

  processed=$(echo "$s" | grep -o '"processed":[0-9]*' | grep -o '[0-9]*')
  failed=$(echo "$s" | grep -o '"failed":[0-9]*' | grep -o '[0-9]*')
  pending=$(echo "$s" | grep -o '"pending":[0-9]*' | grep -o '[0-9]*')
  avail=$(free -m | awk '/Mem:/{print $7}')
  done_key="${processed}-${failed}"

  if [ -z "$pending" ]; then
    log "RISPOSTA INATTESA: $s"
  elif [ "$pending" = "0" ]; then
    log "INGEST COMPLETATO: $s"
    log "=== watcher terminato ==="
    break
  elif [ "$done_key" != "$prev_done" ]; then
    log "progresso: $s | mem_disponibile=${avail}MiB"
    prev_done="$done_key"
  fi

  if [ -n "$avail" ] && [ "$avail" -lt 150 ]; then
    log "ATTENZIONE MEMORIA BASSA: disponibile=${avail}MiB"
  fi

  sleep 60
done
