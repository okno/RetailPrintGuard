# Rapporto di validazione finale

**Stato:** gate software offline completato. La release è pubblicabile e
installabile in staging; l'attivazione sui dispositivi reali resta subordinata
al collaudo controllato descritto nel piano di deployment.

## Release 0.3.0

Il candidato `codex/pos-semantic-parser` introduce il parser ESC/POS `1.2.0`,
la correlazione `rpg-correlation-1.2.0` e la revisione Alembic
`b7631f2a9c4e`. Il gate finale ha prodotto `135 passed`, `24 skipped` e zero
fallimenti; Ruff, compileall, quattro test migrazione, Bash/ShellCheck, privacy
scan, lint, TypeScript, cinque test frontend e build Vite sono passati.

Una verifica operativa read-only su un campione POS autorizzato ha confermato
job completi, hash/timeline coerenti, nessun drop o errore di forwarding e
attribuzione corretta ai tre device configurati. I valori operativi e i RAW
non sono pubblicati. Questa verifica non sostituisce un confronto PCAP
direct-vs-proxy né dimostra la semantica interna delle stampanti.

Il tag immutabile previsto è `v0.3.0`; la migrazione è append-only e conserva
RAW e versioni parser precedenti. L'installazione della release e il reparse
storico restano due change separate con backup e checkpoint intermedi.

## Addendum hotfix 0.2.1

Il gate successivo alla pubblicazione di `0.2.0` corregge il backup eseguito
dall'unità systemd senza ampliare le capability. Le copie spool/archive non
replicano UID/GID e normalizzano i mode di staging a `0750/0640`; il restore
continua ad applicare identità locali verificate. La suite completa resta verde
con `123 passed`, `24 skipped` e nessun fallimento. Questo addendum non modifica
il requisito di collaudo hardware indicato nella decisione originale.

## Identità candidata

| Campo | Valore |
|---|---|
| branch di lavoro | `codex/incident-20260814-forensics` |
| baseline sorgente | release `0.1.9` |
| versione candidata | release `0.2.0` |

Il tag previsto è `v0.2.0`; il suo commit viene identificato direttamente dal
tag annotato pubblicato, evitando un hash anticipato nel file sorgente. La
nuova revisione Alembic è `8d4c2a91f7b0`.

## Gate

| Gate | Stato | Evidenza da registrare |
|---|---|---|
| secret/privacy scan | PASS | 208 candidati; nessun segreto, foto, RAW, DB, archivio o IP operativo |
| Python lint/type/test | PASS | 123 pass, 24 skip motivati, 0 failure; Ruff e compileall PASS |
| frontend lint/test/build | PASS | ESLint, TypeScript, 5 Vitest e build Vite; bundle principale 542,19 kB |
| shell syntax | PASS | `bash -n` su 23 script |
| ShellCheck | PASS | ShellCheck 0.10.0 su tutti gli script |
| link Markdown | PASS | link mancanti = 0 |
| diff/whitespace | PASS | `git diff --check` |
| replay offline campione sintetico | PASS tramite suite | relay/canonical/parser isolati su loopback; nessun apparato raggiunto |
| integrità campione privato | PASS LIMITATO | bundle verificato; valori non pubblicati |
| migrazioni | PASS offline/SQLite | upgrade/downgrade, dedup e DDL MariaDB 3/3; MariaDB con volume reale non eseguita |
| deduplica storica | PASS sintetico | righe preservate, duplicate collegate al canonico, nessuna cancellazione |
| browser/RBAC/download | PASS LIMITATO | login/dashboard/menu locale; API RBAC e RAW/TXT/JSON/PDF testate; hardware escluso |
| deployment produzione | NON ESEGUITO | change approvata |
| collaudo hardware | NON ESEGUITO | verbale separato |

## Risultati forensi già supportati

- nel campione analizzato non sono state rilevate mutazioni, perdite, reorder o
  replay attribuibili al proxy;
- i tentativi RCH annullati derivano da sessioni client distinte;
- il client invia la riga a zero prima dello status d'errore;
- il significato preciso dello status non è attribuito senza manuale vendor;
- il differenziale economico ricostruito è 35,00 €→5,00 €;
- il worker antifrode pre-correzione produceva duplicati e il solo servizio
  antifrode è stato fermato come contenimento;
- non sono stati eseguiti probe, test hardware, modifiche rete o restart proxy.

## Correzioni candidate

- reverse tail idle-resetting e abort sincrono del trasporto su errore;
- timeline con ordine di completion e `observed_sequence` separata;
- capture non bloccante e recovery `PARTIAL` corretta;
- parser RCH/ESC-POS per quantità, copia/IVA, status, annullo e cut legacy;
- chiusura economica distinta dal fiscale e diff post-preconto;
- fingerprint antifrode stabile e test di doppia esecuzione;
- eliminazione dei batch vuoti/solo-duplicati;
- API/UI con paginazione, download auditato, error handling e cache auth;
- renderer PDF deterministico/versionato e test di segmentazione su cinque
  partizioni;
- documentazione e script operativi offline/dry-run.

L'elenco è una dichiarazione del worktree candidato, non di produzione.

## Limitazioni e rischi

Vedere [OPEN_ISSUES.md](OPEN_ISSUES.md) e
[SECURITY_REVIEW.md](SECURITY_REVIEW.md). In particolare restano aperti
deployment, migrazione su copia realistica, semantica vendor e collaudo hardware.

## Decisione

`GO` per commit, push, tag `v0.2.0` e staging offline. `NO-GO` per l'attivazione
sul traffico reale finché non siano completati backup/restore, migrazione su
copia del MariaDB operativo, verifica configurazione e collaudo direct-vs-proxy
con operatore presente e rollback pronto. La trasparenza attestata è quella del
payload applicativo, non una trasparenza TCP/IP di livello rete.
