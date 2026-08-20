# Aggiornamento e rielaborazione dei parser

## Regola fondamentale

Un parser nuovo aggiunge una versione; non modifica RAW, manifest, documenti o
versioni precedenti. La provenienza minima è:

- nome parser;
- versione semantica;
- hash della build;
- versione schema output;
- data rilascio e note;
- fixture sintetiche e casi reali sanitizzati autorizzati.

## Processo di rilascio

1. Congelare una baseline di RAW sintetici e risultati attesi.
2. Implementare parsing bounded e incrementale: nessun assunto
   pacchetto=documento.
3. Conservare comandi/caratteri sconosciuti come warning e span, non scartarli.
4. Aggiungere fixture per segmentazione arbitraria, documenti aggregati,
   encoding e input malformato.
5. Verificare che il relay non importi il nuovo parser.
6. Registrare `parser_versions` con build SHA-256.
7. Eseguire shadow reparse su un campione e confrontare vecchia/nuova versione.
8. Approvare variazioni di tipo, importo, righe e confidenza.
9. Rielaborare a batch limitati; monitorare DB, spool e alert.
10. Conservare un report con conteggi e differenze.

## RCH

Il parser RCH deve rimanere vincolato alle forme osservate e alla documentazione
disponibile. Porta 23, BCC/ACK o un frammento XML non bastano per attribuire una
semantica non dimostrata. Le associazioni risposta/documento devono riportare
livello evidenza e incertezza.

Per gli archivi `commercialRCHproxy` 0.3 con capture RAW v1 usare `--rch-root`:
il RAW importato può essere rielaborato dal parser nativo senza toccare la
sorgente. `--rch-parsed-root` è l'alternativa legacy soltanto quando resta il
derivato `commercialrchproxy.pharsed.v1` e il RAW non è disponibile; non
attribuisce al derivato la forza probatoria del RAW. Non modificare `PHARSED`
manualmente. Vedere [Importazione storica](IMPORT_STORICO.md).

## ESC/POS

I comandi ESC/POS possono includere testo, cambio codepage, raster, QR, barcode,
feed, cut, drawer e status realtime. Un decoder deve:

- mantenere offset raw;
- distinguere testo da dati binari;
- limitare dimensioni raster e loop;
- invocare l'OCR, quando necessario, soltanto nel worker parser con timeout,
  output e pixel bounded; assenza o errore OCR deve produrre warning, non
  perdita del RAW o errore del relay;
- non trasformare preview reverse printproxy in risposta completa;
- marcare `UNKNOWN` quando la semantica business non è sufficiente.

Il parser ESC/POS `1.3.0` ricostruisce esclusivamente gruppi coerenti di bande
`ESC *` osservate e usa Tesseract con lingua configurata dall'ambiente protetto
del servizio. Accetta il tavolo soltanto con pattern stretto e confidenza almeno
80. Testo OCR, confidenza, bounding box, dimensioni, offset e hash del bitmap
derivato restano nei metadati; il payload originale non viene modificato. Se la
sola parte numerica del codice tavolo contiene la tipica confusione OCR `O/0`,
il derivato viene normalizzato (`O1-R` → `01-R`) e conserva nei metadati valore
osservato, valore normalizzato, regola applicata e warning. Codici alfanumerici
come `LAB-22` o `OVEST-1` non sono corretti automaticamente.
L'OCR non è importato né eseguito dai proxy POS/RCH.

## Parser nativi correnti

Sono presenti due parser nativi versionati:

- `retailprintguard-escpos` `1.3.0`: input massimo 16 MiB, massimo 1.024 documenti,
  split su comandi cut osservati, codepage allowlist, controlli resi come marker
  leggibili, quantità firmate, portate, ricomposizione conservativa delle righe
  mandate a capo e OCR raster bounded;
- `retailprintguard-rch-observed` `1.3.0`: framing incrementale sul flusso ricostruito,
  BCC, ACK e issue bounded; ricostruisce aperture/chiusure gestionali e
  commerciali osservate e produce anche `DEVICE_RESPONSE` per frame reverse
  validi. Data e ora sono assegnate al documento solo se visibili nel testo
  catturato; precisione al minuto/secondo e provenienza restano nei metadati.

Entrambi conservano SHA-256, span/direzione/offset, parser/versione, confidenza,
warning e documento `UNKNOWN` quando l'evidenza non basta. Il parser RCH etichetta
le semantiche business inferite come `INFERRED`; un frame valido non equivale a
prova di fiscalizzazione fisica.

Gli adapter continuano inoltre a normalizzare documenti già prodotti dal
Parser RCH 0.3. Il worker DB nativo `retailprintguard-parser` è un processo
separato: seleziona job `IMPORTED` e retry scaduti, verifica di nuovo gli
SHA-256 dei RAW, sceglie il parser dalla configurazione del device e pubblica
in una transazione `documents`, `document_versions`, righe e pagamenti. Gli ID
documento derivano da job/posizione/hash e non dalla versione parser, così una
nuova build aggiunge una versione alla stessa identità. Un output già presente
per parser/build/hash sorgente viene ignorato; il job diventa `PARSED` oppure
`PARSE_EMPTY`. Le `DEVICE_RESPONSE` RCH sono collegate al `RESPONSE_RAW`, non al
RAW della richiesta. Il correlatore le collega alla richiesta/documento prima
per `source_job_id` esatto, altrimenti solo per stessa sessione/device entro la
finestra configurata; il fallback resta un'inferenza dichiarata.

Esecuzione one-shot controllata:

```bash
retailprintguard-parser \
  --config /etc/retailprintguard/config.yaml \
  --once \
  --limit 100 \
  --json
```

Questo comando diretto presuppone che `RPG_DATABASE_URL` sia già disponibile
in un ambiente amministrativo protetto; non copiare il segreto nella command
line o nella history.

La modalità continua è gestita dalla unità
`retailprintguard-parser.service`. Un errore genera un `system_event` senza
modificare il RAW e senza coinvolgere il relay. I retry hanno backoff
esponenziale da 5 secondi fino a un'ora; all'ottavo errore il job passa a
`PARSE_FAILED` e richiede un reparse esplicito. Il worker non è una prova di
copertura del dialetto fisico: fixture/PCAP autorizzati e collaudo hardware
restano necessari.

### Buzzer POS dopo una COMANDA

Il worker può inviare il comando buzzer documentato POS80K non appena una nuova
`KITCHEN_ORDER` completa, proveniente da un device POS/ESC-POS, viene
riconosciuta dalla preclassificazione testuale bounded. Il controllo avviene
prima di OCR, normalizzazione completa e commit: il decoder resta puro e
l'invio è best-effort su una coda bounded distinta per POS. Non viene eseguito
per RCH, preconti, documenti incompleti, retry già persistiti o
`--reparse-all`.

Ingestion e parser eseguono il polling ogni 250 ms; correlazione e antifrode
mantengono il polling a 3 secondi. Il journal espone `capture_to_queue_ms` e
`queue_delay_ms`; oltre 2.000 ms viene registrato
`pos_beeper_latency_budget_missed`. Il limite è un budget operativo misurato
dalla disponibilità del RAW: una sessione di stampa che non ha ancora prodotto
un RAW importabile, scheduler e rete non costituiscono una garanzia real-time.

La configurazione è intenzionalmente separata dal YAML condiviso con i proxy:

```ini
RPG_POS_BEEPER_ENABLED=true
RPG_POS_BEEPER_COUNT=3
RPG_POS_BEEPER_ON_MS=300
RPG_POS_BEEPER_OFF_MS=200
RPG_POS_BEEPER_CONNECT_TIMEOUT_SECONDS=1.0
RPG_POS_BEEPER_QUEUE_SIZE_PER_DEVICE=64
```

Salvarla in `/etc/retailprintguard/parser.env` con proprietario
`root:retailprintguard-config` e modo `0640`. Conteggio ammesso: 1..63; tempi
ON/OFF: 0..25.500 ms in multipli di 100 ms. Host e porta non si duplicano nel
file: derivano dal target del device POS già validato. Il comando non modifica
frequenza/altezza del tono. Un invio TCP riuscito prova soltanto l'accettazione
dei byte da parte dello stack remoto, non che il buzzer sia stato udito.

La repository registra in `parser_versions.build_sha256` un digest del modulo
che implementa il parser. Per ESC/POS il digest include inoltre l'identità del
runtime Tesseract e la lingua configurata; una modifica del codice o del runtime
OCR crea quindi una build distinta anche senza cambio della versione semantica.
La release deve comunque essere content-addressed: il digest del parser non
sostituisce la provenienza del pacchetto completo.

Tipo, sottotipo, riferimenti, tavolo, operatore, terminale e timestamp sono
salvati in ogni `document_version`; la portata appartiene alla relativa riga.
Il reparse aggiorna soltanto la proiezione corrente e aggiunge una versione
immutabile. Per record creati prima della migrazione il backfill può copiare
soltanto l'ultima proiezione legacy disponibile, perché le vecchie differenze
non erano state memorizzate; RAW, hash e catene esistenti restano invariati.

Rielaborazione append-only, esclusivamente one-shot:

```bash
sudo systemctl stop retailprintguard-parser.service \
  retailprintguard-correlation.service \
  retailprintguard-fraud.service
sudo systemd-run --wait --pipe --collect \
  --unit=retailprintguard-parser-reparse \
  --property=Type=oneshot \
  --property=User=retailprintguard-worker \
  --property=Group=retailprintguard-worker \
  --property=EnvironmentFile=/etc/retailprintguard/database.env \
  /opt/retailprintguard/current/.venv/bin/retailprintguard-parser \
  --config /etc/retailprintguard/config.yaml \
  --once --reparse-all --limit 100 --json
sudo systemctl start retailprintguard-parser.service \
  retailprintguard-correlation.service \
  retailprintguard-fraud.service
```

Ripetere per batch dopo aver verificato i risultati. `--reparse-all` include
anche `PARSED`, `PARSE_EMPTY` e `PARSE_FAILED`; la repository non duplica un
output già presente per la stessa build e lo stesso hash sorgente. Il comando
non offre ancora filtri per periodo/device: applicarlo prima su staging o su un
dataset delimitato e conservare il report di ogni batch.

## Rollback parser

Il rollback non elimina la versione nuova:

1. disabilitare il worker interessato;
2. ripristinare la release precedente senza cancellare le nuove versioni;
3. selezionare esplicitamente la versione precedente nel control plane;
4. ricalcolare correlazioni/alert se erano basati sulla nuova interpretazione;
5. conservare le versioni e gli alert prodotti, marcandone la supersessione;
6. registrare proprietario della change, motivo e intervallo coinvolto.

Il modello `active_parser_versions` e i consumer supportano la selezione
esplicita; un cambio del puntatore viene incluso nel fingerprint del watermark
e forza il riesame bounded. Elencare prima le build realmente registrate, senza
inserire credenziali nella command line:

```bash
sudo mariadb --protocol=socket --database retailprintguard --batch \
  --execute='SELECT name, version, build_sha256, installed_at FROM parser_versions ORDER BY name, installed_at'
```

Poi fermare i consumer, selezionare l'identità esatta e lasciare che il comando
riavvolga per default il watermark di correlazione:

```bash
RPG_PARSER_NAME='retailprintguard-escpos'
RPG_PARSER_VERSION='1.3.0'
RPG_PARSER_BUILD_SHA256='incollare-qui-i-64-caratteri-esadecimali-verificati'
RPG_PARSER_CHANGE_REASON='inserire change-id e motivazione approvata'
[[ "${RPG_PARSER_BUILD_SHA256}" =~ ^[0-9a-f]{64}$ ]] || exit 1

sudo systemctl stop retailprintguard-correlation.service \
  retailprintguard-fraud.service
sudo systemd-run --wait --pipe --collect \
  --unit=retailprintguard-parser-activation \
  --property=Type=oneshot \
  --property=User=retailprintguard-worker \
  --property=Group=retailprintguard-worker \
  --property=EnvironmentFile=/etc/retailprintguard/database.env \
  /opt/retailprintguard/current/.venv/bin/retailprintguard-correlate \
  --config /etc/retailprintguard/config.yaml \
  --activate-parser "${RPG_PARSER_NAME}" \
  --parser-version "${RPG_PARSER_VERSION}" \
  --build-sha256 "${RPG_PARSER_BUILD_SHA256}" \
  --activation-reason "${RPG_PARSER_CHANGE_REASON}" \
  --once --json
sudo systemctl start retailprintguard-correlation.service \
  retailprintguard-fraud.service
```

Per RCH il nome corrente è `retailprintguard-rch-observed`. Sostituire versione
e hash esclusivamente con valori letti da `parser_versions`; il comando rifiuta
una build non installata e aggiorna puntatore/watermark nella stessa
transazione. L'esecuzione `--once` tratta al massimo 10.000 seed per default;
il servizio continuo completa i batch successivi.

`--activation-reason` resta sul puntatore corrente, ma la CLI non associa ancora
un utente autenticato: nella CLI di sistema `actor_user_id` è nullo. L'operazione
aggiunge comunque `PARSER_ACTIVATED` sia a `system_events` sia alla catena
globale `audit_log`, includendo build, versione precedente, motivo e scelta di
rewind. Collegare l'output JSON e il record audit alla change operativa esterna
che identifica l'operatore.

L'opzione `--no-rewind` conserva il cursore e aggiorna atomicamente il suo
fingerprint; usarla solo se una validazione approvata dimostra che la nuova
selezione non richiede il ricalcolo storico. Il default con rewind è la scelta
prudente. Non usare SQL improvvisato e non modificare mai una riga
`parser_versions` referenziata per farla apparire come una build diversa.
