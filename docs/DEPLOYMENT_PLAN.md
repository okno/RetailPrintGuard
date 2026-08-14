# Piano di deployment controllato

Questo piano non autorizza da solo un intervento in produzione. La fase di
analisi non ha effettuato restart dei proxy, probe hardware o modifiche di rete.

## 1. Prerequisiti

- branch revisionato, commit firmato/identificato e secret scan verde;
- credenziali esposte ruotate;
- suite Python/frontend e test offline verdi;
- migrazione provata su restore del DB;
- capacità disco/spool verificata;
- backup root-only copiato e verificato su supporto separato;
- configurazione validata senza aprire socket;
- finestra approvata e responsabile rollback presente;
- destinazioni fisiche annotate in un registro privato.

## 2. Build e staging offline

```bash
git fetch --tags origin
git checkout --detach <COMMIT_APPROVATO>
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
./scripts/run_tests.sh
./scripts/check_secrets.sh

cd frontend
corepack enable
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
```

Non usare credenziali sulla command line e non sostituire `<COMMIT_APPROVATO>`
con un branch mobile nel runbook finale.

## 3. Backup e prova restore

Su produzione, in finestra autorizzata:

```bash
sudo /opt/retailprintguard/current/scripts/backup.sh
sudo sha256sum -c /percorso/sidecar-verificato.sha256
sudo tar -tzf /percorso/backup-verificato.tar.gz >/dev/null
```

La prova restore va eseguita su host/DB isolato, non sopra la produzione. Il
backup contiene segreti: permessi `0600` e cifratura esterna.

## 4. Staging della release senza avvio

Dal checkout approvato e con frontend già costruito:

```bash
sudo ./scripts/update.sh --frontend-dir ./frontend/dist --no-start
```

Attenzione: `--no-start` non riavvia servizi né commuta i symlink, ma
l'installer applica le migrazioni DB. Va quindi usato solo dopo backup e prova
migrazione, nella finestra approvata. Verificare `staged-release`, entrypoint,
permessi e revisione Alembic.

## 5. Pre-cutover

```bash
sudo /opt/retailprintguard/current/scripts/status.sh --json
sudo /opt/retailprintguard/current/scripts/healthcheck.sh
sudo systemd-analyze verify /etc/systemd/system/retailprintguard*.service
sudo nginx -t
```

Verificare che non vi siano sessioni di stampa attive. Non inviare richieste
vuote al target RCH: non sono documentate come inerti.

Il worker antifrode resta fermo finché la release correttiva non supera il test
di doppia esecuzione e la conciliazione dei duplicati storici.

## 6. Attivazione

L'attivazione usa l'installer senza `--no-start` e comporta restart delle unità,
inclusi i proxy. Eseguirla solo con autorizzazione esplicita e quiescenza:

```bash
sudo ./scripts/install.sh --frontend-dir ./frontend/dist
```

Non modificare IP/firewall durante questo passaggio. La persistenza dei listener
è un prerequisito di rete separato.

## 7. Verifica immediata

1. tutti i servizi attesi `active`, salvo decisioni di contenimento registrate;
2. quattro listener attribuiti ai due processi corretti;
3. MariaDB loopback-only;
4. API health e login dalla rete amministrativa;
5. spool stabile, nessun errore di permission e nessuna crescita di alert/batch
   a dati invariati;
6. hash/manifest di un nuovo job autorizzato validi;
7. nessun N+1/timeout nelle liste principali.

Il collaudo fisico usa soltanto normali operazioni autorizzate dal gestionale e
segue [TEST_PLAN.md](TEST_PLAN.md#collaudo-hardware-separato).

## 8. Monitoraggio

Sorvegliare per almeno una finestra operativa completa:

- restart count e connessioni proxy;
- errori write/drain/capture;
- dimensione e oldest age dello spool;
- batch inserted/duplicate/failed;
- documenti parser failed/unknown;
- correlazioni e confidenza;
- alert evaluated/inserted/duplicate;
- spazio disco, DB e latenza API.

## 9. Stop condition

Rollback immediato se compaiono mutazione/perdita byte, listener assente,
forwarding bloccato, spool corrotto, crescita duplicati o migrazione incoerente.
Non fare replay automatico di job con esito remoto ignoto.
