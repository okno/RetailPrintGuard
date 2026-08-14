# Piano di test

## Regole del gate

- Tutti i test di questo documento, salvo il collaudo hardware esplicitamente
  autorizzato, sono offline e non aprono connessioni verso apparati reali.
- Fixture e snapshot devono essere sintetici o copie private redatte.
- Un fallimento del control plane non deve essere aggirato disabilitando test
  del data plane.
- Il risultato finale viene registrato in [TEST_REPORT.md](TEST_REPORT.md) e
  [FINAL_VALIDATION_REPORT.md](FINAL_VALIDATION_REPORT.md).

## Gate automatici

```bash
./scripts/check_secrets.sh
./scripts/run_tests.sh
./scripts/run_offline_replay.sh \
  --config config/retailprintguard.example.yaml \
  --canonical-root /percorso/copia-spool
git diff --check
```

Su Debian, validare inoltre gli script con `bash -n scripts/*.sh`, le unità con
`systemd-analyze verify` e nginx con `nginx -t` su staging.

## Scenari di accettazione

### A — Riduzione sospetta

Input sintetico: comanda 100,00 €, preconto 100,00 €, rimozione riga, riduzione
prezzo e chiusura 50,00 € con riferimento correlabile.

Atteso: una transazione, timeline completa, diff, differenza 50,00 €, alert HIGH
con RAW e criteri di correlazione.

### B — Conto separato legittimo

Input: preconto 100,00 € e due Documenti Commerciali completi da 50,00 €.

Atteso: fiscale aggregato 100,00 €, `split_payment=true`, nessun alert di
riduzione.

### C — Database offline

Con fake target locale e DB indisponibile: inviare byte sintetici al relay.

Atteso: target fake riceve gli stessi byte, spool pubblicato, successivo import
una sola volta. Il test non usa stampanti reali.

### D — Segmentazione e aggregazione

Input: un documento diviso su più write e più documenti in una write.

Atteso: byte identici, framing applicativo corretto, nessuna fusione/perdita,
offset monotoni per direzione.

### E — Payload malformato

Atteso: forwarding invariato, RAW conservato, warning/parser error visibile e
nessun crash del proxy.

### F — Quattro dispositivi concorrenti

Tre fake POS e una fake RCH, tutti loopback e porte effimere.

Atteso: nessuna contaminazione, sessioni/job attribuiti correttamente e reverse
RCH inoltrato integralmente.

### G — Riavvio improvviso

Simulare job `.partial` e recovery offline.

Atteso: job coerente pubblicato come completo o `PARTIAL` secondo copertura,
nessuna duplicazione, hash chain verificabile.

## Regressioni incidente

| ID | Test | Atteso |
|---|---|---|
| IR-01 | quantità 2 × prezzo 2 RCH | totale riga 4 |
| IR-02 | copia con totale e IVA | totale non sostituito dall'IVA |
| IR-03 | risposta `ES...` | `DEVICE_RESPONSE/ERROR` |
| IR-04 | comando annullo osservato | `CANCELLATION` |
| IR-05 | cut ESC/POS legacy | confine job riconosciuto |
| IR-06 | camera/non riscosso | chiusura economica, non fiscale |
| IR-07 | preconto 35→esito 5 | differenza 30 e 85,7143% |
| IR-08 | tentativi fiscali annullati | non sommati al fiscale completo |
| IR-09 | fraud worker eseguito due volte | secondo ciclo inserisce 0 alert |
| IR-10 | ingestion su soli duplicati | nessun nuovo batch persistito |

## Regressioni trasporto

- reverse tail con byte periodici oltre il timeout iniziale: non deve chiudere
  finché non decorre una finestra intera di inattività;
- timeout `drain`: abort del trasporto prima del rilascio lock;
- completion fuori ordine: timeline fisica valida e `observed_sequence`
  preservata;
- FIFO capture senza reader: apertura non bloccante e data plane vivo;
- coda capture piena nelle due policy;
- recovery RAW più lungo della timeline e viceversa;
- cache job pubblicati bounded;
- half-close e RST/EOF;
- payload binari con NUL e caratteri di controllo.

## Test database/API/UI

- migrazione upgrade/downgrade su copia MariaDB della major target;
- deduplica storica audit-preserving e conteggi canonici;
- paginazione server-side senza N+1 su dataset voluminoso;
- login valido/errato, lockout, RBAC per ogni endpoint;
- logout/401 cancella token e cache;
- ricerca con tipi canonici;
- export CSV protetto da formula injection;
- download RAW/TXT/JSON autorizzato, hash verificato, path traversal negato;
- PDF deterministico e bounded, con versione renderer e checksum verificato;
- UI responsive, error/empty/loading state e polling bounded.

Il fuzzing/partitioning del trasporto deve coprire almeno cinque partizioni
deterministiche dello stesso payload, oltre ai confini vuoti e aggregati.

## Collaudo hardware separato

Non è stato eseguito durante l'analisi. Richiede autorizzazione, finestra,
operatore, backup e piano rollback. Deve usare il gestionale normale, mai probe
o replay di RAW fiscali. Una route alla volta, poi concorrenza controllata e
riscontro su apparato/giornale elettronico.

## Criterio di pass

Nessun test critico può essere skipped senza motivazione approvata. I conteggi
esatti del gate corrente sono registrati solo dopo l'esecuzione completa; il
repository non usa conteggi storici come attestazione della nuova release.
