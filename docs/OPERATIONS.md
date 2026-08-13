# Guida operativa

## Verifica rapida

Su un'installazione Debian gestita dagli script:

```bash
sudo /opt/retailprintguard/current/scripts/status.sh
sudo /opt/retailprintguard/current/scripts/status.sh --json
```

Il controllo riepiloga:

- sette servizi applicativi systemd (più timer/servizio backup);
- validità della configurazione e presenza locale dei listener;
- raggiungibilità MariaDB via socket;
- raggiungibilità health API su loopback;
- byte spool, job `.ready`, directory `.partial` e spazio libero.

Un exit code non zero richiede analisi. Il solo stato `active` non dimostra che
una stampante abbia ricevuto un job.

## Stato dei servizi

```bash
systemctl status retailprintguard-pos-proxy.service --no-pager
systemctl status retailprintguard-rch-proxy.service --no-pager
systemctl status retailprintguard-ingestion.service --no-pager
systemctl status retailprintguard-parser.service --no-pager
systemctl status retailprintguard-correlation.service --no-pager
systemctl status retailprintguard-fraud.service --no-pager
systemctl status retailprintguard-api.service --no-pager
systemctl status retailprintguard-backup.timer --no-pager
```

Verificare i listener:

```bash
ss -ltnp
```

Health API:

```bash
curl --fail --silent http://127.0.0.1:8080/api/v1/system/health
```

Il body deve essere letto: `status=degraded` può comunque arrivare con HTTP
200.

Webapp dalla rete amministrativa:

```text
http://<indirizzo-server>:8081/
```

Nginx ascolta su `0.0.0.0:8081`, mentre l'API resta su loopback. Limitare la
porta 8081 via firewall e predisporre HTTPS prima dell'esposizione ordinaria.

## Avvio, arresto e riavvio

Gli script sotto acquisiscono il lock di manutenzione e mantengono MariaDB e
nginx come servizi distro separati:

```bash
sudo /opt/retailprintguard/current/scripts/start.sh
sudo /opt/retailprintguard/current/scripts/stop.sh
sudo /opt/retailprintguard/current/scripts/restart.sh
```

`restart.sh` rifiuta il riavvio se rileva sessioni TCP appartenenti ai proxy.
L'opzione `--force-active-sessions` interrompe quelle sessioni ed è ammessa solo
in emergenza autorizzata, accettando job parziali e stampa interrotta.

Log recenti o in tempo reale:

```bash
sudo /opt/retailprintguard/current/scripts/logs.sh --since "-30 minutes"
sudo /opt/retailprintguard/current/scripts/logs.sh --follow
```

## Directory operative

| Percorso | Contenuto |
|---|---|
| `/etc/retailprintguard` | configurazione e secret protetti; non copiarli nei ticket |
| `/opt/retailprintguard/releases` | release applicative immutabili |
| `/opt/retailprintguard/current` | symlink alla release attiva |
| `/var/www/retailprintguard/releases` | build frontend versionate |
| `/var/www/retailprintguard/current` | frontend attivo |
| `/var/lib/retailprintguard/spool` | RAW e manifest canonici append-only per dispositivo/job |
| `/var/lib/retailprintguard/archive` | import e archivi gestiti |
| `/var/lib/retailprintguard/state` | stato manutenzione, release e worker |
| `/var/log/retailprintguard/proxy` | directory log proxy riservata; log principali in journald |
| `/var/log/retailprintguard/worker` | directory log worker; log principali in journald |
| `/var/backups/retailprintguard` | backup applicativi e bundle migrazione |
| `/var/lib/mysql` | dati fisici MariaDB; usare dump/restore, non copiare a caldo |

Non cancellare manualmente `.partial`, `.ready`, RAW, manifest, state file o
directory release. Usare backup, restore, update e uninstall documentati.

## Dove vengono salvate le copie

Il relay salva immediatamente ogni nuova sessione in:

```text
/var/lib/retailprintguard/spool/<device_id>/<job>/
```

File principali:

- `client.raw`: gestionale→stampante;
- `device.raw`: stampante→gestionale;
- `timeline.jsonl`: ordine, offset, timestamp, hash ed esito locale;
- `manifest.json`: riepilogo e completezza;
- `.ready`: commit locale del job.

L'ingestion copia payload e metadati in MariaDB; non deve cancellare o
modificare lo spool sorgente. `/var/lib/retailprintguard/archive` è riservato ad
archivi gestiti/import legacy e non è automaticamente la destinazione di ogni
job canonico nella versione corrente.

Non aprire i RAW con comandi che possano eseguirne il contenuto. Per un controllo
bounded usare hash e dimensioni:

```bash
find /var/lib/retailprintguard/spool -type f -name .ready -print
du -sh /var/lib/retailprintguard/spool
sha256sum /var/lib/retailprintguard/spool/<device_id>/<job>/client.raw
```

Sostituire i placeholder solo con path copiati in modo sicuro da un inventario,
non con input ricevuto via rete.

## Log e diagnosi

I servizi scrivono log strutturati su stdout/stderr, raccolti da journald:

```bash
journalctl -u retailprintguard-pos-proxy.service --since '-30 minutes' --no-pager
journalctl -u retailprintguard-rch-proxy.service --since '-30 minutes' --no-pager
journalctl -u retailprintguard-ingestion.service --since '-30 minutes' --no-pager
journalctl -u retailprintguard-parser.service --since '-30 minutes' --no-pager
```

Report bounded senza payload:

```bash
sudo /opt/retailprintguard/current/scripts/diagnose.sh \
  > /root/retailprintguard-diagnose.txt
```

Revisionare comunque endpoint e identificativi prima di inviarlo a terzi.

Ogni record JSON contiene almeno timestamp UTC, livello, servizio, evento,
messaggio, errore, device, sessione, job e correlation ID; lo stack trace è
aggiunto quando disponibile. I byte passati come metadato non vengono stampati:
sono resi soltanto come tipo, lunghezza e SHA-256. Chiavi e forme testuali comuni
di password/token/Bearer/URL con credenziali vengono redatte e stringhe/collezioni
sono bounded. La redazione è difesa aggiuntiva, non autorizza a loggare segreti o
payload.

Il sink usa una coda non bloccante di 4.096 record per default, così journald
lento non applica backpressure al relay. A coda piena i record sono scartati e
contati; quando torna capacità viene tentato l'evento `log_queue_dropped`.
Monitorare tale evento come perdita di osservabilità, senza fermare le stampe.
Livello e capacità sono configurabili con `RPG_LOG_LEVEL` e
`RPG_LOG_QUEUE_CAPACITY` (1–100.000) tramite un drop-in systemd approvato; dopo
la modifica eseguire `systemctl daemon-reload` e riavviare solo il servizio
interessato in una finestra compatibile.

## Checklist giornaliera

1. Tutti i servizi attesi sono `active`.
2. Nessuna unità è in `failed`.
3. `partial_jobs` non cresce continuamente.
4. `ready_jobs` non cresce più velocemente dell'ingestion.
5. Spazio libero sopra la soglia operativa.
6. Errori di import/parsing e alert critici presi in carico.
7. Ultima connessione/risposta coerente con l'operatività del locale.
8. Backup recente e copia esterna verificata.

La soglia `spool_warning_bytes` non genera da sola una notifica esterna: il
monitoring deve confrontare il valore di health/status e allertare.

Un job in `PARSE_RETRY` viene riprovato con backoff esponenziale; dopo otto
errori diventa `PARSE_FAILED` e non viene ripreso dal ciclo ordinario. Prima di
un retry esplicito correggere parser/configurazione e preservare RAW ed eventi.
Usare `retailprintguard-parser --once --reparse-all` soltanto secondo la
procedura in [AGGIORNAMENTO_PARSER.md](AGGIORNAMENTO_PARSER.md), perché il flag
include tutti i job storici entro il limite, non solo quello fallito.

## Database offline

Non fermare i proxy. Confermare che `client.raw`/`device.raw` e `.ready` vengano
pubblicati, quindi:

```bash
systemctl status mariadb.service --no-pager
journalctl -u retailprintguard-ingestion.service --since '-30 minutes' --no-pager
df -h /var/lib/mysql /var/lib/retailprintguard
```

Dopo il ripristino MariaDB, riavviare solo il control plane necessario. Il
worker riscopre i job e la repository usa `source_key` univoche.

## Arresto e riavvio controllato

Per manutenzione control plane, lasciando operativi i proxy:

```bash
sudo systemctl stop retailprintguard-api.service \
  retailprintguard-fraud.service \
  retailprintguard-correlation.service \
  retailprintguard-parser.service \
  retailprintguard-ingestion.service
```

Per riavviare una sola famiglia proxy:

```bash
sudo systemctl restart retailprintguard-pos-proxy.service
sudo systemctl restart retailprintguard-rch-proxy.service
```

Un restart può interrompere sessioni attive. Eseguirlo in finestra autorizzata e
controllare i job `PARTIAL` successivi.

## Alert handling

L'operatore non deve chiudere automaticamente alert sulla sola descrizione.
Seguire [ALERT_E_REGOLE.md](ALERT_E_REGOLE.md), verificare confidenza,
correlazione, diff e RAW, poi registrare nota/motivazione. Le whitelist non
cancellano il finding.

## Metriche minime da esportare

- sessioni attive/connessioni rifiutate per device;
- byte forward/reverse ed errori;
- job complete/partial e drop cattura;
- spool byte, `.ready`, `.partial`, età job più vecchio;
- free space/inode;
- retry/quarantene/ritardo ingestion;
- errori parser e documenti `UNKNOWN`;
- DB/API health;
- alert per regola/severità/stato.

L'implementazione corrente espone parte di questi dati via log, status e DB;
non include ancora un exporter Prometheus completo.
