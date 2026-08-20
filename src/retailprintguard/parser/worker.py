"""Independent parser worker; no network or proxy imports."""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event
from typing import Protocol

from retailprintguard.parser.beeper import is_complete_pos_command
from retailprintguard.parser.escpos import parse_escpos
from retailprintguard.parser.rch import parse_rch
from retailprintguard.parser.repository import ParserRepositoryError, SqlAlchemyParserRepository

LOGGER = logging.getLogger("retailprintguard.parser")


class PosCommandBeeper(Protocol):
    def enqueue(self, device_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ParserRunReport:
    discovered: int = 0
    parsed_jobs: int = 0
    parsed_documents: int = 0
    failed: int = 0


class ParserWorker:
    def __init__(
        self,
        repository: SqlAlchemyParserRepository,
        *,
        timezone_name: str = "Europe/Rome",
        beeper: PosCommandBeeper | None = None,
    ) -> None:
        self.repository = repository
        self.timezone_name = timezone_name
        self.beeper = beeper

    def run_once(self, *, limit: int = 100, reparse: bool = False) -> ParserRunReport:
        job_ids = self.repository.pending_jobs(limit=limit, reparse=reparse)
        parsed_jobs = 0
        parsed_documents = 0
        failed = 0
        for job_id in job_ids:
            try:
                source = self.repository.load_job(job_id)
                if source is None:
                    continue
                common = {
                    "device_id": source["device_id"],
                    "session_id": source["session_id"],
                    "job_id": source["source_job_id"],
                    "captured_at": source["captured_at"],
                    "manifest_sha256": source["manifest_sha256"],
                    "source_path": source["request_path"],
                }
                parser_kind = source["parser_kind"]
                if parser_kind == "escpos":
                    should_beep = (
                        self.beeper is not None
                        and not reparse
                        and source["import_status"] == "IMPORTED"
                        and is_complete_pos_command(source["request"])
                    )
                    if should_beep:
                        try:
                            queued = self.beeper.enqueue(str(source["device_id"]))
                            captured_at = source["captured_at"]
                            if captured_at.tzinfo is None:
                                captured_at = captured_at.replace(tzinfo=UTC)
                            latency_ms = max(
                                0.0,
                                (datetime.now(UTC) - captured_at).total_seconds() * 1000,
                            )
                            LOGGER.log(
                                logging.INFO if latency_ms <= 2_000 else logging.WARNING,
                                "POS command beeper evaluated before full parsing",
                                extra={
                                    "event": (
                                        "pos_beeper_early_queued"
                                        if queued and latency_ms <= 2_000
                                        else "pos_beeper_latency_budget_missed"
                                        if queued
                                        else "pos_beeper_early_queue_rejected"
                                    ),
                                    "device_id": str(source["device_id"]),
                                    "job_id": str(source["source_job_id"]),
                                    "metrics": {
                                        "capture_to_queue_ms": round(latency_ms, 3),
                                        "budget_ms": 2_000,
                                    },
                                },
                            )
                        except Exception as exc:  # noqa: BLE001 - isolated notification
                            LOGGER.warning(
                                "POS beeper early enqueue failed; parsing continues",
                                extra={
                                    "event": "pos_beeper_enqueue_failed",
                                    "device_id": str(source["device_id"]),
                                    "job_id": str(source["source_job_id"]),
                                    "error": type(exc).__name__,
                                },
                            )
                    documents = parse_escpos(
                        source["request"],
                        timezone_name=self.timezone_name,
                        **common,
                    )
                elif parser_kind == "rch_observed":
                    documents = parse_rch(
                        source["request"],
                        source["response"],
                        response_source_path=source["response_path"],
                        timezone_name=self.timezone_name,
                        **common,
                    )
                else:
                    raise ValueError(f"unsupported parser kind: {parser_kind}")
                inserted = self.repository.store_documents(job_id, documents)
                parsed_documents += inserted
                parsed_jobs += 1
            except (ParserRepositoryError, RuntimeError, TypeError, ValueError) as exc:
                failed += 1
                with contextlib.suppress(ParserRepositoryError):
                    self.repository.record_failure(job_id, f"{type(exc).__name__}: {exc}")
        return ParserRunReport(len(job_ids), parsed_jobs, parsed_documents, failed)

    def run_forever(
        self,
        stop: Event,
        *,
        interval_seconds: float,
        limit: int,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("parser interval must be positive")
        while not stop.is_set():
            try:
                self.run_once(limit=limit)
            except ParserRepositoryError:
                if stop.wait(interval_seconds):
                    return
                continue
            stop.wait(interval_seconds)


def sleep_until(stop: Event, seconds: float) -> None:
    """Injectable monotonic helper retained for operational integrations."""

    deadline = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < deadline:
        stop.wait(min(0.25, max(0.0, deadline - time.monotonic())))


__all__ = ["ParserRunReport", "ParserWorker"]
