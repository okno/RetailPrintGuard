# Configurazione dispositivi

## File canonico

Tutti i servizi condividono un file YAML versione 1. Il riferimento è
[`config/retailprintguard.example.yaml`](../config/retailprintguard.example.yaml).
Il loader usa `yaml.safe_load`, rifiuta campi sconosciuti, file symlink, file
oltre 1 MiB e path con traversal.

Gli indirizzi dell'esempio appartengono a RFC 5737. Devono essere sostituiti
solo sul server di destinazione con valori approvati; non inserire IP, password
o dump reali nel repository.

## Esempio minimo a quattro dispositivi

```yaml
version: 1
timezone: Europe/Rome
spool_root: /var/lib/retailprintguard/spool
archive_root: /var/lib/retailprintguard/archive
log_root: /var/log/retailprintguard
database_url_env: RPG_DATABASE_URL

devices:
  - id: pos_1
    name: Stampante BAR POS80BL
    mac_address: "02:00:00:00:01:01"
    department: BAR
    role: comande_bar
    type: pos
    listen_ip: 192.0.2.220
    listen_port: 9100
    target_ip: 192.0.2.200
    target_port: 9100
    parser: escpos
    bidirectional: true
    enabled: true
    allowed_networks: [192.0.2.0/24]

  - id: pos_2
    name: Stampante CUCINA POS80BL
    mac_address: "02:00:00:00:01:02"
    department: CUCINA
    role: comande_cucina
    type: pos
    listen_ip: 192.0.2.221
    listen_port: 9100
    target_ip: 192.0.2.201
    target_port: 9100
    parser: escpos
    bidirectional: true
    enabled: true
    allowed_networks: [192.0.2.0/24]

  - id: pos_3
    name: Stampante PIZZERIA POS80BL
    mac_address: "02:00:00:00:01:03"
    department: PIZZERIA
    role: comande_pizzeria
    type: pos
    listen_ip: 192.0.2.222
    listen_port: 9100
    target_ip: 192.0.2.202
    target_port: 9100
    parser: escpos
    bidirectional: true
    enabled: true
    allowed_networks: [192.0.2.0/24]

  - id: rch_1
    name: CASSA RCH Print! F
    mac_address: "02:00:00:00:01:04"
    department: CASSA
    role: fiscale_gestionale
    type: rch
    listen_ip: 192.0.2.231
    listen_port: 23
    target_ip: 192.0.2.251
    target_port: 23
    parser: rch_observed
    bidirectional: true
    enabled: true
    allowed_clients: [192.0.2.10]
```

Il numero di porta non determina da solo la semantica del protocollo. In
particolare, `23` non autorizza a trattare il flusso RCH come Telnet: il relay è
protocol-neutral e il parser usa soltanto forme osservate.

## Campi del dispositivo

| Campo | Vincolo |
|---|---|
| `id` | univoco, 2–64 caratteri, pattern `[a-z][a-z0-9_-]+` |
| `name` | 1–120 caratteri, etichetta operativa |
| `type` | `pos` oppure `rch` |
| `mac_address` | MAC amministrativo opzionale in formato canonico; non usato per il routing |
| `department` | reparto operativo opzionale, mostrato in UI |
| `role` | ruolo funzionale opzionale del dispositivo |
| `listen_ip`/`listen_port` | endpoint virtuale assegnato al server |
| `target_ip`/`target_port` | endpoint fisico della stampante |
| `parser` | `escpos` per POS, `rch_observed` per RCH |
| `bidirectional` | deve essere `true` |
| `enabled` | abilita/disabilita la route senza rimuoverla |
| `allowed_clients` | singoli IPv4 autorizzati |
| `allowed_networks` | reti IPv4 autorizzate |

Almeno una tra `allowed_clients` e `allowed_networks` è obbligatoria. Le ACL
sono applicate dal relay, ma non sostituiscono firewall e segmentazione di rete.

La validazione globale impedisce:

- ID o listener duplicati;
- target duplicati tra route abilitate;
- listener uguale al proprio target;
- un target coincidente con qualsiasi listener abilitato;
- path di spool, archivio e log uguali o impostati su radici di sistema
  pericolose.

Il MAC non viene usato come controllo di autenticazione o risoluzione del
target: IP e porta configurati restano gli endpoint autorevoli. I MAC
dell'esempio sono valori localmente amministrati sintetici; non committare
quelli del sito.

Il vincolo sui target duplicati è intenzionalmente conservativo: una stampante
fisica non può ricevere due route logiche concorrenti nella stessa istanza.

## Sezioni operative

### `proxy`

| Campo | Default esempio | Significato |
|---|---:|---|
| `connect_timeout_seconds` | 30 | apertura connessione al target |
| `forward_timeout_seconds` | 30 | limite per inoltro/drain |
| `response_tail_timeout_seconds` | 10 | coda risposta dopo FIN client |
| `session_idle_timeout_seconds` | 300 | inattività complessiva |
| `shutdown_grace_seconds` | 15 | chiusura controllata |
| `read_chunk_bytes` | 65536 | buffer per `read`; non è un boundary documento |
| `capture_queue_max_events` | 4096 | limite eventi in memoria |
| `max_connections` | 128 | sessioni globali massime |
| `fsync_each_event` | `true` | durabilità per evento, con costo I/O |
| `storage_failure_policy` | `continue` | `continue` o `abort` |

Con `continue`, la stampa può proseguire dopo un errore di storage e il manifest
segnala byte/eventi mancanti. Con `abort`, la sessione interessata viene chiusa.
La scelta va documentata nell'analisi del rischio.

### `ingestion`

- `scan_interval_seconds`: intervallo del worker ingestion continuo; il valore
  operativo consigliato è `0.25` secondi per il budget buzzer, mentre
  correlazione e antifrode fissano separatamente 3 secondi nelle proprie unit;
- `retry_initial_seconds` e `retry_max_seconds`: backoff esponenziale limitato;
- `max_batch_jobs`: candidati massimi per adapter e scansione;
- `spool_warning_bytes`: soglia informativa per gli allarmi di crescita.

Le sorgenti legacy del servizio sono opzionali in
`/etc/retailprintguard/ingestion.env`. `RPG_RCH_LEGACY_ROOT` viene passato come
`--rch-root` e deve quindi indicare capture RAW v1, non l'albero `PHARSED`.
L'import di soli derivati usa il comando one-shot con `--rch-parsed-root`.
`RPG_PRINTPROXY_LEGACY_ROOT` e l'eventuale
`RPG_PRINTPROXY_HMAC_KEY_FILE` configurano printproxy v3. I valori sono
argomenti letterali e non vengono valutati dalla shell.

### `correlation`

- `time_window_seconds`: finestra massima tra documenti compatibili;
- `minimum_score`: punteggio minimo 0–100.

### `fraud`

Contiene i default di sito per calo importo, chiusura fiscale tardiva e variazione
estrema. Le regole persistenti sono versionate nel database; l'integrazione deve
registrare quale configurazione è stata realmente applicata a ogni valutazione.

### `api`

L'API è predefinita su `127.0.0.1:8080`. `allowed_origins` è vuoto; abilitarlo
solo con origini HTTPS esatte. Il rate limit locale del login non sostituisce
quello del reverse proxy.

La password di conferma per la revisione degli incompleti non ha alcun campo
YAML configurabile. Soltanto il servizio API legge il nome ambiente fisso dal
file systemd opzionale `/etc/retailprintguard/review.env`, creato esclusivamente
dalla CLI interattiva:

```bash
sudo /opt/retailprintguard/current/.venv/bin/retailprintguard-configure-review
sudo systemctl restart retailprintguard-api.service
```

La password in chiaro non viene persistita. Non aggiungere nomi di variabile,
password o hash a `config.yaml`: senza il file protetto e un hash valido le
azioni di revisione ad alto impatto falliscono chiuse, mentre la consultazione
resta disponibile secondo RBAC.

### `retention`

Il valore `0` significa nessuna cancellazione automatica. La presenza dei campi
non prova che un executor di retention sia attivo: verificare la versione
installata e la procedura operativa prima di impostare valori diversi da zero.

## Segreti e ambiente

[`../.env.example`](../.env.example) documenta solo nomi e valori fittizi:

```text
RPG_CONFIG=/etc/retailprintguard/config.yaml
RPG_DATABASE_URL=mysql+pymysql://utente:segreto@127.0.0.1:3306/retailprintguard?charset=utf8mb4
RPG_JWT_SECRET_FILE=/etc/retailprintguard/jwt.secret
RPG_LOG_LEVEL=INFO
```

Non salvare la password nel YAML. Il file JWT deve essere regolare, non symlink,
contenere almeno 32 byte e non essere leggibile da utenti non autorizzati.

## Validazione

```bash
retailprintguard-proxy \
  --config /etc/retailprintguard/config.yaml \
  --check-config
```

Per validare solo un sottoinsieme:

```bash
retailprintguard-proxy --config /etc/retailprintguard/config.yaml \
  --device-type pos --check-config
retailprintguard-proxy --config /etc/retailprintguard/config.yaml \
  --device-type rch --check-config
```

## Compatibilità con i proxy legacy

Il compilatore genera file `KEY=VALUE` senza segreti:

```bash
retailprintguard-compile-legacy \
  --config /etc/retailprintguard/config.yaml \
  --output-directory /tmp/rpg-legacy-config
```

Non sovrascrive file esistenti senza `--overwrite`. Tutte le route POS devono
avere ACL identiche perché `printproxy` legacy usa una ACL globale. Il compilatore
supporta una sola route RCH per invocazione perché `commercialRCHproxy` legacy è
monodispositivo per processo.

La compilazione è un ausilio di migrazione, non avvia servizi e non assegna IP.
