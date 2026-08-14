# Istruzioni per agenti e contributori

Queste regole valgono per l'intero repository RetailPrintGuard.

## Priorità operative

1. Non alterare, ritardare o duplicare i byte inoltrati dai proxy.
2. Non rendere database, parser, API o web application dipendenze sincrone del
   data plane.
3. Conservare sempre il RAW originale e distinguere fatti, inferenze e ipotesi.
4. Non dichiarare mai il sistema *tamper-proof*: le garanzie implementate sono
   *tamper-evident*.

## Divieti su apparati e produzione

- Non eseguire probe, replay, test di stampa, `ping`, `nc`, `telnet`, `nmap` o
  connessioni applicative verso listener e stampanti reali senza una finestra
  esplicitamente autorizzata.
- Non riavviare proxy POS/RCH e non modificare indirizzi, route, firewall o
  configurazioni di produzione durante analisi e test offline.
- Gli strumenti chiamati `replay` in questo repository devono elaborare file
  locali e non devono aprire socket di rete.
- Qualsiasi comando root eseguito in produzione deve essere registrato in forma
  redatta, con motivazione, risultato e rollback.

## Evidenze e privacy

- Non committare fotografie reali, RAW, PCAP, dump DB, archivi, credenziali,
  token, hash completi di evidenze, UUID operativi, MAC, IP privati o PII.
- Usare nelle fixture soltanto dati sintetici e indirizzi riservati alla
  documentazione (RFC 5737/RFC 3849).
- I file originali sono immutabili. Le interpretazioni successive devono essere
  versionate e non sovrascrivere il risultato precedente.
- Un'immagine prova l'aspetto del documento; non prova i byte trasmessi, il
  framing, la risposta dell'apparato o la consegna fisica.

## Modifiche e verifiche

- Preferire interventi minimi, spiegabili e retrocompatibili.
- Ogni correzione al relay richiede test su segmentazione, aggregazione,
  full-duplex, half-close, timeout, backpressure e failure dello spool.
- Ogni modifica parser richiede fixture sintetiche bounded, offset RAW e test
  di non regressione; una semantica RCH non documentata resta `UNKNOWN`.
- Ogni regola antifrode deve essere deterministica, idempotente, versionata e
  accompagnata da evidenze leggibili.
- Prima del commit eseguire almeno `scripts/run_tests.sh`,
  `scripts/check_secrets.sh` e `git diff --check`. Se un gate non può essere
  eseguito, dichiararlo esplicitamente nel report.
- Non usare `--force` nei push e non riscrivere la storia condivisa.

## Shell e manutenzione

Gli script Bash devono iniziare con `set -Eeuo pipefail`, usare path quotati,
validare ogni target distruttivo e adottare dry-run per default quando scrivono
database o file persistenti. Nessuno script applicativo deve gestire
implicitamente gli IP del sito.
