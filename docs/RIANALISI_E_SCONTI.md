# Rianalisi dello storico e sconti osservati

La pagina **Rianalisi** avvia un'elaborazione dell'intero storico acquisito.
ADMIN e AUDITOR possono creare la richiesta e riprovare un'elaborazione fallita;
gli stessi ruoli possono consultarne avanzamento e risultati. La vista degli
sconti nelle interpretazioni attuali è disponibile anche agli utenti READ_ONLY.
Lo storico conserva le versioni utilizzate, le impronte degli input, le fasi
completate e i rapporti JSON/CSV. Una sola rianalisi può essere attiva alla volta.
Il JSON contiene il rapporto completo; il CSV contiene le righe di sconto,
con la verifica della singola riga e quella del documento in colonne distinte.

La rianalisi legge i RAW archiviati e aggiunge interpretazioni versionate.
Non invia dati alle stampanti, non riproduce traffico e non attiva il beeper.
Documenti, RAW e interpretazioni precedenti restano disponibili. Le richieste
PDF/ZIP già completate mantengono i propri snapshot e non vengono riscritte;
per esportare una nuova interpretazione creare una nuova richiesta.

Il parser viene attivato dopo il completamento della fase di parsing. Seguono
la ricostruzione delle correlazioni, la valutazione antifrode e l'analisi degli
sconti. Un'interruzione conserva i progressi. Un errore resta visibile e non è
presentato come risultato completo; il nuovo tentativo riprende la richiesta.
Le impostazioni e le versioni software devono coincidere con quelle congelate
all'avvio, altrimenti occorre una nuova richiesta.

## Come leggere gli sconti

**Sconti dipendenti osservati** somma soltanto righe negative che riportano
esplicitamente la dicitura dipendenti e il 30%. Gli sconti generici del 30%
sono separati: una differenza matematica del 30% da sola non prova che si
tratti di uno sconto dipendenti.

Il numero associato è il conteggio dei documenti, non delle persone o delle
vendite uniche. Documenti distinti con importi uguali non vengono accorpati
senza un'identità dimostrabile. Una riga può quadrare aritmeticamente rispetto
all'articolo precedente mentre il documento complessivo rimane da verificare.
La verifica completa richiede anche il totale stampato, la quadratura di tutte
le righe e l'acquisizione completa.

Queste verifiche descrivono il contenuto acquisito. Non certificano il diritto
allo sconto, l'autorizzazione aziendale o la corrispondenza con un documento
fiscale; le regole antifrode mantengono i propri requisiti.

I filtri della dashboard e della vista **Sconti** usano la data di acquisizione,
con estremi UTC inclusivo/esclusivo nell'API. La data stampata rimane distinta.
I filtri sui risultati di una rianalisi non limitano gli input rielaborati:
ogni richiesta riguarda l'intero storico congelato al momento dell'avvio.

## Distribuzione

La migrazione `5f9a2c7d1e84` aggiunge le tabelle delle richieste, degli input e
dei risultati. Il servizio `retailprintguard-analysis` usa lo stesso database
dei processi di analisi e il loro lock MariaDB, separato per database.
I proxy non partecipano al lock. Parser, correlazione e antifrode devono essere
aggiornati insieme al nuovo servizio per rispettare questo coordinamento.

Il ritorno a una versione software precedente richiede una verifica delle
versioni parser attive e delle richieste in corso. Non eliminare le nuove
interpretazioni o lo storico per effettuare il rollback. Conservare un backup
coordinato del database, dei RAW e degli archivi prima dell'aggiornamento.
