# Troubleshooting

## Checklist rapida

```bash
sudo /opt/retailprintguard/current/scripts/status.sh --json
sudo /opt/retailprintguard/current/scripts/logs.sh --since "-15 minutes"
systemctl --failed --no-pager
ss -Hltpn
df -h /var/lib/retailprintguard /var/lib/mysql
df -i /var/lib/retailprintguard /var/lib/mysql
```

Verificare nell'ordine: configurazione, servizi, listener, spazio/inode, spool,
MariaDB, API e solo infine i target fisici. Non inviare probe o byte casuali a
una RCH e non cancellare evidenze per liberare la coda.

## Webapp non raggiungibile

```bash
ss -Hltpn | grep ':8081'
nginx -t
systemctl status nginx.service retailprintguard-api.service --no-pager
curl --fail http://127.0.0.1:8081/
curl --fail http://127.0.0.1:8080/api/v1/system/health
```

Se loopback funziona ma la LAN no, controllare il firewall per TCP/8081 e il
percorso di rete. Non esporre direttamente la porta API 8080.

## L'installer rifiuta un indirizzo RFC 5737

Messaggio tipico: configurazione con indirizzo di documentazione. Gli IP
`192.0.2.0/24`, `198.51.100.0/24` e `203.0.113.0/24` sono solo esempi e vengono
rifiutati in deployment.

Soluzione: sostituirli nel file locale con IPv4 privati approvati dal gestore di
rete. Non modificare il validatore e non committare gli IP del sito.

## Listener non assegnato / bind fallito

```bash
ip -j -4 address show
ss -ltnp
```

Il listener deve essere già presente su un'interfaccia e non occupato. Gestire
l'indirizzo con il network manager del server, in modo persistente e con
controllo collisione. L'installer RetailPrintGuard non cambia la rete.

## Il servizio risulta failed

```bash
systemctl status <nome-servizio> --no-pager
journalctl -u <nome-servizio> -b --no-pager -n 200
```

Controllare in ordine: eseguibile `ExecStart`, configurazione, permessi,
listener, dipendenze DB, path `ReadWritePaths`. Se parser/correlation/fraud
riportano “module not found”, unità, virtualenv e codice appartengono a release
diverse o l'installazione è incompleta: non forzare restart loop; ripristinare
una release content-addressed coerente.

## Ingestion esce con errore 78

Verificare:

```bash
/opt/retailprintguard/current/.venv/bin/retailprintguard-ingestion --help
sed -n '1,160p' /opt/retailprintguard/current/scripts/run_ingestion.sh
```

L'opzione del wrapper deve coincidere con quella della CLI (`--canonical-root`
nella versione documentata). Il wrapper corrente la rileva tramite `--help`.
Ogni installazione deve avere almeno spool canonico o una sorgente legacy
configurata. Correggere eventuali mismatch nel codice sorgente e rilasciare, non
editare in-place una release content-addressed.

## Database offline

Sintomi: API `degraded`/503, ingestion retry, proxy ancora attivo.

```bash
systemctl status mariadb.service --no-pager
mariadb-admin --protocol=socket ping
df -h /var/lib/mysql /var/lib/retailprintguard
```

Non riavviare ripetutamente i proxy. Dopo il DB, riavviare ingestion/API e
verificare che i job diventino duplicate/imported senza doppie righe.

## Spool cresce

```bash
sudo /opt/retailprintguard/current/scripts/status.sh --json
find /var/lib/retailprintguard/spool -type f -name .ready | wc -l
find /var/lib/retailprintguard/spool -type d -name '*.partial' | wc -l
df -h /var/lib/retailprintguard
df -i /var/lib/retailprintguard
```

Distinguere backlog `.ready` da sessioni attive/recuperate `.partial`. Fermare
ingestion per diagnosi se necessario, mai cancellare job manualmente. La
retention automatica non è implementata.

## Target fisico offline

Il relay connette la stampante prima di leggere il gestionale. Verificare
connettività in finestra autorizzata:

```bash
ip route get <ip-stampante>
```

Usare probe TCP soltanto se approvati: alcuni dispositivi interpretano una
connessione come sessione applicativa. Non inviare byte casuali o test print in
produzione.

## Secondo client rifiutato

È il comportamento previsto quando il target è occupato. Cercare client
duplicati, retry aggressivi o sessione precedente bloccata. Il proxy evita di
accodare una stampa che potrebbe avvenire più tardi.

## HMAC printproxy invalido

- usare la chiave originale della stessa installazione/source scope;
- verificare file regolare, non symlink, permessi e lunghezza ≥32 byte;
- non usare `--allow-unauthenticated-printproxy` per bypassare un HMAC presente;
- conservare ledger/head/raw invariati e creare un ticket di quarantena.

## Sorgente storica “busy”

Il marker/file è cambiato durante la snapshot. Attendere che il proxy legacy
termini la pubblicazione o montare uno snapshot filesystem read-only, poi
rilanciare con gli stessi instance ID.

## Schema sconosciuto o quarantena

Non correggere JSON/RAW manualmente. Identificare versione sorgente, conservare
hash, aggiungere un adapter/test per quello schema e reimportare. La quarantena
è un record logico; il file originale non viene spostato.

## Login sempre rifiutato

- verificare che esista un utente attivo con ruolo;
- controllare `locked_until` e fallimenti;
- controllare il segreto JWT e ora di sistema;
- se il database non contiene ancora utenti, eseguire una sola volta la CLI
  interattiva `retailprintguard-admin` come descritto nella guida installazione;
- non sostituire Argon2 con password plaintext nel DB.

Il bootstrap rifiuta una seconda esecuzione quando esiste almeno un utente. Se
la CLI segnala questo stato, usare il workflow amministrativo; non cancellare
utenti/audit per riabilitare artificialmente il bootstrap.

## RAW non disponibile

Il download richiede ruolo `AUDITOR` o `ADMIN` e viene auditato. Un documento
può non avere `raw_payload_id`; il repository tenta il primo raw
client→dispositivo del job. Verificare import completo e hash senza cambiare
l'associazione manualmente.

## Revisione incompleto restituisce 503 o 403

- 503: l'hash di conferma non è configurato o non è stato caricato. Eseguire da
  root `retailprintguard-configure-review` e riavviare soltanto
  `retailprintguard-api.service`;
- 403: la password di conferma è errata; non copiarla in comandi/log e attendere
  il throttle prima di riprovare;
- 409: il job non soddisfa i criteri tecnici di incompletezza;
- 429: troppi tentativi nello stesso intervallo.

Non modificare manualmente `review.env` e non cancellare il job per aggirare la
revisione. `EXCLUDED` significa fuori dall'analisi, non eliminato.

## Filtro periodo restituisce 422

Verificare che `from` e `to` siano timestamp ISO 8601 con offset esplicito e che
`to` sia strettamente successivo a `from`. L'intervallo è `[from, to)`: il
secondo estremo non è incluso.

## Journal vuoto con servizi attivi

Un account non privilegiato può non avere accesso al journal di sistema. Prima
di concludere che non esistano eventi, verificare l'autorizzazione con il
responsabile del server oppure richiedere un export bounded/redatto prodotto da
root. Non aggiungere autonomamente utenti a gruppi amministrativi. Se `sudo`
non è disponibile o autorizzato, registrare il limite e fermarsi: l'accesso SSH
non autorizza escalation alternativa.

## Orari incoerenti

Il database conserva UTC; la UI usa locale italiano. Verificare NTP, timezone
`Europe/Rome`, timestamp aware nei parser e orologio dei dispositivi. Non
“correggere” RAW o timestamp storici; aggiungere una nota/trasformazione
versionata.

## Diagnostica da allegare

```bash
sudo /opt/retailprintguard/current/scripts/diagnose.sh \
  > /root/retailprintguard-diagnose.txt
sha256sum /root/retailprintguard-diagnose.txt
```

Allegare release, UTC, dispositivo, session/job ID, correlation ID e passaggi
riproducibili. Non allegare RAW, password, token, chiavi o dati cliente senza un
canale e un'autorizzazione specifici.
