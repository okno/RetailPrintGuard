# Matrice delle evidenze

Gli identificativi seguenti sono pseudonimi. I file originali, i relativi hash
completi e i riferimenti di rete restano nel deposito privato dell'incidente e
non sono parte del repository.

## Fonti

| ID | Fonte | Cosa può dimostrare | Cosa non può dimostrare |
|---|---|---|---|
| IMG-01 | collage fotografie POS/RCH | testo visibile, importi, sequenza manuale | byte, framing, consegna TCP |
| IMG-02 | collage documenti annullati | presenza visiva di annulli/riferimenti | autore del comando o causa protocollo |
| IMG-03 | collage esiti successivi | totale e natura visibile dei documenti | correlazione automatica certa |
| RAW-POS | RAW/timeline dei tre POS | direzione, ordine, offset, payload, cut | stampa fisica |
| RAW-RCH | RAW/timeline RCH | frame, BCC, ACK/status e comando client | significato vendor non documentato |
| DB-RO | estratti database read-only | entità persistite, cardinalità, duplicati | eventi mai acquisiti |
| LOG-RO | journal read-only | cicli worker, errori e frequenza | contenuto non loggato |
| CODE | sorgente e test | comportamento implementato e rischi latenti | comportamento hardware reale |

## Correlazione visivo/RAW

| Evento | Riscontro immagine | Riscontro RAW | Esito |
|---|---|---|---|
| Comanda Stampante BAR POS80BL | IMG-01 | RAW-POS, route isolata | coerente |
| Comanda Stampante CUCINA POS80BL | IMG-01 | RAW-POS, route isolata | coerente |
| Comanda Stampante PIZZERIA POS80BL | IMG-01 | RAW-POS, route isolata | coerente |
| Preconto 35,00 € | IMG-01 | RAW-RCH, frame completi | coerente |
| Rimozione articolo | IMG-02 | RAW-POS, quantità negativa/rimozione | coerente |
| Quattro tentativi annullati | IMG-02/IMG-03 | quattro sessioni RAW-RCH distinte | coerente |
| Riga valorizzata a zero | leggibile nei documenti | comando client e status successivo | coerente; semantica status non attribuita |
| Operazione valida 2,00 € | IMG-03 | sessione RAW-RCH successiva | coerente ma distinta |
| Regolamento camera 5,00 € | IMG-03 | sessione RAW-RCH successiva | coerente; PII redatta |

## Verifiche di integrità offline

| Controllo | Risultato sul campione | Limite |
|---|---|---|
| hash archivio privato prima dell'estrazione | verificato | valore non pubblicato |
| manifest e marker `.ready` | coerenti | solo job inclusi nel bundle |
| hash dei file e slice | coerenti | non è un anchor esterno |
| catene timeline | coerenti | root può alterare dati e head insieme |
| sequenze e offset per direzione | coerenti | orologio sorgenti non perfettamente allineato |
| BCC frame RCH | valido nei frame osservati | non sostituisce documentazione vendor |
| ACK/NAK | ACK associati; nessun NAK osservato | uno status applicativo può comunque segnalare errore |
| errori/drop forwarding | nessuno osservato | non prova ricezione/stampa fisica |

## Evidenze mancanti

- PCAP indipendente ai due lati del proxy;
- log autorevole del gestionale che identifichi azione e operatore;
- documentazione RCH esatta per firmware e codice status osservato;
- audit dell'apparato o giornale elettronico associato;
- test direct-vs-proxy autorizzato sulla stessa sequenza.

Le evidenze mancanti non vengono sostituite con inferenze. La catena di custodia
operativa è descritta in [SECURITY_REVIEW.md](SECURITY_REVIEW.md).
