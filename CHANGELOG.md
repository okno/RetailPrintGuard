# Changelog

Tutte le modifiche rilevanti sono documentate in questo file. Il progetto segue
la struttura di Keep a Changelog.

## [Unreleased]

## [0.4.8] — 2026-08-21

### Added

- intestazione RCH strutturata e versionata con insegna, ragione sociale,
  indirizzo, telefono, codice fiscale e partita IVA, disponibile nel dettaglio
  Web, nella tabella Documenti e nei derivati TXT, JSON e PDF;
- provenienza esplicita `RCH_PRINTED_HEADER` per i valori realmente osservati
  nel flusso e `DEVICE_METADATA_CONFIGURED` per il fallback amministrativo del
  dispositivo quando l'intestazione viene generata internamente dalla RCH.

### Changed

- parser RCH `1.6.0` e renderer PDF `1.4.0`; le righe dell'intestazione restano
  nel testo normalizzato e negli span RAW, ma non diventano righe economiche;
- l'intestazione segue la versione parser attiva e il rollback interpretativo,
  senza sovrascrivere le versioni precedenti né alterare il RAW.

### Fixed

- i derivati leggibili non duplicano un'intestazione già osservata nel corpo;
  il PDF manda a capo anche i metadati lunghi invece di tagliarli.

## [0.4.7] — 2026-08-21

### Added

- tipi documentali autonomi `SHIFT_END_REPORT` (“REPORT DI FINE TURNO”) e
  `INVOICE` (“FATTURA”), disponibili in parser RCH, filtri, dashboard, WebUI e
  PDF; il riconoscimento fattura richiede un'intestazione o un numero documento
  forte e non scatta sulla riga aggregata “Fatture” di un report;
- campi versionati separati per ora applicativa, ora del footer e seriale RCH,
  con precisione/provenienza, scarto fra i due orologi e acquisizione server
  mantenuta distinta; un seriale configurato è dichiarato esplicitamente come
  metadato non osservato sul flusso;
- contatori dashboard per report di fine turno e fatture, e identità RCH
  completa anche nella tabella Documenti.

### Changed

- parser RCH `1.5.0`, correlatore `rpg-correlation-1.5.0`, motore antifrode
  `rpg-fraud-1.3.0` e renderer PDF `1.3.0`;
- report di fine turno e fatture restano evidenze amministrative ma sono
  esclusi da ordini, attribuzione prezzi e alert di vendita; un reparse che
  corregge una precedente classificazione gestionale ritira correlazioni e
  alert derivati in modo append-only, senza eliminare documenti o cronologia;
- i frammenti `INCOMPLETE` privi di righe, identità vendita, importi o pagamenti
  non compaiono più nelle viste operative predefinite; restano nel database,
  nel RAW e nelle viste tecniche esplicite per audit.

### Fixed

- evitata la sovrapposizione fra ora applicativa stampata e orologio interno
  della RCH: quando il footer non transita nel flusso viene mostrato come non
  osservato, senza sostituirlo con l'ora server;
- timeline, ordini e ricerca operativi non mostrano più proiezioni vendita
  obsolete appartenenti a report/fatture riclassificati; lo storico rimane
  consultabile e tamper-evident.

## [0.4.6] — 2026-08-20

### Fixed

- concede esclusivamente all'API la capability `CAP_NET_RAW`, necessaria al
  monitor ICMP delle stampanti quando il servizio usa `NoNewPrivileges`; gli
  altri servizi, inclusi proxy e worker, mantengono il bounding set vuoto.

## [0.4.5] — 2026-08-20

### Added

- allowlist parser-only `RPG_POS_BEEPER_PRINTER` per limitare il buzzer a POS
  specifici (per esempio `2,3`), watcher read-only dello spool in crescita a
  100 ms con deduplica del fallback DB e monitor ICMP asincrono dei target
  fisici ogni 10 secondi per gli stati dashboard;
- colonna Web “Ora cassa” distinta dall’ora di acquisizione, con precisione e
  provenienza osservate; intestazioni della tabella Documenti riordinabili via
  trascinamento o tastiera e ordine persistito nel browser;
- notifica opzionale con buzzer POS80 non appena la preclassificazione bounded e
  senza OCR riconosce una nuova `COMANDA` POS completa, prima di OCR e commit:
  polling ingestion/parser a 250 ms, budget osservabile di 2 secondi e code
  separate per device, mai su RCH o durante reparse storici;
- correlazione `rpg-correlation-1.4.0` della sequenza di vendita composta da
  comanda, baseline gestionale/preconto, Documento Commerciale e copia
  gestionale, con vincoli congiunti su tavolo, articolo, direzione temporale e
  finestre bounded anti-bridge;
- campi versionati distinti per progressivo proprio osservato, suffisso RCH e
  riferimento commerciale, con ricerca, tabelle Web, JSON e PDF; l'eventuale
  codice commerciale completo risolto dalla correlazione resta una proiezione
  read-only con provenienza esplicita;
- dashboard con elenco rosso degli episodi economici nel periodo selezionato,
  totale dell'ammanco potenziale e collegamenti alla transazione e ai documenti.

### Changed

- parser ESC/POS `1.3.0`: normalizzazione dichiarata della sola `O` scambiata
  dall’OCR per zero nel prefisso numerico del tavolo (`O1-R` → `01-R`), con
  valore OCR originale conservato; parser RCH `1.3.0`: data/ora estratta solo
  quando visibile nel testo stampato catturato;
- la WebUI presenta `KITCHEN_ORDER` come “COMANDA” e i device `pos_1`, `pos_2`
  e `pos_3` come BAR, CUCINA e PIZZERIA, mantenendo invariati gli identificatori
  persistiti e i contratti API;
- motore antifrode `rpg-fraud-1.2.0`: una riduzione materiale tra baseline e
  chiusura genera un solo `MODIFICA_POST_PRECONTO`, includendo importi,
  percentuale, differenze di riga e riferimenti documentali append-only;
- risposte dispositivo, copie di output, footer tecnici a zero e sintomi
  economici già assorbiti dall'alert primario non generano più alert operativi
  duplicati; gli alert storici riconoscibili vengono riclassificati senza
  cancellarne evidenza o storia;
- il parser RCH conserva soltanto il suffisso realmente osservato nelle
  risposte di stato e non costruisce un falso codice completo dai digit di
  stato; i progressivi generati internamente dalla RCH sono dichiarati non
  osservabili nel flusso quando manca una fonte indipendente.

## [0.4.2] — 2026-08-16

### Fixed

- normalizzati a `0755` per le directory e `0644` per i file i contenuti
  statici frontend, inclusi quelli già presenti con lo stesso hash, impedendo
  il `403 Forbidden` nginx causato dalla build con `umask 027`;
- aggiunta una postcondizione HTTP sulla root WebUI: l'updater non dichiara più
  successo se API e servizi sono sani ma `index.html` non è servibile.

## [0.4.1] — 2026-08-16

### Added

- quattro temi web persistenti e accessibili (`Office`, `Scuro`, `Unix old
  school`, `Hacker`), selezionabili dall'angolo superiore destro anche prima
  del login;
- updater da tag Git annotato `update_control_plane_from_git.sh`, con build
  frontend pulita, verifica identità versione e invarianti PID, invocation ID,
  timestamp di avvio e listener dei due proxy.

### Changed

- la modalità `--control-plane-only` mantiene un unico lock tra backup e
  installazione, non esegue mutazioni APT e non riavvia MariaDB; nuove
  dipendenze o configurazioni DB richiedono una finestra di manutenzione;
- drawer, AppBar, login, dashboard, tabelle, stati e anteprima scontrino usano
  token cromatici coerenti e leggibili in tutti i temi.

## [0.4.0] — 2026-08-15

### Added

- correlazione degli episodi di vendita `rpg-correlation-1.3.0`, con confini
  dopo la chiusura economica, conflitti espliciti di identità, diametro bounded
  dei dispatch POS e componenti ausiliari non colleganti;
- attribuzioni append-only dei prezzi mancanti sulle comande POS, con fonte a
  riga/versione completa, algoritmo, confidenza e conflitti visibili senza
  selezione arbitraria;
- filtri temporali timezone-aware e half-open su dashboard, transazioni,
  documenti, ricerca, job, alert ed export; dashboard web sugli ultimi sette
  giorni per default;
- pagina di revisione dei job tecnicamente incompleti, download RAW autorizzato
  e azioni `VERIFY_USABLE`, `EXCLUDE_FROM_ANALYSIS` e `REOPEN_REVIEW`;
- metriche dashboard distinte per alert operativi, episodi con riduzione,
  differenza economica non duplicata, incompleti pendenti e alert archiviati;
- provisioning interattivo `retailprintguard-configure-review`, che conserva
  soltanto un hash Argon2id in un file ambiente protetto.

### Changed

- un'unica anomalia economica `MODIFICA_POST_PRECONTO` raccoglie importi e diff
  di righe senza moltiplicare alert o perdita stimata; conti divisi completi,
  rimborsi separati, addebiti camera e delta di comanda sono trattati secondo la
  loro semantica osservata;
- alert legacy riconducibili a difetti noti vengono riclassificati in modo
  append-only e hash-chained; gli alert superseded o non operativi restano
  nell'archivio ma non alterano lo stato corrente della transazione;
- il PDF delle comande usa un layout termico compatto con tavolo, coperti,
  portate, quantità leggibili, prezzi osservati/derivati e provenienza, evitando
  la duplicazione del testo normalizzato;
- `update.sh --control-plane-only` rifiuta release con artefatti data plane
  o closure runtime differenti prima di qualsiasi DDL, riavvia soltanto
  worker/API, ricarica nginx e verifica che i PID dei due proxy siano rimasti
  invariati;
- alert operativi come vista web predefinita, archivio esplicito e drill-down
  economico limitato a episodi chiusi con riduzione attiva.

### Security

- la revisione ad alto impatto degli incompleti richiede ruolo `ADMIN`, nuova
  autenticazione con segreto dedicato e motivazione; l'esclusione riguarda solo
  l'analisi e non elimina RAW, manifest, documenti o audit; una revisione
  riaperta resta esclusa fino a `VERIFY_USABLE`;
- il segreto di conferma non è ammesso in chiaro in YAML, repository, argomenti
  della shell o log.

## [0.3.1] — 2026-08-14

### Fixed

- corretti i parametri delle rotte frontend per i dettagli di documenti e
  transazioni: la pagina ora interroga realmente l'API usando l'UUID indicato
  nell'URL;
- la diagnostica usa le metriche spool persistite dall'ingestion worker e non
  sovrascrive più lo stato reale con `unknown`;
- API, dashboard, ricerca e transazioni selezionano la versione parser attiva,
  mantenendo coerenti reparse, attivazione e rollback;
- la vista scontrino e il PDF usano una proiezione leggibile che nasconde le
  annotazioni tecniche ESC/POS senza modificare testo normalizzato o RAW;
- le risposte tecniche RCH sono separate dalla vista documentale primaria, ma
  restano consultabili come evidenze; stampe distinte in job differenti non
  vengono eliminate o fuse;
- errori e retry del parser concorrono allo stato diagnostico e i contatori
  dashboard derivano dai documenti della versione parser selezionata.

## [0.3.0] — 2026-08-14

### Added

- parser ESC/POS `1.2.0` con ricostruzione bounded delle bande raster `ESC *` e
  OCR Tesseract confinato al worker parser, per estrarre il tavolo senza
  coinvolgere il relay o modificare il RAW;
- semantica POS versionata per portata, quantità firmate, descrizioni mandate
  a capo, timestamp documento e campi identificativi; una variazione `-1x`
  resta un delta e diventa rimozione solo quando lo stato derivato arriva a
  zero;
- correlazione `rpg-correlation-1.2.0` per dispatch simultanei su reparti POS
  differenti e per variazioni recenti sullo stesso tavolo/dispositivo, con
  overlap articolo obbligatorio e criteri leggibili;
- migrazione append-only dei campi semantici in `document_versions` e della
  portata in `document_lines`, con selezione della versione parser attiva.

### Changed

- l'identità immutabile della build ESC/POS include anche versione e lingua
  del runtime OCR disponibile; il reparse crea una nuova versione senza
  sovrascrivere RAW o interpretazioni precedenti;
- l'installer Debian aggiunge i pacchetti Tesseract italiano e inglese soltanto
  al control plane parser. I servizi proxy restano privi di OCR e database.

## [0.2.1] — 2026-08-14

### Fixed

- il backup pianificato conserva contenuti e timestamp senza replicare UID/GID
  nello staging root-only e normalizza i mode delle evidenze a `0750/0640`;
  resta quindi compatibile con l'unità systemd priva di `CAP_CHOWN` e con
  `RestrictSUIDSGID=yes`, mentre il restore applica esplicitamente le identità
  locali verificate;
- aggiunta una regressione sul contratto tra hardening dell'unità backup e
  opzioni `rsync`.

## [0.2.0] — 2026-08-14

### Added

- renderer PDF receipt-style deterministico, versionato e bounded, endpoint
  autenticato con checksum e download integrato nella web application;
- metadati dispositivo validati (`mac_address`, reparto e ruolo) con migrazione
  MariaDB e valori di esempio esclusivamente sintetici;
- regola composita `MODIFICA_POST_PRECONTO` con chiusure economiche distinte da
  chiusure fiscali e diff spiegabile;
- pacchetto documentale di incident assessment, matrice delle evidenze, analisi
  della causa, data flow as-is/to-be, ADR del trasporto e piani verificabili di
  test, deployment e rollback;
- strumenti operativi sicuri per test, validazione offline delle catture,
  reprocessing controllato, verifica segreti ed export auditato dei documenti;
- `AGENTS.md` con vincoli espliciti per data plane, apparati, privacy ed
  evidenze.

### Documentation

- registrata la distinzione tra osservazioni fotografiche, RAW/timeline,
  database e log, senza pubblicare IP, MAC, PII, UUID o hash operativi;
- documentata la mitigazione dell'incidente: arresto del solo worker antifrode,
  con proxy POS/RCH lasciati attivi e senza probe o restart degli apparati.

### Fixed

- reverse tail ora basata su inattività, abort sincrono del trasporto prima del
  rilascio lock, ordine timeline completion-safe, capture non bloccante e
  recovery `PARTIAL` coerente;
- parser ESC/POS/RCH corretti per cut legacy, quantità/prezzo, totale copia/IVA,
  status d'errore, annulli e documenti camera/non riscossi;
- fingerprint antifrode reso stabile tra polling e batch ingestion
  solo-duplicati non più persistiti;
- duplicati alert storici preservati ma marcati con relazione canonica tramite
  migrazione non distruttiva;
- API e frontend corretti per paginazione, query N+1, mapping, logout/cache,
  ricerca, export sicuro, error state e download evidenze.

### Security

- download RAW/TXT/JSON/PDF verificati con SHA-256 e auditati;
- nginx corredato da esempio TLS e security gate per segreti/artefatti privati.

## [0.1.9] — 2026-08-13

### Changed

- nginx espone esplicitamente la webapp su `0.0.0.0:8081`, mantenendo FastAPI
  su loopback; documentati firewall e HTTPS richiesti;
- aggiunti comandi operativi protetti per start, stop, restart e log unificati,
  oltre all'inventario delle directory dati e al troubleshooting rapido.

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
