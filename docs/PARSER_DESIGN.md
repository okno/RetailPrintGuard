# Progettazione dei parser

## Confine architetturale

I parser operano dopo la pubblicazione e l'import del job. Sono funzioni pure o
worker control-plane bounded: non vengono importati dal relay e un'eccezione non
può chiudere una sessione di stampa. L'input è sempre non fidato.

Ogni risultato conserva:

- parser, versione e hash build;
- tipo/sottotipo e confidenza;
- testo normalizzato e righe strutturate;
- encoding rilevato;
- warning/errori;
- offset/spans nel RAW originale;
- versione precedente, senza overwrite.

## Segmentazione

TCP non espone documenti. Il parser riceve il flusso ricostruito per direzione e
usa framer incrementali. Deve supportare:

- un frame/documento diviso su più chunk;
- più frame/documenti nello stesso chunk;
- sequenze di controllo incomplete a EOF;
- byte sconosciuti, conservati e resi visibili;
- limiti di dimensione, righe e profondità.

Un confine non dimostrabile produce `UNKNOWN` o warning, non una fusione
silenziosa.

## ESC/POS osservato

Il parser riconosce testo, quantità/prezzo, identificativi e comandi di taglio
osservati, incluso il comando legacy `ESC m`. La decodifica testuale non modifica
mai il RAW; un carattere non decodificabile è rappresentato in modo leggibile
nel derivato.

Classificazioni minime: comanda, modifica comanda, preconto e documento non
fiscale sconosciuto. Quantità negative/rimozioni devono diventare eventi
espliciti e mantenere la riga sorgente.

Il parser ESC/POS `1.3.0` interpreta una riga articolo soltanto in presenza di
un prefisso quantità esplicito, esclude intestazioni/separatori e conserva
frammenti e span quando una descrizione viene mandata a capo. `Portata` viene
associata alle righe successive. Una quantità negativa è un delta osservato:
il parser non inventa una rimozione completa; il correlatore applica il delta
allo snapshot precedente e marca la rimozione solo a quantità residua zero.

I banner raster composti da bande `ESC *` coerenti possono essere passati a un
OCR bounded nel solo worker parser. Il limite è di quattro immagini e quattro
milioni di pixel, con timeout e output limitati; il tavolo viene accettato solo
da un match stretto sopra la soglia di confidenza. Backend assente, timeout,
output eccessivo o bassa confidenza degradano a metadato/warning. Nessuna di
queste condizioni modifica il RAW o il forwarding.

Una `O` letta al posto dello zero viene corretta soltanto in un prefisso tavolo
numeric-like che contenga anche almeno una cifra. Il testo OCR originale,
la trasformazione applicata e il valore risultante restano verificabili nei
metadati della versione; nessuna regola modifica identificativi alfanumerici.

## RCH osservato

Il parser RCH è evidence-driven e non deduce il protocollo dal numero di porta.
Per i frame osservati:

- valida framing e BCC quando presenti;
- mantiene request e response separate;
- una risposta `ES...` è `DEVICE_RESPONSE` con stato `ERROR`, non un documento
  commerciale completato;
- il comando di annullo/pulizia osservato (`=k`) è classificato
  `CANCELLATION`, senza inventarne effetti fiscali ulteriori;
- una riga con quantità due e prezzo unitario due produce totale riga quattro,
  non due;
- in una copia, il totale documento non deve essere sostituito dal valore IVA
  stampato più in basso;
- tavolo, codice ordine e codice documento sono estratti solo quando presenti;
- data e ora sono estratte solo se entrambe visibili nel testo RCH catturato;
  l’assenza di secondi resta precisione `MINUTE` e non viene presentata come
  un’osservazione al secondo;
- un documento camera/non riscosso è una chiusura economica gestionale distinta
  da un Documento Commerciale.

Il significato preciso di un codice status resta `UNKNOWN` finché non è
associato a documentazione vendor/versione firmware verificata.

## Chiusure economiche e fiscali

La normalizzazione distingue:

- Documento Commerciale completo: contribuisce al totale fiscale aggregato;
- Documento Commerciale annullato/incompleto: resta evidenza, non contribuisce
  come chiusura positiva;
- Documento Gestionale/preconto: baseline economica;
- regolamento camera/non riscosso validato: esito economico osservato, non
  fiscale;
- risposta device: stato tecnico collegato a job/sessione.

Questa distinzione evita sia di ignorare una riduzione reale sia di trattare un
tentativo annullato da zero come incasso fiscale.

## Reprocessing

`retailprintguard-parser --once --reparse-all` aggiunge output della build
corrente. L'attivazione per la correlazione è una decisione separata e auditata.
Usare [scripts/reprocess_captures.sh](../scripts/reprocess_captures.sh) prima in
dry-run e solo su backup/staging. Il reprocessing non modifica i file RAW.

## Test obbligatori

- chunk split/coalesced e EOF parziale;
- quantità/prezzo RCH e totale/IVA copia;
- status errore separato dal documento;
- annullo esplicito;
- cut ESC/POS legacy;
- bande raster multi-strip, OCR assente/errore/bassa confidenza e tavolo ad alta
  confidenza;
- descrizioni mandate a capo, portate e quantità firmate con stato derivato;
- encoding e caratteri di controllo;
- limite input/malformed senza crash;
- determinismo e versioni append-only;
- scenario preconto→modifica→esito economico;
- conto separato fiscale senza falso positivo.
