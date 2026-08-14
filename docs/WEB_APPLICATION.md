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
commerciali, alert aperti/critici, differenza economica, dispositivi,
spool/errori e grafici di trend/concentrazione quando forniti dall'API.

### Transazioni

Elenco paginato con tavolo, ordine, operatore, preconto, fiscale, differenza,
stato, alert e confidenza. I filtri sono conservati nella query string. Il
dettaglio mostra timeline, confronto economico e diff strutturato delle righe.

### Documenti

Elenco filtrabile per tipo, dispositivo e ordine. Il dettaglio offre:

- scontrino ricostruito da `normalized_text`;
- righe strutturate con rimosso/annullato;
- vista esadecimale tecnica limitata a 64 KiB;
- provenienza, hash, parser, confidenza e warning.

La richiesta RAW completa usa l'endpoint auditato. La schermata visualizza solo
il prefisso per evitare blocchi del browser; il byte array restituito dall'API
non viene interpretato come HTML.

### Alert

Workbench con filtri per severità, stato, regola e dispositivo, paginazione,
evidenze JSON, presa in carico, nota e giustificazione. L'export CSV è riservato
ad auditor/admin. Gli stati sono:

`OPEN`, `UNDER_REVIEW`, `CONFIRMED`, `FALSE_POSITIVE`, `JUSTIFIED`, `CLOSED`.

### Regole

Mostra codice, nome, severità, peso, soglia e versione. Solo `ADMIN` può usare
lo switch di attivazione.

### Ricerca globale

Invia query di almeno due caratteri e mostra risultati document/transazione.
La semantica effettiva — codice, tavolo, articolo, importo, hash o testo — è
implementata dal repository API e va testata su MariaDB.

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

## Limiti UI attuali

- non è presente una pagina dedicata a sessioni/job nonostante gli endpoint;
- il diff è reso come JSON leggibile, non come griglia semantica affiancata;
- il documento singolo dispone di RAW/TXT/JSON/PDF; il fascicolo ZIP
  multi-documento non è ancora disponibile;
- filtri per periodo e operatore alert non sono tutti esposti dalla pagina;
- non sono implementati aggiornamenti realtime SSE/WebSocket;
- ordinamento server-side generico e persistenza avanzata dei filtri non sono
  ancora contrattualizzati dall'API.

Questi limiti non devono essere descritti come funzionalità completate durante
un collaudo di accettazione.
