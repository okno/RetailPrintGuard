# Rapporto test

> I conteggi in questo documento appartengono ai gate storici indicati nelle
> rispettive sezioni. Non attestano automaticamente il worktree correttivo
> post-incidente. Il piano corrente è in [TEST_PLAN.md](TEST_PLAN.md) e i
> risultati finali saranno consolidati in
> [FINAL_VALIDATION_REPORT.md](FINAL_VALIDATION_REPORT.md).

## Release 0.4.0 — episodi di vendita e riduzione falsi positivi

**Stato:** gate software offline completato il 15 agosto 2026. Nessun payload,
foto, identificatore operativo o segreto è incluso nelle fixture pubbliche.

| Controllo | Risultato |
|---|---|
| suite Python completa | `186 passed`, `25 skipped`, `0 failed`; una warning Starlette/httpx nota |
| Ruff e compileall su `src`, `tests`, `migrations` | PASS |
| migrazioni | PASS nel full gate: upgrade/downgrade, DDL offline MariaDB e nuove revisioni prezzi/revisione |
| frontend ESLint e TypeScript | PASS |
| frontend Vitest completo | `18 passed`, `0 failed` |
| wheel Python | PASS, `retailprintguard-0.4.0`, 10 entry point |
| PDF sintetico | PASS test e QA visuale 80 mm, inclusi prezzi derivati/conflitti |
| `bash -n` | PASS sui 23 script tramite Git Bash |
| ShellCheck | NON ESEGUITO: binario e distribuzione WSL assenti sul runner corrente |
| build Vite | da eseguire obbligatoriamente su Linux prima dello staging; runtime Windows fermo senza errore in trasformazione |
| privacy scan | PASS su 231 candidati |
| diff data-plane rispetto a `v0.3.1` | vuoto per proxy, moduli condivisi verificati, lock e unità POS/RCH |

Le regressioni coprono episodi separati, split fiscali, rimborsi, addebiti
camera, chiusure incomplete, sconti globali, prezzi discordanti, rollback parser,
filtri alert combinati, correlazioni superseded e l'intero ciclo di revisione
append-only degli incompleti. I 25 skip dipendono da funzionalità POSIX non
disponibili nel runner Windows (24) e da symlink di directory (1), non da
failure applicativi.

## Release 0.3.1 — visualizzazione documenti e diagnostica

**Stato:** gate software offline completato il 14 agosto 2026. La verifica
finale su browser e dati operativi deve essere eseguita dopo l'aggiornamento
controllato della produzione; nessun RAW operativo è incluso nella release.

| Controllo | Risultato |
|---|---|
| suite Python completa | `141 passed`, `24 skipped`, `0 failed` |
| Ruff, compileall e `git diff --check` | PASS |
| frontend ESLint e TypeScript | PASS |
| frontend Vitest completo | `10 passed`, `0 failed` |
| selezione versione parser attiva e paginazione documenti | PASS su SQLite; SQL MariaDB compilabile |
| build Vite | da certificare sul server Linux prima dell'attivazione; il runtime Windows locale è rimasto fermo su `transforming...` senza errore |

Le regressioni coprono i parametri UUID delle rotte documento/transazione, la
separazione delle risposte tecniche senza deduplicare job distinti, la
proiezione scontrino leggibile, lo stato spool derivato dalle metriche e il
divieto di propagare campi da una versione parser shadow a quella attiva.

## Release 0.3.0 — parser POS e correlazione `1.2.0`

**Stato:** gate offline completato il 14 agosto 2026; nessun RAW o dato
operativo è incluso nelle fixture pubbliche.

| Controllo | Risultato |
|---|---|
| suite Python completa | `135 passed`, `24 skipped`, `0 failed` |
| parser/versioning/correlazione mirati | `53 passed`, `23 skipped`, `0 failed` |
| stress idle-tail attivo | `10/10 passed`; risposta sintetica 800 ms, idle 500 ms |
| Ruff e compileall | PASS |
| Alembic SQLite + DDL offline MariaDB | `4 passed` |
| `install.sh` Bash/ShellCheck | PASS |
| `git diff --check` | PASS |

La matrice sintetica copre bande raster multi-strip, OCR bounded, descrizioni
mandate a capo, portate, quantità `2x` e delta `-1x`, rollback della versione
attiva, dispatch su tre reparti, riuso tavolo e variazione con articolo non
corrispondente. Gli skip sono 23 test POSIX non disponibili sul runner Windows
e un test symlink condizionale.

## Hotfix 0.2.1

**Stato:** completato offline il 14 agosto 2026; hardware e produzione esclusi.

La regressione riproduce `rsync` con capability bounding set vuoto: il comando
storico termina con codice `23`, mentre la copia con UID/GID esclusi e mode
normalizzati termina con codice `0` e contenuto identico. Il contratto è stato
verificato anche sotto sandbox systemd con `RestrictSUIDSGID=yes`.

| Controllo | Risultato |
|---|---|
| suite Python completa | `123 passed`, `24 skipped`, `0 failed` |
| test operativi mirati | `13 passed`, `23 skipped`, `0 failed` |
| Ruff | PASS |
| `bash -n` e ShellCheck su backup/libreria | PASS |
| identità versione backend/frontend | `0.2.1` |
| `git diff --check` | PASS |

## Gate candidato 0.2.0

**Stato:** completato offline il 14 agosto 2026; hardware e produzione esclusi.

| Controllo globale | Risultato finale |
|---|---|
| pytest pass | `123` |
| pytest skip motivati | `24` (23 test Bash indisponibili sul runner Windows, 1 symlink) |
| frontend test pass | `5` |
| warning accettati | `1` deprecazione Starlette/httpx |

La regressione mirata del trasporto include cinque partizioni deterministiche
del payload; il renderer PDF è deterministico, versionato e bounded. Ruff,
compileall, tre test migrazione, sintassi dei 23 script, ShellCheck e
l'installazione delle 32 dipendenze con `--require-hashes` sono passati. Bandit
non era installato nel runner e resta un gate non eseguito, non un PASS.

## Snapshot storico

Data: 13 agosto 2026. Il gate RetailPrintGuard fu eseguito immediatamente prima
del commit iniziale `fffb8d3`. È riportato solo come baseline storica e non
descrive la release 0.2.0.

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
- correlazione, diff, split payment, 16 regole di capitolato e la regola
  composita `MODIFICA_POST_PRECONTO`;
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
