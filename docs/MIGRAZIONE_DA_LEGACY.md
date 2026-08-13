# Migrazione da commercialRCHproxy e printproxy

Questa procedura sostituisce le installazioni standalone con RetailPrintGuard
senza cancellare le evidenze storiche. È un cambio del data plane: richiede una
finestra di manutenzione, quattro endpoint approvati e una possibilità reale di
riportare il gestionale direttamente alle stampanti fisiche.

## Invarianti e divieti

- Non interrompere una sessione TCP o una stampa in corso.
- Non avviare mai proxy legacy e RetailPrintGuard sugli stessi listener.
- Non cancellare RAW, spool, chiave HMAC, configurazioni o backup legacy.
- Non riprodurre dump verso le stampanti e non usare `telnet`/`nc` come probe RCH.
- Non usare `ip address add` come configurazione permanente.
- Non applicare una configurazione NetworkManager/networkd/ifupdown senza aver
  prima identificato il backend e il profilo realmente associato alla NIC.
- Conservare separatamente la possibilità di impostare nel gestionale i target
  fisici POS e RCH per un rollback di emergenza.

RetailPrintGuard non assegna indirizzi, non cambia route e non installa regole
firewall. Questa separazione impedisce a un aggiornamento applicativo di
riconfigurare accidentalmente la rete del server.

## 1. Congelare e identificare le sorgenti

Annotare commit/tag finali dei due standalone e il commit RetailPrintGuard:

```bash
git -C /srv/printproxy rev-parse HEAD
git -C /srv/commercialRCHproxy rev-parse HEAD
git -C /srv/RetailPrintGuard rev-parse HEAD
```

Non proseguire se un checkout contiene modifiche locali non inventariate.

## 2. Inventario del server

Eseguire come root e conservare l'output fuori dal server:

```bash
install -d -m 0700 /root/retailprintguard-migration
systemctl list-unit-files 'printproxy*' 'commercialrchproxy*' \
  > /root/retailprintguard-migration/legacy-units.txt
systemctl --no-pager --full status \
  printproxy.service commercialrchproxy-dumper.service \
  commercialrchproxy-parser.service \
  > /root/retailprintguard-migration/legacy-status.txt 2>&1 || true
ip -j -4 address show > /root/retailprintguard-migration/ip-address-before.json
ip -j -4 route show table all > /root/retailprintguard-migration/ip-route-before.json
ss -Hltunp > /root/retailprintguard-migration/listeners-before.txt
nft list ruleset > /root/retailprintguard-migration/nft-before.txt 2>&1 || true
```

Leggere, senza copiarli nel repository:

- `/etc/printproxy/printproxy.conf` e `/etc/printproxy/install-state`;
- `/etc/printproxy/integrity.key` — non mostrarne il contenuto;
- `/etc/commercialrchproxy/commercialrchproxy.conf`;
- `/run/commercialrchproxy-secondary-ip/state`, se presente;
- tutti i path `DATA_DIR`, `SPOOL_DIR`, `OUTPUT_DIR` e `LOG_DIR` configurati.

Costruire la matrice approvata:

| Device | Listener del gestionale | Target fisico | Porta | Client ammesso |
|---|---|---|---|---|
| POS 1 | `<IP_VIRTUALE_POS_1>` | `<IP_FISICO_POS_1>` | `<PORTA_POS>` | `<IP_GESTIONALE>/32` |
| POS 2 | `<IP_VIRTUALE_POS_2>` | `<IP_FISICO_POS_2>` | `<PORTA_POS>` | `<IP_GESTIONALE>/32` |
| POS 3 | `<IP_VIRTUALE_POS_3>` | `<IP_FISICO_POS_3>` | `<PORTA_POS>` | `<IP_GESTIONALE>/32` |
| RCH | `<IP_VIRTUALE_RCH>` | `<IP_FISICO_RCH>` | leggere la configurazione installata | `<IP_GESTIONALE>/32` |

Non dedurre il target RCH da fotografie o esempi. La porta tipica osservata è
23, ma il valore autorevole per il sito è quello della configurazione
installata e approvata.

## 3. Preparare la configurazione RetailPrintGuard

```bash
install -d -m 0700 /root/retailprintguard-migration
cp /srv/RetailPrintGuard/config/retailprintguard.example.yaml \
  /root/retailprintguard-migration/site.yaml
chmod 0600 /root/retailprintguard-migration/site.yaml
editor /root/retailprintguard-migration/site.yaml
```

Configurare esattamente tre device `pos` con parser `escpos` e un device `rch`
con parser `rch_observed`. Tutti devono avere `bidirectional: true`, ID stabili,
target univoci e ACL ristrette. Non inserire password nel file.

Prima verifica, senza aprire socket:

```bash
cd /srv/RetailPrintGuard
.venv/bin/retailprintguard-proxy \
  --config /root/retailprintguard-migration/site.yaml --check-config
python3 scripts/validate_site_config.py \
  --config /root/retailprintguard-migration/site.yaml
```

## 4. Rendere persistenti i quattro listener

Identificare prima interfaccia e backend:

```bash
ip -j -4 address show
systemctl is-active NetworkManager.service || true
systemctl is-active systemd-networkd.service || true
networkctl status 2>/dev/null || true
test -r /run/network/ifstate && cat /run/network/ifstate || true
```

Usare **uno solo** dei metodi seguenti, adattato dall'amministratore di rete.

### NetworkManager

Ricavare la connessione realmente associata all'interfaccia e aggiungere i
quattro `<IP>/<PREFISSO>` con `nmcli connection modify <CONNESSIONE>
+ipv4.addresses ...`. Applicare durante la finestra con `nmcli device reapply
<INTERFACCIA>` quando supportato. Verificare che il profilo non cambi gateway,
DNS o indirizzo primario.

### systemd-networkd

Individuare il file `.network` effettivamente selezionato e creare un drop-in
amministrativo con quattro righe `Address=<IP>/<PREFISSO>` nella sezione
`[Network]`. Eseguire `networkctl reload` e il `reconfigure` della sola
interfaccia nella finestra autorizzata.

### ifupdown

Integrare gli indirizzi nel file dell'interfaccia gestito dal sito, usando le
direttive supportate dalla versione Debian installata. Non modificare
automaticamente `/etc/network/interfaces` da uno script applicativo.

Dopo l'applicazione e dopo un reboot di prova:

```bash
ip -j -4 address show
python3 /srv/RetailPrintGuard/scripts/validate_site_config.py \
  --config /root/retailprintguard-migration/site.yaml \
  --require-assigned-listeners --require-deployment-layout
```

## 5. Preparare il firewall sostitutivo

Se `FIREWALL_OWNED=yes` nello state printproxy, il suo uninstall rimuoverà la
tabella `inet printproxy_filter`. Prima del cutover integrare nel firewall
nativo del sito regole che consentano esclusivamente al gestionale autorizzato
di raggiungere:

- i tre listener POS sulla porta configurata;
- il listener RCH sulla porta configurata;
- API/UI soltanto dalla rete amministrativa prevista.

MariaDB deve restare su loopback/socket locale. Applicare e verificare la
policy del sito senza fare `nft flush ruleset`. L'ACL applicativa di ogni device
è una seconda barriera, non sostituisce il firewall host.

## 6. Preparare la nuova release senza attivarla

Costruire il frontend e poi installare in staging:

```bash
cd /srv/RetailPrintGuard/frontend
corepack enable
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build

cd /srv/RetailPrintGuard
sudo ./scripts/install.sh \
  --config /root/retailprintguard-migration/site.yaml \
  --no-start
```

`--no-start` prepara release, ambiente Python, MariaDB e migrazioni, ma non
commuta `current`, non installa unità e non avvia listener.

## 7. Dry-run della pulizia legacy

La copia dell'uninstaller printproxy deve provenire dal repository standalone
congelato. Portarla in un percorso root-only:

```bash
sudo install -m 0700 -o root -g root \
  /srv/printproxy/uninstall.sh \
  /root/retailprintguard-migration/printproxy-uninstall.sh

cd /srv/RetailPrintGuard
sudo ./scripts/cleanup_legacy.sh \
  --printproxy-uninstaller \
    /root/retailprintguard-migration/printproxy-uninstall.sh
```

Il dry-run non ferma servizi e non modifica rete o file. Controllare tutti i
path, gli stati delle unità e gli indirizzi attesi.

## 8. Cutover e backup verificato

1. Bloccare temporaneamente nuove stampe dal gestionale.
2. Attendere che non vi siano sessioni attive; lo script rifiuta processi
   legacy con connessioni TCP ancora aperte.
3. Chiudere/registrare eventuali job `UNKNOWN` o in coda.
4. Accertare che gli IP siano ora proprietà del network manager e che il
   firewall sostitutivo sia pronto.

Eseguire:

```bash
cd /srv/RetailPrintGuard
sudo ./scripts/cleanup_legacy.sh \
  --execute \
  --network-handover-confirmed \
  --firewall-handover-confirmed \
  --printproxy-uninstaller \
    /root/retailprintguard-migration/printproxy-uninstall.sh
```

Omettere `--firewall-handover-confirmed` se il dry-run dichiara esplicitamente
che il legacy non possiede la tabella. Omettere
`--network-handover-confirmed` soltanto se nessun indirizzo risulta posseduto.

Lo script:

- ferma il timer di riconciliazione prima di rimuovere VIP;
- ferma i proxy e verifica il ledger/HMAC printproxy;
- conserva snapshot di unità, journal, IP, route, socket e nftables;
- crea un inventario per-file con SHA-256;
- crea un archivio root-only, ne verifica gzip, indice tar e SHA-256;
- invoca gli uninstaller standalone verificati senza opzioni di purge;
- elimina soltanto gli esatti residui runtime conosciuti;
- verifica che unità, `/opt`, tabella nft legacy e indirizzi attesi siano nello
  stato previsto.

Un errore prima dell'inizio della rimozione riavvia i servizi prima attivi. Un
errore dopo l'inizio della rimozione si ferma e indica l'archivio di recovery:
non tentare restart o replay alla cieca.

Verificare e copiare il bundle su supporto separato/cifrato:

```bash
cd /var/backups/retailprintguard/legacy/<directory-creata>
sha256sum -c legacy-print-proxies.tar.gz.sha256
tar -tzf legacy-print-proxies.tar.gz >/dev/null
```

## 9. Installare e avviare RetailPrintGuard

```bash
cd /srv/RetailPrintGuard
sudo ./scripts/install.sh
sudo systemd-analyze verify /etc/systemd/system/retailprintguard*.service \
  /etc/systemd/system/retailprintguard*.timer \
  /etc/systemd/system/retailprintguard.target
sudo nginx -t
sudo /opt/retailprintguard/current/scripts/status.sh --json
```

Creare il primo amministratore con la procedura interattiva descritta in
[INSTALLAZIONE_DEBIAN.md](INSTALLAZIONE_DEBIAN.md#primo-utente). Non passare la
password sulla command line.

## 10. Collaudo controllato

Riabilitare una route alla volta dal gestionale:

1. POS 1, POS 2 e POS 3: una stampa non fiscale autorizzata ciascuna;
2. RCH: una vera operazione autorizzata dal normale gestionale, verificando
   anche la risposta; niente probe TCP e niente replay;
3. quattro device concorrenti;
4. MariaDB temporaneamente fermo: la stampa deve proseguire e lo spool deve
   essere importato una sola volta alla ripresa.

Per ogni test verificare output fisico, byte RAW nelle due direzioni, `.ready`,
manifest/hash, import batch, documento, timeline e UI. Conservare orari,
operatore e risultato del test.

## 11. Importare lo storico

Usare i path legacy preservati, non il tar come directory di lavoro. Eseguire
prima `--validate-only`, poi l'import persistente, infine ripetere lo stesso
comando: la seconda esecuzione deve produrre solo duplicati.

```bash
retailprintguard-import --config /etc/retailprintguard/config.yaml \
  --printproxy-root /var/lib/printproxy \
  --printproxy-hmac-key-file /etc/printproxy/integrity.key \
  --validate-only --json

retailprintguard-import --config /etc/retailprintguard/config.yaml \
  --rch-root /var/lib/commercialrchproxy/jobs \
  --validate-only --json
```

Consultare [IMPORT_STORICO.md](IMPORT_STORICO.md) per il comando DB e per i
path personalizzati rilevati dalla vecchia configurazione.

## 12. Rollback

Se il collaudo fallisce:

1. bloccare nuove stampe;
2. fermare **solo** i due proxy RetailPrintGuard;
3. riportare temporaneamente nel gestionale ogni destinazione al target fisico
   annotato nella matrice;
4. preservare spool, manifest, journal e PCAP del tentativo;
5. reinstallare il legacy dai tag congelati e ripristinare config/chiave dai
   path preservati o dal bundle verificato;
6. riabilitare un solo data plane;
7. non riprodurre job `UNKNOWN` o RAW catturati.

Il rollback del solo control plane può invece usare
`scripts/rollback.sh` senza toccare i proxy, se la stampa continua correttamente.

## 13. Rimozione finale dei dati legacy

Non fa parte del cutover. Solo dopo import verificato, scadenza della retention,
backup esterno leggibile e autorizzazione formale si possono eliminare account,
configurazioni e directory legacy. Il cleanup non offre un flag di purge per
evitare che “pulire tutto” distrugga le evidenze che la piattaforma antifrode
deve conservare.
