"""CLI for the independent parser worker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from threading import Event

from sqlalchemy.exc import SQLAlchemyError

from retailprintguard import __version__
from retailprintguard.common.config import DEVICE_ID_RE, DeviceType, load_settings
from retailprintguard.common.logging import StructuredLoggingRuntime, configure_structured_logging
from retailprintguard.common.pipeline_lock import run_pipeline_once, safe_worker_error
from retailprintguard.db.session import create_db_engine, session_factory
from retailprintguard.parser.beeper import (
    PosBeeperConfiguration,
    PosBeeperDispatcher,
    PosBeeperTarget,
)
from retailprintguard.parser.repository import ParserRepositoryError, SqlAlchemyParserRepository
from retailprintguard.parser.spool_beeper import PosSpoolBeeperWatcher
from retailprintguard.parser.worker import ParserRunReport, ParserWorker

LOGGER = logging.getLogger("retailprintguard.parser")


def _limit(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= result <= 10_000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 10000")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Parse imported RetailPrintGuard RAW evidence")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--reparse-all",
        action="store_true",
        help="append outputs for the current parser build to historical jobs",
    )
    parser.add_argument(
        "--device-id",
        help="with --once --reparse-all, limit the append-only reparse to one configured device",
    )
    parser.add_argument("--limit", type=_limit, default=100)
    parser.add_argument("--interval-seconds", type=float, default=0.25)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="emit bounded structured service logs to stderr; report stdout is unchanged",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _report(report: ParserRunReport, *, as_json: bool) -> None:
    value = {
        "discovered": report.discovered,
        "parsed_jobs": report.parsed_jobs,
        "parsed_documents": report.parsed_documents,
        "failed": report.failed,
    }
    if as_json:
        print(json.dumps(value, sort_keys=True))
    else:
        print("parser: " + " ".join(f"{key}={item}" for key, item in value.items()))


def _log_report(report: ParserRunReport) -> None:
    metrics = {
        "discovered": report.discovered,
        "parsed_jobs": report.parsed_jobs,
        "parsed_documents": report.parsed_documents,
        "failed": report.failed,
    }
    LOGGER.log(
        logging.WARNING if report.failed else logging.INFO,
        "parser iteration completed",
        extra={"event": "parser_iteration_completed", "metrics": metrics},
    )


def cli(argv: Sequence[str] | None = None) -> int:
    os.umask(0o027)
    args = _parser().parse_args(argv)
    runtime: StructuredLoggingRuntime | None = None
    beeper: PosBeeperDispatcher | None = None
    spool_beeper: PosSpoolBeeperWatcher | None = None
    engine = None
    if args.json_logs:
        runtime = configure_structured_logging("parser")
    try:
        if args.reparse_all and not args.once:
            raise ValueError("--reparse-all requires --once")
        if args.device_id is not None and not (args.reparse_all and args.once):
            raise ValueError("--device-id requires --once --reparse-all")
        if args.device_id is not None and not DEVICE_ID_RE.fullmatch(args.device_id):
            raise ValueError("--device-id must be a valid device id")
        settings = load_settings(args.config)
        if args.device_id is not None and args.device_id not in {
            device.id for device in settings.devices
        }:
            raise ValueError("--device-id is not present in the site configuration")
        beeper_configuration = PosBeeperConfiguration.from_environment()
        if beeper_configuration.enabled and not args.reparse_all:
            pos_targets = tuple(
                PosBeeperTarget(
                    device_id=device.id,
                    host=str(device.target_ip),
                    port=device.target_port,
                )
                for device in settings.devices
                if device.enabled and device.type is DeviceType.POS
            )
            beeper_configuration.validate_selection(
                tuple(target.device_id for target in pos_targets)
            )
            selected_targets = tuple(
                target for target in pos_targets if beeper_configuration.selects(target.device_id)
            )
            beeper = PosBeeperDispatcher(
                beeper_configuration,
                selected_targets,
            )
            spool_beeper = PosSpoolBeeperWatcher(
                settings.spool_root,
                tuple(target.device_id for target in selected_targets),
                beeper,
                poll_seconds=beeper_configuration.spool_poll_seconds,
            )
            spool_beeper.start()
        engine = create_db_engine(settings.database_url().get_secret_value())
        worker = ParserWorker(
            SqlAlchemyParserRepository(session_factory(engine)),
            timezone_name=settings.timezone,
            beeper=beeper,
        )
        if args.once:
            run_options: dict[str, object] = {
                "limit": args.limit, "reparse": args.reparse_all,
            }
            if args.device_id is not None:
                run_options["device_id"] = args.device_id
            report = run_pipeline_once(
                engine,
                lambda: worker.run_once(**run_options),
                required=True,
            )
            if report is None:
                raise RuntimeError("Parser cycle did not execute")
            if beeper is not None and not beeper.drain(timeout=30):
                LOGGER.warning(
                    "POS beeper drain timed out; parser output remains valid",
                    extra={"event": "pos_beeper_drain_timeout"},
                )
            _report(report, as_json=args.json)
            return 1 if report.failed else 0
        stop = Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        while not stop.is_set():
            try:
                report = run_pipeline_once(engine, lambda: worker.run_once(limit=args.limit))
                if report is not None and runtime is not None:
                    _log_report(report)
            except (ParserRepositoryError, RuntimeError, SQLAlchemyError) as exc:
                LOGGER.error(
                    "parser iteration failed",
                    extra={
                        "event": "parser_iteration_failed",
                        "error": safe_worker_error(exc),
                    },
                )
            stop.wait(args.interval_seconds)
        return 0
    except (OSError, ParserRepositoryError, RuntimeError, ValueError, SQLAlchemyError) as exc:
        if runtime is None:
            print(f"ERROR: {safe_worker_error(exc)}", file=sys.stderr)
        else:
            LOGGER.error(
                "parser service failed",
                extra={
                    "event": "parser_service_failed",
                    "error": safe_worker_error(exc),
                },
            )
        return 2
    finally:
        if spool_beeper is not None:
            spool_beeper.close()
        if beeper is not None:
            beeper.close()
        if engine is not None:
            engine.dispose()
        if runtime is not None:
            runtime.stop()


if __name__ == "__main__":
    raise SystemExit(cli())
