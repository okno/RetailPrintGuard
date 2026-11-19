"""Dedicated export worker, isolated from synchronous capture and API requests."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from retailprintguard.api.exports import ExportService
from retailprintguard.common.config import load_settings
from retailprintguard.common.logging import configure_structured_logging
from retailprintguard.db.session import create_db_engine, session_factory


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RetailPrintGuard PDF/ZIP export worker")
    parser.add_argument(
        "--config", default=os.environ.get("RPG_CONFIG", "/etc/retailprintguard/config.yaml")
    )
    parser.add_argument("--once", action="store_true", help="Process one queued request and exit")
    args = parser.parse_args(argv)
    # Linux systemd worker; import lazily to keep ordinary API tests portable.
    import fcntl

    runtime = configure_structured_logging("export")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        settings = load_settings(args.config)
        service = ExportService(
            session_factory(
                create_db_engine(
                    settings.database_url().get_secret_value(), pool_size=2, max_overflow=0
                )
            ),
            settings.archive_root,
        )
        service.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        lock_path = Path(service.directory) / ".worker.lock"
        if lock_path.is_symlink():
            raise RuntimeError("Invalid worker lock path")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            needs_recovery = True
            while not stop.is_set():
                try:
                    if needs_recovery:
                        service.recover()
                        needs_recovery = False
                    processed = service.run_one()
                except Exception:
                    # Do not log database exceptions containing connection strings.
                    logging.getLogger(__name__).error("export_worker_database_unavailable")
                    needs_recovery = True
                    if args.once:
                        return 1
                    stop.wait(5)
                    continue
                if args.once:
                    break
                if not processed:
                    stop.wait(2)
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
