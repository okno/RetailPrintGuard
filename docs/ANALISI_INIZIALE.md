# Analisi iniziale dei repository esistenti

## Perimetro e provenienza

L'analisi ha riguardato i repository locali `commercialRCHproxy` e
`printproxy`, i relativi test, configurazioni, servizi, documentazione e gli
artefatti di esempio forniti. I dump originali e le immagini non sono copiati
in questo repository perché possono contenere dati commerciali, fiscali,
personali e di rete.

Questa distinzione di provenienza è essenziale:

- `printproxy` era sul tag `v3.0.0`, commit applicativo documentato e worktree
  pulito;
- `commercialRCHproxy` aveva `HEAD` su `76b02fa`, ma la variante dichiarata
  `0.3.0` era composta da numerosi file modificati e non tracciati. Le
  osservazioni sulla nuova architettura Dumper/Parser descrivono quindi quel
  **worktree**, non una release immutabile.

## Inventario sintetico

| Area | commercialRCHproxy | printproxy |
|---|---|---|
| Linguaggio | Python 3.11+, `asyncio` | Python 3.11+, `asyncio` |
| Protocollo osservato | flusso RCH bidirezionale su porta configurabile | RAW/JetDirect ESC/POS configurabile |
| Data plane | listener → RCH e RCH → gestionale | una o più route POS full-duplex |
| Segmentazione | framer incrementale basato su evidenze RCH | sessione/chiusura e split ESC/POS opzionale |
| Artefatti | RAW richiesta, RAW risposta, timeline, manifest, output Parser | RAW richiesta, sidecar TXT/PDF/JSON, stato e ledger |
| Integrità | SHA-256 e pubblicazione `.ready`; Parser `.parsed` | SHA-256, catena ledger e HMAC opzionale/atteso |
| Configurazione | file `KEY=VALUE`, una RCH per processo | file `KEY=VALUE`, mapping CSV multi-POS |
| Servizi | legacy unico e nuova coppia Dumper/Parser | proxy più VIP/firewall/watch opzionali |
| Test | parser, proxy, spool, strumenti offline, systemd/script | proxy, duplex, multi-route, renderer, spool, lifecycle |

## commercialRCHproxy

### Struttura e processi

Il pacchetto è suddiviso in:

- `proxy/`: server, sessioni e pompe TCP;
- `dumper/`: processo di acquisizione separato nella variante 0.3;
- `parser/`: watcher/worker indipendente;
- `rch/`: framing, comandi, risposte, XML7, stato e document types;
- `capture/` e `storage/`: job, manifest, contatori, locking e pubblicazione;
- `render/`: modello documento, testo tecnico/pulito e PDF;
- `tools/`: inspect, replay e reparse offline.

Gli entry point osservati sono `commercialrchproxy`,
`commercialrchproxy-dumper`, `commercialrchproxy-parser`, gli strumenti di
ispezione, replay e reparse. La configurazione è condivisa da Dumper e Parser.
Le unità systemd comprendono sia il servizio legacy sia unità separate per i
due nuovi processi.

### Flusso e confine probatorio

Il Dumper conserva separatamente client→RCH e RCH→client. La timeline registra
sequenza, offset per direzione, orari e risultato del write/drain locale. Tale
risultato non prova che il dispositivo abbia fisicamente ricevuto o fiscalizzato
il documento; il campo di arrivo remoto resta sconosciuto senza cattura
indipendente.

La segmentazione ricostruisce il flusso applicativo incrementalmente. Non
assume che un `recv()` o pacchetto TCP coincida con un documento. I parser RCH
sono limitati ai frame e alle semantiche osservate nei dump e non inferiscono
automaticamente Telnet, XML puro o un protocollo non dimostrato dal solo numero
di porta.

### Formato 0.3 osservato

La radice ha struttura per stampante/data/codice locale. Un job pubblicato
contiene `file_*.raw`, `response_*.raw`, `timeline_*.jsonl`, `manifest.json` e
`.ready`. Il manifest usa `commercialrchproxy.capture.v1`.

Il Parser scrive esclusivamente nel sottoalbero letterale `PHARSED/`, con
`parsed.json`, TXT/PDF ricostruiti e marker `.parsed`. Lo schema si chiama
intenzionalmente `commercialrchproxy.pharsed.v1`, inclusa l'ortografia storica.
L'output interpretato non sostituisce mai i RAW.

## printproxy

### Struttura e processi

Il progetto è un'applicazione Python a moduli principali
`printproxy.py`, `printproxy_core.py`, `printproxyctl.py` e
`receipt_renderer.py`. Supporta configurazione legacy singola e mapping
posizionali multipli, con una route isolata per stampante. L'installer e i
servizi opzionali gestiscono anche VIP, firewall e controllo periodico.

La modalità `transparent_duplex` apre la connessione alla stampante fisica e
inoltra byte e risposte senza ACK sintetici. La modalità store-forward ha
semantica diversa e va valutata separatamente: un job con stato incerto non deve
essere ristampato automaticamente.

### Spool, encoding e renderer

`printproxy` v3 archivia job e un ledger canonico `manifest.jsonl` schema 1,
con metadata/stato schema 2, catena SHA-256, head e HMAC. Il renderer ESC/POS
riconosce testo e comandi osservati, CP858, raster, barcode e QR entro limiti
espliciti; comandi sconosciuti restano conservativi.

Limite importante per l'import storico: il RAW autorevole è il flusso
client→stampante. Il reverse storico è conservato nei metadata come preview
esadecimale e può essere troncato secondo il limite configurato. Non va
presentato come copia completa del flusso stampante→client.

## Dump, log e fotografie

Gli esempi disponibili mostrano documenti POS, Documenti Gestionali e
Documenti Commerciali, incluse variazioni di prezzo e riferimenti tavolo. Sono
stati utili per verificare forme e aspettative dell'operatore, ma una fotografia
non dimostra i byte trasmessi, l'ordine dei frame, il momento di consegna o la
risposta completa del dispositivo.

I dump devono quindi prevalere per ricostruzione tecnica; le immagini possono
essere solo riscontro visivo. Qualunque conclusione su consegna fisica,
fiscalizzazione o equivalenza direct-vs-proxy richiede PCAP e test hardware
autorizzati, al momento non disponibili come evidenza verificata.

## Baseline di test rilevata

| Repository/snapshot | Evidenza disponibile | Interpretazione |
|---|---|---|
| `commercialRCHproxy` worktree 0.3 | report interno: Windows `200 passed, 15 skipped`; Debian/WSL `215 passed` | risultato dichiarato nel worktree, non tag immutabile |
| `printproxy` v3.0.0 | report CI del tag: 145 test su Ubuntu e Windows, Python 3.11/3.13; 4 skip Windows | baseline versionata; hardware escluso |
| RetailPrintGuard durante l'integrazione | suite locale sintetica eseguita e registrata in `TEST_REPORT.md` | copre software, non apparati reali |

Non è corretto sommare questi numeri o usarli come attestazione del sistema
integrato sul target finale.

## Decisioni derivate dall'analisi

1. Monorepo condiviso, processi critici separati.
2. Relay canonico unico per POS/RCH, ma selezionabile per tipo in processi
   differenti.
3. Spool locale prima del control plane, con formati pubblicati atomicamente.
4. Parser, correlazione e antifrode fuori dal percorso di forwarding.
5. Import legacy tramite adapter read-only e contratti di schema stretti.
6. Normalizzazione versionata: un nuovo parser aggiunge una versione, non
   sovrascrive la precedente.
7. Correlazione multi-criterio e spiegabile; mai solo “stesso codice”.
8. Evidenze e audit tamper-evident con catene hash serializzate per scope.

## Gap ereditati e risoluzione

- Il reverse RAW storico POS non è ricostruibile oltre il preview disponibile;
  il nuovo relay canonico conserva entrambe le direzioni per i nuovi job.
- La variante RCH 0.3 deve essere congelata prima di una migrazione ripetibile;
  l'adapter verifica comunque schema, marker e hash.
- Gli IP virtuali dei progetti legacy non vengono hardcoded nel nuovo codice;
  tutte le route sono in YAML e validate.
- La disponibilità di un parser non autorizza a cambiare il data plane: gli
  errori di parsing restano eventi del control plane.
