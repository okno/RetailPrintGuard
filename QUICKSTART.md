# Guida rapida

Questa guida serve a validare il software con configurazione sintetica. Non è
una procedura di go-live: indirizzi, ACL, account, backup e collaudo hardware
devono essere approvati per il sito.

## 1. Preparare Python

Richiede Python 3.11 o successivo.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

In PowerShell l'attivazione equivalente è:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## 2. Eseguire i controlli locali

```bash
python -m pytest -q
python -m ruff check src tests
retailprintguard-proxy --config config/retailprintguard.example.yaml --check-config
```

`--check-config` valida il file ma non apre socket e non assegna indirizzi di
rete. Gli IP `192.0.2.0/24` dell'esempio appartengono a RFC 5737 e non devono
essere usati come rete di produzione.

## 3. Preparare una configurazione locale

```bash
sudo install -d -m 0750 /etc/retailprintguard
sudo install -m 0640 config/retailprintguard.example.yaml \
  /etc/retailprintguard/config.yaml
sudoedit /etc/retailprintguard/config.yaml
```

Sostituire tutti gli endpoint con valori autorizzati, restringere
`allowed_clients`/`allowed_networks` e verificare che:

- ogni listener sia univoco e realmente assegnato al server;
- ogni target sia la stampante fisica corretta;
- listener e target non coincidano;
- nessun target sia anche un listener;
- le directory `spool_root`, `archive_root` e `log_root` siano assolute e
  distinte.

La guida campo per campo è in [docs/CONFIGURAZIONE.md](docs/CONFIGURAZIONE.md).

## 4. Avviare il relay in laboratorio

Solo dopo aver predisposto le interfacce e fake printer autorizzate:

```bash
retailprintguard-proxy --config /etc/retailprintguard/config.yaml
```

È possibile limitare un processo alle route POS o RCH:

```bash
retailprintguard-proxy --config /etc/retailprintguard/config.yaml --device-type pos
retailprintguard-proxy --config /etc/retailprintguard/config.yaml --device-type rch
```

Non avviare contemporaneamente due processi che tentano di occupare gli stessi
listener. Per ogni target il relay accetta una sola sessione attiva; un secondo
client viene rifiutato, evitando una stampa differita o interlacciata.

## 5. Validare archivi storici senza database

Il comando seguente legge soltanto le sorgenti e non scrive MariaDB:

```bash
retailprintguard-import \
  --config /etc/retailprintguard/config.yaml \
  --rch-root /percorso/archivio-rch \
  --validate-only \
  --json
```

`--rch-root` indica l'`OUTPUT_DIR` con capture RAW v1. Se sono rimasti soltanto
derivati `PHARSED` legacy, usare invece `--rch-parsed-root` in un comando
separato. Le due opzioni sono mutuamente esclusive e il derivato non sostituisce
il RAW mancante.

Per `printproxy` v3 autenticato occorre la chiave HMAC originale:

```bash
retailprintguard-import \
  --config /etc/retailprintguard/config.yaml \
  --printproxy-root /percorso/archivio-printproxy \
  --printproxy-hmac-key-file /percorso/protetto/integrity.key \
  --validate-only \
  --json
```

L'opzione `--allow-unauthenticated-printproxy` accetta solo ledger che
dichiarano esplicitamente HMAC assente; la catena hash rimane obbligatoria. Non
è un modo per ignorare un HMAC errato.

Per l'import persistente è obbligatoria una factory fidata che implementi il
protocollo `IngestionRepository`:

```bash
retailprintguard-import \
  --config /etc/retailprintguard/config.yaml \
  --rch-root /percorso/archivio-rch \
  --repository-factory retailprintguard.db.repository:create_ingestion_repository \
  --json
```

Lo spool nativo può essere validato o importato con
`--canonical-root /var/lib/retailprintguard/spool`. La factory SQLAlchemy
sincronizza i dispositivi configurati e persiste envelope, raw, chunk e
documenti in una transazione idempotente.

Il contratto e le cautele dell'adapter MariaDB sono documentati in
[docs/IMPORT_STORICO.md](docs/IMPORT_STORICO.md).

## 6. Frontend

Con Node.js e pnpm disponibili nel PATH:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
pnpm dev
```

Il frontend usa `/api/v1` sullo stesso origin. In sviluppo Vite inoltra le
richieste a `http://127.0.0.1:8080`. Non esporre direttamente il server di
sviluppo in produzione.

## 7. Prima del go-live

Completare almeno:

1. backup e prova di restore;
2. migrazioni su database di staging;
3. test dei quattro dispositivi concorrenti;
4. prova database offline con verifica dello spool;
5. confronto direct-vs-proxy dei due flussi TCP tramite PCAP autorizzato;
6. prova di riavvio improvviso e recupero spool;
7. test su Debian 12 target, systemd e filesystem reali;
8. approvazione di ACL, TLS, retention e privacy.

I test automatici non sostituiscono queste verifiche.
