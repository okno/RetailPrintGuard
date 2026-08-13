"""Bounded, non-blocking structured logging for long-running services.

Application threads only prepare a ``LogRecord`` and attempt ``put_nowait`` on
the bounded queue.  A dedicated ``QueueListener`` owns the stderr handler, so a
slow journal or pipe cannot apply backpressure to the TCP relay.  Records that
arrive while the queue is full are counted and summarized during orderly
shutdown.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import queue
import re
import sys
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener
from threading import Lock
from types import TracebackType
from typing import Any, TextIO

_SERVICE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|passphrase|token|secret|authorization|api[_-]?key|credential)",
    re.IGNORECASE,
)
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|passphrase|token|secret|authorization|api[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_URI_PASSWORD_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s:/@]+:)[^\s/@]+(@)")
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]+")
_MAX_TEXT = 16_384
_MAX_COLLECTION = 100
_MAX_DEPTH = 4

_SAFE_EXTRA_FIELDS = frozenset(
    {
        "client",
        "client_bytes",
        "device_bytes",
        "details",
        "dropped_records",
        "listen",
        "metrics",
        "parser_name",
        "parser_version",
        "parser_version_id",
        "reason",
        "rewind",
        "source",
        "status",
        "target",
    }
)


def _redact_text(value: str) -> str:
    bounded = value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…<truncated>"
    bounded = _URI_PASSWORD_RE.sub(r"\1<redacted>\2", bounded)
    bounded = _KEY_VALUE_SECRET_RE.sub(r"\1\2<redacted>", bounded)
    return _BEARER_RE.sub(r"\1 <redacted>", bounded)


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if depth >= _MAX_DEPTH:
        return "<maximum-depth>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:_MAX_COLLECTION]:
            key = _redact_text(str(raw_key))
            result[key] = (
                "<redacted>"
                if _SECRET_KEY_RE.search(key)
                else _safe_value(item, depth=depth + 1)
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:_MAX_COLLECTION]]
    return _redact_text(str(value))


class StructuredJsonFormatter(logging.Formatter):
    """Render a stable JSON event without copying arbitrary record attributes."""

    def __init__(self, service: str) -> None:
        super().__init__()
        if not _SERVICE_RE.fullmatch(service):
            raise ValueError("service must match [a-z][a-z0-9_.-]{1,63}")
        self.service = service

    @staticmethod
    def _context(record: logging.LogRecord, *aliases: str) -> Any:
        for name in aliases:
            if hasattr(record, name):
                return _safe_value(getattr(record, name))
        return None

    def format(self, record: logging.LogRecord) -> str:
        stack_trace = getattr(record, "stack_trace", None)
        if stack_trace is None and record.exc_info:
            stack_trace = self.formatException(record.exc_info)
        error = getattr(record, "error", None)
        if error is None and record.exc_info and record.exc_info[1] is not None:
            exception = record.exc_info[1]
            error = f"{type(exception).__name__}: {exception}"
        document: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self.service,
            "event": _safe_value(getattr(record, "event", "log")),
            "message": _redact_text(record.getMessage()),
            "error": _safe_value(error),
            "device": self._context(record, "device", "device_id"),
            "session": self._context(record, "session", "session_id"),
            "job": self._context(record, "job", "job_id"),
            "correlation_id": self._context(record, "correlation_id", "correlation"),
        }
        for name in _SAFE_EXTRA_FIELDS:
            if hasattr(record, name):
                document[name] = _safe_value(getattr(record, name))
        if stack_trace is not None:
            document["stack_trace"] = _safe_value(stack_trace)
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class BoundedQueueHandler(QueueHandler):
    """QueueHandler whose hot path never waits for queue capacity or stderr."""

    def __init__(self, event_queue: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(event_queue)
        self._dropped_records = 0
        self._unreported_drops = 0
        self._counter_lock = Lock()

    @property
    def dropped_records(self) -> int:
        with self._counter_lock:
            return self._dropped_records

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        prepared = copy.copy(record)
        prepared.message = record.getMessage()
        prepared.msg = prepared.message
        prepared.args = None
        if record.exc_info:
            prepared.stack_trace = logging.Formatter().formatException(record.exc_info)
        prepared.exc_info = None
        prepared.exc_text = None
        prepared.stack_info = None
        return prepared

    def enqueue(self, record: logging.LogRecord) -> None:
        with self._counter_lock:
            if self._unreported_drops:
                summary = logging.LogRecord(
                    name="retailprintguard.logging",
                    level=logging.WARNING,
                    pathname=__file__,
                    lineno=0,
                    msg="structured log records dropped because the queue was full",
                    args=(),
                    exc_info=None,
                )
                summary.event = "log_queue_dropped"
                summary.dropped_records = self._unreported_drops
                try:
                    self.queue.put_nowait(summary)
                except queue.Full:
                    pass
                else:
                    self._unreported_drops = 0
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with self._counter_lock:
                self._dropped_records += 1
                self._unreported_drops += 1


class _DrainableQueueListener(QueueListener):
    """Allow orderly shutdown even when the bounded queue is full."""

    def __init__(
        self,
        event_queue: queue.Queue[logging.LogRecord],
        *handlers: logging.Handler,
    ) -> None:
        super().__init__(event_queue, *handlers, respect_handler_level=True)
        self.discarded_on_stop = 0

    def enqueue_sentinel(self) -> None:
        while True:
            try:
                self.queue.put_nowait(self._sentinel)
                return
            except queue.Full:
                # Service shutdown must not deadlock behind a stalled sink.
                # Discard the oldest queued record and account for it in the
                # final loss summary emitted after the listener is joined.
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    continue
                self.discarded_on_stop += 1
                if hasattr(self.queue, "task_done"):
                    self.queue.task_done()

    def stop(self, timeout: float = 0.1) -> bool:
        """Signal the daemon listener and wait only for a small bounded interval."""

        thread = self._thread
        if thread is None:
            return True
        self.enqueue_sentinel()
        thread.join(timeout=max(0.0, timeout))
        stopped = not thread.is_alive()
        # QueueListener threads are daemon threads.  If a broken sink remains
        # blocked, detaching it is safer than blocking proxy or service exit.
        self._thread = None
        return stopped


class StructuredLoggingRuntime:
    """Own a service logging queue and restore the prior logger on shutdown."""

    def __init__(
        self,
        *,
        service: str,
        logger: logging.Logger,
        queue_handler: BoundedQueueHandler,
        listener: _DrainableQueueListener,
        sink: logging.Handler,
        previous_handlers: tuple[logging.Handler, ...],
        previous_level: int,
        previous_propagate: bool,
    ) -> None:
        self.service = service
        self.logger = logger
        self.queue_handler = queue_handler
        self.listener = listener
        self.sink = sink
        self.previous_handlers = previous_handlers
        self.previous_level = previous_level
        self.previous_propagate = previous_propagate
        self._stopped = False

    @property
    def dropped_records(self) -> int:
        return self.queue_handler.dropped_records + self.listener.discarded_on_stop

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.logger.handlers[:] = list(self.previous_handlers)
        self.logger.setLevel(self.previous_level)
        self.logger.propagate = self.previous_propagate
        self.listener.stop()

    def __enter__(self) -> StructuredLoggingRuntime:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.stop()


def _log_level(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    name = (value or os.environ.get("RPG_LOG_LEVEL", "INFO")).upper()
    resolved = logging.getLevelNamesMapping().get(name)
    if not isinstance(resolved, int):
        raise ValueError(f"unsupported log level: {name}")
    return resolved


def _queue_capacity(value: int | None) -> int:
    if value is None:
        raw = os.environ.get("RPG_LOG_QUEUE_CAPACITY", "4096")
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("RPG_LOG_QUEUE_CAPACITY must be an integer") from exc
    if not 1 <= value <= 100_000:
        raise ValueError("log queue capacity must be between 1 and 100000")
    return value


def configure_structured_logging(
    service: str,
    *,
    level: str | int | None = None,
    queue_capacity: int | None = None,
    logger: logging.Logger | None = None,
    sink: logging.Handler | None = None,
    stream: TextIO | None = None,
) -> StructuredLoggingRuntime:
    """Install one bounded QueueHandler and start its stderr QueueListener."""

    if sink is not None and stream is not None:
        raise ValueError("sink and stream are mutually exclusive")
    formatter = StructuredJsonFormatter(service)
    target = logger or logging.getLogger()
    output = sink or logging.StreamHandler(stream or sys.stderr)
    output.setFormatter(formatter)
    event_queue: queue.Queue[logging.LogRecord] = queue.Queue(
        maxsize=_queue_capacity(queue_capacity)
    )
    queue_handler = BoundedQueueHandler(event_queue)
    previous_handlers = tuple(target.handlers)
    previous_level = target.level
    previous_propagate = target.propagate
    target.handlers[:] = [queue_handler]
    target.setLevel(_log_level(level))
    if target is not logging.getLogger():
        target.propagate = False
    listener = _DrainableQueueListener(event_queue, output)
    listener.start()
    return StructuredLoggingRuntime(
        service=service,
        logger=target,
        queue_handler=queue_handler,
        listener=listener,
        sink=output,
        previous_handlers=previous_handlers,
        previous_level=previous_level,
        previous_propagate=previous_propagate,
    )


__all__ = [
    "BoundedQueueHandler",
    "StructuredJsonFormatter",
    "StructuredLoggingRuntime",
    "configure_structured_logging",
]
