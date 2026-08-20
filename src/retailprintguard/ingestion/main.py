"""CLI for continuous or one-shot read-only spool ingestion."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import re
import signal
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from threading import Event
from typing import Any

from retailprintguard import __version__
from retailprintguard.common.config import Settings, load_settings
from retailprintguard.common.logging import StructuredLoggingRuntime, configure_structured_logging
from retailprintguard.importer.historical import HistoricalImporter
from retailprintguard.ingestion.adapters import SourceAdapter
from retailprintguard.ingestion.canonical import CanonicalCaptureV1Adapter
from retailprintguard.ingestion.printproxy import PrintProxyV3Adapter
from retailprintguard.ingestion.rch import RCHCaptureV1Adapter, RCHParsedV1Adapter
from retailprintguard.ingestion.repository import (
    IngestionRepository,
    ValidationIngestionRepository,
)
from retailprintguard.ingestion.worker import IngestionRunReport, IngestionWorker

_FACTORY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
LOGGER = logging.getLogger("retailprintguard.ingestion")


def _positive_job_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("job limit must be an integer") from exc
    if not 1 <= parsed <= 100_000:
        raise argparse.ArgumentTypeError("job limit must be between 1 and 100000")
    return parsed


def _positive_scan_interval(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("scan interval must be numeric") from exc
    if not 0.05 <= parsed <= 3600:
        raise argparse.ArgumentTypeError(
            "scan interval must be between 0.05 and 3600 seconds"
        )
    return parsed


def _parser(*, historical: bool = False) -> argparse.ArgumentParser:
    description = "Import historical proxy evidence once" if historical else "Ingest proxy evidence"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--canonical-root", type=Path, help="RetailPrintGuard native spool root")
    parser.add_argument("--canonical-instance", default="retailprintguard-primary")
    parser.add_argument("--rch-root", type=Path, help="commercialRCHproxy v0.3 OUTPUT_DIR")
    parser.add_argument(
        "--rch-parsed-root",
        type=Path,
        help="import only legacy PHARSED derivatives when authoritative RAW is unavailable",
    )
    parser.add_argument("--rch-instance", default="commercialrchproxy-primary")
    parser.add_argument("--printproxy-root", type=Path, help="printproxy v3 DATA_DIR root")
    parser.add_argument("--printproxy-instance", default="printproxy-primary")
    parser.add_argument("--printproxy-hmac-key-file", type=Path)
    parser.add_argument(
        "--allow-unauthenticated-printproxy",
        action="store_true",
        help="accept only ledgers explicitly created without HMAC; hash-chain remains mandatory",
    )
    parser.add_argument(
        "--repository-factory",
        help="trusted Python factory as module:function; called with validated Settings",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate in memory without writing MariaDB or source spool",
    )
    parser.add_argument("--once", action="store_true", default=historical)
    parser.add_argument(
        "--scan-interval-seconds",
        type=_positive_scan_interval,
        help="continuous scan interval override; ignored by one-shot runs",
    )
    parser.add_argument(
        "--max-jobs",
        type=_positive_job_limit,
        help="maximum candidates per adapter (historical default: 10000)",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--json-logs",
        action="store_true",
        help="emit bounded structured service logs to stderr; report stdout is unchanged",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def _read_hmac_key(path: Path | None) -> bytes | None:
    if path is None:
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        link_info = path.lstat()
        if stat.S_ISLNK(link_info.st_mode):
            raise ValueError("printproxy HMAC key must not be a symlink")
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open printproxy HMAC key safely: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= 4096:
            raise ValueError(
                "printproxy HMAC key must be a regular non-symlink file up to 4096 bytes"
            )
        chunks: list[bytes] = []
        total = 0
        while total <= 4096:
            chunk = os.read(fd, min(4097 - total, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("printproxy HMAC key changed while it was being read")
        key = b"".join(chunks)
    finally:
        os.close(fd)
    if len(key) > 4096:
        raise ValueError("printproxy HMAC key exceeds 4096 bytes")
    if len(key) < 32:
        raise ValueError("printproxy HMAC key must contain at least 32 bytes")
    return key


def _device_map(settings: Settings) -> dict[tuple[str, int], str]:
    return {
        (str(device.target_ip), device.target_port): device.id
        for device in settings.devices
        if device.enabled
    }


def build_adapters(args: argparse.Namespace, settings: Settings) -> tuple[SourceAdapter, ...]:
    devices = _device_map(settings)
    adapters: list[SourceAdapter] = []
    if args.canonical_root is not None:
        adapters.append(
            CanonicalCaptureV1Adapter(
                args.canonical_root,
                source_instance_id=args.canonical_instance,
                devices_by_target=devices,
            )
        )
    if args.rch_root is not None:
        adapters.append(
            RCHCaptureV1Adapter(
                args.rch_root,
                source_instance_id=args.rch_instance,
                devices_by_target=devices,
            )
        )
    if args.rch_parsed_root is not None:
        if args.rch_root is not None:
            raise ValueError(
                "--rch-root and --rch-parsed-root are mutually exclusive for one run; "
                "prefer authoritative RAW"
            )
        adapters.append(
            RCHParsedV1Adapter(
                args.rch_parsed_root,
                source_instance_id=args.rch_instance,
                devices_by_target=devices,
            )
        )
    if args.printproxy_root is not None:
        key = _read_hmac_key(args.printproxy_hmac_key_file)
        adapters.append(
            PrintProxyV3Adapter(
                args.printproxy_root,
                source_instance_id=args.printproxy_instance,
                devices_by_target=devices,
                hmac_key=key,
                require_hmac=not args.allow_unauthenticated_printproxy,
            )
        )
    if not adapters:
        raise ValueError(
            "at least one --canonical-root, --rch-root, --rch-parsed-root or "
            "--printproxy-root is required"
        )
    return tuple(adapters)


def _repository(
    spec: str | None, settings: Settings, *, validate_only: bool
) -> IngestionRepository:
    if validate_only:
        if spec is not None:
            raise ValueError("--validate-only and --repository-factory are mutually exclusive")
        return ValidationIngestionRepository()
    if spec is None or not _FACTORY.fullmatch(spec):
        raise ValueError("--repository-factory module:function is required outside --validate-only")
    module_name, function_name = spec.split(":", 1)
    module = importlib.import_module(module_name)
    factory: Any = getattr(module, function_name, None)
    if not callable(factory):
        raise ValueError(f"repository factory is not callable: {spec}")
    repository = factory(settings)
    if not isinstance(repository, IngestionRepository):
        raise TypeError("repository factory result does not implement IngestionRepository")
    return repository


def report_dict(report: IngestionRunReport) -> dict[str, Any]:
    return {
        "discovered": report.discovered,
        "imported": report.imported,
        "duplicates": report.duplicates,
        "quarantined": report.quarantined,
        "retry_exhausted": report.retry_exhausted,
        "source_busy": report.source_busy,
        "errors": list(report.errors),
    }


def _print_report(report: IngestionRunReport, *, json_output: bool) -> None:
    value = report_dict(report)
    if json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    print(
        "ingestion: "
        f"discovered={report.discovered} imported={report.imported} "
        f"duplicates={report.duplicates} quarantined={report.quarantined} "
        f"retry_exhausted={report.retry_exhausted} source_busy={report.source_busy}"
    )
    for error in report.errors:
        print(f"warning: {error}", file=sys.stderr)


def _log_report(report: IngestionRunReport) -> None:
    value = report_dict(report)
    degraded = bool(report.quarantined or report.retry_exhausted or report.source_busy)
    LOGGER.log(
        logging.WARNING if degraded else logging.INFO,
        "ingestion iteration completed",
        extra={
            "event": "ingestion_iteration_completed",
            "metrics": value,
            "error": "; ".join(report.errors) if report.errors else None,
        },
    )


def run_cli(argv: Sequence[str] | None = None, *, historical: bool = False) -> int:
    args = _parser(historical=historical).parse_args(argv)
    runtime: StructuredLoggingRuntime | None = None
    if args.json_logs:
        runtime = configure_structured_logging("ingestion")
    try:
        settings = load_settings(args.config)
        adapters = build_adapters(args, settings)
        repository = _repository(
            args.repository_factory, settings, validate_only=args.validate_only
        )
        maximum = args.max_jobs or (10_000 if historical else settings.ingestion.max_batch_jobs)
        if historical:
            report = HistoricalImporter(
                repository,
                adapters,
                retry_initial_seconds=settings.ingestion.retry_initial_seconds,
                retry_max_seconds=settings.ingestion.retry_max_seconds,
            ).run(maximum_per_adapter=maximum)
            _print_report(report, json_output=args.json)
            return 1 if report.quarantined or report.retry_exhausted or report.source_busy else 0

        worker = IngestionWorker(
            repository,
            adapters,
            retry_initial_seconds=settings.ingestion.retry_initial_seconds,
            retry_max_seconds=settings.ingestion.retry_max_seconds,
        )
        if args.once:
            report = worker.run_once(maximum_per_adapter=maximum)
            _print_report(report, json_output=args.json)
            return 1 if report.quarantined or report.retry_exhausted or report.source_busy else 0

        stop = Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        worker.run_forever(
            stop,
            scan_interval_seconds=(
                args.scan_interval_seconds or settings.ingestion.scan_interval_seconds
            ),
            maximum_per_adapter=maximum,
            on_report=(
                _log_report
                if runtime is not None
                else lambda report: _print_report(report, json_output=args.json)
            ),
        )
        return 0
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        if runtime is None:
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        else:
            LOGGER.error(
                "ingestion service failed",
                extra={
                    "event": "ingestion_service_failed",
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
