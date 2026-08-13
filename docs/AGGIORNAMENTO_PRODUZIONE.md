# Aggiornamento e rollback in produzione

## Strategia

Le release applicative sono installate in directory content-addressed sotto
`/opt/retailprintguard/releases`; il symlink `current` viene cambiato
atomicamente. Il frontend usa lo stesso schema in `/var/www`. Configurazione,
spool, database e backup non sono dentro la release.

## 1. Preparare la release fuori produzione

```bash
git fetch --all --tags
git checkout <commit-o-tag-approvato>
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src
./scripts/test_ops.sh
cd frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build
cd ..
```

I comandi frontend richiedono Node.js e pnpm disponibili nel PATH della
macchina di build; il server Debian riceve la directory `dist` già costruita.

Verificare che `requirements/production.lock` appartenga allo stesso commit e
che tutti gli `ExecStart` delle unità abbiano un modulo/entry point reale.
Verificare inoltre `retailprintguard-admin --help`: non eseguirlo durante un
update, perché il bootstrap è riservato al database senza utenti.

## 2. Preflight sul server

```bash
sudo /opt/retailprintguard/current/scripts/status.sh --json
sudo /opt/retailprintguard/current/scripts/backup.sh
sudo systemctl --failed --no-pager
df -h /opt /var/lib/retailprintguard /var/lib/mysql /var/backups
```

Registrare release corrente, backup prodotto e hash. Non procedere con spool in
crescita non spiegata, DB degradato o backup non verificabile.

## 3. Installare senza riavvio

Dal clone della nuova release:

```bash
sudo ./scripts/update.sh --no-start
```

Questo crea un ulteriore backup, prepara release/virtualenv/frontend e applica
le migrazioni, ma non cambia i symlink `current`, non installa nuove unità/nginx
e non avvia o riavvia servizi. La release attiva continua a funzionare: lo
schema migrato deve quindi restarle compatibile.

I file
`/var/lib/retailprintguard/state/staged-release` e `staged-web-release`
contengono i path preparati. Verificare che restino nelle directory gestite e
usare il path staged esplicito per `--help`/`--check-config`; i comandi sotto
`/opt/retailprintguard/current` controllerebbero ancora la versione vecchia.
Eseguire inoltre sui file della nuova sorgente:

```bash
sudo systemd-analyze verify ./systemd/retailprintguard*.service \
  ./systemd/retailprintguard*.timer \
  ./systemd/retailprintguard.target
sudo nginx -t
sudo <staged-release-path>/.venv/bin/retailprintguard-proxy \
  --config /etc/retailprintguard/config.yaml --check-config
sudo <staged-release-path>/.venv/bin/alembic \
  -c <staged-release-path>/alembic.ini current
```

L'ultimo comando richiede `RPG_DATABASE_URL`; usarlo nell'ambiente protetto del
servizio o caricare `/etc/retailprintguard/database.env` senza stamparne il
contenuto. `nginx -t` controlla ancora la configurazione attiva; la verifica sui
file `./systemd` copre sintassi/dependenze ma gli unit installati restano quelli
vecchi. Il gate completo della nuova integrazione avviene durante l'attivazione.

Quando lo staging è approvato, dalla stessa sorgente eseguire:

```bash
sudo ./scripts/update.sh
```

Questo produce un nuovo backup, attiva insieme applicazione e frontend,
installa/verifica le integrazioni e riavvia i servizi. Non esiste un comando
separato che attivi implicitamente i file staged.

## 4. Attivazione e ripresa

`update.sh` senza `--no-start` riavvia anche entrambi i proxy. Eseguirlo soltanto
in finestra autorizzata e senza sessioni di stampa attive. Non ripetere subito i
restart dopo un update riuscito. Se un singolo componente control plane resta
in errore, si può riprendere separatamente:

```bash
sudo systemctl restart retailprintguard-ingestion.service
sudo systemctl restart retailprintguard-parser.service
sudo systemctl restart retailprintguard-correlation.service
sudo systemctl restart retailprintguard-fraud.service
sudo systemctl restart retailprintguard-api.service
sudo systemctl reload nginx.service
```

Verificare health/UI. Riavviare manualmente una famiglia proxy soltanto se il
suo stato lo richiede e dopo aver nuovamente escluso sessioni attive:

```bash
sudo systemctl restart retailprintguard-pos-proxy.service
sudo systemctl restart retailprintguard-rch-proxy.service
```

Eseguire una stampa sintetica autorizzata per route, controllando byte ricevuti,
risposta RCH, `.ready`, import e UI. Un restart proxy può produrre un job
`PARTIAL`; non presentarlo come completo.

Se la release modifica un parser, non lanciare automaticamente un reparse
globale durante l'update. Confrontare prima un campione e seguire
[AGGIORNAMENTO_PARSER.md](AGGIORNAMENTO_PARSER.md); il normale worker elabora i
nuovi job, mentre `--once --reparse-all` è un'operazione amministrativa
esplicita.

## 5. Verifica post-update

```bash
sudo /opt/retailprintguard/current/scripts/status.sh --json
sudo /opt/retailprintguard/current/scripts/diagnose.sh \
  > /root/retailprintguard-post-update.txt
```

Confrontare contatori, backlog, ultimi errori e release. Conservare il report e
il suo SHA-256 nel ticket di change.

## Rollback applicazione e frontend

Se il nuovo codice è incompatibile ma lo schema DB resta backward-compatible:

```bash
sudo /opt/retailprintguard/current/scripts/rollback.sh
```

Lo script seleziona atomically sia la release applicativa sia il frontend
registrato per quella release e riavvia i servizi abilitati. Se uno switch o il
restart fallisce tenta di ripristinare entrambi i link. Non esegue downgrade del
database.

## Rollback dati/schema

Solo con backup esplicito e change approvato:

```bash
sudo /opt/retailprintguard/current/scripts/restore.sh \
  --archive /var/backups/retailprintguard/<backup>.tar.gz \
  --confirm-destructive-database-restore
```

Il restore elimina e ricrea il database applicativo; vedere
[BACKUP_RESTORE_DR.md](BACKUP_RESTORE_DR.md). I proxy restano operativi e
accumulano spool mentre il control plane è fermo.

## Aggiornamento configurazione

`update.sh` preserva `/etc/retailprintguard/config.yaml`. Per cambiare endpoint o
ACL:

1. preparare e validare un file nuovo;
2. salvare backup e diff senza segreti;
3. usare `install.sh --config FILE --replace-config --no-start`;
4. verificare listener e target;
5. riavviare una route alla volta.

Non modificare listener e target durante una sessione attiva e non riutilizzare
un IP finché il precedente host può ancora rispondere.
