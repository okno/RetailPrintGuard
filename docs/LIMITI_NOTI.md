# Limiti noti e rischi residui

Per lo stato del worktree correttivo e i criteri di chiusura usare anche
[OPEN_ISSUES.md](OPEN_ISSUES.md). Le voci sottostanti descrivono la baseline
storica dichiarata e non vanno considerate chiuse senza un nuovo gate.

Stato verificato sul worktree del 13 agosto 2026.

## Bloccanti per accettazione completa

- Nessun collaudo su tre stampanti POS fisiche e una RCH reale.
- Nessun confronto PCAP autorizzato direct-vs-proxy per byte, timing,
  FIN/RST/half-close e status.
- Nessuna esecuzione dell'installazione completa sul target Debian 12/systemd
  252; i test shell disponibili non equivalgono al boot reale.
- I parser nativi puri ESC/POS/RCH e il worker DB transazionale sono presenti.
  Le pure function, la persistenza, il collegamento response-RAW, il reparse
  append-only e il backoff sono testati su SQLite/fixture sintetiche; la
  copertura dei dialetti/device reali resta da attestare.
- L'OCR ESC/POS è un derivato inferenziale: una lettura del tavolo sotto soglia,
  conflittuale o non disponibile resta vuota/warning. Non sostituisce il RAW,
  la fotografia autorizzata o un identificativo testuale esplicito.
- Il backfill della migrazione semantica non può ricostruire differenze tra
  vecchie versioni che in precedenza non memorizzavano tipo/tavolo/operatore:
  copia la proiezione legacy disponibile senza modificare hash o RAW.
- `--once --reparse-all` è volutamente globale entro `--limit`: non espone
  ancora filtri per device, data, batch o versione sorgente, né orchestra da
  solo il ricalcolo selettivo di correlazioni e alert. Usarlo prima su un
  campione/staging e in batch controllati.
- La CLI di correlazione può selezionare una build esatta in
  `active_parser_versions`, richiede una motivazione e riavvolge il watermark.
  Registra storia hash-chained e `system_event`; essendo una CLI di sistema non
  associa però un utente autenticato (`actor_user_id` nullo). Collegarla a una
  change esterna con proprietario e non sostituirla con SQL ad hoc.
- I worker DB di correlazione e antifrode, i rispettivi entry point e le unità
  systemd sono presenti e hanno test integrati su SQLite. Restano da collaudare
  transazioni concorrenti, lock e prestazioni sulla MariaDB della major target.
- L'installer usa `requirements/production.lock` con hash, ora presente; la
  prova di installazione completa sul target resta comunque aperta.
- API e worker condividono un singolo account MariaDB DML sul database
  applicativo; migrazioni e proxy sono separati, ma account per-servizio e un
  account backup read-only restano hardening futuro.

## Incompletezze funzionali

- La UI mostra un diff JSON, non una comparazione affiancata semanticamente
  completa.
- Il documento singolo è esportabile in RAW/TXT/JSON/PDF e gli alert in CSV;
  non esiste ancora un fascicolo ZIP multi-documento firmato.
- Mancano ordinamento generico server-side, filtri temporali uniformi su tutte
  le liste e persistenza avanzata dei filtri. Dashboard, documenti, ricerca,
  transazioni, job, alert ed export usano comunque intervalli timezone-aware
  `[from, to)`.
- Nessun SSE/WebSocket: lo stato device viene aggiornato a polling.
- La build frontend passa, ma Vite segnala il chunk principale minificato di
  circa 540 kB; misurare il caricamento sui client reali e valutare ulteriore
  code splitting.
- Non esiste un executor di retention/anonymization; i campi di configurazione
  da soli non cancellano nulla.
- Trend e concentrazione dashboard dipendono dai dati repository; la
  concentrazione operator/device non è ancora popolata in ogni percorso.
- La CLI crea in sicurezza soltanto il primo `ADMIN`; non è ancora presente un
  workflow completo di amministrazione utenti/ruoli, reset password e revoca
  sessioni dalla UI/API.
- Health API indica database/spool, ma non è una metrica Prometheus né un
  readiness endpoint con codice HTTP distinto.
- Correlazione limita il numero di documenti e antifrode il numero di
  transazioni; non espongono ancora selezione per batch storico/periodo. La
  supersessione giustifica gli alert sostituiti e riclassifica alcuni difetti
  legacy deterministici, ma non può decidere automaticamente ogni falso
  positivo operativo.
- L'attribuzione del prezzo alle comande richiede codice esatto oppure
  descrizione normalizzata esatta e quantità compatibile. Sinonimi, modificatori
  testuali o candidati monetari discordanti restano senza prezzo risolto o
  `AMBIGUOUS`; non vengono scelti euristicamente. Documenti monetari incompleti
  sono esclusi dalle fonti di attribuzione.
- `EXCLUDE_FROM_ANALYSIS` non è una cancellazione e non soddisfa eventuali
  obblighi di erasure/retention: RAW, manifest, documenti, audit e storia
  restano conservati.
- Il PDF comanda è una vista derivata bounded. La leggibilità su carta termica,
  font del browser e stampanti reali richiede collaudo visuale del sito; non
  dimostra il flusso byte o la consegna fisica.

## Limiti delle evidenze legacy

- `printproxy` v3 conserva RAW autorevole solo client→stampante; il reverse è
  preview e può essere troncato. Non è recuperabile a posteriori.
- `commercialRCHproxy` è congelato a `v0.3.0` e `printproxy` al tag
  `standalone-final-2026-08-13`; un archivio operativo va comunque associato al
  relativo commit e verificato prima dell'import.
- Fotografie e scontrini ricostruiti non dimostrano i byte, il protocollo o la
  ricezione fisica.
- `local_write_drain_completed` prova solo avanzamento locale del socket.

## Rischi operativi

- `storage_failure_policy=continue` privilegia la stampa ma può produrre
  evidenza incompleta; `abort` ha il trade-off opposto.
- `fsync_each_event=true` aumenta durabilità e latenza I/O: va misurato sul
  disco target.
- Il backup `.tar.gz` include segreti e non è cifrato nativamente.
- Un root ostile può riscrivere dati e head; serve anchor/backup esterno.
- Un indirizzo listener configurato ma non gestito persistentemente dalla rete
  può scomparire al reboot.
- Il time sync errato degrada correlazione e valore probatorio dei timestamp.
- Un account SSH privo di accesso al journal può ottenere un risultato vuoto
  durante l'analisi dei log. Serve un export redatto prodotto da un account
  autorizzato; “zero righe” non prova assenza di eventi. La disponibilità di
  SSH non implica privilegi root o presenza di `sudo`.

## Cosa è verificato in software

- full duplex byte-exact con fake device, inclusi quattro device concorrenti;
- isolamento dal database e spool durante forwarding;
- ACL, lock target, failure policy e recovery `.partial`;
- validazione canonical/RCH/printproxy, tamper detection, retry, quarantena e
  idempotenza;
- modelli MariaDB/SQLite, repository SQLAlchemy, API/RBAC e regole pure;
- parser DB con identità documento stabile, versioni append-only, build hash,
  response RAW e retry bounded;
- scenari 100→50 e 100→50+50 su dati sintetici.

Questi risultati riducono il rischio software ma non chiudono i punti bloccanti.
