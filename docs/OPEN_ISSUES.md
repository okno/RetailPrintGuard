# Questioni aperte e rischi residui

Gli ID sono stabili per i report; `OPEN` significa che il repository non può
attestare la chiusura senza il gate indicato.

| ID | Priorità | Stato | Questione | Criterio di chiusura |
|---|---|---|---|---|
| OI-001 | P0 | OPEN | correzione non ancora distribuita | deployment controllato e monitorato |
| OI-002 | P0 | OPEN | duplicati antifrode storici | migrazione audit-preserving verificata su copia e produzione |
| OI-003 | P0 | OPEN | credenziali esposte nel canale operativo | rotazione e revoca confermate, senza valori nel report |
| OI-004 | P1 | OPEN | semantica esatta dello status RCH | manuale vendor/firmware verificato |
| OI-005 | P1 | OPEN | origine a monte della riga a zero | audit gestionale/configurazione articolo/operatore |
| OI-006 | P1 | OPEN | nessun PCAP direct-vs-proxy indipendente | collaudo autorizzato con cattura ai due lati |
| OI-007 | P1 | OPEN | test hardware quattro dispositivi | finestra autorizzata, verbale e rollback |
| OI-008 | P1 | OPEN | migrazione MariaDB su volume reale | restore staging, upgrade e benchmark |
| OI-009 | P1 | OPEN | batch storici solo-duplicati | correzione, test e politica di archiviazione |
| OI-010 | P1 | OPEN | TLS non attestato end-to-end | certificato, redirect e test browser |
| OI-011 | P2 | OPEN | account DB condiviso API/worker | credenziali/GRANT per servizio |
| OI-012 | P2 | OPEN | anchor hash esterno assente | checkpoint firmato/off-host periodico |
| OI-013 | P2 | OPEN | retention/anonymization senza executor | worker, legal hold e test |
| OI-014 | P2 | OPEN | amministrazione utenti incompleta | reset/revoca/ruoli auditati |
| OI-015 | P2 | OPEN | frontend bundle voluminoso | budget e misura su client reali |

## Elementi non classificati come difetto

- l'assenza di probe RCH durante l'analisi è una misura di sicurezza;
- il PDF ricostruito è un derivato versionato, non sostituisce il RAW;
- un job `PARTIAL` non è perdita nascosta: è una dichiarazione probatoria;
- uno status `UNKNOWN` è preferibile a una semantica inventata.

Per i limiti generali non legati all'incidente vedere
[LIMITI_NOTI.md](LIMITI_NOTI.md).
