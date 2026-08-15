# Episodi di vendita e riduzione dei falsi positivi

## Scopo e confine di sicurezza

Questa elaborazione appartiene esclusivamente al **control plane**. Legge le
interpretazioni già persistite, crea correlazioni, attribuzioni di prezzo e
alert derivati, ma non importa codice del proxy e non legge o scrive i socket
dei dispositivi. RAW, manifest, timeline e byte inoltrati restano invariati.

Un episodio di vendita è una vista ricostruibile di documenti distinti, non una
fusione distruttiva. Ogni documento conserva identità, versione parser, job e
provenienza originali. Correlazione e regole sono inferenze versionate e non
costituiscono da sole prova di frode.

## Algoritmo dell'episodio

La correlazione `rpg-correlation-1.3.0` ordina deterministicamente i documenti
della versione parser attiva e considera soltanto job non esclusi dall'analisi.
La selezione dei candidati è bounded per chiavi operative e finestra temporale;
non assume che una connessione TCP corrisponda a una singola vendita.

Il collegamento procede con queste regole:

1. codici ordine, tavolo e riferimenti forti esplicitamente incompatibili
   bloccano il collegamento, anche quando altri criteri sono vicini;
2. un Documento Commerciale completo o un Documento Gestionale marcato come
   chiusura economica delimita l'episodio: una nuova comanda successiva non
   viene trascinata nella vendita precedente;
3. le comande inviate simultaneamente a reparti differenti sono riunite entro
   30 secondi sullo stesso tavolo; il limite è applicato al diametro dell'intero
   gruppo, così una catena di ticket intermedi non estende indefinitamente la
   finestra;
4. una modifica sullo stesso tavolo e dispositivo può seguire la comanda entro
   300 secondi soltanto quando esiste sovrapposizione di articoli;
5. copie conformi e ristampe sono figli non economici e non fanno da ponte tra
   due vendite; un rimborso resta un aggiustamento post-chiusura separato;
6. una risposta RCH si collega soltanto al documento osservato nello stesso
   job duplex esatto. Una sessione TCP RCH persistente non ha peso di identità
   commerciale e non può unire scontrini diversi;
7. più Documenti Commerciali compatibili rappresentano una possibile chiusura
   suddivisa: importi e righe fiscali vengono aggregati prima del confronto col
   preconto;
8. una nuova interpretazione non sovrascrive la precedente: la correlazione
   sostitutiva è append-only e la precedente diventa `SUPERSEDED`.

Ogni gruppo conserva score, criteri soddisfatti e mancanti, spiegazione,
versione algoritmo, membri e fingerprint dell'input. Gli stessi input ordinati
producono lo stesso risultato.

## Confronto economico

Il motore distingue tre quantità:

- `prebill_total`: ultimo preconto osservato nell'episodio;
- `fiscal_total`: somma dei soli Documenti Commerciali completi;
- `observed_final_total`: totale fiscale oppure, in sua assenza, somma delle
  chiusure economiche gestionali validate, inclusi gli addebiti camera
  esplicitamente classificati.

I rimborsi non riducono retroattivamente il valore della vendita originaria.
Tentativi fiscali incompleti o annullati restano nella timeline ma non diventano
un totale fiscale positivo.

Il diff aggrega prima le righe della chiusura suddivisa e poi classifica
`ADDED`, `REMOVED`, `QUANTITY_CHANGED`, `PRICE_CHANGED`, `DISCOUNT_CHANGED` e
`UNCHANGED`. La regola `MODIFICA_POST_PRECONTO` crea un solo incidente economico
operativo e incorpora nello stesso finding le variazioni di riga, prezzo e
quantità. Le regole secondarie equivalenti restano dettagli tecnici, evitando
di moltiplicare sia gli alert sia la differenza economica in dashboard.

## Riduzione dei falsi positivi

Sono operativi soltanto gli alert canonici negli stati `OPEN`, `UNDER_REVIEW` o
`CONFIRMED`. `FALSE_POSITIVE`, `JUSTIFIED`, `CLOSED` e gli alert legati a una
correlazione superseded restano consultabili nell'archivio, ma non concorrono
allo stato corrente della transazione.

Le correzioni specifiche sono:

- un delta negativo di `ORDER_CHANGE` rappresenta una rimozione, non un
  articolo venduto a prezzo negativo;
- annullo e rimozione collegati allo stesso documento sono conteggiati una sola
  volta;
- una modifica è “tardiva” soltanto se avviene strettamente dopo la chiusura,
  non semplicemente vicino ad essa;
- l'addebito camera esplicito soddisfa la chiusura economica e non genera
  automaticamente “ordine senza fiscale”;
- duplicati e gap globali sono ancorati a un solo episodio coinvolto e non
  replicati su tutte le transazioni della finestra;
- la concentrazione operatore non include l'alert che sta calcolando nel proprio
  denominatore e richiede un operatore realmente identificato;
- quando una correlazione aggiornata sostituisce quella precedente, gli alert
  ormai obsoleti vengono marcati `JUSTIFIED` con nuova evidenza e storia;
- gli alert legacy noti come prodotti da auto-amplificazione operatore o da
  duplicati esterni alla transazione vengono riclassificati
  `FALSE_POSITIVE`. La transizione è idempotente, motivata e hash-chained: non
  viene cancellato alcun record storico.

La dashboard conta una riduzione economica una sola volta per episodio, anche
se più regole descrivono la stessa evidenza. Espone separatamente alert
operativi, episodi con riduzione, differenza economica operativa, job incompleti
da verificare e conteggi archiviati come falsi positivi/giustificati.
La webapp apre questo quadro sugli ultimi sette giorni. Le card “episodi con
riduzione” e “differenza economica” portano alla stessa lista filtrata per
periodo, alert economico operativo, chiusura osservata e differenza positiva.

## Prezzi derivati per le comande

Le righe POS senza prezzo osservato possono ricevere attribuzioni prodotte da
`line-price-attribution/1.0.0`. Le sole fonti monetarie ammesse sono versioni
**complete** di preconto, Documento Gestionale e Documento Commerciale
appartenenti allo stesso episodio.
Risposte dispositivo, copie conformi, ristampe e rimborsi non sono fonti.

L'abbinamento è conservativo:

1. codice articolo esatto, quando disponibile;
2. altrimenti descrizione normalizzata esatta;
3. quantità assolute compatibili, quando presenti.

Il prezzo unitario osservato sulla comanda resta autoritativo e non viene
sostituito. Per una riga priva di prezzo, ogni attribuzione registra fonte,
riga/versione sorgente, base del match, quantità, valore, confidenza, algoritmo
e fingerprint. Candidati concordi sono `AGREED`; candidati incompatibili sono
`AMBIGUOUS` e restano visibili senza scelta arbitraria. La proiezione UI può
preferire, tra attribuzioni risolte, Documento Commerciale, Gestionale e
preconto in quest'ordine soltanto quando tutte le fonti monetarie disponibili
concordano. In presenza di qualunque conflitto non pubblica un prezzo derivato:
mostra “Prezzi in conflitto” e mantiene consultabili tutti i candidati.

`document_lines` e RAW non vengono aggiornati: una nuova versione dell'algoritmo
aggiunge nuove attribuzioni e conserva le precedenti.

## Esempi di controllo

### Riduzione reale da verificare

Preconto 100,00 EUR, rimozione documentata di una riga e chiusura completa a
50,00 EUR:

- un episodio;
- differenza operativa 50,00 EUR;
- un finding `MODIFICA_POST_PRECONTO` con importi, percentuale, diff righe e
  riferimenti alle evidenze;
- nessun raddoppio dell'importo dovuto a regole secondarie.

### Conto diviso legittimo

Preconto 100,00 EUR e due Documenti Commerciali completi da 50,00 EUR:

- totale fiscale aggregato 100,00 EUR;
- differenza zero;
- nessun alert di riduzione del valore.

### Nuovo servizio sullo stesso tavolo

Una nuova comanda osservata dopo la chiusura economica precedente avvia un
nuovo episodio, anche se riusa tavolo o codice. Non viene correlata alla vendita
chiusa soltanto per prossimità temporale.

## Revisione e limiti

Prima di confermare un alert verificare timeline, criteri, completezza dei job,
versioni parser, diff e RAW autorizzato. Clock errato, documenti non osservati,
identificativi riusati o testo ambiguo possono ridurre la confidenza. Il prezzo
derivato è un'inferenza con provenienza, non un importo stampato sulla comanda.
Il PDF è una vista leggibile derivata e non sostituisce il documento fisico o il
RAW.
