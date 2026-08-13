"""Independent parser worker; no network or proxy imports."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from threading import Event

from retailprintguard.parser.escpos import parse_escpos
from retailprintguard.parser.rch import parse_rch
from retailprintguard.parser.repository import ParserRepositoryError, SqlAlchemyParserRepository


@dataclass(frozen=True, slots=True)
class ParserRunReport:
    discovered: int = 0
    parsed_jobs: int = 0
    parsed_documents: int = 0
    failed: int = 0


class ParserWorker:
    def __init__(self, repository: SqlAlchemyParserRepository) -> None:
        self.repository = repository

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
                    documents = parse_escpos(source["request"], **common)
                elif parser_kind == "rch_observed":
                    documents = parse_rch(
                        source["request"],
                        source["response"],
                        response_source_path=source["response_path"],
                        **common,
                    )
                else:
                    raise ValueError(f"unsupported parser kind: {parser_kind}")
                parsed_documents += self.repository.store_documents(job_id, documents)
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
