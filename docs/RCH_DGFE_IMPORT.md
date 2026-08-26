# Importazione DGFE e memoria fiscale RCH

## Scopo e confine di sicurezza

Questa procedura porta in RetailPrintGuard un export XML prodotto da una RCH
senza aggiungere alcuna dipendenza al proxy e senza inviare comandi alla cassa
dal server Linux. L'acquisizione e l'importazione sono due operazioni separate:

```text
RCH accesa e inattiva
  -> RCH XTools (Windows/Android, lettura autorizzata)
  -> file XML originale
  -> inbox Linux protetta
  -> validazione/import offline RetailPrintGuard
```

Il wrapper Linux non contiene un client per la porta 23, non conosce indirizzi
di sito e non apre socket verso la RCH. Non arresta, riavvia o ricarica i proxy
POS/RCH. Il file esportato resta evidenza distinta dal RAW osservato dal MITM:
le due fonti possono essere correlate, ma mai sovrascritte l'una con l'altra.

## Perché l'acquisizione non è nativa Linux

La procedura RCH documentata per `PRINT! F` usa **RCH XTools**. Le versioni
ufficialmente documentate sono Windows e Android; non è stato individuato un
collector Linux ufficiale né un contratto pubblico completo e firmware-specifico
per implementarne uno in sicurezza. Le pagine pubbliche del protocollo nominano
le famiglie di lettura, ma non forniscono i corpi comando necessari per costruire
un client verificabile.

Per questo motivo è vietato dedurre opcode dal traffico osservato, usare `telnet`,
`nc` o inoltrare comandi attraverso il listener MITM. La porta LAN della RCH non
è un'API generica e un comando sbagliato può avere effetti fiscali o di
programmazione.

Riferimenti del produttore e normativi:

- [Manuale RCH XTools](https://support.rch.it/download/28926/);
- [Manuale protocollo PRINT! F](https://support.rch.it/docs/print-f/manuale-protocollo/);
- [Specifiche tecniche RT 11.2](https://www.agenziaentrate.gov.it/portale/documents/20143/9796628/Specifiche_Tecniche_RT_V11.2.pdf/271e6bac-1ec0-6e6f-3493-32dd369d2c21?download=true).

## Topologie ammesse

### Preferita: sidecar XTools fuori banda

Usare un PC Windows o un dispositivo Android autorizzato con XTools, collegato
alla seriale di servizio supportata dalla RCH. Il sidecar esporta il file e lo
trasferisce al server Linux. Il collegamento seriale evita di condividere la
sessione Ethernet usata in produzione.

### LAN: solo dopo conferma RCH

XTools può essere eseguito su un host Windows/Android nella stessa LAN e
collegarsi all'indirizzo **fisico** della RCH. Questa variante è ammessa soltanto
se il produttore o il tecnico RCH conferma per il modello e firmware installati
che una sessione di lettura concorrente non interferisce con la connessione di
stampa. La lettura va comunque effettuata a cassa accesa ma inattiva, senza
documenti aperti e in una finestra senza vendite.

Non usare mai l'indirizzo virtuale del proxy come destinazione XTools. Se manca
la conferma sulla concorrenza LAN, usare la seriale oppure una finestra in cui
la RCH non sta ricevendo stampe; il proxy resta comunque in esecuzione.

### XTools in VM Windows sul server Linux

Una VM Windows con interfaccia `bridged` può sostituire il PC sidecar. È una
scelta operativa, non un collector Linux nativo. Richiede:

1. licenza e credenziali XTools legittime;
2. rete della VM separata dal listener virtuale del proxy;
3. accesso diretto all'indirizzo fisico della RCH;
4. conferma RCH sulla sessione LAN concorrente;
5. disattivazione di snapshot, backup o sospensione della VM durante la lettura.

Non configurare port-forward, NAT applicativo o bridge trasparente attraverso
RetailPrintGuard.

## Lettura pilota con XTools

Prima del primo export annotare modello, matricola RT, firmware, versione XTools,
operatore e ticket/finestra autorizzata. Usare un documento già chiuso e noto.

1. Verificare che la RCH sia accesa, in stato pronto e senza documento aperto.
2. Verificare che non vi siano sessioni di vendita attive; non fermare i proxy.
3. Aprire XTools con profilo `Utente`.
4. Aprire esclusivamente `Funzioni Cassa -> Letture DGFE`.
5. Impostare una data passata e lo stesso progressivo come inizio e fine.
6. Selezionare `Export V11 XML` e salvare l'originale senza aprirlo in editor.
7. Se XTools richiede PIN tecnico, abilitazione memoria, aggiornamento firmware o
   programmazione, annullare e rivolgersi al tecnico RCH.
8. Calcolare SHA-256 sul sidecar e trasferire file e hash al server Linux.

Non utilizzare chiusura giornaliera, annullo, reso, azzeramento, inizializzazione
o sostituzione DGFE, modifica data/ora/rete, aggiornamento firmware, `invio di
prova` o pannello comandi protocollo.

La lettura non emette una vendita, ma viene registrata dalla RCH come operazione
gestionale. Va quindi eseguita da personale autorizzato e lasciata nella pista
di audit.

## Companion automatizzato Windows

`scripts/windows/Acquire-RchDgfeExport.ps1` automatizza il tratto Windows che
segue l'azione di export in XTools. Può avviare l'eseguibile ufficiale, fotografa
lo stato iniziale della cartella, attende soltanto XML nuovi o modificati e li
accetta quando dimensione e timestamp restano stabili per un intervallo
configurabile. Per ogni file:

- impedisce link/reparse point e limita la dimensione a 64 MiB;
- usa un lettore XML con DTD/entity e resolver esterni disabilitati;
- ammette soltanto `MemoriaDettaglio` o `MemoriaRiepilogo`, namespace noto e
  versione payload `3.0`;
- verifica che sia presente una sola `MatricolaRT` e, se configurata, che
  coincida con la cassa attesa;
- conserva una copia byte-identica con nome content-addressed, SHA-256 e
  manifest JSON scritto atomicamente in una cartella privata;
- rileva la presenza di XMLDSIG, ma non dichiara verificata la firma in assenza
  della trust chain del dispositivo.

Lo script non conosce l'IP della RCH, non apre socket, non invia comandi e non
usa GUI automation. L'operatore deve ancora selezionare esplicitamente in
XTools `Funzioni Cassa -> Letture DGFE -> Export V11 XML`. Questa separazione è
intenzionale: la versione pubblica di XTools non espone una CLI documentata per
la lettura e un'automazione basata su coordinate/clic potrebbe attivare una
funzione fiscale diversa dopo un aggiornamento dell'interfaccia.

### Primo avvio

Aprire PowerShell come l'utente autorizzato e indicare i percorsi reali di
XTools e della sua cartella export. Non usare `ExecutionPolicy Bypass` in
produzione: firmare lo script secondo la policy aziendale oppure usare
`RemoteSigned`/`AllSigned`.

```powershell
powershell.exe -NoProfile -ExecutionPolicy AllSigned `
  -File "C:\RetailPrintGuard\scripts\windows\Acquire-RchDgfeExport.ps1" `
  -ExportDirectory "C:\RCH-DGFE\export" `
  -OutboxDirectory "C:\ProgramData\RetailPrintGuard\RchDgfe\outbox" `
  -ExpectedRchSerial "SOSTITUIRE1" `
  -LaunchXTools `
  -XToolsPath "C:\PERCORSO_UFFICIALE\RCH XTools.exe" `
  -TimeoutSeconds 900 `
  -StableSeconds 10
```

`SOSTITUIRE1` è soltanto un segnaposto sintatticamente valido e deve essere
sostituito con la matricola RT realmente autorizzata prima dell'esecuzione.

`-LaunchXTools` richiede una firma Authenticode valida e non offre bypass. Se il
pacchetto non risulta firmato, verificare l'installazione col produttore e
avviare XTools separatamente; lo script può continuare a sorvegliare la cartella
senza `-LaunchXTools`. Lo script non chiude XTools al termine.

Per acquisire in modo controllato un XML già esportato:

```powershell
powershell.exe -NoProfile -ExecutionPolicy AllSigned `
  -File "C:\RetailPrintGuard\scripts\windows\Acquire-RchDgfeExport.ps1" `
  -SourceFile "C:\RCH-DGFE\export\export.xml" `
  -OutboxDirectory "C:\ProgramData\RetailPrintGuard\RchDgfe\outbox" `
  -ExpectedRchSerial "SOSTITUIRE1"
```

L'output standard è JSON ed è quindi acquisibile da Utilità di pianificazione.
Il task può avviare il watcher, ma non può rendere non presidiata la selezione
del comando in XTools. Una vera esecuzione unattended sarà possibile soltanto
se RCH pubblicherà una CLI/SDK con una funzione allow-listed di sola lettura.

## Inbox Linux

Percorso suggerito:

```text
/var/lib/retailprintguard/import/rch-memory/inbox
```

Crearlo una sola volta come `root`, adattando il gruppo all'identità di controllo
già presente sul sito:

```bash
sudo install -d -o root -g retailprintguard-config -m 0750 \
  /var/lib/retailprintguard/import/rch-memory/inbox
```

Trasferire prima in un percorso di staging con suffisso `.part`, verificare
l'hash calcolato sul sidecar e pubblicare nell'inbox con un rename atomico. Il
file finale deve essere regolare, non symlink, non scrivibile da gruppo/altri e
con estensione `.xml`:

```bash
sha256sum --check --strict export.xml.sha256
sudo install -o root -g retailprintguard-config -m 0640 -- \
  export.xml \
  /var/lib/retailprintguard/import/rch-memory/inbox/export.xml
```

Non usare cartelle condivise bidirezionali tra XTools e l'inbox definitiva. Non
modificare, rinominare secondo il contenuto o normalizzare l'XML originale.

## Validazione Linux: comportamento predefinito

Il wrapper `scripts/import_rch_memory.sh` è read-only per impostazione
predefinita. Controlla il percorso dell'inbox e chiama l'importatore in modalità
di validazione. Non legge `/etc/retailprintguard/database.env` e non scrive nel
database:

```bash
sudo /opt/retailprintguard/current/scripts/import_rch_memory.sh \
  --inbox /var/lib/retailprintguard/import/rch-memory/inbox \
  --device rch_1 \
  --output /var/log/retailprintguard/rch-memory-validate.json
```

La validazione verifica il formato supportato, i limiti strutturali, i campi
identificativi e l'hash dell'artefatto. Una validazione positiva non equivale a
un import e non dimostra ancora l'idempotenza sul database.

Il report non deve contenere l'intero XML, dati personali o hash completi se
viene copiato in un ticket non protetto.

## Import persistente esplicito

Prima dell'esecuzione persistente:

1. esaminare il report di validazione e risolvere ogni file in quarantena;
2. creare e verificare un backup RetailPrintGuard;
3. assicurarsi che `/etc/retailprintguard/database.env` sia root-owned e protetto;
4. registrare una motivazione operativa di almeno otto caratteri;
5. non arrestare proxy POS/RCH; l'import è control-plane e offline.

L'import persistente richiede esplicitamente `--execute`:

```bash
sudo /opt/retailprintguard/current/scripts/import_rch_memory.sh \
  --inbox /var/lib/retailprintguard/import/rch-memory/inbox \
  --device rch_1 \
  --config /etc/retailprintguard/config.yaml \
  --reason "Import DGFE pilota autorizzato" \
  --execute \
  --output /var/log/retailprintguard/rch-memory-import.json
```

Il wrapper non sposta né cancella gli originali dopo l'import. Una seconda
esecuzione sugli stessi file deve produrre zero duplicati logici e zero nuovi
documenti. La chiave logica fiscale è formata almeno da matricola RT, numero
azzeramento, numero documento e tipo; l'hash identifica invece lo specifico
artefatto ricevuto.

## Dati e limiti

Un export DGFE valido può contenere data/ora fiscale, matricola RT, numero
azzeramento e documento, righe, quantità, prezzi, sconti, IVA, totali, pagamenti
e riferimenti di annullo/reso. `DataOra` del documento non deve essere sostituita
con l'ora dell'export o dell'import.

Non sono garantiti nel DGFE:

- comande cucina/bar e preconti POS;
- documenti mai consolidati dalla RCH;
- fatture che il firmware non è tenuto a conservare nella memoria fiscale;
- il PDF/PaDES originario dello scontrino elettronico.

Le assenze restano `UNKNOWN`: non vanno ricostruite inventando dati dal riepilogo
giornaliero.

## Verifiche dopo l'import

1. Controllare il codice uscita e il report JSON.
2. Ripetere l'import e verificare l'idempotenza.
3. Confrontare un documento con la copia cartacea nota:
   - matricola RT;
   - progressivo `azzeramento-documento`;
   - data/ora fiscale;
   - totale, pagamenti, sconti e righe.
4. Verificare che l'evidenza DGFE sia aggiunta come fonte distinta e non sostituisca
   RAW richiesta/risposta già acquisiti.
5. Verificare che PID e listener proxy siano rimasti invariati usando le normali
   procedure di osservazione, senza riavvio.

In caso di XML sconosciuto, firma non valida, file che cambia durante la lettura,
collisione di chiave con contenuto divergente o schema non supportato, conservare
l'originale e fermare l'import. Non correggere il file a mano.
