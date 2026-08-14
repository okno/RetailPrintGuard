# Piano di rollback

## Principio

La continuità del data plane viene prima del control plane. Un rollback non deve
riemettere payload: dopo un timeout l'esito sul dispositivo può essere ignoto.

## Livelli

### Livello 1 — Solo control plane

Se stampa e spool sono corretti ma API/parser/correlazione/antifrode falliscono:

1. fermare solo il servizio secondario interessato;
2. lasciare attivi POS/RCH;
3. conservare journal e stato DB;
4. applicare una correzione o ripristinare la release control-plane in finestra.

La mitigazione dell'incidente ha seguito questo modello fermando solo il worker
antifrode. Il comando inverso va eseguito soltanto dopo il gate correttivo.

### Livello 2 — Release applicativa precedente

Richiede quiescenza e autorizzazione perché `rollback.sh` commuta symlink e
riavvia i servizi installati:

```bash
sudo /opt/retailprintguard/current/scripts/rollback.sh
```

Oppure indicare un'identità release content-addressed già installata:

```bash
sudo /opt/retailprintguard/current/scripts/rollback.sh --release <20_HEX>
```

Lo script non esegue downgrade DB. La migrazione deve essere forward-compatible
con la release precedente o il rollback applicativo non è sicuro.

### Livello 3 — Restore database

Solo se una migrazione dati rende inutilizzabile anche la release precedente:

1. fermare control plane e bloccare scritture;
2. preservare DB/spool correnti come evidenza;
3. verificare archivio e sidecar del backup scelto;
4. eseguire restore su copia e validarlo;
5. solo con approvazione, usare `restore.sh` sul target;
6. riallineare Alembic e verificare catene/conteggi;
7. avviare prima control plane in isolamento, poi data plane in finestra.

Un restore DB può perdere record successivi al backup; tali dati vanno prima
conservati e riconciliati, mai sovrascritti alla cieca.

### Livello 4 — Bypass/legacy

Ultima risorsa per un difetto del relay:

1. bloccare nuove stampe;
2. attendere sessioni chiuse;
3. fermare solo i proxy RetailPrintGuard;
4. riportare il gestionale ai target fisici annotati nel registro privato o
   reinstallare le release legacy congelate;
5. assicurare che un solo data plane possieda listener/VIP;
6. preservare tutti i job `UNKNOWN` e non riprodurli.

## Dati da conservare prima del rollback

- spool completo e marker;
- release e frontend attivi/staged;
- revisione DB e dump consistente;
- journal servizi e nginx;
- stato socket/indirizzi in forma privata;
- metriche/conteggi prima e dopo;
- motivo, approvatore, operatore e comandi root redatti.

## Verifica post-rollback

- un solo listener per endpoint;
- servizi nel profilo atteso;
- nessun restart loop;
- hash/manifest verificabili;
- database e API coerenti con la release;
- nessun nuovo duplicato a input invariato;
- evento di rollback registrato nel change log operativo.

## Divieti

- nessun `git reset --hard` come procedura di deployment;
- nessun `DROP DATABASE` manuale fuori da `restore.sh` autorizzato;
- nessun `nft flush ruleset`;
- nessun replay di RAW fiscale;
- nessuna eliminazione dei duplicati storici o dei job incompleti.
