# Rapporto test

## Snapshot

Data: 13 agosto 2026. Il gate RetailPrintGuard è stato eseguito immediatamente
prima del commit iniziale `fffb8d3`; le sole modifiche successive sono
documentazione di provenienza dei repository congelati.

| Controllo | Ambiente | Risultato |
|---|---|---|
| baseline `python -m pytest -q` prima dei worker DB finali | Windows, CPython 3.12.13 | `61 passed, 13 skipped`, 1 warning deprecazione Starlette/httpx; non è il risultato finale della release |
| `.venv\\Scripts\\python -m pytest -q -p no:cacheprovider` sul monorepo `0.1.9` prima della pubblicazione | Windows, CPython 3.12.13 | `96 passed, 18 skipped`, 1 warning Starlette/httpx; include bind UI e lifecycle operativo |
| `.venv\\Scripts\\python -m pytest -q tests/test_parsers.py` | Windows, ambiente virtuale del worktree | `3 passed`; parser puri ESC/POS e RCH |
| `.venv\\Scripts\\python -m pytest -q tests/test_parser_worker_db.py` | stesso ambiente | `3 passed`; persistenza/idempotenza, response RAW, reparse append-only e failure lifecycle |
| `.venv\\Scripts\\python -m pytest -q tests/test_analysis_workers.py` | stesso ambiente | `7 passed`; worker DB, scenari A/B, late arrival, regole A→B→A, watermark e attivazione/rollback parser su SQLite |
| `.venv\\Scripts\\python -m pytest -q tests/test_admin_cli.py` | stesso ambiente | `3 passed`; bootstrap unico anche concorrente, ruoli, Argon2id, audit e password deboli rifiutate |
| `python -m ruff check src/retailprintguard/parser` | Windows | PASS |
| `python -m ruff check src/retailprintguard/correlation src/retailprintguard/fraud tests/test_analysis_workers.py` | Windows | PASS |
| `git diff --check` | stesso ambiente | PASS |
| `.venv\\Scripts\\python -m ruff check src tests migrations` | stesso ambiente | PASS sul worktree congelato |
| `python -m bandit -q -r src -c pyproject.toml` | stesso ambiente | PASS |
| `.venv\\Scripts\\python -m compileall -q src migrations` | stesso ambiente | PASS |
| `scripts/test_ops.sh` | Debian 13/WSL | PASS: sintassi dei 17 script Bash, ShellCheck e `systemd-analyze verify` |
| `tests/test_structured_logging.py` | Windows | `3 passed`; sink lento non blocca il relay, coda bounded/drop accounting e shutdown limitato |
| installazione editable `--no-deps --no-build-isolation` + `--help` | Windows | PASS per tutti i 9 entry point console dichiarati |
| lock `build`/`production` + `pip check` | Windows/Linux | 3 build pin e 29 runtime pin esatti con hash; il build lock corregge esplicitamente la venv Debian 12 |
| frontend Vitest | Windows, runtime Node bundled | `1 passed` (`StatusChip`); invocazione diretta del runner perché il wrapper pnpm locale applica una policy build-script |
| frontend TypeScript/Vite | stesso ambiente | `tsc -b` PASS; build Vite PASS in 3m29s, con warning chunk principale 540,05 kB |
| frontend ESLint | stesso ambiente | PASS con ESLint 9.39.5 e configurazione flat; invocazione diretta del runner per la policy build-script del wrapper pnpm locale |
| Alembic su MariaDB reale | Debian 13, ambiente temporaneo poi rimosso | PASS: upgrade a `29517f373309`, downgrade a zero tabelle applicative, re-upgrade con revision marker presente; non è il target Debian 12 |
| hardware/PCAP | — | non eseguito |

I 18 skip dipendono principalmente dalla parametrizzazione sui 17 script Bash quando un Bash
POSIX non è disponibile sul runner Windows e, nell'altro caso condizionale,
dalla disponibilità di symlink di directory.
La warning FastAPI segnala la deprecazione dell'uso `httpx` con
`starlette.testclient`; non ha causato failure, ma va risolta aggiornando il
test stack in una release compatibile.

## Copertura per requisito

| Scenario | Test | Stato |
|---|---|---|
| A — 100,00 € → 50,00 € | engine puro + `test_database_workers_persist_scenario_a_idempotently` | PASS sintetico: transazione, diff, alert/evidenze persistenti e seconda esecuzione idempotente |
| B — due fiscali da 50,00 € | engine puro + `test_database_workers_aggregate_legitimate_split_without_amount_drop` | PASS sintetico: aggregazione 100,00 € e nessun amount-drop |
| C — DB offline | ingestion worker + `test_database_offline_does_not_block_forwarding_or_spool` | PASS con fake repository/device; MariaDB reale non usata |
| D — payload segmentato/malformato | relay byte-exact + `test_parsers.py` + `test_parser_worker_db.py` | PASS sintetico: forwarding invariato, parser su flusso ricostruito/multi-documento e persistenza DB |
| E — documento/sorgente malformata | adapter/worker unknown schema e quarantine | PASS, raw sorgente non modificato |
| F — quattro device concorrenti | `test_four_devices_concurrently...` | PASS con fake printer, RCH reverse incluso |
| G — riavvio improvviso | `test_unclean_partial_is_recovered_once...` | PASS recovery spool; boot systemd reale non testato |

## Aree testate

### Data plane

- byte forward/reverse e attribuzione route;
- quattro dispositivi concorrenti senza contaminazione;
- database assente;
- stream frammentato e payload opaco;
- ACL, target busy, capture startup e policy storage;
- coda bounded e recovery `.partial`.

### Ingestion/import

- canonical full-duplex e chain timeline;
- RCH capture/parsed v1, path traversal, replay offline e tamper raw;
- printproxy head/ledger/HMAC, schema sconosciuto e noncanonical JSON;
- symlink non seguito;
- retry/backoff, retry esaurito, quarantena e import duplicato;
- repository SQLAlchemy atomica/idempotente con raw/chunk/decimal.

### Dominio/control plane

- tipi documento/evento e timestamp UTC;
- schema completo, migrazione upgrade/downgrade SQLite;
- correlazione, diff, split payment e 16 regole;
- parser DB idempotente, reparse versionato, retry bounded e corretta
  associazione delle risposte RCH al reverse RAW;
- API login, route protette, RBAC raw/export/rule, workflow alert e audit;
- bootstrap locale e concorrente del primo admin con lock MariaDB, Argon2id e audit chain;
- header di sicurezza e health pubblico.

### Deployment

I test statici verificano unità separate/hardenizzate, nginx loopback, assenza
di mutazioni rete nell'installer, presenza del requisito lock e validatore sito.
Sono presenti 18 file nella directory `scripts/`: 17 script Bash e un validatore
Python; la sintassi Bash viene verificata quando un Bash POSIX è
disponibile.

## Baseline dei repository sorgente

Questi numeri provengono dai report dei relativi repository e non sono stati
rieseguiti come parte della suite RetailPrintGuard:

- `commercialRCHproxy` release finale `v0.3.0`, commit `7bb17f8`: Windows
  `200 passed, 15 skipped`; precedente gate Debian/WSL `215 passed`;
- `printproxy` tag finale `standalone-final-2026-08-13`, commit `1291b84`:
  rerun Windows finale `140 passed, 5 skipped` in 75,22 s, incluso il test
  stress byte-exact da 2 MiB; la baseline Linux documentata del progetto resta
  verde.

## Matrice di collaudo ancora obbligatoria

1. Debian 12 target, install/update/rollback/uninstall e boot.
2. MariaDB del target Debian 12: ripetere migrazione; collaudare repository,
   concorrenza, charset, backup e restore end-to-end.
3. Stampanti fisiche: testo/encoding, grafica, cut/drawer, status, carta
   esaurita, RST, power loss e half-close.
4. PCAP direct-vs-proxy in entrambe le direzioni e hash del fascicolo.
5. Performance disco con `fsync_each_event` e spool crescente.
6. Frontend su browser supportati, accessibilità e reverse proxy HTTPS.
7. Recovery con DB indisponibile a lungo e riavvio host reale.

Un go-live richiede un report firmato con modello/firmware, data, operatore,
release, PCAP hash, anomalie e decisione esplicita.
