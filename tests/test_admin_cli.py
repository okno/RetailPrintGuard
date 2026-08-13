from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import func, select

from retailprintguard.admin.main import BootstrapError, bootstrap_admin
from retailprintguard.api.auth import PasswordService
from retailprintguard.db import Base, create_db_engine, session_factory
from retailprintguard.db.models import AuditLog, Role, User, UserRole


def test_bootstrap_creates_argon2_admin_roles_and_audit_chain_once() -> None:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    user_id = bootstrap_admin(
        factory,
        username="Initial.Admin",
        display_name="Amministratore iniziale",
        password="Synthetic-Strong-2042!",
    )
    with factory() as session:
        user = session.scalar(select(User))
        assert user is not None and str(user.id) == user_id
        assert user.username == "initial.admin"
        assert PasswordService().verify(user.password_hash, "Synthetic-Strong-2042!")
        assert {role.code for role in session.scalars(select(Role)).all()} == {
            "ADMIN",
            "AUDITOR",
            "OPERATOR",
            "READ_ONLY",
        }
        assert session.scalar(select(func.count()).select_from(UserRole)) == 1
        audit = session.scalar(select(AuditLog))
        assert audit is not None and audit.event_type == "ADMIN_BOOTSTRAPPED"
        assert audit.previous_record_hash == "0" * 64

    try:
        bootstrap_admin(
            factory,
            username="second.admin",
            display_name="Secondo amministratore",
            password="Another-Strong-2042!",
        )
    except BootstrapError as exc:
        assert "esiste già" in str(exc)
    else:
        raise AssertionError("a second bootstrap must be rejected")
    engine.dispose()


def test_bootstrap_password_policy_is_enforced_before_database_write() -> None:
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    for password in ("short", "alllowercasebutlong"):
        try:
            bootstrap_admin(
                factory,
                username="initial.admin",
                display_name="Admin",
                password=password,
            )
        except BootstrapError:
            pass
        else:
            raise AssertionError("weak password was accepted")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0
    engine.dispose()


def test_concurrent_bootstrap_creates_exactly_one_first_admin(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    Base.metadata.create_all(engine)
    factory = session_factory(engine)
    barrier = threading.Barrier(2)

    def attempt(index: int) -> str:
        barrier.wait(timeout=5)
        try:
            bootstrap_admin(
                factory,
                username=f"initial.admin.{index}",
                display_name=f"Admin {index}",
                password=f"Synthetic-Concurrent-{index}!",
            )
        except BootstrapError:
            return "refused"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(attempt, (1, 2)))

    assert sorted(outcomes) == ["created", "refused"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 1
    engine.dispose()
