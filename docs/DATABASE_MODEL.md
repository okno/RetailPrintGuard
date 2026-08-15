# Modello database e integrità

Questa è la vista di assurance del modello. Tipi, foreign key e diagramma ER
completo sono in [DATABASE.md](DATABASE.md); Alembic resta la fonte autorevole
dello schema installato.

## Aree del modello

| Area | Entità principali | Scopo |
|---|---|---|
| dispositivi | `devices`, `device_status` | identità, ruolo, stato e telemetria |
| trasporto | `proxy_sessions`, `stream_chunks`, `print_jobs`, `raw_payloads` | provenienza e byte originali |
| interpretazione | `parser_versions`, `active_parser_versions`, `documents`, `document_versions`, `document_lines` | output parser append-only |
| ordini | `orders`, `order_events`, `order_snapshots`, `payments` | evoluzione economica ricostruibile |
| correlazione | `document_correlations`, `document_correlation_members`, `line_price_attributions` | score, criteri, membri e prezzi POS derivati con provenienza |
| antifrode | `fraud_rules`, `fraud_rule_versions`, `fraud_whitelists`, `fraud_alerts`, `fraud_alert_evidence`, `fraud_alert_history` | regole ed evidenze spiegabili |
| accesso | `users`, `roles`, `user_roles`, `audit_log` | autenticazione, RBAC e audit |
| sistema | `system_events`, `import_batches`, `import_items`, `analysis_watermarks`, `hash_chain_heads` | operazioni e idempotenza |

## Regole dati

- InnoDB, foreign key e `utf8mb4` in produzione;
- valori monetari `DECIMAL`, mai floating point;
- timestamp persistiti UTC;
- UUID interni non usati come prova esterna e non pubblicati nei report;
- payload originali distinti dai derivati normalizzati;
- ogni nuova interpretazione crea una `document_version` e non sovrascrive la
  precedente;
- `source_key`, hash e vincoli univoci rendono gli import idempotenti;
- payload incompleti restano importabili ma sono marcati e non promossi a prova
  completa.
- i prezzi ricavati da preconto, documento gestionale o commerciale sono
  inferenze append-only; puntano alla riga e versione sorgente e non modificano
  la riga POS o il RAW. Versioni monetarie incomplete non sono fonti; valori
  discordanti restano evidenze di conflitto e non producono un prezzo scelto.
- la revisione tecnica di un job incompleto aggiorna una proiezione auditata
  (`PENDING`, `VERIFIED_USABLE`, `EXCLUDED`); la riapertura resta esclusa dalle
  analisi fino a una nuova verifica esplicita e non cancella job o payload.

## Catene tamper-evident

Le catene coprono payload/documenti, eventi ordine, storia alert e audit. Ogni
record include hash precedente e hash corrente; `hash_chain_heads` mantiene il
checkpoint. Questo consente di rilevare una modifica rispetto a un head
affidabile, non impedisce a root di riscrivere record e head. Backup e anchor
esterni sono richiesti per una catena di custodia più forte.

## Idempotenza e duplicati

L'identità di un finding non deve includere valori volatili come l'istante di
valutazione. Un ciclo ripetuto sugli stessi input deve restituire zero nuovi
alert. I duplicati storici non vengono eliminati. La migrazione 0.2.0 aggiunge
`is_canonical`, `duplicate_of_alert_id`, istante e motivo di deduplica; per la
regola affetta conserva il primo record per versione/transazione e collega i
successivi. L'interfaccia li esclude dai conteggi operativi predefiniti,
lasciandoli consultabili per audit.

Analogamente, una scansione ingestion con soli `source_key` già presenti non
deve creare un nuovo `import_batch`. Un batch deve rappresentare lavoro
materiale o errori da governare, non il semplice polling.

## Privilegi e connessioni

- MariaDB ascolta solo su loopback/socket locale;
- un account DDL effimero applica le migrazioni;
- l'account applicativo ha soli privilegi DML necessari;
- i proxy non ricevono URL/credenziali DB;
- API e worker ottengono il segreto da file/env protetti;
- query ORM/SQL sono parametrizzate.

## Retention

I parametri di retention non sono un executor. Prima di cancellare dati servono
policy approvata, legal hold, backup verificato, catena audit e implementazione
dedicata. RAW o record di incidente non devono essere rimossi da una semplice
operazione di cleanup.

## Migrazione e verifica

1. backup online consistente;
2. restore su staging isolato;
3. `alembic upgrade head` con account DDL temporaneo;
4. controllo revision marker, FK, indici, charset e conteggi;
5. doppia esecuzione dei worker su dati invariati;
6. downgrade provato solo sulla copia;
7. nessun downgrade automatico del DB durante rollback applicativo.
