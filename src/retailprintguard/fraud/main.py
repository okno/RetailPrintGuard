"""Command-line entry point for the deterministic fraud worker."""

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
from retailprintguard.db.session import create_db_engine, session_factory
from retailprintguard.fraud.worker import FraudWorker

LOGGER = logging.getLogger("retailprintguard.fraud")


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not 0 < parsed <= 3600:
        raise argparse.ArgumentTypeError("poll interval must be between 0 and 3600 seconds")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 1_000_000:
        raise argparse.ArgumentTypeError("max transactions must be between 1 and 1000000")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate RetailPrintGuard fraud rules")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=_positive_float)
    parser.add_argument("--max-transactions", type=_positive_int, default=10_000)
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
        "fraud: " + " ".join(f"{key}={value}" for key, value in payload.items()),
        flush=True,
    )


def _log_report(report: object) -> None:
    LOGGER.info(
        "fraud iteration completed",
        extra={"event": "fraud_iteration_completed", "metrics": asdict(report)},  # type: ignore[arg-type]
    )


def run_cli(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    runtime: StructuredLoggingRuntime | None = None
    if args.json_logs:
        runtime = configure_structured_logging("fraud")
    try:
        settings = load_settings(args.config)
        engine = create_db_engine(settings.database_url().get_secret_value())
        worker = FraudWorker(
            session_factory(engine),
            minimum_score=settings.correlation.minimum_score,
            time_window_seconds=settings.correlation.time_window_seconds,
            default_amount_drop_percent=settings.fraud.default_amount_drop_percent,
            order_without_fiscal_close_minutes=(settings.fraud.order_without_fiscal_close_minutes),
            extreme_price_change_percent=settings.fraud.extreme_price_change_percent,
        )
        if args.once:
            _print_report(
                worker.run_once(max_transactions=args.max_transactions),
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
                report = worker.run_once(max_transactions=args.max_transactions)
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
                                "service": "fraud",
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
                        "fraud iteration failed",
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
                "fraud service failed",
                extra={
                    "event": "fraud_service_failed",
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
