# Formato spool ed evidenze

## Spool canonico RetailPrintGuard

Il relay scrive sotto `spool_root`, separato per `device_id`:

```text
<spool_root>/
  pos_1/
    <timestamp><job-id>.partial/
      session.json
      client.raw
      device.raw
      timeline.jsonl
    <timestamp><job-id>/
      session.json
      client.raw
      device.raw
      timeline.jsonl
      manifest.json
      .ready
```

La directory `.partial` è privata alla cattura. In chiusura, i file vengono
flushati e sincronizzati, viene creato il manifest, la directory viene rinominata
e infine appare `.ready`. Un consumer deve ignorare qualunque directory senza
marker valido.

### File raw

- `client.raw`: concatenazione byte-exact client→dispositivo;
- `device.raw`: concatenazione byte-exact dispositivo→client.

Sono separati perché una semplice concatenazione interdirezionale perderebbe
offset e semantica del flusso. L'ordine relativo si ricostruisce dalla timeline.

### `timeline.jsonl`

Ogni riga canonica schema 1 contiene:

- sessione, job e dispositivo;
- sequenza globale osservata e sequenza per direzione;
- direzione e tipo evento (`data` o `eof`);
- timestamp UTC e monotonic;
- offset e lunghezza nel RAW direzionale;
- SHA-256 del payload;
- esito e ora dell'inoltro locale;
- eventuale errore;
- hash dell'evento precedente e hash dell'evento corrente.

La timeline non duplica il payload. L'hash dell'evento copre i metadati canonici
e il riferimento al precedente, formando una catena per sessione.

### `manifest.json`

Il formato è `retailprintguard-bidirectional-v1`, `schema_version: 1`. Registra
endpoint osservati/configurati, orari, motivo chiusura, completezza trasporto e
storage, contatori osservati/catturati, EOF, drop, errori e hash/dimensioni dei
quattro artefatti.

`status: COMPLETE` richiede contemporaneamente trasporto completo, conteggi
coerenti e assenza di errori/drop. In caso contrario il job è `PARTIAL`; i byte
presenti restano evidenza utile e non vengono eliminati.

### `.ready`

Il marker lega `job_id` e SHA-256 esatto del manifest. Non è una ricevuta della
stampante e non prova fiscalizzazione: dichiara solo che lo spool locale è stato
pubblicato.

## Recupero dopo crash

All'avvio, il `CaptureManager` cerca directory incomplete per dispositivo:

- un manifest esistente e leggibile viene preservato e pubblicato con marker;
- un manifest non affidabile viene rinominato `manifest.untrusted*.json`;
- se manca il manifest, vengono preservati/creati gli artefatti minimi e
  generato un manifest `PARTIAL` con motivo
  `recovered_after_unclean_shutdown`;
- non viene mai inventata completezza oltre i prefissi realmente catturati.

La recovery non interpreta il protocollo e non ritrasmette un job.

## Formati legacy importabili

### commercialRCHproxy 0.3

L'adapter riconosce esclusivamente:

- `commercialrchproxy.capture.v1` con `.ready`, manifest e hash coerenti;
- `commercialrchproxy.pharsed.v1` con `PHARSED/parsed.json` e `.parsed` coerenti.

Il nome `pharsed` è storico e intenzionale. I RAW request/response restano
immutati e il parsed è un secondo envelope, non una sostituzione della cattura.

### printproxy v3

L'adapter verifica il ledger canonico `manifest.jsonl` schema 1, i documenti
metadata/stato schema 2, la catena, il head e l'HMAC quando richiesto. Il RAW
storico autorevole è client→stampante. Il reverse è un preview metadata che può
essere troncato e viene marcato come tale.

## Sicurezza dei reader

Gli adapter storici:

- non scrivono né rinominano la sorgente;
- rifiutano symlink e path fuori radice;
- limitano dimensioni, numero di entry e profondità;
- leggono file regolari con controlli pre/post per rilevare mutazioni;
- validano schema, marker, dimensioni, hash e HMAC prima di creare il DTO;
- mappano il target a un `device_id` abilitato della configurazione;
- mettono in quarantena logica schemi sconosciuti o artefatti incoerenti.

“Quarantena” in questo contesto è un record nel repository, non lo spostamento
del file sorgente.

## Catene hash e limiti

Una catena hash rileva modifiche se il head affidabile e la sequenza vengono
conservati separatamente e verificati. Non impedisce a un attaccante con accesso
completo a dati e head di riscrivere entrambi. HMAC, backup immutabili, permessi,
monitoraggio e segregazione sono livelli complementari.

Lo spool canonico è letto da `CanonicalCaptureV1Adapter`, che verifica entrambi
i RAW, la catena timeline, i contatori, gli endpoint e il binding `.ready` →
manifest prima di consegnare l'envelope alla repository idempotente.
