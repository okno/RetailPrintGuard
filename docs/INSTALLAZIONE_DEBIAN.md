# Installazione e aggiornamento Debian

## Stato di supporto

Gli script accettano Debian 12 e 13. Sono stati sottoposti a controlli statici,
ma l'installazione completa sul target Debian 12, systemd 252, NIC e stampanti
reali non è attestata. Prima del go-live chiudere i blocker in
[LIMITI_NOTI.md](LIMITI_NOTI.md).

## Prerequisiti

- accesso root autorizzato;
- configurazione di sito con soli IPv4 privati approvati;
- tutti i listener già assegnati persistentemente al server;
- repository sorgente su commit verificato;
- frontend `frontend/dist/index.html` precompilato;
- `requirements/production.lock` con hash presente;
- `requirements/build.lock` con hash presente per il backend di packaging;
- backup/rollback e finestra operativa;
- nessun altro servizio in ascolto sugli endpoint proxy.

L'installer non aggiunge o rimuove IP, route, DNS o firewall. Se un listener non
è assegnato, si ferma con errore.

## Preflight in staging

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src
./scripts/test_ops.sh
```

Build frontend su una macchina con Node.js e pnpm disponibili nel PATH:

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
cd ..
```

Validare il sito senza cambiare rete:

```bash
PYTHONPATH=src python3 scripts/validate_site_config.py \
  --config /root/retailprintguard-site.yaml \
  --require-deployment-layout \
  --require-assigned-listeners
```

Il validatore rifiuta RFC 5737, IP non privati, layout diverso da quello delle
unità e listener non presenti in `ip -j -4 address show`.

## Prima installazione prudente

Se sul server sono presenti `printproxy` o `commercialRCHproxy`, non attivare
questa installazione sopra i loro listener. Eseguire prima il runbook
[Migrazione dai proxy standalone](MIGRAZIONE_DA_LEGACY.md), inclusi handover
persistente degli IP, backup e pulizia non distruttiva del runtime legacy.

Preparare inizialmente senza attivazione:

```bash
sudo ./scripts/install.sh \
  --config /root/retailprintguard-site.yaml \
  --no-start
```

Lo script:

- installa dipendenze Debian;
- crea utenti/gruppi separati;
- prepara `/etc`, `/opt`, `/var/lib`, `/var/log`, `/var/backups` e `/var/www`;
- crea una release content-addressed e virtualenv da lock hashato;
- configura MariaDB loopback, DB e account DML;
- applica Alembic con account DDL effimero;
- genera segreto JWT e password DB locali;
- crea la release frontend content-addressed;
- con `--no-start` registra `staged-release` e `staged-web-release`, ma lascia
  invariati i symlink `current`, non installa/abilita unità o nginx e non avvia
  processi;
- senza `--no-start` attiva insieme codice e frontend, installa/verifica
  systemd/nginx/logrotate, abilita il target e il timer backup e riavvia i
  servizi.

Attenzione: `--no-start` evita l'attivazione dei binari, ma prepara comunque
MariaDB e applica le migrazioni. Su un aggiornamento, lo schema nuovo deve
quindi essere backward-compatible con la release ancora attiva.

Non usare `--allow-unlocked` in produzione. `--replace-config` sostituisce
esplicitamente la configurazione installata; senza tale flag quella esistente è
preservata.

## Gate dello staging

Con `--no-start`, `/opt/retailprintguard/current` continua a indicare la release
precedente oppure non esiste alla prima installazione. Leggere i path staged
senza eseguirne il contenuto:

```bash
sudo sed -n '1p' /var/lib/retailprintguard/state/staged-release
sudo sed -n '1p' /var/lib/retailprintguard/state/staged-web-release
```

Verificare che il primo path sia sotto `/opt/retailprintguard/releases`, abbia
`.release-complete` e che il secondo sia sotto
`/var/www/retailprintguard/releases` con `index.html`. Eseguire `--help` e il
check configurazione usando il path staged esplicito; non usare `current`, che
in questa fase identifica intenzionalmente la release vecchia.

Per attivare dopo il gate, rieseguire l'installer dalla stessa sorgente senza
`--no-start`:

```bash
sudo ./scripts/install.sh
```

Alla prima esecuzione il file sito è già preservato in `/etc`; non serve
ripassare `--config` se non si intende sostituirlo.

## Gate dopo l'attivazione

```bash
sudo systemd-analyze verify /etc/systemd/system/retailprintguard*.service \
  /etc/systemd/system/retailprintguard*.timer \
  /etc/systemd/system/retailprintguard.target
sudo nginx -t
sudo /opt/retailprintguard/current/scripts/status.sh --json
```

Verificare inoltre che ogni eseguibile referenziato da `ExecStart` risponda a
`--help`. Il wrapper ingestion rileva `--canonical-root` e usa lo spool
canonico. Parser, correlazione e antifrode dispongono di entry point DB reali;
la loro presenza non sostituisce i collaudi MariaDB e hardware elencati in
`LIMITI_NOTI.md`.

L'installer senza `--no-start` ha già abilitato target/timer e riavviato i
servizi. I comandi seguenti sono utili per una ripresa manuale, non sono
normalmente necessari subito dopo un'installazione riuscita:

```bash
sudo systemctl enable retailprintguard.target nginx.service
sudo systemctl start retailprintguard-pos-proxy.service
sudo systemctl start retailprintguard-rch-proxy.service
sudo systemctl start retailprintguard-ingestion.service
sudo systemctl start retailprintguard-parser.service
sudo systemctl start retailprintguard-correlation.service
sudo systemctl start retailprintguard-fraud.service
sudo systemctl start retailprintguard-api.service
sudo systemctl reload nginx.service
```

Nginx espone la webapp su `0.0.0.0:8081`; aprire la porta esclusivamente dalla
rete amministrativa autorizzata. FastAPI resta confinata su
`127.0.0.1:8080`. Prima dell'uso ordinario configurare HTTPS tramite reverse
proxy approvato e una regola firewall LAN esplicita.

## Aggiornamento

Nel clone della nuova release:

```bash
git fetch --all --tags
git checkout <commit-o-tag-approvato>
python -m pytest -q
python -m ruff check src
cd frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
cd ..
sudo ./scripts/update.sh
```

`update.sh` crea prima un backup e richiama l'installer idempotente. La nuova
release è installata accanto alla precedente e selezionata con symlink atomico.
La configurazione di sito resta preservata.

Per preparare senza riavvio:

```bash
sudo ./scripts/update.sh --no-start
```

## Rollback codice e frontend

Rollback atomico dell'applicazione e del frontend abbinato alla release
precedente:

```bash
sudo /opt/retailprintguard/current/scripts/rollback.sh
```

Oppure a una release installata esplicita, il cui nome è un ID esadecimale di
20 caratteri:

```bash
sudo /opt/retailprintguard/current/scripts/rollback.sh --release <release-id>
```

Lo script richiede la mappatura frontend registrata, sposta entrambi i symlink e
riavvia i servizi abilitati; in caso di errore tenta di ripristinare entrambi i
link. Il database non viene downgradato. Se lo schema non è
backward-compatible, usare un backup esplicito e la procedura restore
autorizzata.

## Uninstall non distruttivo

```bash
sudo /opt/retailprintguard/current/scripts/uninstall.sh
```

Disabilita servizi e integrazione nginx/logrotate ma preserva configurazione,
segreti, dati, database, backup e release. `--remove-code` elimina solo le
directory codice/web gestite; dati e DB restano comunque preservati.

## Percorsi installati

| Percorso | Contenuto |
|---|---|
| `/etc/retailprintguard` | config, env DB, segreti, hash conferma revisione, sorgenti ingestion |
| `/opt/retailprintguard/releases` | release applicative immutabili |
| `/opt/retailprintguard/current` | symlink release attiva |
| `/var/lib/retailprintguard/spool` | catture canoniche |
| `/var/lib/retailprintguard/archive` | archivi gestiti/legacy |
| `/var/lib/retailprintguard/state` | stato lifecycle |
| `/var/log/retailprintguard` | eventuali file log applicativi |
| `/var/backups/retailprintguard` | backup root-only |
| `/var/www/retailprintguard/current` | frontend attivo |

## Primo utente

Dopo le migrazioni e prima di consentire l'accesso UI, eseguire localmente:

```bash
sudo systemd-run --wait --pipe --collect --pty \
  --unit=retailprintguard-admin-bootstrap \
  --property=Type=oneshot \
  --property=User=retailprintguard-worker \
  --property=Group=retailprintguard-worker \
  --property=EnvironmentFile=/etc/retailprintguard/database.env \
  /opt/retailprintguard/current/.venv/bin/retailprintguard-admin \
  --config /etc/retailprintguard/config.yaml \
  --username <username-amministratore> \
  --display-name '<nome visualizzato>'
```

La password viene richiesta due volte tramite prompt, non come argomento. Deve
avere 14–1.024 caratteri e almeno tre classi tra minuscole, maiuscole, cifre e
simboli; viene salvata con Argon2id. Il comando crea i quattro ruoli, assegna
solo `ADMIN` al primo utente e aggiunge `ADMIN_BOOTSTRAPPED` a una catena audit.
Un lock di processo e, su MariaDB, un advisory lock connection-scoped
serializzano bootstrap concorrenti. Si rifiuta se esiste già qualunque utente:
non è un comando per aggiungere un secondo amministratore o reimpostare
password.

Non usare SQL manuale e non inserire la password in file, command line, log o
ticket. Verificare poi login e audit senza copiare il token in output condivisi.

## Password di conferma per i job incompleti

La revisione/esclusione di un job incompleto richiede, oltre a un account
`ADMIN`, un segreto dedicato. Configurarlo con doppio prompt locale:

```bash
sudo /opt/retailprintguard/current/.venv/bin/retailprintguard-configure-review
sudo systemctl restart retailprintguard-api.service
```

Il comando accetta soltanto il path gestito
`/etc/retailprintguard/review.env`, rifiuta symlink, scrive atomicamente il solo
hash Argon2id e assegna root/gruppo API con mode `0640`. La password in chiaro
non va passata come argomento o memorizzata in YAML. Senza questa configurazione
l'endpoint di revisione fallisce chiuso; proxy e forwarding non sono coinvolti.
