# Valutazione dell'incidente

## Sintesi esecutiva

La transazione esaminata mostra un preconto da **35,00 €**, seguito da una
modifica POS e da più tentativi RCH annullati; il primo esito economico valido
successivo è pari a **5,00 €**. La differenza osservabile è quindi **30,00 €**
(`85,7143%`). Una successiva operazione distinta da 2,00 € e un regolamento
camera/non riscosso da 5,00 € sono presenti nella stessa finestra, ma non devono
essere sommati automaticamente senza criteri di correlazione espliciti.

L'analisi byte-level non ha rilevato, nel campione acquisito, mutazioni,
troncamenti, riordini o replay prodotti dal proxy. I tentativi annullati sono
preceduti da comandi inviati esplicitamente dal client gestionale; la riga a
prezzo zero riceve in modo ripetibile uno status d'errore RCH. Il significato
commerciale esatto di quel codice resta un'inferenza finché non viene associato
alla documentazione ufficiale del firmware/protocollo in uso.

In parallelo sono stati individuati difetti **latenti** nel relay/spool e un
difetto **attivo** di idempotenza del worker antifrode. I difetti latenti non
spiegano l'incidente osservato, ma devono essere corretti perché potevano
compromettere futuri flussi o evidenze. Il difetto antifrode produceva alert
duplicati a ogni ciclo ed è stato contenuto fermando solo quel worker.

## Perimetro

Sono stati esaminati offline:

- fotografie private dei documenti, senza copiarle nel repository;
- un bundle privato di spool, verificato prima dell'estrazione;
- manifest, RAW client→device e device→client e timeline;
- estratti read-only di database e journal;
- codice della release installata e worktree correttivo.

Non sono stati eseguiti test sugli apparati o connessioni verso i loro endpoint.

## Timeline normalizzata

Gli orari reali sono omessi. `T0` indica l'inizio della sequenza osservata.

| Fase | Sorgente | Evento | Importo/stato |
|---|---|---|---|
| T0 | Stampante BAR POS80BL | comanda iniziale bevande | parziale |
| T0+2 | Stampante CUCINA POS80BL | aggiunta riga cucina | parziale |
| T0+5 | Stampante PIZZERIA POS80BL | aggiunta righe pizzeria | parziale |
| T0+8 | CASSA RCH Print! F | Documento Gestionale/preconto | 35,00 € |
| T0+22 | Stampante BAR POS80BL | rimozione di una bevanda | variazione |
| T0+25 | CASSA RCH Print! F | quattro sessioni distinte con riga a zero | status errore/annullo |
| T0+28 | CASSA RCH Print! F | comando client di pulizia/annullo | annullo esplicito |
| T0+28 | CASSA RCH Print! F | operazione distinta valida | 2,00 € |
| T0+33 | CASSA RCH Print! F | regolamento camera/non riscosso | 5,00 € |

È osservato uno scarto di orologio di circa due minuti tra fonti POS e RCH.
L'engine deve quindi correlare entro una finestra configurabile, mantenendo gli
orari sorgente e acquisizione separati.

## Modifiche economiche rilevate

Tra preconto e primo esito economico riconducibile alla sequenza:

- una riga da 4,00 € è rimossa;
- una riga passa complessivamente da 4,00 € a 1,00 €;
- righe da 8,00 €, 7,00 € e 8,00 € non risultano nell'esito da 5,00 €;
- il totale passa da 35,00 € a 5,00 €.

La conclusione antifrode deve mostrare il diff e il livello di confidenza, non
attribuire automaticamente intenzione o responsabilità a una persona.

## Stato delle acquisizioni

Nel bundle privato analizzato:

- tutti i job erano pubblicati con manifest e marker coerenti;
- hash di file, slice, offset e catene timeline risultavano coerenti;
- i frame RCH verificabili avevano BCC valido;
- ogni richiesta RCH osservata aveva un ACK associato e non risultavano NAK;
- la latenza locale di forwarding osservata restava inferiore a 1 ms;
- non sono emersi drop, errori di inoltro o contaminazione tra dispositivi.

Questi risultati valgono solo per il campione. Un `drain()` completato dimostra
la consegna al socket locale, non la stampa fisica.

## Contenimento

Il worker antifrode generava duplicati perché un timestamp di valutazione
variabile entrava nell'evidenza usata dal fingerprint. Il numero di alert e
batch cresceva a ogni polling su dati invariati. È stato fermato unicamente il
servizio antifrode; data plane, ingestion, parser, correlazione e API non sono
stati usati per probe o restart degli apparati.

La riattivazione richiede:

1. test di idempotenza su due esecuzioni consecutive;
2. migrazione/conciliazione audit-preserving dei duplicati preesistenti;
3. backup verificato;
4. deployment autorizzato e monitoraggio dei contatori.

## Classificazione delle conclusioni

| Conclusione | Classe |
|---|---|
| I byte acquisiti nel campione sono coerenti con quelli inoltrati | fatto supportato da RAW/timeline |
| Le sessioni annullate sono state iniziate dal client | fatto supportato dai frame client→RCH |
| La riga a zero precede lo status di errore | fatto ripetibile nel campione |
| Lo status indica esattamente una regola fiscale specifica | inferenza non confermata dal vendor |
| Il proxy ha causato i documenti annullati | non supportato dalle evidenze |
| Il gestionale o un operatore ha agito con intento fraudolento | non determinabile tecnicamente |

## Stato di chiusura

La ricostruzione tecnica è sufficiente per escludere una mutazione dimostrata
del proxy nel campione, ma non per certificare il comportamento fisico
dell'apparato o l'intento umano. Il gate finale resta aperto fino ai test e al
deployment controllato elencati in [TEST_PLAN.md](TEST_PLAN.md) e
[FINAL_VALIDATION_REPORT.md](FINAL_VALIDATION_REPORT.md).
