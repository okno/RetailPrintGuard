# Analisi read-only della giornata del 18 novembre 2026

Intervallo: giornata civile completa in Europe/Zurich. Controllate anche le
acquisizioni disponibili del 19 novembre, ancora assenti al momento dell'analisi.
Nessun probe, comando di stampa, replay di rete o modifica ai proxy è stato
eseguito. Le copie private di lavoro e gli originali non fanno parte di Git.

## Acquisizione e servizi

| Famiglia | Sessioni | Stato | Byte persi dichiarati |
| --- | ---: | --- | ---: |
| POS | 47 | COMPLETE, EOF bidirezionale regolare | 0 |
| RCH | 21 | COMPLETE, EOF bidirezionale regolare | 0 |
| Epson | 50 | COMPLETE, EOF bidirezionale regolare | 0 |

La metrica `failed` del parser è zero. Nei log strutturati risultano tre warning
di ingestione, ciascuno con un candidato in quarantena per percorso sorgente
mancante o non risolvibile, due warning di dispositivo non raggiungibile e 15
rifiuti della coda anticipata del beeper. Non risultano log ERROR/CRITICAL.
La priorità del journal da sola non distingue i warning JSON, quindi è stato
verificato anche il campo `level` dei messaggi applicativi.
Le richieste API registrate riportano 1.261 risposte HTTP 200 e 8 HTTP 401;
nessun HTTP 5xx.
Il confronto read-only con il database conferma tutte le 118 acquisizioni della
giornata in stato `PARSED` (50 Epson, 47 POS, 21 RCH), senza differenze rispetto
all'inventario dei manifest completi. I tre eventi di quarantena restano evidenze
da conservare; non sono stati cancellati o riprocessati durante l'analisi.
Questi dati descrivono i log disponibili, non dimostrano l'assenza di traffico
che non abbia attraversato i listener o la consegna fisica delle stampe.

## Epson

Le 50 sessioni contengono 64.061 byte complessivi nelle due direzioni e una
richiesta SOAP/HTTP ciascuna. Distribuzione: 42 DirectIO, 7 aperture cassetto,
un rapporto X+Z. Tutte le risposte HTTP sono 200 e dichiarano `success=true`.
Non sono stati osservati messaggi di vendita, righe articolo o pagamenti.

Il parser nuovo rappresenta il rapporto nel modello comune a RCH e conserva
comandi tecnici e risposte come evidenze separate. Il rapporto non alimenta i
totali delle vendite. Payload DirectIO e protocolli non riconosciuti restano
non interpretati; dati di configurazione non diventano intestazioni stampate.

## Preparazione della prossima distribuzione

- Il template nginx resta `listen 0.0.0.0:8081;`, coerente con la modifica
  effettuata manualmente in produzione. Non applicare l'override di un binding
  precedente durante il prossimo aggiornamento.
- Anteprima web, TXT e nuovi PDF mantengono i dati dell'intestazione configurata
  senza le due diciture di configurazione richieste dall'utente. La provenienza
  resta nei metadati. PDF/ZIP già pubblicati non vengono riscritti.
- Questa analisi e l'implementazione offline non attivano una nuova release
  né rianalizzano il database di produzione.

## Verifiche dell'implementazione

- Confronto offline di tutte le 50 sessioni Epson: un rapporto X+Z esportabile,
  49 comandi tecnici e 50 evidenze del flusso di risposta; nessuna vendita creata.
- 97 test mirati superati, inclusi offset RAW, risposte delimitate da chiusura
  HTTP, XML ostile, idempotenza del worker e contenuti delle intestazioni nei PDF.
- Suite backend del control plane: 664 test superati e 5 saltati. Non ripetuta
  la suite invariata `test_proxy_dataplane.py`, i cui fallimenti erano già stati
  rilevati e documentati prima di questa modifica.
- Frontend: 81 test superati. Controlli statici e privacy completati senza
  rilevazioni di segreti o artefatti privati nei candidati Git.
