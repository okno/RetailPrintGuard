# API REST

## Convenzioni

- base path: `/api/v1`;
- OpenAPI: `/api/openapi.json`;
- Swagger UI: `/api/docs`;
- autenticazione: `Authorization: Bearer <JWT>`;
- correlation ID: header `X-Correlation-ID`, accettato solo se contiene 8–128
  caratteri sicuri; altrimenti viene generato un UUID;
- timestamp: ISO 8601 UTC nell'API, conversione locale nella UI;
- intervalli temporali: `from` incluso e `to` escluso; entrambi devono includere
  un offset/fuso e vengono normalizzati in UTC;
- paginazione: oggetto `{items, total, limit, offset}`;
- importi: stringhe/numero JSON derivati da `Decimal`, mai calcoli binari nel
  database.

La risposta include sempre `X-Correlation-ID`. Gli errori del repository
diventano `503 CONTROL_PLANE_UNAVAILABLE`; gli errori imprevisti diventano 500
senza stack trace al client. Le eccezioni HTTP validate da FastAPI usano il
campo standard `detail`.

## Autenticazione e ruoli

`POST /auth/login` riceve:

```json
{"username":"auditor","password":"valore-fornito-fuori-repository"}
```

Il primo account non si crea via API: usare una sola volta la CLI locale
interattiva `retailprintguard-admin` descritta in
[Installazione Debian](INSTALLAZIONE_DEBIAN.md). Questo evita un endpoint di
bootstrap remoto; le credenziali non devono essere inserite nella command line.

Il token è JWT HS256 con issuer/audience `retailprintguard`, scadenza
configurabile e ruoli incorporati. Le password sono verificate con Argon2. Il
throttle login è locale al processo e chiave per IP/username; serve comunque un
rate limit condiviso sul reverse proxy.

| Capacità | READ_ONLY | OPERATOR | AUDITOR | ADMIN |
|---|:---:|:---:|:---:|:---:|
| leggere dashboard, device, documenti, transazioni, alert, regole | sì | sì | sì | sì |
| vedere batch di import | no | sì | sì | sì |
| aggiornare/prendere in carico alert | no | sì | sì | sì |
| scaricare RAW | no | no | sì | sì |
| esportare alert CSV | no | no | sì | sì |
| verificare/escludere un job incompleto | no | no | no | sì |
| attivare/disattivare regole | no | no | no | sì |

Download RAW, export, login, modifica alert e toggle regole producono audit
tramite il repository.

## Endpoint

| Metodo e path | Filtri/input principali | Risposta |
|---|---|---|
| `POST /auth/login` | username/password | token e utente |
| `GET /auth/me` | — | principal corrente |
| `GET /dashboard` | `from`, `to` | contatori operativi, episodi, differenze, trend, stato |
| `GET /devices` | — | device, ping ICMP fisico ogni 10 secondi e attività/spool |
| `GET /sessions` | `limit`, `offset`, `device_id` | sessioni TCP |
| `GET /jobs` | `limit`, `offset`, `device_id`, `status`, `incomplete`, `review_state`, `from`, `to` | job cattura/import/revisione |
| `POST /jobs/{id}/review` | azione, motivazione, password di conferma | proiezione job aggiornata e auditata |
| `GET /documents` | `limit`, `offset`, `type`, `device_id`, `order_code`, progressivo/suffisso/riferimento, `from`, `to` | documenti |
| `GET /documents/{id}` | UUID | dettaglio e righe |
| `GET /documents/{id}/raw` | UUID | `application/octet-stream` auditato |
| `GET /documents/{id}/txt` | UUID | testo normalizzato derivato |
| `GET /documents/{id}/json` | UUID | documento normalizzato derivato |
| `GET /documents/{id}/pdf` | UUID | PDF receipt-style derivato e versionato |
| `GET /orders` | `limit`, `offset`, `table_code`, `order_code` | ordini |
| `GET /transactions` | paginazione, tavolo/ordine/operatore, `minimum_difference`, `reduction_only`, `operational_economic_only`, `from`, `to` | transazioni e drill-down economico |
| `GET /transactions/{id}` | UUID | timeline e diff |
| `GET /alerts` | severità, regola, stato, device, operatore, `view`, `from`, `to` | alert paginati |
| `GET /alerts/{id}` | UUID | evidenze e storia |
| `PATCH /alerts/{id}` | stato, assegnazione, nota, motivazione | alert aggiornato |
| `GET /alerts/export.csv` | filtri alert, `view`, `from`, `to` | CSV UTF-8 BOM auditato |
| `GET /rules` | — | regole/versioni |
| `PATCH /rules/{code}` | query `enabled=true|false` | regola aggiornata |
| `GET /search` | `q` 2–200 caratteri, paginazione, `from`, `to` | risultati globali |
| `GET /imports` | paginazione | batch import |
| `GET /system/health` | pubblico | stato API/database/spool |

I massimi server-side sono 500 elementi per sessioni/job/alert e 200 per
documenti/ordini/transazioni/ricerca/import. L'export è limitato internamente a
10.000 alert.

## Periodi e viste alert

Gli endpoint temporali accettano intervalli half-open `[from, to)`. Per esempio,
una giornata locale va inviata con l'offset locale nei due estremi; l'API la
normalizza in UTC. Un timestamp privo di fuso, oppure `to <= from`, produce 422.
Omettere un estremo crea un intervallo aperto da quel lato.

`view=operational` limita gli alert canonici agli stati `OPEN`, `UNDER_REVIEW` e
`CONFIRMED`. `view=archive` permette la consultazione degli stati conclusi e
delle classificazioni storiche senza reinserirli nelle metriche operative. La
webapp aggiunge `view=operational` per default; archivio e vista completa sono
richieste esplicite. I client API devono inviare `view` quando vogliono un
perimetro non ambiguo.

La dashboard web applica per default gli ultimi sette giorni di calendario e
invia sempre i due estremi all'API. L'endpoint non inventa un periodo quando
viene chiamato direttamente senza filtri. Il drill-down economico usa
`operational_economic_only=true`, `reduction_only=true` e una differenza minima
positiva: include soltanto episodi chiusi con un alert economico operativo,
preservando `from` e `to` della card selezionata.

I documenti distinguono `external_document_code` (progressivo proprio completo
osservato), `external_document_code_suffix` (solo suffisso RCH),
`commercial_reference_code` (riferimento a un Documento Commerciale) e
`progressive_observation_status`. Quando una correlazione automatica forte
risolve in modo univoco il codice commerciale completo, l'API espone
`resolved_external_document_code` e la relativa provenienza senza modificare
il documento o la versione parser.

I tipi `SHIFT_END_REPORT` e `INVOICE` distinguono rispettivamente il report di
fine turno e la fattura dai generici documenti gestionali. Il dettaglio espone
`application_timestamp`, `rch_footer_timestamp`, `rch_serial_number`, le
relative evidenze/precisioni e `rch_clock_offset_seconds`; valori non osservati
restano null. `receipt_header` espone la versione schema, i campi anagrafici
disponibili e una provenienza obbligatoria: `RCH_PRINTED_HEADER` significa che
il blocco è stato osservato nel payload, mentre `DEVICE_METADATA_CONFIGURED`
indica un fallback amministrativo non presente nel flusso. Le liste operative
nascondono per default soltanto i frammenti
incompleti privi di contenuto commerciale, conservandoli con
`include_technical=true`.

## Revisione dei job incompleti

`POST /jobs/{id}/review` è riservato ad `ADMIN` e accetta:

```json
{
  "action": "VERIFY_USABLE",
  "reason": "Motivazione tecnica verificabile di almeno dieci caratteri",
  "confirmation_password": "inserita-interattivamente"
}
```

Le azioni ammesse sono `VERIFY_USABLE`, `EXCLUDE_FROM_ANALYSIS` e
`REOPEN_REVIEW`. La password dedicata viene nuovamente verificata con Argon2id;
tentativi errati sono soggetti a throttle. Se il segreto non è configurato
l'endpoint fallisce chiuso con 503. Una password non valida produce 403, un job
non incompleto 409 e un ID inesistente 404.

L'hash viene caricato dal solo processo API tramite il nome ambiente fisso nel
file protetto `/etc/retailprintguard/review.env`. Non esiste un override YAML;
proxy e worker non ricevono quel file.

L'azione aggiorna soltanto la proiezione di revisione, invalida il watermark di
correlazione quando necessario e registra actor, motivazione, prima/dopo e
correlation ID nell'audit hash-chained. `EXCLUDE_FROM_ANALYSIS` giustifica gli
alert derivati collegati e impedisce il riuso del job nei successivi calcoli,
ma non elimina RAW, manifest, documenti o righe. `REOPEN_REVIEW` porta il job a
`PENDING` ma, per sicurezza, mantiene `analysis_excluded=true`: il job rientra
nell'analisi soltanto dopo una successiva decisione esplicita
`VERIFY_USABLE`.

## Esempi

```bash
curl --fail --silent \
  -H 'Content-Type: application/json' \
  -d '{"username":"auditor","password":"<inserire-interattivamente>"}' \
  https://guard.example.invalid/api/v1/auth/login
```

Evitare password nella history della shell: l'esempio mostra soltanto la forma.
In produzione usare un client che legga il segreto da prompt protetto.

```bash
curl --fail --silent \
  -H "Authorization: Bearer ${RPG_ACCESS_TOKEN}" \
  -H 'X-Correlation-ID: audit-session-0001' \
  'https://guard.example.invalid/api/v1/transactions?minimum_difference=0.01&limit=50'
```

Il dominio `.invalid` è deliberatamente non operativo.

## Health e readiness

`GET /system/health` è pubblico per consentire probe locali. Restituisce
`status=ok` solo quando `database_health()` è `ok`; altrimenti `degraded`.
Il campo spool deriva dal contesto API e deve essere alimentato dall'adapter di
produzione.

Un health 200 con `degraded` non significa readiness applicativa. Reverse proxy
e orchestrazione devono leggere il body, non solo il codice HTTP.

## Header di sicurezza

Il middleware imposta:

- `Content-Security-Policy` restrittiva;
- `X-Content-Type-Options: nosniff`;
- `X-Frame-Options: DENY`;
- `Referrer-Policy: no-referrer`;
- `Permissions-Policy` senza camera/microfono/geolocalizzazione;
- `Cache-Control: no-store`.

HSTS e TLS sono responsabilità del reverse proxy, che deve rimuovere header
client non fidati e impostare limiti di richiesta.

## Adapter di produzione

L'app factory è testabile tramite il protocollo `ApiRepository`. L'entry point
di produzione costruisce `SqlAlchemyApiRepository` dalla URL DB validata; ogni
metodo apre e chiude la propria sessione e restituisce DTO, non ORM live.
`EmptyRepository` resta un fallback sicuro per factory/test e non viene usato
dalla CLI di produzione.

L'adapter SQLAlchemy copre autenticazione/blocco, viste paginated, raw,
transazioni, alert, regole, ricerca, audit hash-chained e health. I test correnti
usano SQLite; il collaudo MariaDB reale resta obbligatorio.
