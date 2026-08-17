# Web application

## Stack e avvio

Il frontend usa React 19, TypeScript, Vite, Material UI e TanStack Query. La
lingua è italiana e il layout è responsive. Durante lo sviluppo:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm dev
```

Vite ascolta su loopback e inoltra `/api` a `127.0.0.1:8080`. La build:

```bash
pnpm build
```

produce `frontend/dist` senza sourcemap. Il server Vite non è un server di
produzione.

## Sessione

Il token è mantenuto solo in memoria JavaScript, non in `localStorage` o cookie.
Un refresh richiede nuovo login; questo riduce la persistenza del bearer token
ma non elimina il rischio XSS. Al primo 401 la sessione viene cancellata.

## Sezioni implementate

### Dashboard

Visualizza conteggi di documenti, ordini/preconti, documenti gestionali e
commerciali, alert operativi/critici, episodi con riduzione, differenza
economica non duplicata, job incompleti da verificare, dispositivi,
spool/errori e grafici di trend/concentrazione quando forniti dall'API. Il
selettore periodo aggiorna tutti i contatori documentali e antifrode della
dashboard usando lo stesso intervallo.
All'apertura, la webapp seleziona gli ultimi sette giorni di calendario nel fuso
di visualizzazione, così una vendita conclusa prima di mezzanotte resta
visibile. L'operatore può scegliere oggi, ieri, tutto o un intervallo
personalizzato.
Gli episodi economici del periodo sono elencati in rosso con baseline, totale
finale, ammanco potenziale, tavolo, confidenza e collegamento alla transazione.

### Transazioni

Elenco paginato con tavolo, ordine, operatore, preconto, fiscale, differenza,
stato, alert e confidenza. I filtri sono conservati nella query string. Il
dettaglio mostra timeline, confronto economico e diff strutturato delle righe.
Il filtro “Riduzioni antifrode operative” richiede insieme un alert economico
canonico ancora operativo, una chiusura economica osservata e una differenza
positiva. I collegamenti dalle due card economiche della dashboard conservano
il periodo e impostano tale filtro, evitando transazioni prive di chiusura o
alert tecnici non economici.

### Documenti

Elenco filtrabile per tipo, dispositivo, ordine, progressivo proprio, suffisso
RCH, riferimento commerciale e periodo. Il dettaglio offre:

- scontrino ricostruito da `normalized_text`;
- righe strutturate con rimosso/annullato;
- prezzi osservati oppure prezzi derivati con fonte e confidenza, senza
  confonderli con il valore originale della comanda;
- vista esadecimale tecnica limitata a 64 KiB;
- provenienza, hash, parser, confidenza e warning.

Progressivo proprio, suffisso e riferimento commerciale sono campi distinti.
Un codice completo risolto dalla copia gestionale correlata è marcato come
derivato con la sua provenienza; un progressivo generato internamente dalla RCH
ma non transitato nel flusso viene mostrato come “non osservato”, mai ricostruito
da cifre di stato non probanti.

Un documento monetario incompleto non può fornire un prezzo derivato. Quando
fonti complete correlate riportano prezzi incompatibili, la colonna mostra
“Prezzi in conflitto” e rende consultabili i candidati; non espone un valore
derivato scelto arbitrariamente.

La richiesta RAW completa usa l'endpoint auditato. La schermata visualizza solo
il prefisso per evitare blocchi del browser; il byte array restituito dall'API
non viene interpretato come HTML.

Il PDF delle comande usa una proiezione termica compatta: tavolo, coperti,
ordine, portata, quantità e articoli strutturati compaiono una sola volta. I
prezzi derivati sono marcati come tali con provenienza/confidenza. Identificativo
e hash sono abbreviati nel footer; il RAW resta l'evidenza autoritativa. Il PDF
è quindi una ricostruzione leggibile, non la fotografia né il flusso originale.

### Alert

Workbench con filtri per severità, stato, regola e dispositivo, paginazione,
evidenze JSON, presa in carico, nota e giustificazione. L'export CSV è riservato
ad auditor/admin. La vista iniziale è sempre “Operativi”; “Archivio” e “Tutti”
richiedono una scelta esplicita e restano nella query string. Gli stati sono:

`OPEN`, `UNDER_REVIEW`, `CONFIRMED`, `FALSE_POSITIVE`, `JUSTIFIED`, `CLOSED`.

### Regole

Mostra codice, nome, severità, peso, soglia e versione. Solo `ADMIN` può usare
lo switch di attivazione.

### Ricerca globale

Invia query di almeno due caratteri e mostra risultati document/transazione.
La semantica effettiva — codice, tavolo, articolo, importo, hash o testo — è
implementata dal repository API e va testata su MariaDB. Il periodo è
conservato nella query string e applicato anche ai risultati documentali e alle
transazioni/ordini rappresentati.

### Job incompleti

La pagina dedicata elenca le acquisizioni tecnicamente incomplete e distingue
`PENDING`, `VERIFIED_USABLE` ed `EXCLUDED`. Un `ADMIN`, dopo aver letto warning,
dimensioni e RAW autorizzato, può:

- verificare il job come utilizzabile;
- escluderlo dalle sole elaborazioni derivate;
- riaprire una revisione precedente.

Ogni azione richiede motivazione e password di conferma dedicata. Non elimina
RAW, manifest, documenti o audit. “Riapri revisione” imposta `PENDING` ma lascia
il job fuori dall'analisi: soltanto “Verifica e usa” lo reinserisce. Ogni cambio
invalida il watermark del control plane perché la correlazione venga
ricalcolata.

### Dispositivi e importazioni

La pagina device aggiorna ogni 30 secondi stato, endpoint, ultime attività,
versione, errore e spool. La pagina import, riservata ai reviewer, mostra batch,
duplicati ed errori.

## Accessibilità e sicurezza

Material UI fornisce componenti accessibili; label, tabelle e controlli hanno
testo visibile. Prima del rilascio eseguire comunque audit tastiera, contrasto e
screen reader su browser supportati.

Il frontend:

- non usa `dangerouslySetInnerHTML` per payload;
- non esegue comandi ESC/POS/RCH;
- tratta testo e JSON come dati;
- delega autorizzazione all'API: nascondere un controllo non è una misura RBAC;
- non deve essere pubblicato con directory listing o sourcemap di produzione.

## Filtri temporali

I selettori data/ora inviano timestamp ISO 8601 con fuso orario. Il contratto è
half-open: `from` è incluso, `to` è escluso. Sono esposti su dashboard,
transazioni, documenti, ricerca e revisione incompleti; l'API applica lo stesso
contratto anche ad alert ed export. Intervalli senza fuso o con `to <= from`
sono rifiutati.

## Limiti UI attuali

- il diff è reso come JSON leggibile, non come griglia semantica affiancata;
- il documento singolo dispone di RAW/TXT/JSON/PDF; il fascicolo ZIP
  multi-documento non è ancora disponibile;
- non tutte le liste espongono ancora gli stessi filtri temporali e la
  persistenza avanzata dei filtri non è disponibile;
- non sono implementati aggiornamenti realtime SSE/WebSocket;
- ordinamento server-side generico e persistenza avanzata dei filtri non sono
  ancora contrattualizzati dall'API.

Questi limiti non devono essere descritti come funzionalità completate durante
un collaudo di accettazione.
