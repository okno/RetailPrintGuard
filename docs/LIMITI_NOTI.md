# Limiti noti e rischi residui

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
- Non esiste ancora export fascicolo evidenze PDF/ZIP; l'API offre CSV alert e
  download RAW singolo.
- Mancano ordinamento generico server-side, filtri temporali completi e
  persistenza avanzata filtri.
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
  transazioni; non espongono ancora selezione per batch storico/periodo o una
  policy automatica completa di supersessione/chiusura degli alert quando input
  o regole cambiano.

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
