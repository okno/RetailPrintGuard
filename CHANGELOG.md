# Changelog

Tutte le modifiche rilevanti sono documentate in questo file. Il progetto segue
la struttura di Keep a Changelog.

## [Unreleased]

## [0.1.8] — 2026-08-13

### Fixed

- `status.sh` e `healthcheck.sh` producono nuovamente output su Debian: il
  comando `df` non combina più le opzioni GNU incompatibili `-P` e `--output`.

## [0.1.7] — 2026-08-13

### Fixed

- la release e la virtualenv sono ora leggibili e attraversabili, ma non
  scrivibili, dagli account systemd isolati; l'installer verifica realmente
  l'import del package come utenti POS e RCH prima di pubblicare la release.

## [0.1.6] — 2026-08-13

### Fixed

- la virtualenv Debian viene ora costruita direttamente nel percorso definitivo
  della release content-addressed: gli shebang di Alembic e degli entrypoint
  systemd non fanno più riferimento alla directory temporanea `.stage.*`;
- installer e restore invocano Alembic tramite il Python della release e
  l'installer verifica preventivamente tutti gli entrypoint di servizio.

## [0.1.5] — 2026-08-13

### Fixed

- il contratto tra validatore, installer e restore usa ora un separatore TAB
  esplicito per tipo e ID dispositivo, evitando che l'`IFS` shell sicuro
  interpreti valori come `pos pos_1` come un unico tipo non supportato.

## [0.1.4] — 2026-08-13

### Fixed

- l'installer Debian installa da un lock SHA-256 separato `setuptools`,
  `wheel` e `packaging` prima del package con `--no-build-isolation`, evitando
  l'incompatibilità del `setuptools` incluso nella venv Debian 12.

## [0.1.3] — 2026-08-13

### Fixed

- pnpm è fissato alla versione usata dal progetto e `esbuild` è l'unica
  dipendenza frontend autorizzata a eseguire il proprio script di build.

## [0.1.2] — 2026-08-13

### Fixed

- il cutover richiede ed esegue un helper di rete root-owned specifico del
  sito dopo la rimozione dei VIP legacy e prima del postcheck dei listener.

## [0.1.1] — 2026-08-13

### Fixed

- il cleanup legacy distingue i runtime installati dalle configurazioni ed
  evidenze preservate, rendendo una seconda esecuzione un no-op sicuro.

## [0.1.0] — 2026-08-13

### Added

- `cleanup_legacy.sh`: inventario dry-run, quiescenza TCP, backup verificato,
  handover esplicito di IP/firewall e rimozione non distruttiva dei runtime
  standalone tramite i rispettivi uninstaller congelati;
- runbook completo di migrazione da `printproxy` e `commercialRCHproxy`;
- monorepo RetailPrintGuard con configurazione YAML strict per tre POS e una
  RCH;
- relay TCP full-duplex protocol-neutral con ACL, timeout, backpressure, lock
  per target e failure policy esplicita;
- spool bidirezionale append-only con RAW separati, timeline hash-chained,
  manifest, `.ready` e recovery di job incompleti;
- adapter read-only per spool canonico, `commercialrchproxy.capture.v1`,
  `commercialrchproxy.pharsed.v1` e printproxy v3;
- parser nativi puri e bounded per ESC/POS e frame RCH osservati, con span raw,
  confidenza, warning e risposte device separate;
- worker parser DB separato con verifica RAW, SHA-256 della build, identità
  documento stabile, versionamento append-only, response RAW, retry/backoff e
  reparse one-shot;
- ingestion/import storico con DTO normalizzati, retry/backoff, quarantena e
  contratto transazionale idempotente;
- schema SQLAlchemy/Alembic MariaDB per evidenze, documenti versionati, ordini,
  correlazioni, antifrode, utenti, audit e import;
- repository SQLAlchemy per API e ingestion;
- correlation engine `rpg-correlation-1.0.0`, diff righe e gestione conti
  separati, incluso il legame bounded delle risposte RCH al job/sessione;
- sedici regole antifrode deterministiche con whitelist e fingerprint;
- worker DB e CLI one-shot/continui per correlazione e antifrode, con
  persistenza idempotente di correlazioni, ordini, eventi, snapshot, alert,
  evidenze e storia;
- selezione transazionale della build parser attiva con motivo, rewind
  controllato del watermark, evento tecnico e audit hash-chained;
- API FastAPI `/api/v1`, Argon2/JWT/RBAC, audit e header di sicurezza;
- CLI locale per bootstrap unico del primo `ADMIN`, password solo da doppio
  prompt, policy forte, Argon2id, ruoli iniziali e audit hash-chained;
- frontend React/TypeScript/MUI in italiano;
- asset Debian per installazione, systemd, nginx, MariaDB, logrotate, backup,
  restore, update, rollback, diagnosi e uninstall non distruttivo;
- lifecycle Alembic corretto per il commit esplicito del revision marker su
  MariaDB e downgrade in ordine inverso delle dipendenze foreign key;
- suite sintetica Python e documentazione operativa/architetturale.

### Security

- segreti esclusi dal YAML e dagli ambienti proxy;
- validazione path/symlink/size/schema/hash/HMAC sugli archivi;
- MariaDB, API e nginx limitati a loopback per default;
- servizi separati con identità minime e hardening systemd.
- account Linux distinti per proxy POS e RCH e spool canonico read-only per
  ingestion;

### Known limitations

- copertura dei dialetti reali, filtri fini del reparse storico e
  orchestrazione avanzata dei ricalcoli non ancora complete;
- collaudi hardware, PCAP, runtime DB/restore e Debian 12 target non attestati;
- vedere [docs/LIMITI_NOTI.md](docs/LIMITI_NOTI.md).
