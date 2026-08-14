# ADR trasporto — Relay trasparente prioritario e capture disaccoppiata

**Stato:** accettata nel codice correttivo; deployment pendente.

## Contesto

RetailPrintGuard è in linea tra un gestionale e dispositivi di stampa. Un
blocco, una mutazione o un replay può avere impatto operativo e fiscale. TCP è
un flusso: i confini di `recv()` non sono confini documento e una singola
lettura può contenere frame parziali o multipli.

## Decisione

Usare un relay protocol-neutral full-duplex per il data plane e una coda locale
bounded per duplicare gli eventi verso lo spool. POS e RCH condividono il
motore, ma sono processi systemd separati. Il relay:

- inoltra lo stesso `bytes` ricevuto senza decode/encode;
- mantiene pompe e offset indipendenti per direzione;
- usa backpressure del socket e limiti espliciti;
- serializza sessioni dirette allo stesso target quando necessario;
- propaga half-close e chiude in modo deterministico;
- non chiama database, parser, HTTP o shell;
- non esegue e non deserializza in modo arbitrario i payload.

La capture è append-only. `observed_sequence` e `sequence` hanno semantiche
distinte e validate. Un write non riuscito causa abort sincrono del trasporto
prima che una sessione successiva possa usare il target.

## Timeout

- `connect_timeout`: limite per aprire il target;
- `forward_timeout`: limite per una singola operazione write/drain;
- `session_idle_timeout`: inattività complessiva;
- `response_tail_timeout`: inattività rinnovabile dopo EOF client;
- `shutdown_grace`: tempo bounded per una terminazione ordinata.

Nessun timeout autorizza un replay automatico: dopo un esito incerto non è
possibile sapere se il dispositivo abbia ricevuto il comando.

## Failure policy dello spool

`storage_failure_policy=continue` privilegia la stampa: il relay continua e
produce un evento/fallimento capture quando possibile. `abort` privilegia la
completezza probatoria e chiude la sola sessione interessata. La scelta è di
sito e deve essere motivata; nessuna delle due offre simultaneamente garanzia
assoluta di stampa e prova completa in ogni guasto disco.

## Alternative scartate

- **Parsing inline:** aggiunge latenza e trasforma input malformati in rischio
  per il forwarding.
- **Database sincrono:** rende MariaDB un single point of failure del data
  plane.
- **Store-and-forward/retry automatico:** può duplicare stampe o operazioni
  fiscali con esito remoto ignoto.
- **Un processo per tutti i componenti:** aumenta il blast radius.
- **Un broker esterno obbligatorio:** complessità non giustificata dal volume e
  nuova dipendenza operativa.

## Conseguenze

Il sistema resta stampabile durante guasti control-plane, ma può produrre
evidenza parziale con policy `continue`. La capacità spool/disco deve essere
monitorata. Le garanzie si fermano al socket locale: PCAP indipendenti e riscontro
apparato sono necessari per attestare consegna fisica.

## Verifica richiesta

La decisione è coperta dai casi D, E, F e G di
[TEST_PLAN.md](TEST_PLAN.md), inclusi payload segmentati/aggregati, malformed,
quattro device concorrenti, recovery e timeout della coda di risposta.
