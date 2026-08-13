# Registro delle decisioni architetturali

## ADR-001 — Monorepo modulare

**Stato:** accettata.

Codice condiviso per configurazione, dominio, database e contratti; processi
indipendenti per data e control plane. Riduce duplicazioni senza creare un
singolo processo critico.

## ADR-002 — Relay POS e RCH separati

**Stato:** accettata.

Lo stesso motore byte-exact viene avviato con `--device-type pos` e
`--device-type rch` in unità diverse. Un crash o rollout di una famiglia non
deve fermare l'altra. Alternative scartate: processo unico per tutti i device,
o mantenimento indefinito di due implementazioni TCP divergenti.

## ADR-003 — Database fuori dal data plane

**Stato:** accettata, non negoziabile.

Il proxy non riceve URL DB e non importa moduli database. L'indisponibilità di
MariaDB deve degradare ingestion/API, non il forwarding.

## ADR-004 — Spool filesystem locale come journal

**Stato:** accettata.

Directory atomiche, RAW direzionali, timeline e `.ready` formano il contratto
tra relay e ingestion. È più semplice da recuperare e auditare rispetto a un
broker aggiuntivo; Redis/RabbitMQ/Kafka non sono introdotti.

## ADR-005 — Full duplex canonico

**Stato:** accettata.

Ogni nuovo job conserva entrambi i flussi integralmente. Il formato storico
printproxy v3 non può ricostruire il reverse oltre il preview disponibile;
questa lacuna viene preservata come warning, non colmata con dati inventati.

## ADR-006 — YAML schema 1 e segreti esterni

**Stato:** accettata.

Un file Pydantic strict sostituisce configurazioni duplicate. Gli endpoint e le
ACL sono nel YAML; password DB, JWT e HMAC sono file/env protetti. Il compilatore
legacy è un ponte temporaneo e non modifica la rete.

## ADR-007 — MariaDB/InnoDB e DECIMAL

**Stato:** accettata.

MariaDB soddisfa il requisito di sito. UUID binari riducono gli indici,
`DECIMAL(19,4)` evita errori monetari, timestamp UTC rendono confrontabili le
sorgenti. SQLite resta un backend di test, non produzione.

## ADR-008 — Interpretazioni versionate

**Stato:** accettata.

Un documento ha identità stabile e più `document_versions`. Aggiornare un
parser non sovrascrive output storico; hash, build, confidenza e warning restano
attribuibili.

## ADR-009 — Correlazione multi-criterio

**Stato:** accettata.

Un singolo codice può essere riutilizzato o mancante. Il motore usa punteggi
ponderati, conserva spiegazione e aggrega i fiscali per riconoscere conti
separati. L'algoritmo è rielaborabile e versionato.

## ADR-010 — Regole deterministiche prima del machine learning

**Stato:** accettata.

Le sedici regole producono evidenze leggibili e soglie versionate. Un modello ML
potrà essere un segnale aggiuntivo, mai una dipendenza del relay o una prova non
spiegabile.

## ADR-011 — Tamper-evident, non tamper-proof

**Stato:** accettata.

SHA-256, HMAC e catene rilevano alterazioni rispetto a head affidabili. Non si
promette impossibilità di modifica contro root. Backup/anchor esterni sono
necessari per rafforzare la catena di custodia.

## ADR-012 — API sincrona con repository boundary

**Stato:** accettata.

FastAPI espone DTO indipendenti dall'ORM; ogni call repository apre/chiude la
sessione. Il relay non condivide questo adapter. L'implementazione può evolvere
senza cambiare il contratto HTTP.

## ADR-013 — Frontend statico e bearer token in memoria

**Stato:** accettata per la prima release.

La build Vite è servita da nginx e usa API same-origin. Il token non persiste
tra refresh. Alternative future, come cookie HttpOnly/CSRF, richiedono una ADR
specifica e test di sicurezza.

## ADR-014 — Migrazione per adapter read-only

**Stato:** accettata.

Gli archivi legacy non vengono rinominati o marcati. L'idempotenza appartiene a
MariaDB tramite `source_key`; schema non noto o hash errato è quarantena logica.

## ADR-015 — Nessuna gestione implicita degli IP

**Stato:** accettata.

Installer e proxy verificano che i listener siano assegnati ma non cambiano
indirizzi, route, DNS o firewall. La rete è un prerequisito esplicito e deve
essere gestita dal sistema del sito o da uno strumento separato e approvato.
