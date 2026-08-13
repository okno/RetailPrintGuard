# API REST

## Convenzioni

- base path: `/api/v1`;
- OpenAPI: `/api/openapi.json`;
- Swagger UI: `/api/docs`;
- autenticazione: `Authorization: Bearer <JWT>`;
- correlation ID: header `X-Correlation-ID`, accettato solo se contiene 8–128
  caratteri sicuri; altrimenti viene generato un UUID;
- timestamp: ISO 8601 UTC nell'API, conversione locale nella UI;
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
| attivare/disattivare regole | no | no | no | sì |

Download RAW, export, login, modifica alert e toggle regole producono audit
tramite il repository.

## Endpoint

| Metodo e path | Filtri/input principali | Risposta |
|---|---|---|
| `POST /auth/login` | username/password | token e utente |
| `GET /auth/me` | — | principal corrente |
| `GET /dashboard` | — | contatori, differenze, trend, stato |
| `GET /devices` | — | device e ultima attività/spool |
| `GET /sessions` | `limit`, `offset`, `device_id` | sessioni TCP |
| `GET /jobs` | `limit`, `offset`, `device_id`, `status` | job cattura/import |
| `GET /documents` | `limit`, `offset`, `type`, `device_id`, `order_code` | documenti |
| `GET /documents/{id}` | UUID | dettaglio e righe |
| `GET /documents/{id}/raw` | UUID | `application/octet-stream` auditato |
| `GET /orders` | `limit`, `offset`, `table_code`, `order_code` | ordini |
| `GET /transactions` | `limit`, `offset`, `table_code`, `operator_code`, `minimum_difference` | transazioni |
| `GET /transactions/{id}` | UUID | timeline e diff |
| `GET /alerts` | severità, regola, stato, device, operatore | alert paginati |
| `GET /alerts/{id}` | UUID | evidenze e storia |
| `PATCH /alerts/{id}` | stato, assegnazione, nota, motivazione | alert aggiornato |
| `GET /alerts/export.csv` | `severity`, `status` | CSV UTF-8 BOM auditato |
| `GET /rules` | — | regole/versioni |
| `PATCH /rules/{code}` | query `enabled=true|false` | regola aggiornata |
| `GET /search` | `q` 2–200 caratteri, paginazione | risultati globali |
| `GET /imports` | paginazione | batch import |
| `GET /system/health` | pubblico | stato API/database/spool |

I massimi server-side sono 500 elementi per sessioni/job/alert e 200 per
documenti/ordini/transazioni/ricerca/import. L'export è limitato internamente a
10.000 alert.

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
