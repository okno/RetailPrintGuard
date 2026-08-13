# Importazione storica

## Obiettivo e proprietà

`retailprintguard-import` scansiona archivi esistenti senza modificarli. Usa gli
stessi adapter del worker continuo, ma esegue una scansione one-shot con limite
predefinito 10.000 candidati per adapter.

Formati contrattualizzati:

- `commercialrchproxy.capture.v1`;
- `commercialrchproxy.pharsed.v1`;
- `printproxy.archive.v3` basato su ledger v3 schema 1 e metadata schema 2.

Per commercialRCHproxy i due ingressi sono intenzionalmente distinti:

- `--rch-root` accetta soltanto job `commercialrchproxy.capture.v1` pubblicati
  con RAW autorevole richiesta/risposta, timeline e marker ready;
- `--rch-parsed-root` accetta soltanto derivati legacy
  `commercialrchproxy.pharsed.v1` quando il RAW non è disponibile.

Le opzioni sono mutuamente esclusive nella stessa esecuzione. Preferire sempre
`--rch-root`: un derivato PHARSED conserva provenienza/confidenza, ma non
ricostruisce byte mancanti e non equivale a evidenza RAW.

Uno schema sconosciuto non viene “interpretato al meglio”: diventa quarantena
logica. Un file che cambia durante la snapshot diventa `source_busy` e viene
riprovato in una scansione successiva.

## Preflight read-only

```bash
retailprintguard-import \
  --config /etc/retailprintguard/config.yaml \
  --rch-root /mnt/archivi/commercialrchproxy/jobs \
  --printproxy-root /mnt/archivi/printproxy/jobs \
  --printproxy-hmac-key-file /run/credentials/printproxy.integrity.key \
  --validate-only \
  --max-jobs 10000 \
  --json
```

Se esiste soltanto l'albero legacy PHARSED, validarlo in una scansione separata:

```bash
retailprintguard-import \
  --config /etc/retailprintguard/config.yaml \
  --rch-parsed-root /mnt/archivi/commercialrchproxy/PHARSED \
  --validate-only \
  --max-jobs 10000 \
  --json
```

`--validate-only` usa un sink a memoria limitata che non conserva chiavi o
payload. Verifica formato e integrità, ma non dimostra l'idempotenza su MariaDB.

Per ridurre il rischio operativo, montare gli archivi in sola lettura e creare
prima un inventario con numero file, dimensione e hash del supporto.

## Import persistente

Fuori da `--validate-only` è obbligatoria una factory Python fidata:

```bash
retailprintguard-import \
  --config /etc/retailprintguard/config.yaml \
  --rch-root /mnt/archivi/commercialrchproxy/jobs \
  --repository-factory retailprintguard.db.repository:create_ingestion_repository \
  --json
```

Per importare il solo derivato legacy sostituire `--rch-root` con
`--rch-parsed-root`; non passarli insieme e registrare nel report che il RAW
autorevole non era disponibile.

La funzione riceve `Settings` validato e deve restituire un oggetto conforme a
`IngestionRepository`:

```python
class IngestionRepository(Protocol):
    def store_import(self, envelope: NormalizedEnvelope) -> RepositoryImportResult: ...
    def record_retry(self, retry: RetryRecord) -> None: ...
    def quarantine(self, record: QuarantineRecord) -> None: ...
```

`store_import` deve acquisire la `source_key` e scrivere envelope, artefatti,
chunk e documenti in un'unica transazione. Una collisione concorrente restituisce
`DUPLICATE`. Non deduplicare soltanto per hash payload: due stampe reali identiche
sono eventi distinti.

La factory SQLAlchemy inclusa sincronizza i device configurati e inserisce
sessione, job, raw, timeline, parsed document/line/payment e ledger import in
una transazione. La unique `source_key` restituisce `DUPLICATE` sulle
riesecuzioni. Prima del go-live va comunque provata contro MariaDB reale.

Per lo spool nativo:

```bash
retailprintguard-import \
  --config /etc/retailprintguard/config.yaml \
  --canonical-root /var/lib/retailprintguard/spool \
  --repository-factory retailprintguard.db.repository:create_ingestion_repository \
  --json
```

## HMAC printproxy

Per default l'adapter richiede HMAC e una chiave originale di almeno 32 byte in
un file regolare non symlink. Il file è letto con limite 4096 byte e controllo
pre/post.

```bash
--printproxy-hmac-key-file /run/credentials/printproxy.integrity.key
```

`--allow-unauthenticated-printproxy` accetta soltanto ledger creati
esplicitamente senza HMAC. Non bypassa HMAC presente ma invalido e non disattiva
la verifica della catena/hash/head.

## Mappatura dispositivi

L'adapter ricava il target dall'archivio e lo associa alla coppia
`target_ip:target_port` dei dispositivi abilitati nel YAML. Un target storico
non mappato viene rifiutato. Usare un `source_instance_id` stabile e distinto
per ogni installazione sorgente:

```bash
--rch-instance rch-sede-a
--printproxy-instance pos-sede-a
```

Gli ID accettano lettere, numeri, punto, underscore e trattino, massimo 128
caratteri.

## Retry e risultati

Il worker usa cinque tentativi con backoff esponenziale tra
`retry_initial_seconds` e `retry_max_seconds`. Il report contiene:

- `discovered`;
- `imported`;
- `duplicates`;
- `quarantined`;
- `retry_exhausted`;
- `source_busy`;
- `errors`.

Codici uscita:

- `0`: scansione completata senza quarantena/retry esauriti/source busy;
- `1`: almeno una di tali condizioni richiede attenzione;
- `2`: errore di configurazione, I/O o contratto/factory.

Database offline non modifica il relay o l'archivio. Se tutti i retry falliscono,
il job resta nella sorgente e una scansione successiva lo riscopre; l'unicità
transazionale evita duplicazioni dopo un commit il cui esito client fosse
incerto.

Gli adapter non spostano in una directory fisica i candidati in quarantena e
non marcano i file sorgente come importati: ledger, retry e quarantena sono nel
database. La unità ingestion monta lo spool canonico in sola lettura e consente
scrittura soltanto ad archive/state/log; il contratto read-only vale anche se
il processo viene compromesso entro i limiti del sandbox systemd.

## Report e riconciliazione

Per ogni batch persistente registrare almeno:

1. radice e instance ID;
2. data/ora UTC e versione software;
3. numero scoperti/importati/duplicati/quarantinati;
4. elenco bounded degli errori;
5. hash o identificativo dell'inventario sorgente;
6. operatore e ticket autorizzativo;
7. risultato di una seconda esecuzione, che deve riportare duplicati e zero
   nuovi record logici.

Non cancellare o “normalizzare in-place” l'archivio storico dopo l'import. La
retention della copia sorgente segue una decisione separata e documentata.

## Recovery

- `source_busy`: assicurarsi che il proxy legacy abbia finito di pubblicare e
  ripetere;
- quarantena schema/hash: conservare il file, verificare release sorgente e
  chiave; non correggere manualmente l'evidenza;
- DB offline: ripristinare MariaDB, controllare le transazioni e rilanciare;
- disco DB pieno: fermare ingestion, non il relay; liberare capacità in modo
  approvato e rilanciare;
- import interrotto: rilanciare con gli stessi instance ID e radici.

I test sintetici di questi casi sono descritti in [TEST_REPORT.md](TEST_REPORT.md).
