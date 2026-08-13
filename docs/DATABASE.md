# Database MariaDB

## Principi

Lo schema di produzione è progettato per MariaDB con InnoDB e `utf8mb4`.
SQLAlchemy 2 gestisce il modello e Alembic le migrazioni. SQLite è ammesso nei
test veloci. Il lifecycle Alembic è stato provato anche su MariaDB reale in un
ambiente Debian 13 temporaneo; questo non sostituisce il collaudo di repository,
concorrenza e restore sul target Debian 12.

- UUID applicativi: `BINARY(16)`;
- importi: `DECIMAL(19,4)`, mai floating point;
- timestamp: input timezone-aware, normalizzati in UTC e salvati come
  `DATETIME(6)`; la UI visualizza in `Europe/Rome`;
- payload: `LONGBLOB` dove richiesto;
- transazioni: isolamento `READ COMMITTED`;
- pool applicativo: pre-ping, recycle 1800 secondi, default 10 + 20 overflow;
- foreign key e indici sono nominati in modo deterministico.

La migrazione iniziale è `29517f373309`. `env.py` esegue commit esplicito sia
dopo la configurazione della sessione sia dopo il revision marker: è necessario
con l'autobegin MariaDB, altrimenti il DDL può restare visibile mentre
`alembic_version` rimane vuota. Per applicarla:

```bash
export RPG_DATABASE_URL='mysql+pymysql://utente:password@127.0.0.1:3306/retailprintguard?charset=utf8mb4'
alembic upgrade head
```

Non passare `RPG_DATABASE_URL` ai processi proxy. Il downgrade può eliminare
tabelle: eseguire prima backup e prova di restore; in produzione si preferisce
rollback applicativo con schema compatibile.

## ER semplificato

```mermaid
erDiagram
    DEVICES ||--o{ DEVICE_STATUS : reports
    DEVICES ||--o{ PROXY_SESSIONS : owns
    PROXY_SESSIONS ||--o{ PRINT_JOBS : contains
    PRINT_JOBS ||--o{ RAW_PAYLOADS : preserves
    PROXY_SESSIONS ||--o{ STREAM_CHUNKS : orders
    PRINT_JOBS ||--o{ DOCUMENTS : produces
    PARSER_VERSIONS ||--o{ DOCUMENT_VERSIONS : parses
    PARSER_VERSIONS ||--o| ACTIVE_PARSER_VERSIONS : selected_by
    DOCUMENTS ||--o{ DOCUMENT_VERSIONS : versions
    DOCUMENT_VERSIONS ||--o{ DOCUMENT_LINES : contains
    ORDERS ||--o{ ORDER_EVENTS : evolves
    ORDERS ||--o{ ORDER_SNAPSHOTS : snapshots
    ORDERS ||--o{ PAYMENTS : receives
    DOCUMENT_CORRELATIONS ||--o{ DOCUMENT_CORRELATION_MEMBERS : groups
    DOCUMENTS ||--o{ DOCUMENT_CORRELATION_MEMBERS : participates
    FRAUD_RULES ||--o{ FRAUD_RULE_VERSIONS : versions
    FRAUD_RULE_VERSIONS ||--o{ FRAUD_ALERTS : raises
    FRAUD_ALERTS ||--o{ FRAUD_ALERT_EVIDENCE : proves
    FRAUD_ALERTS ||--o{ FRAUD_ALERT_HISTORY : tracks
    USERS ||--o{ USER_ROLES : has
    ROLES ||--o{ USER_ROLES : grants
    IMPORT_BATCHES ||--o{ IMPORT_ITEMS : contains
```

## Catalogo tabelle

### Acquisizione

| Tabella | Scopo |
|---|---|
| `devices` | inventario logico e endpoint configurati |
| `device_status` | osservazioni temporali di stato e ultimo errore |
| `proxy_sessions` | sessioni TCP, endpoint, apertura/chiusura e completezza |
| `print_jobs` | job pubblicati e stato import/parser; `source_key` univoca |
| `raw_payloads` | artefatti originali, dimensione/hash, storage e catena |
| `stream_chunks` | direzione, sequenza, offset, timing e forwarding |
| `system_events` | eventi tecnici correlati a servizio/device/sessione/job |

### Parsing e documenti

| Tabella | Scopo |
|---|---|
| `parser_versions` | nome, versione e build del parser |
| `active_parser_versions` | puntatore esplicito alla build attiva per nome parser, con motivo/data |
| `documents` | identità stabile del documento sorgente |
| `document_versions` | interpretazioni versionate, importi, testo, warning, hash |
| `document_lines` | articoli, quantità, prezzi, sconti, IVA e span raw |
| `payments` | pagamenti associati a ordine o versione documento |

La coppia `document_id`/`version_sequence` è univoca. Anche
`document_id`/`parser_version_id`/`source_payload_sha256` impedisce di creare due
risultati uguali per lo stesso parser e payload. Una rielaborazione non deve
sovrascrivere la versione precedente.

In assenza di un puntatore in `active_parser_versions`, i consumer scelgono la
versione con sequenza più alta. Un puntatore consente un rollback interpretativo
esplicito senza eliminare versioni. La CLI coordina puntatore e watermark e
conserva data/motivazione correnti; ogni cambio aggiunge `PARSER_ACTIVATED` a
`system_events` e alla catena globale `audit_log`. La CLI di sistema non dispone
di un principal utente e registra quindi actor nullo, da ricondurre alla change
operativa come descritto in [Aggiornamento parser](AGGIORNAMENTO_PARSER.md).

### Ordini e correlazione

| Tabella | Scopo |
|---|---|
| `orders` | identità ordine per dispositivo/data/codice e stato corrente |
| `order_events` | eventi append-only con sequenza e catena hash |
| `order_snapshots` | stato versionato ricostruibile |
| `document_correlations` | transazione, algoritmo, score e spiegazione |
| `document_correlation_members` | relazione molti-a-molti documento/correlazione |

### Antifrode

| Tabella | Scopo |
|---|---|
| `fraud_rules` | identità e stato della regola |
| `fraud_rule_versions` | parametri, soglia, peso e severità versionati |
| `fraud_whitelists` | eccezioni motivate, per scope e finestra temporale |
| `fraud_alerts` | finding corrente e workflow di revisione |
| `fraud_alert_evidence` | riferimenti ordinati a documenti/job/raw e dati esplicativi |
| `fraud_alert_history` | ogni transizione/note con catena hash |

### Identità, audit e import

| Tabella | Scopo |
|---|---|
| `users` | account, password hash, stato e blocco |
| `roles` | ruoli `ADMIN`, `AUDITOR`, `OPERATOR`, `READ_ONLY` |
| `user_roles` | assegnazioni con attore e data |
| `audit_log` | azioni append-only con correlation ID e catena hash |
| `import_batches` | esecuzione di import, contatori e report |
| `import_items` | ledger idempotente per artefatto/evento sorgente |
| `hash_chain_heads` | head serializzato per ogni scope di catena |
| `analysis_watermarks` | cursori durevoli dei worker derivati, conteggio e metadati lookback |

## Integrità e idempotenza

Le unicità principali impediscono doppie sessioni/job della stessa sorgente,
doppie sequenze e doppie importazioni. La sola unique constraint non basta: la
repository di ingestion deve acquisire la `source_key` e inserire tutti i record
correlati nella **stessa transazione**. Una race deve tornare `DUPLICATE`, non
generare un secondo documento.

Le catene sono previste almeno per raw, versioni documento, eventi/snapshot
ordine, storia alert e audit. `hash_chain_heads` permette di serializzare
l'append per scope; l'implementazione deve bloccare il head nella transazione,
calcolare il record successivo e aggiornare entrambi atomicamente.

## Account e privilegi

L'installer corrente applica questa separazione:

- account migrazioni effimero: DDL sul solo database applicativo, eliminato
  subito dopo `alembic upgrade head`;
- unico account `retailprintguard_app`: `SELECT`, `INSERT`, `UPDATE`, `DELETE`
  sull'intero database applicativo, condiviso da API e worker control plane;
- backup/restore: accesso amministrativo locale tramite socket, eseguito dalla
  unità root;
- proxy: **nessun account database e nessuna URL DB**.

L'account DML non può eseguire DDL né amministrare utenti, ma non è ancora
separato per servizio/tabella; anche il backup non ha ancora un account dedicato
read-only. Questa è un'area di hardening residua, non una separazione già
implementata.

MariaDB deve ascoltare su loopback o socket Unix e non essere esposto alla LAN
stampanti. Le password vengono fornite da file ambiente protetto o credential
manager di sistema, mai committate.

## Controlli dopo migrazione

```bash
alembic current
alembic heads
python -m pytest -q tests/test_db_models.py
```

Prima di una release eseguire anche upgrade e restore su una MariaDB della
stessa major del target; il test SQLite verifica mapping e vincoli portabili,
non charset, lock, DDL o comportamento InnoDB reali.

Il controllo su MariaDB Debian 13 ha verificato la sequenza upgrade → revision
`29517f373309` → downgrade a zero tabelle applicative → re-upgrade con marker
presente. Il downgrade elimina le tabelle in ordine inverso di dipendenza;
evita di rimuovere prima indici ancora richiesti dalle foreign key (errore
MariaDB 1553). Database, account e virtualenv temporanei usati dalla prova sono
stati rimossi.
