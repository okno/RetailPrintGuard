# Security review

Data della review: worktree correttivo post-incidente. La configurazione di sito
e i segreti non sono stati inclusi.

## Modello di minaccia

| Asset | Minacce principali | Controlli |
|---|---|---|
| continuità stampa | blocco DB/parser, backpressure illimitata, deadlock | data plane isolato, queue/timeout bounded |
| byte inoltrati | mutazione, reorder, replay | relay protocol-neutral, test byte-exact, no retry automatico |
| evidenza RAW | cancellazione, sostituzione, path traversal | spool append-only, path validation, SHA/catene, permessi |
| account web | brute force, token theft, escalation | Argon2id, delay/limit, RBAC, token breve |
| API/browser | XSS, injection, CSV formula, download arbitrario | React escaping, query parametrizzate, sanitizzazione/export e allowlist path |
| host | compromissione servizio, secret disclosure | utenti separati, systemd hardening, file group protetti |
| supply chain | package alterato | lock con hash, release content-addressed, CI/test |

## Risultati principali

### Alta priorità

1. Credenziali operative comparse in un canale di assistenza devono essere
   considerate esposte e ruotate. Il repository non ne riporta i valori.
2. L'interfaccia HTTP in LAN non sostituisce HTTPS: nginx deve terminare TLS o
   essere raggiungibile soltanto tramite rete amministrativa protetta. Il bearer
   token non va trasmesso su una rete non fidata.
3. Il worker antifrode pre-correzione poteva amplificare dati senza limite. Il
   contenimento ha fermato solo il worker; serve gate di idempotenza prima del
   riavvio.

### Media priorità

- il bearer token frontend è in memoria e si perde al refresh; riduce la
  persistenza ma non protegge da XSS nella pagina corrente;
- account DB per API e worker condiviso: separarli ulteriormente ridurrebbe i
  privilegi;
- il backup contiene segreti ed evidenze: è `0600` ma va cifrato e copiato su
  un repository separato;
- manca un anchor esterno periodico delle catene hash;
- retention/anonymization è configurabile ma non ancora eseguita da un worker
  dedicato;
- l'UI deve invalidare cache e token al logout/401 e non mostrare dati della
  sessione precedente.

## Controlli implementati

- nessun `eval`, shell interpolation di payload o deserializzazione arbitraria;
- Pydantic strict e whitelist dei valori configurabili;
- MariaDB loopback-only e migrazioni con account effimero;
- account Linux distinti per POS, RCH, worker e API;
- spool accessibile ai proxy/worker tramite gruppi minimi;
- security headers, CORS configurabile e RBAC `ADMIN`, `AUDITOR`, `OPERATOR`,
  `READ_ONLY`;
- Argon2id e bootstrap admin interattivo senza password in argv;
- download RAW/TXT/JSON/PDF autorizzati, checksum-verificati e auditati;
- renderer PDF deterministico, versionato e soggetto a limiti di input/output;
- nomi download sanitizzati e contenuti serviti come attachment;
- logging strutturato con correlation ID e senza segreti intenzionali.

## Catena di custodia

1. copiare gli originali in deposito root-only/read-only;
2. calcolare l'hash senza rinominare o modificare il file;
3. validare archivio, indice e manifest prima dell'estrazione;
4. lavorare su una copia separata;
5. registrare strumento/versione, orario UTC e operatore;
6. non pubblicare hash completi, UUID, endpoint, MAC o PII;
7. conservare il report come interpretazione, non come sostituto dei RAW;
8. ancorare manifest/head su supporto indipendente.

## Comandi privilegiati dell'analisi

Sono documentati in forma redatta in
[SYSTEM_INVENTORY.md](SYSTEM_INVENTORY.md#azioni-root-registrate-in-forma-redatta).
Non sono stati eseguiti probe, replay, test hardware, modifiche rete o restart
dei proxy.

## Gate sicurezza prima del merge

```bash
./scripts/check_secrets.sh
./scripts/run_tests.sh
git diff --check
```

Prima del deployment: secret rotation, backup verificato, migrazione su copia,
TLS/firewall, test idempotenza, review delle unità systemd e autorizzazione della
finestra. I dettagli sono in [DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md).
