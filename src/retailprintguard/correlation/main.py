"""Command-line entry point for the database correlation worker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from threading import Event

from retailprintguard import __version__
from retailprintguard.common.config import load_settings
from retailprintguard.common.logging import StructuredLoggingRuntime, configure_structured_logging
from retailprintguard.correlation.worker import CorrelationWorker, activate_parser_version
from retailprintguard.db.session import create_db_engine, session_factory

LOGGER = logging.getLogger("retailprintguard.correlation")


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed <= 3600:
        raise argparse.ArgumentTypeError("poll interval must be between 0 and 3600 seconds")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 1_000_000:
        raise argparse.ArgumentTypeError("max documents must be between 1 and 1000000")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Correlate normalized RetailPrintGuard data")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=_positive_float)
    parser.add_argument("--max-documents", type=_positive_int, default=10_000)
    parser.add_argument("--activate-parser", metavar="NAME")
    parser.add_argument("--parser-version", metavar="VERSION")
    parser.add_argument("--build-sha256", metavar="SHA256")
    parser.add_argument("--activation-reason", metavar="REASON")
    parser.add_argument(
        "--no-rewind",
        action="store_true",
        help="keep the current correlation watermark after parser activation",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="emit bounded structured service logs to stderr; report stdout is unchanged",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _print_report(report: object, *, json_output: bool) -> None:
    payload = asdict(report)  # type: ignore[arg-type]
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
        return
    print(
        "correlation: " + " ".join(f"{key}={value}" for key, value in payload.items()),
        flush=True,
    )


def _log_report(report: object) -> None:
    LOGGER.info(
        "correlation iteration completed",
        extra={"event": "correlation_iteration_completed", "metrics": asdict(report)},  # type: ignore[arg-type]
    )


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime: StructuredLoggingRuntime | None = None
    if args.json_logs:
        runtime = configure_structured_logging("correlation")
    try:
        settings = load_settings(args.config)
        engine = create_db_engine(settings.database_url().get_secret_value())
        factory = session_factory(engine)
        activation_values = (
            args.activate_parser,
            args.parser_version,
            args.build_sha256,
            args.activation_reason,
        )
        if any(activation_values) and not all(activation_values):
            raise ValueError(
                "--activate-parser, --parser-version, --build-sha256 and "
                "--activation-reason must be provided together"
            )
        if all(activation_values):
            activated_id = activate_parser_version(
                factory,
                parser_name=args.activate_parser,
                parser_version=args.parser_version,
                build_sha256=args.build_sha256,
                reason=args.activation_reason,
                rewind=not args.no_rewind,
            )
            activation = {
                "parser_name": args.activate_parser,
                "parser_version": args.parser_version,
                "parser_version_id": str(activated_id),
                "rewind": not args.no_rewind,
            }
            if runtime is None:
                print(
                    json.dumps(
                        {"event": "parser_activated", **activation},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    flush=True,
                )
            else:
                LOGGER.info(
                    "parser version activated",
                    extra={"event": "parser_activated", **activation},
                )
        worker = CorrelationWorker(
            factory,
            minimum_score=settings.correlation.minimum_score,
            time_window_seconds=settings.correlation.time_window_seconds,
            timezone=settings.timezone,
        )
        if args.once:
            _print_report(
                worker.run_once(max_documents=args.max_documents),
                json_output=args.json,
            )
            engine.dispose()
            return 0

        stop = Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        interval = args.poll_interval or settings.ingestion.scan_interval_seconds
        while not stop.is_set():
            try:
                report = worker.run_once(max_documents=args.max_documents)
                if runtime is None:
                    _print_report(report, json_output=args.json)
                else:
                    _log_report(report)
            except Exception as exc:  # noqa: BLE001 - long-running worker retry boundary
                if runtime is None:
                    print(
                        json.dumps(
                            {
                                "level": "ERROR",
                                "service": "correlation",
                                "event": "worker_iteration_failed",
                                "error": type(exc).__name__,
                                "message": str(exc),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    LOGGER.error(
                        "correlation iteration failed",
                        extra={
                            "event": "worker_iteration_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
            stop.wait(interval)
        engine.dispose()
        return 0
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if runtime is None:
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            LOGGER.error(
                "correlation service failed",
                extra={
                    "event": "correlation_service_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        return 2
    finally:
        if runtime is not None:
            runtime.stop()


def cli(argv: Sequence[str] | None = None) -> int:
    os.umask(0o027)
    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(cli())
