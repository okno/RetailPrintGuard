# Acquisizione di una stampante fiscale Epson

La famiglia `type: epson` usa il relay TCP trasparente comune con acquisizione
RAW integrale nelle due direzioni. Il processo dedicato è
`retailprintguard-epson-proxy.service`, selezionato con `--device-type epson`.
L'unità viene saltata se non esistono dispositivi Epson abilitati.

La configurazione di acquisizione resta `parser: opaque_raw`, compatibile con
i proxy già installati. Il worker usa ora l'adapter passivo
`retailprintguard-epson-observed` per HTTP/SOAP EpsonFpMate verificabile, con
fallback RAW per protocolli e comandi non supportati. Non occorre cambiare la
configurazione o riavviare i proxy per introdurre questa interpretazione.

## Interpretazione passiva dei messaggi osservati

- Framing HTTP/1.0 e HTTP/1.1 con `Content-Length` univoco, inclusi più messaggi
  nella stessa cattura; risposte finali delimitate da `Connection: close`, come
  nel firmware osservato, solo con SOAP XML completo. Chunked, compressione, stream incompleti o ambigui
  restano RAW, senza associazioni euristiche delle risposte.
- SOAP 1.1 UTF-8, con limiti di dimensione, messaggi, nodi, profondità e attributi;
  DTD, entità dichiarate, namespace estranei e campi risposta duplicati rifiutati.
- Rapporti `printXReport`, `printZReport`, `printXZReport` nel modello
  `NormalizedDocument`, tipo `SHIFT_END_REPORT`, condiviso con RCH. Progressivo Z
  e totale giornaliero sono ricavati dalla risposta corrispondente solo se
  HTTP 200 e `success=true`, con dati validi. Il totale è cumulativo giornaliero,
  non una vendita; per X il progressivo Z non è usato come identità del rapporto.
- Il testo del rapporto è una proiezione dichiarata dei dati SOAP, non una copia
  del layout cartaceo. Righe, provenienza, offset RAW e render PDF usano lo stesso
  modello comune degli altri parser. Nessuna data fiscale o intestazione è
  sostituita con data di cattura o dati letti da comandi di configurazione.
- `directIO` e `openDrawer` sono evidenze tecniche, senza righe vendita, totali,
  pagamenti o PDF di scontrino. Il payload nativo DirectIO non viene decodificato.
  L'eco del comando DirectIO deve coincidere per annotare un riscontro positivo.
- L'intero flusso inverso resta disponibile come `DEVICE_RESPONSE`, con hash e
  percorso propri. Una risposta positiva non dimostra la consegna fisica.

L'identità della build include il fallback RAW. Le rianalisi aggiungono versioni
senza riscrivere RAW, precedenti interpretazioni o archivi già pubblicati.
I test impiegano soltanto fixture sintetiche; le catture reali restano fuori Git.
Per rianalizzare solo Epson, il parser CLI accetta
`--once --reparse-all --device-id <id-configurato>`; la selezione avviene nel
database prima di caricare i RAW, sotto il lock condiviso della pipeline.

## Evidenze disponibili e limite sulle vendite

L'analisi read-only della giornata del 18 novembre 2026 ha trovato 50 sessioni
Epson complete: 42 DirectIO, 7 aperture cassetto e un rapporto X+Z, con risposte
SOAP positive. Non erano presenti `printerFiscalReceipt`, articoli o pagamenti.
Il confronto con le catture storiche disponibili ha trovato soltanto accessi
HTTP all'interfaccia amministrativa, senza ulteriori documenti fiscali.

La struttura dei rapporti e i campi della risposta sono documentati nella
guida Epson ePOS Fiscal revisione T, sezioni 3.4 e 5.8; DirectIO nella 5.3.2.
La cattura conferma framing e campi del rapporto X+Z. X e Z sono coperti da test
sintetici sulla struttura documentata, senza dichiararli validati su produzione.
Le vendite Epson restano `UNKNOWN` finché non sono disponibili catture reali
verificabili: non si riutilizza il parser RCH su byte Epson e non si deducono
scontrini da un semplice HTTP 200, da testo libero o da una configurazione.

## Porte e identità del dispositivo

Epson documenta trasporti diversi:

- EpsonFpMate in modalità LAN usa un indirizzo e una porta TCP configurabili;
  la porta predefinita documentata è 9100 (guida EpsonFpMate, pagina 28).
- La soluzione ePOS Fiscal usa SOAP su HTTP/HTTPS all'endpoint
  `/cgi-bin/fpmate.cgi` (guida ePOS Fiscal, pagine 11–13).

Il modello da solo non determina quale modalità sia usata dal gestionale.
Listener e target devono conservare la porta effettivamente configurata.
Quando la porta è interna al driver e non viene mostrata dal gestionale,
`additional_ports` permette di esporre più trasporti dello stesso dispositivo:

```yaml
type: epson
parser: opaque_raw
listen_ip: 192.0.2.40
listen_port: 9100
target_ip: 192.0.2.41
target_port: 9100
additional_ports: [80]
```

Questo frammento appartiene alla stessa voce dispositivo: non aggiunge una
seconda stampante. Ogni porta aggiuntiva inoltra alla medesima porta del target
(80 verso 80 nell'esempio). La selezione dipende dalla connessione originaria
del gestionale; non vengono inviati probe, richieste applicative o copie del
flusso alle altre porte. Un solo processo e un solo gestore dello spool
mantengono lo stesso `device_id`; ciascuna sessione conserva nei metadati i
propri endpoint reali e i RAW nelle due direzioni. ACL e limite connessioni
restano condivisi, mentre l'esclusione di client concorrenti resta per endpoint
fisico. Arresto e avvio riguardano tutti i listener insieme.

L'opzione è ammessa solo per Epson, è vuota per default e accetta al massimo
otto porte intere distinte (1–65535), diverse dalle due porte primarie. Tutte
le porte partecipano ai controlli di collisione e di loop del sito.
La guida Cassa in Cloud identifica il driver `EPSON RT v.10 XML7`, ma non
ne specifica la porta: il nome XML7 da solo non prova l'uso di ePOS/HTTP.

Il relay non converte protocolli, non sostituisce driver e non termina TLS.
Con TLS il RAW resta cifrato, e la verifica del nome/IP del certificato da parte
del client deve rimanere valida dopo l'inserimento del proxy.

Prima dell'attivazione verificare modello/firmware, protocollo del gestionale,
porta, IP virtuale e target, ACL e identità della macchina. Nessun probe,
comando fiscale o stampa di prova è necessario per configurare il supporto
software offline. La verifica di stampa reale resta una fase operativa
separata e autorizzata.

La pagina ufficiale Epson identifica la famiglia commerciale TM-T800F; la
corrispondenza di un codice di etichetta come M261A con modello e revisione
deve essere verificata sul dispositivo. Non viene dedotta dal solo codice.

## Conservare la validità dello spool dopo un cambio IP

Le acquisizioni originali conservano l'endpoint fisico usato al momento della
sessione. Quando cambia l'IP o la porta di un dispositivo, mantenere lo stesso
`id` e dichiarare gli endpoint precedenti verificati nella relativa voce:

```yaml
target_ip: 192.0.2.41
target_port: 9100
historical_targets:
  - ip: 198.51.100.40
    port: 9100
```

`historical_targets` è una whitelist per l'ingestione, disponibile per tutte
le famiglie di dispositivi. Non crea listener e non viene usata per inoltrare
traffico. Accetta al massimo 32 endpoint distinti; un endpoint storico non può
essere attribuito a due dispositivi abilitati o al target corrente di un altro
dispositivo. Le porte aggiuntive correnti partecipano allo stesso controllo.

Ricavare gli alias dalla configurazione precedente verificata. L'ingestione
continua a verificare identità nel manifest e nel percorso, hash, timeline e
RAW. Uno storico già importato viene riconosciuto come duplicato; un'acquisizione
storica ancora pendente può essere importata con il suo endpoint originale.
Un'identità discordante o un endpoint non dichiarato rimane in quarantena.
Non riscrivere i manifest, i RAW o le sessioni precedenti per sostituirvi gli IP.

## Fonti primarie

- [Epson TM-T800F Series](https://www.epson.it/prodotti/stampanti/stampanti-pos/stampanti-pos/dispositivo-fiscale/epson-tm-t800f-series/p/11648)
- [EpsonFpMate Development Guide, revisione 29](https://download4.epson.biz/sec_pubs/bs/pdf/EpsonFpMateDevGuideRev29.pdf)
- [ePOS Fiscal Print Solution Development Guide, revisione T](https://download4.epson.biz/sec_pubs/bs/pdf/ePOS%20Fiscal%20Print%20Solution%20Development%20Guide%20Rev%20T.pdf)
- [Protocollo fiscale nativo, download ufficiale Epson Scontrino Smart](https://www.scontrinosmart.it/it/item/9a224f16-8875-4ea9-aa20-65d5a5d1aabf/download)
- [Installazione Epson e driver Cassa in Cloud](https://cassaincloud.zendesk.com/hc/it/articles/13286906101661-Manuale-installazione-Registratore-Telematico-Epson-FP81II-Epson-FP90III-e-Access-Point)

La specifica del protocollo fiscale nativo riguarda FP-81 II RT, FP-90 III RT e RT Server. La sua
applicabilità al dispositivo installato deve essere confermata prima di
sviluppare o attivare un parser semantico.
