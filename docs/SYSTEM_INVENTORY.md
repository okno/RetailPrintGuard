# Inventario del sistema

Stato dell'inventario: analisi offline del codice e osservazione read-only del
target, con identificativi di sito redatti. L'inventario storico dettagliato dei
due proxy standalone è in [ANALISI_INIZIALE.md](ANALISI_INIZIALE.md).

## Componenti applicativi

| Componente | Processo | Dipendenze sincrone | Dati prodotti |
|---|---|---|---|
| Proxy POS | `retailprintguard-pos-proxy` | rete client/target, filesystem spool | RAW full-duplex, timeline, manifest |
| Proxy RCH | `retailprintguard-rch-proxy` | rete client/target, filesystem spool | RAW full-duplex, risposte, timeline, manifest |
| Ingestion | `retailprintguard-ingestion` | spool read-only, MariaDB | sessioni, chunk, payload, job, batch |
| Parser | `retailprintguard-parser` | MariaDB/archivio | documenti e versioni interpretate |
| Correlazione | `retailprintguard-correlate` | MariaDB | ordini, eventi, snapshot, transazioni |
| Antifrode | `retailprintguard-fraud` | MariaDB | alert, evidenze e storia |
| API | `retailprintguard-api` | MariaDB, file RAW autorizzati | REST `/api/v1`, OpenAPI, audit download |
| Web app | build React/TypeScript servita da nginx | API same-origin | nessuna evidenza autorevole |

I proxy non importano moduli DB/API/parser. Ogni famiglia viene eseguita in una
unità systemd distinta e con un account Linux dedicato.

## Dispositivi logici

La configurazione di produzione prevede tre route POS e una route RCH. IP,
porte, ACL, MAC amministrativi e target sono dati di sito e non sono riportati
nel repository pubblico. Il file di esempio usa esclusivamente valori
sintetici:

| Alias documentale | Tipo | Funzione | Direzioni conservate |
|---|---|---|---|
| `Stampante BAR POS80BL` | POS | comande bar | client→device e device→client |
| `Stampante CUCINA POS80BL` | POS | comande cucina | client→device e device→client |
| `Stampante PIZZERIA POS80BL` | POS | comande pizzeria | client→device e device→client |
| `CASSA RCH Print! F` | RCH | documenti fiscali/gestionali e status | client→device e device→client |

Gli ID stabili effettivi sono definiti solo in
`/etc/retailprintguard/config.yaml`, protetto da gruppo. La procedura di
configurazione è in [CONFIGURAZIONE.md](CONFIGURAZIONE.md).

## Servizi di sistema

Unità applicative previste:

- `retailprintguard-pos-proxy.service`;
- `retailprintguard-rch-proxy.service`;
- `retailprintguard-ingestion.service`;
- `retailprintguard-parser.service`;
- `retailprintguard-correlation.service`;
- `retailprintguard-fraud.service`;
- `retailprintguard-api.service`;
- `retailprintguard-backup.timer` e relativo servizio;
- nginx come reverse proxy/static server;
- MariaDB su loopback/socket locale.

Durante il contenimento dell'incidente è stato fermato **solo** il worker
antifrode, che generava record duplicati. I proxy POS/RCH sono rimasti attivi.
La correzione nel repository non è da considerarsi attiva finché non viene
completato il deployment controllato descritto in
[DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md).

## Directory installate

| Percorso | Contenuto | Scrittura prevista |
|---|---|---|
| `/etc/retailprintguard` | YAML, URL DB e segreti protetti | root/installer |
| `/opt/retailprintguard/releases` | release content-addressed | installer |
| `/opt/retailprintguard/current` | symlink release attiva | update/rollback |
| `/var/lib/retailprintguard/spool` | job locali per dispositivo | soli proxy |
| `/var/lib/retailprintguard/archive` | job importati | worker |
| `/var/lib/retailprintguard/state` | stato release e worker | installer/worker |
| `/var/log/retailprintguard` | log applicativi | servizi autorizzati |
| `/var/backups/retailprintguard` | backup root-only | backup/restore |
| `/var/www/retailprintguard/releases` | build frontend | installer |
| `/var/www/retailprintguard/current` | symlink frontend attivo | update/rollback |

## Formati e codifiche

- spool canonico `retailprintguard-bidirectional-v1` con RAW separati per
  direzione, timeline JSONL, manifest e marker atomico `.ready`;
- import legacy `commercialrchproxy.capture.v1`, derivati storici
  `commercialrchproxy.pharsed.v1` e printproxy v3;
- ESC/POS conservato in byte originali, con decodifica best-effort e caratteri
  di controllo resi visibili soltanto nei derivati;
- RCH interpretato esclusivamente secondo frame/documenti osservati; BCC e
  risposte restano evidenza separata dal testo normalizzato;
- timestamp persistiti in UTC e presentati in `Europe/Rome`.

## Stato probatorio dell'incidente

Il campione privato analizzato comprende job POS e RCH, fotografie e record DB.
Tutti i riferimenti pubblici sono pseudonimizzati. Per il dettaglio si vedano:

- [INCIDENT_ASSESSMENT.md](INCIDENT_ASSESSMENT.md);
- [EVIDENCE_MATRIX.md](EVIDENCE_MATRIX.md);
- [ROOT_CAUSE_ANALYSIS.md](ROOT_CAUSE_ANALYSIS.md).

## Azioni root registrate, in forma redatta

Nel corso dell'analisi autorizzata sono state eseguite soltanto queste classi di
comandi privilegiati:

1. creazione di un backup applicativo in directory root-only;
2. verifica `gzip`, indice `tar` e sidecar SHA-256 del backup;
3. inventario read-only di unità, socket, filesystem, log e conteggi DB;
4. copia root-only di un sottoinsieme spool per analisi offline;
5. arresto del solo `retailprintguard-fraud.service` per contenere la crescita
   incontrollata degli alert.

Non sono stati eseguiti probe sugli apparati, replay, stampe di test, modifiche
di rete o restart dei proxy. Il rollback della mitigazione è l'avvio del solo
worker antifrode, da effettuare **dopo** correzione e approvazione.
