# Considerazioni di sicurezza

## Modello di minaccia

Asset principali:

- continuità delle stampe e delle risposte di dispositivo;
- RAW e timeline originali;
- chiavi HMAC, password DB, segreto JWT e credenziali utenti;
- documenti normalizzati, correlazioni, alert e audit;
- disponibilità di spool, database e interfaccia investigativa.

Input non fidati:

- qualunque byte ricevuto da gestionale o stampante;
- file e metadata degli archivi legacy;
- query/body/header HTTP;
- configurazione di sito prima della validazione;
- nomi/percorso presenti nei manifest.

Avversari considerati includono un client di rete non autorizzato, payload
malformati, archivio manomesso, account web con privilegi insufficienti e
operatore host non privilegiato. Un amministratore root completamente ostile
può sostituire codice, dati e chiavi: le catene hash non risolvono tale scenario
senza un anchor esterno.

## Controlli implementati

### Data plane

- relay senza parser/database nel percorso TCP;
- ACL per IP/rete e un solo client per target;
- timeout, backpressure e limiti di connessioni/chunk/coda;
- nessuna esecuzione o interpretazione del payload nel relay;
- separazione utenti systemd POS/RCH dal control plane;
- `RPG_DATABASE_URL` e `RPG_JWT_SECRET_FILE` rimossi dall'ambiente proxy;
- unità hardenizzate con filesystem protetto, namespace/privilegi ridotti e
  sole famiglie socket necessarie.

La capability `CAP_NET_BIND_SERVICE` è necessaria alla route RCH se usa una
porta inferiore a 1024; non abilita configurazione degli indirizzi. L'installer
non modifica IP, route, DNS o firewall.

### Evidenze

- file regolari e `O_NOFOLLOW` dove disponibile;
- pubblicazione atomica e `fsync`;
- SHA-256 per payload/file/manifest;
- catene per timeline e record sensibili;
- HMAC per ledger printproxy quando disponibile;
- reader con containment, limiti e controllo pre/post;
- raw e interpretazione separati, con versioning del parser.

Il formatter JSON applicativo ammette solo campi extra espliciti, limita
profondità/lunghezza, rappresenta i byte come lunghezza+SHA-256 e redige pattern
comuni di credenziali. La coda di log del relay è bounded e non bloccante: una
perdita di record di log non deve diventare una perdita di disponibilità del
data plane.

I due data plane usano account distinti
`retailprintguard-pos-proxy`/`retailprintguard-rch-proxy`, con accesso allo
spool tramite gruppo condiviso. Ingestion usa `retailprintguard-worker`: lo
spool canonico è dichiarato `ReadOnlyPaths`, mentre scritture sono ammesse solo
su archive, state e log worker. Nessuno dei proxy appartiene al gruppo DB.

I processi control plane condividono al momento l'account MariaDB DML
`retailprintguard_app`; l'account di migrazione è effimero e i proxy non hanno
credenziali DB. Separare in futuro API/worker e backup per tabelle/operazioni
ridurrebbe ulteriormente il blast radius.

Questi controlli sono tamper-evident. Per rendere utile la rilevazione occorre
spedire periodicamente head, audit e backup verso storage separato, immutabile o
con retention amministrativa indipendente.

### API e identità

- Argon2 per password;
- JWT HS256 con issuer, audience e scadenza;
- RBAC server-side;
- blocco temporaneo dell'account web dopo cinque fallimenti per cinque minuti e
  throttle locale al processo;
- verifica Argon2 fittizia per ridurre differenze temporali quando l'utente non
  esiste;
- query SQLAlchemy parametrizzate;
- audit hash-chained su azioni API sensibili, bootstrap e attivazione parser;
  quest'ultima proviene da una CLI di sistema e registra actor nullo;
- bootstrap locale del solo primo amministratore, password via doppio prompt,
  policy 14–1.024 caratteri/tre classi e audit hash-chained;
- correlation ID e messaggi di errore bounded;
- security header e CORS allowlist.

Il token in-memory del frontend non persiste al refresh. TLS/HSTS, limite
richieste e rate limit condiviso sono responsabilità del reverse proxy
autorizzato.

L'API non autentica tramite cookie: richiede un bearer token esplicito, quindi
la protezione CSRF basata su token/cookie non è applicabile al flusso corrente.
Se in futuro si introducono cookie di sessione, serviranno `SameSite`, flag
`Secure`/`HttpOnly` e un controllo CSRF dedicato prima del rilascio.

`retailprintguard-admin` non accetta password come parametro o environment e si
rifiuta quando esiste già un utente. Va eseguito localmente con accesso al file
DB protetto; il nome utente viene normalizzato case-insensitive e un advisory
lock MariaDB impedisce a due host di completare contemporaneamente il primo
bootstrap. Il workflow successivo di gestione utenti non deve riutilizzare il
bootstrap.

## Segreti

I segreti non devono apparire in Git, log, CLI, ticket o screenshot.

Percorsi di installazione:

- `/etc/retailprintguard/database.password`: root `0600`;
- `/etc/retailprintguard/database.env`: root/gruppo DB `0640`;
- `/etc/retailprintguard/jwt.secret`: root/gruppo API `0640`;
- chiave HMAC printproxy: file regolare non symlink, almeno 32 byte.

La stringa DB nell'env contiene la password. Non eseguire `systemctl show
--property=Environment` in output condivisi e non usare `set -x` negli script.
La rotazione richiede aggiornamento atomico del file e riavvio dei soli servizi
control plane.

## Rete

- MariaDB è configurata su `127.0.0.1`;
- API su `127.0.0.1:8080`;
- nginx espone intenzionalmente la UI su `0.0.0.0:8081`;
- il firewall deve limitare TCP/8081 alla sola rete amministrativa autorizzata;
- ACL applicativa e firewall devono limitare i gestionali autorizzati;
- la pubblicazione definitiva richiede reverse proxy HTTPS approvato; il bind
  pubblico IPv4 da solo non fornisce cifratura o controllo perimetrale.

Non usare gli indirizzi RFC 5737 dell'esempio. Gli IP reali vengono mantenuti
solo nella configurazione locale protetta.

## Backup

Il backup operativo contiene dump DB, evidenze e copie di configurazione/segreti
con permessi `0600`, ma il formato `.tar.gz` **non è cifrato**. Il file deve
essere trasferito su storage cifrato e accessibile solo agli amministratori.
La copia fuori sito deve avere politica di cancellazione, test restore e log di
accesso.

## Privacy e minimizzazione

RAW, testo, tavoli, operatori e pagamenti possono contenere dati personali o
commerciali. Prima del go-live definire:

- base giuridica e informativa;
- ruoli autorizzati e separazione dei compiti;
- retention distinta per raw, normalizzato, audit e backup;
- procedura di accesso/esportazione/cancellazione compatibile con obblighi
  fiscali e investigativi;
- pseudonimizzazione degli operatori quando l'identità completa non serve;
- divieto di payload nei log tecnici.

`RETENTION_DAYS=0` significa conservazione indefinita finché non interviene una
procedura approvata; non è un default privacy-neutro.

## Logging sicuro

I log strutturati devono contenere solo identificativi, contatori e messaggi
bounded. Non abilitare hexdump/payload in produzione. `diagnose.sh` produce un
report bounded e payload-free, ma i log possono comunque rivelare endpoint e
identificativi: revisionarli prima di inviarli fuori organizzazione.

## Hardening ancora richiesto al sito

- TLS con certificati e rinnovo gestito;
- firewall host/network e segmentazione VLAN;
- NTP affidabile e monitorato;
- storage cifrato, quota e allarme disco;
- export dei log/head verso un dominio amministrativo separato;
- vulnerability/patch management;
- revisione periodica utenti/ruoli;
- test di restore e incident response;
- validazione Debian 12/systemd 252 e NIC reali.

## Risposta a un sospetto incidente

1. Non modificare o cancellare lo spool.
2. Annotare UTC, release, device/session/job e correlation ID.
3. Creare backup verificato; proteggere hash e supporto separatamente.
4. Limitare l'accesso web, senza spegnere il relay se la stampa deve continuare.
5. Esportare log e head senza payload non necessario.
6. Analizzare copie read-only; documentare ogni comando e custode.
7. Ruotare credenziali solo con piano che non distrugga la verificabilità degli
   HMAC storici.

Un alert applicativo è un indizio tecnico, non una conclusione disciplinare o
legale automatica.
