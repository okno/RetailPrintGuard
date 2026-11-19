# Esportazioni PDF e ZIP

La pagina **Esportazioni**, riservata ad ADMIN e AUDITOR, crea una richiesta
persistente e mostra lo storico condiviso con stato, autore, periodo, numero di
documenti, dimensione, checksum e collegamento al download. Il servizio dedicato
`retailprintguard-export` elabora le richieste in sequenza, fuori dal data plane.

## Selezione e conservazione

Le date sono giorni di calendario inclusivi in `Europe/Rome`, anche durante i
cambi di ora legale. Si usa il timestamp documento della versione selezionata;
solo in sua assenza si usa il timestamp di acquisizione. Per versioni legacy
prive di semantica si mantiene il fallback documentato alla proiezione documento.
Il manifest indica per ogni documento quale timestamp è stato usato.

La selezione predefinita comprende i documenti e le comande leggibili che il
parser ha osservato completi. I documenti leggibili ma incompleti hanno una
selezione separata, **Parziali da verificare**. La completezza descrive la cattura
del documento, non certifica la sua emissione fiscale o la stampa fisica.

La policy versionata `rpg-document-pdf-policy-1.0.0` esclude dai PDF le risposte
del dispositivo, i comandi tecnici di annullo, i corpi vuoti o composti solo da
controlli, le sole code di riferimento, i template di report senza dati e il
contenuto non classificato come documento. L'intestazione configurata della
stampante non è sufficiente a rendere valido un corpo vuoto. Non sono invece
requisiti obbligatori un importo, un numero fiscale o una data documento: una
comanda autentica può esserne priva. Lo stato del job di acquisizione e quello
della revisione restano distinti da questa classificazione.

**Intero storico disponibile** non usa date fittizie: salva gli estremi richiesti
come null e mostra la copertura effettiva degli elementi selezionati. La scelta
**Intervallo di date** mantiene estremi inclusivi. La copertura può comprendere
date documento e date di acquisizione, con conteggi distinti e base esplicita
per ciascun elemento. La lista Documenti filtra invece per data di acquisizione,
come indicato nella pagina.

**Ultimi 10 documenti validi** seleziona fino a dieci documenti completi e
stampabili, ordinati per acquisizione decrescente e, a parità di istante, ID
documento crescente. Le evidenze tecniche e i parziali sono esclusi prima di
applicare il limite. Si può filtrare per dispositivo; non si impostano date.
Se sono disponibili meno di dieci documenti, lo storico riporta il numero
effettivo; se non ne esiste alcuno, la richiesta viene rifiutata con un messaggio
esplicito. Il manifest conserva ordine, limite richiesto, intervallo effettivo
di acquisizione e numero di record più vecchi non esaminati dopo il decimo.
La copertura e le date dei PDF continuano a indicare la data documento, con
fallback all'acquisizione: sono distinte dal criterio di ordinamento.

Ogni documento selezionato produce un PDF. Versioni storiche, RAW, documenti e
revisioni rimangono nel database: non vengono cancellati o aggiornati
dall'esportazione. Le evidenze escluse restano consultabili nella vista dedicata
e scaricabili nei formati RAW, TXT e JSON; il download PDF è negato.
Ogni richiesta fissa la versione scelta, i dati della vista, le attribuzioni e
l'appartenenza dei documenti in una transazione. Le modifiche successive non
cambiano gli archivi già richiesti.

Il PDF è una **vista derivata**, non una copia fiscale certificata. Il RAW
immutabile resta l'evidenza autoritativa. I dati opachi/UNKNOWN rimangono
disponibili come evidenze, senza inventare una semantica fiscale o generare PDF
di scontrini.

Dal renderer `rpg-receipt-pdf-2.0.0` il PDF contiene esattamente la proiezione
testuale della scheda web **Scontrino**, inclusa l'eventuale intestazione già
mostrata nella scheda. Non aggiunge titoli, tabelle economiche, identificativi
tecnici, checksum, piè di pagina o numeri di pagina. Il formato è largo 80 mm,
con font monospaziato Unicode incorporato: conserva spazi, colonne e righe vuote.
I documenti lunghi continuano su più pagine, senza troncare il contenuto.
Il download singolo e le nuove esportazioni usano lo stesso renderer; gli
archivi precedentemente completati restano invariati.

Gli archivi creati prima della policy sono indicati nello storico come
**selezione non filtrata** e possono contenere risposte tecniche. I loro ZIP,
snapshot, manifest e checksum non vengono riscritti. Eventuali estremi storici
molto ampi descrivono il filtro originario, non la data dei documenti. Il nuovo
nome di download identifica questi archivi come precedenti.

## Consultazione prima del download

**Apri archivio** mostra un elenco paginato dei PDF e permette di consultarli
prima di scaricare lo ZIP. La presentazione carica un solo PDF alla volta;
passando al successivo o chiudendo la vista, il browser annulla il caricamento
precedente e libera il relativo URL temporaneo. Lo ZIP non viene scaricato nel
browser per produrre le anteprime.

L'anteprima restituisce esattamente i byte del PDF già conservato nello ZIP,
senza rigenerarlo con il renderer corrente. Il server verifica SHA-256 e
dimensione dell'archivio, manifest e, per il PDF richiesto, hash e dimensione.
Non estrae file sul filesystem. La stessa consultazione è disponibile per gli
archivi precedenti con manifest compatibile, indicati come selezione legacy;
anche eventuali evidenze tecniche presenti in quei vecchi ZIP restano immutate.
Elenco e anteprime richiedono ADMIN/AUDITOR e sono registrati nell'audit.

## Integrità e limiti

Lo ZIP contiene `documents/*.pdf`, `manifest.json` e `manifest.sha256`. Il manifest
registra ID documento e versione, timestamp, hash sorgente, hash dello snapshot,
hash e dimensione di ogni PDF, versione del renderer e avvisi. Le nuove richieste
conservano anche la versione della policy, l'ambito completo/parziale, la
copertura effettiva, i conteggi per categoria e motivo e le esclusioni con
riferimento alla versione esaminata. Lo storico conserva
anche lo SHA-256 dell'intero ZIP, verificato prima di ogni download. I nomi dei
file contengono la data del documento, il tipo, il numero osservato e il
dispositivo, con componenti normalizzati e limitati per impedire percorsi o
intestazioni non validi. Il progressivo persistente dell'esportazione precede
ogni nome nello ZIP e distingue anche documenti con lo stesso numero.
Quando manca la data documento il nome indica esplicitamente `acquisizione-`;
quando manca il numero fiscale usa il numero ordine, il solo suffisso osservato
oppure `senza-numero-fiscale`. Nessuna numerazione fiscale viene inventata.
I dati tecnici di verifica rimangono nel manifest, fuori dal contenuto dei PDF.
Nei nuovi ZIP anche la data della voce PDF deriva dal timestamp congelato del
documento o dell'acquisizione, in `Europe/Rome`. Il formato ZIP ha una precisione
di due secondi e rappresenta gli anni 1980–2107: gli eventuali adattamenti a
questi limiti sono dichiarati nel manifest, che conserva il timestamp originale.
Il timestamp delle voci manifest è quello della richiesta. Gli archivi legacy
mantengono i metadati originali per consentirne la ricostruzione identica.

Limiti espliciti: 100.000 record esaminati, 10.000 documenti selezionati,
256 MiB di snapshot, 4 MiB per singolo snapshot,
16 MiB per PDF, 1 GiB per ZIP e almeno 256 MiB di spazio libero. Ogni utente può
avere fino a tre richieste in attesa/elaborazione. Per richieste più grandi,
suddividere il periodo o selezionare un dispositivo. I risultati **non vengono
troncati**: un errore di selezione annulla l'intera richiesta; un errore di
rendering marca la richiesta come fallita e non pubblica archivi parziali.
I conteggi di esclusione sono completi; il dettaglio degli identificativi esclusi
è limitato a 10.000 riferimenti e dichiara quanti ulteriori riferimenti non sono
elencati. Questo limite non tronca i PDF selezionati.
Per gli ultimi dieci, i conteggi di esclusione riguardano i record effettivamente
esaminati; quelli più vecchi non esaminati hanno un contatore distinto.
La consultazione limita inoltre il manifest a 64 MiB e l'indice ZIP a 16 MiB,
prima di decomprimere i contenuti. Ogni apertura verifica nuovamente lo ZIP:
il lavoro server è proporzionale alla sua dimensione, mentre il browser riceve
solo i metadati richiesti o il singolo PDF.

Gli archivi e lo storico non hanno una cancellazione automatica. Il worker
riprende le richieste interrotte usando gli snapshot già salvati e ricostruisce
lo ZIP in modo deterministico. La pubblicazione non sostituisce file esistenti;
un file diverso richiede verifica manuale. Il lock locale ammette un solo worker
sullo stesso storage; l'installazione supportata è un singolo server.

## API e distribuzione

- `POST /api/v1/exports`: `{scope: "all_history", document_scope: "complete" | "partial", device_ids?: string[]}`; HTTP 202.
- Per un periodo: `{scope: "date_range", date_from, date_to, document_scope: "complete" | "partial", device_ids?: string[]}`. I client precedenti che inviano solo le date usano `date_range` e la nuova selezione `complete`.
- Per i più recenti: `{scope: "latest_10", document_scope: "complete", device_ids?: string[]}`; nessuna data e nessuna selezione parziale.
- `GET /api/v1/exports?limit=25&offset=0`: storico paginato (massimo 100 righe).
- `GET /api/v1/exports/{id}`: dettaglio e avanzamento.
- `GET /api/v1/exports/{id}/download`: ZIP completato; HTTP 409 se non disponibile.
- `GET /api/v1/exports/{id}/documents?limit=25&offset=0`: metadati paginati dei PDF conservati, massimo 100 righe, sequenza e URL di anteprima; include indicazione legacy e renderer originario.
- `GET /api/v1/exports/{id}/documents/{sequence}/pdf`: PDF originario inline, con checksum e `Cache-Control: no-store`; HTTP 409 per archivio non disponibile o integrità non verificata, 404 per richiesta/sequenza inesistente.

La creazione fissa lo snapshot con query in batch, mentre rendering, compressione
e checksum del download avvengono senza caricare l'intero archivio in memoria.
La creazione e il download sono registrati nell'audit applicativo.

La migrazione additiva `12c4f6a8b0d2` crea solo `export_requests` e `export_items`.
Il downgrade è rifiutato se esiste uno storico, per evitarne la perdita.
La successiva `3e7c9a21d4f6` aggiunge soltanto `selection_metadata` e rende nullable
le due date di richiesta. Non modifica le richieste precedenti. Anche il suo
downgrade è rifiutato se eliminerebbe lo storico della selezione.

Creare `/var/lib/retailprintguard/exports` con proprietario/gruppo `retailprintguard-api`,
modo 0750; il percorso `ReadWritePaths` nella unità systemd deve coincidere.
Il servizio API legge gli archivi, il worker li crea con modo finale 0440.
Entrambi ricevono `RPG_EXPORT_ROOT=/var/lib/retailprintguard/exports`; senza override
si usa `${archive_root}/exports`. Non occorre concedere al processo API accesso al
gruppo spool. Entrambi usano la configurazione e la connessione DB esistenti. Includere questa
directory insieme al dump DB nei backup; conservare snapshot e ZIP coordinati.
