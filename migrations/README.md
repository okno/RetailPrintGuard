# Migrazioni database

Le migrazioni sono l'unico percorso supportato per creare o aggiornare lo
schema. `RPG_DATABASE_URL` deve puntare al database locale MariaDB e non deve
essere fornita ai processi proxy.

```bash
RPG_DATABASE_URL='mysql+pymysql://...@localhost/retailprintguard?charset=utf8mb4' \
  alembic upgrade head
```

I test usano SQLite soltanto per verifiche veloci di schema e comportamento
ORM. La validazione di release deve eseguire upgrade, downgrade e restore su
MariaDB della stessa major installata in produzione.
