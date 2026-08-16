# Rapporto di validazione finale

**Stato:** gate software offline completato. La release è pubblicabile e
installabile in staging; l'attivazione sui dispositivi reali resta subordinata
al collaudo controllato descritto nel piano di deployment.

## Release 0.4.1 — temi web e aggiornamento senza restart dei relay

Il candidato `codex/web-ui-themes` aggiunge quattro temi persistenti e un
updater riutilizzabile che lavora esclusivamente da tag Git annotati. Il gate
finale del 16 agosto 2026 ha prodotto `187 passed`, `26 skipped` e zero
failure; Ruff, `git diff --check`, ESLint, TypeScript, 23 test frontend,
`bash -n` e ShellCheck sono passati.

Il diff della closure data-plane rispetto a `v0.4.0` è vuoto. L'updater
mantiene un lock unico, costruisce il frontend prima del backup, invoca
`--control-plane-only` e verifica in ogni uscita PID, invocation ID, timestamp
di avvio, stato e listener dei proxy POS/RCH. Non contiene rollback o fallback
che possa fermarli. Decisione: `GO` per commit, tag `v0.4.1` e pubblicazione;
la build Vite finale è passata (`11783` moduli, solo warning non bloccante sul
chunk principale) e viene comunque ripetuta dall'updater su Debian come
pre-gate fail-closed prima di qualsiasi attivazione.

## Release 0.4.0 — antifrode per episodi di vendita

Il candidato `codex/antifraud-sale-episodes` introduce correlazione e antifrode
basate sull'episodio di vendita, prezzi POS derivati con provenienza, filtri
temporali, PDF comanda compatto e revisione auditata dei job incompleti. La
revisione non elimina mai RAW, manifest, documenti o storico degli alert.

Il gate finale del 15 agosto 2026 ha prodotto `186 passed`, `25 skipped` e zero
failure. Gli skip sono 23 test di sintassi Bash e un test POSIX del gate
control-plane non disponibili al Python runner Windows, oltre a un test symlink
condizionale. Ruff, compileall, migrazioni Alembic incluse nel full test,
`git diff --check`, ESLint, TypeScript e 18 test frontend sono passati. Il wheel
`retailprintguard-0.4.0` è stato costruito e contiene tutti i 10 entry point.
Il privacy scan ha ispezionato 231 file senza trovare segreti o evidenze private.

`bash -n` è passato sui 23 script tramite Git Bash. ShellCheck non era
disponibile su questo host e non viene dichiarato come PASS. Anche la build Vite
locale è rimasta bloccata senza errore nel runtime Windows: `pnpm build` sul
server Linux è pertanto un gate obbligatorio prima dello staging. La QA visuale
del PDF sintetico ha verificato layout 80 mm, portate, prezzi derivati e
conflitti senza clipping.

Il diff rispetto a `v0.3.1` è vuoto per proxy, configurazione/logging condivisi,
lock runtime e unità POS/RCH. `--control-plane-only` verifica questa closure
prima di pacchetti e DDL, mantiene i due processi proxy e controlla che entrambi
i PID restino invariati. Decisione: `GO` per commit, tag e pubblicazione del
sorgente; attivazione produzione soltanto dopo build Linux, backup verificato e
preflight documentato.

## Hotfix 0.3.1

Il candidato `codex/web-documents-hotfix` corregge il contratto delle rotte
frontend, la diagnostica spool, la selezione della versione parser attiva e la
presentazione leggibile dei documenti. Le risposte di protocollo RCH restano
evidenze consultabili ma sono separate dalla lista operativa; job distinti non
sono deduplicati né cancellati.

Il gate backend ha prodotto `141 passed`, `24 skipped` e zero fallimenti;
Ruff, compileall e controllo diff sono passati. ESLint, TypeScript e i dieci
test frontend sono passati. La build Vite deve essere eseguita e verificata sul
server Linux prima dello switch della release, perché il runtime Node Windows
locale si è fermato senza errore durante la trasformazione. Il reparse storico
rimane un'operazione append-only separata dall'aggiornamento applicativo.

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
