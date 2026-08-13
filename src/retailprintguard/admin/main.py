"""Interactive, local-only bootstrap for the first administrator."""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from retailprintguard.api.auth import PasswordService
from retailprintguard.common.config import load_settings
from retailprintguard.common.hashchain import ZERO_HASH, chained_hash
from retailprintguard.db.models import AuditLog, Role, User, UserRole
from retailprintguard.db.session import create_db_engine, session_factory

_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")
_ROLE_NAMES = {
    "ADMIN": "Amministratore",
    "AUDITOR": "Auditor",
    "OPERATOR": "Operatore",
    "READ_ONLY": "Sola lettura",
}
_BOOTSTRAP_PROCESS_LOCK = threading.Lock()
_BOOTSTRAP_DATABASE_LOCK = "retailprintguard:first-admin-bootstrap"


class BootstrapError(RuntimeError):
    """Bootstrap was refused without leaking credentials."""


def _valid_password(password: str) -> None:
    if len(password) < 14 or len(password) > 1024:
        raise BootstrapError("la password deve contenere da 14 a 1024 caratteri")
    classes = sum(
        bool(pattern.search(password))
        for pattern in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"[0-9]"),
            re.compile(r"[^A-Za-z0-9]"),
        )
    )
    if classes < 3:
        raise BootstrapError("la password deve usare almeno tre classi di caratteri")


def bootstrap_admin(
    factory: sessionmaker[Session],
    *,
    username: str,
    display_name: str,
    password: str,
) -> str:
    normalized = username.strip().casefold()
    if not _USERNAME.fullmatch(normalized):
        raise BootstrapError("username non valido: usare 3-64 caratteri [A-Za-z0-9_.-]")
    display = display_name.strip()
    if not 1 <= len(display) <= 191:
        raise BootstrapError("nome visualizzato non valido")
    _valid_password(password)

    # The process lock protects SQLite/tests and concurrent calls in one CLI
    # process. MariaDB additionally needs a connection-scoped advisory lock so
    # two hosts cannot both observe an empty users table under READ COMMITTED.
    with _BOOTSTRAP_PROCESS_LOCK:
        session = factory()
        lock_connection = None
        database_lock_acquired = False
        try:
            bind = session.get_bind()
            if bind.dialect.name in {"mysql", "mariadb"}:
                lock_connection = bind.connect()
                acquired = lock_connection.scalar(
                    text("SELECT GET_LOCK(:name, :timeout)"),
                    {"name": _BOOTSTRAP_DATABASE_LOCK, "timeout": 10},
                )
                if acquired != 1:
                    raise BootstrapError(
                        "bootstrap occupato da un'altra operazione amministrativa"
                    )
                database_lock_acquired = True

            with session.begin():
                if int(session.scalar(select(func.count()).select_from(User)) or 0) != 0:
                    raise BootstrapError(
                        "bootstrap rifiutato: esiste già almeno un utente; "
                        "usare il workflow amministrativo"
                    )
                roles: dict[str, Role] = {}
                for code, name in _ROLE_NAMES.items():
                    role = session.scalar(select(Role).where(Role.code == code))
                    if role is None:
                        role = Role(code=code, name=name)
                        session.add(role)
                        session.flush()
                    roles[code] = role
                user = User(
                    username=normalized,
                    display_name=display,
                    password_hash=PasswordService().hash(password),
                )
                session.add(user)
                session.flush()
                session.add(UserRole(user_id=user.id, role_id=roles["ADMIN"].id))
                details = {
                    "username": normalized,
                    "roles": ["ADMIN"],
                    "method": "local_cli",
                }
                record_hash = chained_hash(
                    {
                        "sequence": 1,
                        "event_type": "ADMIN_BOOTSTRAPPED",
                        "resource_id": str(user.id),
                        "details": details,
                        "previous_hash": ZERO_HASH,
                    },
                    ZERO_HASH,
                )
                session.add(
                    AuditLog(
                        chain_scope="audit:bootstrap",
                        sequence=1,
                        actor_user_id=user.id,
                        event_type="ADMIN_BOOTSTRAPPED",
                        resource_type="user",
                        resource_id=str(user.id),
                        details=details,
                        previous_record_hash=ZERO_HASH,
                        record_hash=record_hash,
                    )
                )
                user_id = str(user.id)
            return user_id
        except SQLAlchemyError as exc:
            raise BootstrapError("impossibile completare il bootstrap amministratore") from exc
        finally:
            session.close()
            if lock_connection is not None:
                try:
                    if database_lock_acquired:
                        lock_connection.execute(
                            text("SELECT RELEASE_LOCK(:name)"),
                            {"name": _BOOTSTRAP_DATABASE_LOCK},
                        )
                finally:
                    lock_connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create the first RetailPrintGuard ADMIN using an interactive password prompt"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", required=True)
    return parser


def cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        first = getpass.getpass("Nuova password: ")
        second = getpass.getpass("Ripetere la password: ")
        if first != second:
            raise BootstrapError("le password non coincidono")
        settings = load_settings(args.config)
        factory = session_factory(create_db_engine(settings.database_url().get_secret_value()))
        user_id = bootstrap_admin(
            factory,
            username=args.username,
            display_name=args.display_name,
            password=first,
        )
        print(f"Amministratore creato: {args.username.casefold()} ({user_id})")
        return 0
    except (BootstrapError, OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(cli())
