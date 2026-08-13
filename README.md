# RetailPrintGuard

RetailPrintGuard è un monorepo Python/React per acquisire in modo trasparente i
flussi di tre stampanti POS e di una stampante RCH, conservare le evidenze
originali, normalizzare i documenti, correlare le transazioni e applicare
regole antifrode deterministiche.

La priorità del progetto è il data plane: il relay TCP inoltra i byte in entrambe
le direzioni e non importa parser, API o database. La cattura usa una coda
limitata e uno spool locale; un guasto del control plane non deve trasformarsi
in una dipendenza sincrona della stampa.

> Stato: release iniziale `0.1.2`. I test software sintetici coprono relay,
> spool, configurazione, import, correlazione, regole, modelli e API. Il collaudo
> su hardware reale, il confronto PCAP direct-vs-proxy e l'installazione sul
> target Debian 12 non sono ancora attestati da questo repository. Consultare
> [Limiti noti](docs/LIMITI_NOTI.md) prima di un go-live.

## Componenti

- `retailprintguard-proxy`: relay TCP byte-exact per route POS e RCH, con
  isolamento per dispositivo e spool bidirezionale append-only;
- `retailprintguard-ingestion`: validazione e importazione asincrona degli
  spool, con retry e contratto di idempotenza;
- `retailprintguard-import`: import storico one-shot degli stessi formati;
- `retailprintguard-parser`: worker DB indipendente che applica parser nativi
  puri e bounded a ESC/POS e ai frame RCH realmente osservati, senza dipendenze
  dal relay;
- `retailprintguard-correlate`: worker DB per correlazione deterministica, diff
  delle righe, ordini/eventi/snapshot e aggregazione dei documenti fiscali;
- `retailprintguard-fraud`: worker DB per sedici regole spiegabili, evidenze,
  storia alert e whitelist documentate;
- `retailprintguard-api`: API FastAPI versionate, autenticazione e RBAC;
- `retailprintguard-admin`: bootstrap interattivo e auditato del solo primo
  amministratore;
- `frontend/`: applicazione React/TypeScript in italiano;
- `migrations/`: schema MariaDB/InnoDB versionato con Alembic.

## Principi di sicurezza e integrità

- i byte inoltrati non vengono decodificati o riscritti dal relay;
- ogni direzione mantiene sequenza, offset, timestamp, hash e stato di inoltro;
- i parser leggono soltanto job pubblicati e validati;
- gli input sono non fidati: niente `eval`, esecuzione di payload o
  deserializzazione arbitraria;
- i dati grezzi e i risultati interpretati restano distinti;
- le catene hash rendono le modifiche rilevabili, non impossibili: il sistema è
  **tamper-evident**, non “tamper-proof”;
- i segreti non appartengono alla configurazione YAML né al repository.

## Avvio per sviluppo

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest -q
retailprintguard-proxy --config config/retailprintguard.example.yaml --check-config
```

L'esempio usa esclusivamente indirizzi RFC 5737 non instradabili. Prima di
avviare un listener occorre creare una configurazione locale autorizzata e
predisporre gli indirizzi sul server. La validazione non modifica la rete.

Per il frontend (richiede Node.js e pnpm disponibili nel PATH):

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
```

La procedura completa è in [QUICKSTART.md](QUICKSTART.md). Installazione,
permessi, unità systemd e reverse proxy sono descritti solo per gli artefatti
effettivamente presenti in [Installazione Debian](docs/INSTALLAZIONE_DEBIAN.md).

## Struttura del monorepo

```text
RetailPrintGuard/
├── config/                  configurazione canonica e validata
├── frontend/                web application React/TypeScript
├── migrations/              migrazioni Alembic
├── src/retailprintguard/
│   ├── api/                 API e confine repository
│   ├── common/              configurazione e dominio condivisi
│   ├── correlation/         correlazione e diff
│   ├── db/                  modelli SQLAlchemy e sessioni
│   ├── fraud/               regole antifrode
│   ├── importer/            import storico
│   ├── ingestion/           adapter spool e worker
│   ├── parser/              parser nativi ESC/POS e RCH osservato
│   └── proxy/               relay e cattura locale
└── tests/                   test sintetici e di integrazione
```

## Documentazione

- [Analisi iniziale](docs/ANALISI_INIZIALE.md)
- [Architettura](docs/ARCHITETTURA.md)
- [Configurazione dispositivi](docs/CONFIGURAZIONE.md)
- [Formato spool](docs/FORMATO_SPOOL.md)
- [Database ed ER](docs/DATABASE.md)
- [API e RBAC](docs/API.md)
- [Web application](docs/WEB_APPLICATION.md)
- [Correlazione, alert e regole](docs/ALERT_E_REGOLE.md)
- [Importazione storica](docs/IMPORT_STORICO.md)
- [Migrazione dai proxy standalone](docs/MIGRAZIONE_DA_LEGACY.md)
- [Guida operativa](docs/OPERATIONS.md)
- [Installazione Debian](docs/INSTALLAZIONE_DEBIAN.md)
- [Aggiornamento e rollback produzione](docs/AGGIORNAMENTO_PRODUZIONE.md)
- [Backup, restore e disaster recovery](docs/BACKUP_RESTORE_DR.md)
- [Aggiornamento parser](docs/AGGIORNAMENTO_PARSER.md)
- [Sicurezza](docs/SECURITY.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Decisioni architetturali](docs/ADR.md)
- [Rapporto test](docs/TEST_REPORT.md)
- [Limiti noti](docs/LIMITI_NOTI.md)
- [Registro modifiche](CHANGELOG.md)

## Provenienza e migrazione

Il progetto integra concetti e formati dei repository esistenti
`commercialRCHproxy` e `printproxy`, senza sostituirli implicitamente. Gli
adapter storici sono read-only. La migrazione deve essere eseguita per route,
con rollback pronto e verifica byte-exact su dati sintetici autorizzati.

I due progetti standalone sono stati congelati e archiviati in sola lettura il
13 agosto 2026:

- `commercialRCHproxy` release finale `v0.3.0`, commit
  `7bb17f81276144c2ae4a255066f8e4dfa0241478`;
- `printproxy` tag finale `standalone-final-2026-08-13`, commit
  `1291b847ce589c4a336369ccd81165b702035dba` (la release applicativa resta
  `v3.0.0`).

Entrambi restano software autonomi utilizzabili per audit e rollback
controllato; ogni sviluppo futuro avviene qui. `printproxy` conserva
autorevolmente il RAW storico client→stampante, mentre il reverse storico è
solo metadata/preview e può essere troncato. Il nuovo spool canonico conserva
entrambe le direzioni integralmente per i nuovi job.

## Licenza

MIT, vedere [LICENSE](LICENSE).
