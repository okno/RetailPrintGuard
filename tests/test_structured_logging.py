from __future__ import annotations

import io
import json
import logging
import time
from threading import Event

from retailprintguard.common.config import DeviceConfig
from retailprintguard.common.logging import configure_structured_logging
from retailprintguard.proxy.relay import RelayService


def _device() -> DeviceConfig:
    return DeviceConfig.model_validate(
        {
            "id": "pos_test",
            "name": "POS sintetica",
            "type": "pos",
            "listen_ip": "192.0.2.10",
            "listen_port": 9100,
            "target_ip": "192.0.2.20",
            "target_port": 9100,
            "parser": "escpos",
            "bidirectional": True,
            "allowed_networks": ["192.0.2.0/24"],
        }
    )


def test_structured_json_has_context_utc_and_redacts_secrets() -> None:
    stream = io.StringIO()
    runtime = configure_structured_logging("unit-test", stream=stream, queue_capacity=8)
    try:
        logging.getLogger("retailprintguard.test").error(
            "database password=never-log Bearer abc.def.ghi",
            extra={
                "event": "database_failed",
                "error": "mysql+pymysql://user:super-secret@127.0.0.1/database",
                "device": "pos_1",
                "session_id": "session-1",
                "job_id": "job-1",
                "correlation_id": "correlation-1",
                "details": {"token": "private", "payload": b"raw-secret-payload"},
            },
        )
    finally:
        runtime.stop()

    line = json.loads(stream.getvalue())
    assert line["timestamp"].endswith("Z")
    assert line["level"] == "ERROR"
    assert line["service"] == "unit-test"
    assert line["event"] == "database_failed"
    assert line["device"] == "pos_1"
    assert line["session"] == "session-1"
    assert line["job"] == "job-1"
    assert line["correlation_id"] == "correlation-1"
    assert line["details"]["token"] == "<redacted>"
    assert line["details"]["payload"]["length"] == len(b"raw-secret-payload")
    rendered = stream.getvalue()
    assert "never-log" not in rendered
    assert "abc.def.ghi" not in rendered
    assert "super-secret" not in rendered
    assert "raw-secret-payload" not in rendered


class _SlowHandler(logging.Handler):
    def __init__(self, release_gate: Event) -> None:
        super().__init__()
        self.release_gate = release_gate
        self.started = Event()

    def emit(self, _record: logging.LogRecord) -> None:
        self.started.set()
        self.release_gate.wait(timeout=5)


def test_slow_sink_never_blocks_relay_log_call_and_drops_are_counted() -> None:
    release = Event()
    sink = _SlowHandler(release)
    runtime = configure_structured_logging("proxy", sink=sink, queue_capacity=1)
    try:
        device = _device()
        RelayService._log(logging.INFO, "listener_started", device)
        started = time.perf_counter()
        for sequence in range(100):
            RelayService._log(
                logging.INFO,
                "session_closed",
                device,
                session_id=f"session-{sequence}",
                job_id=f"job-{sequence}",
            )
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0
        assert runtime.dropped_records > 0
        stop_started = time.perf_counter()
        runtime.stop()
        assert time.perf_counter() - stop_started < 0.5
    finally:
        release.set()
        runtime.stop()


def test_logger_state_is_restored_after_runtime_shutdown() -> None:
    logger = logging.getLogger("retailprintguard.logging-state-test")
    original = logging.NullHandler()
    logger.handlers[:] = [original]
    logger.setLevel(logging.ERROR)
    logger.propagate = True
    runtime = configure_structured_logging(
        "state-test", logger=logger, stream=io.StringIO(), queue_capacity=2
    )
    runtime.stop()
    assert logger.handlers == [original]
    assert logger.level == logging.ERROR
    assert logger.propagate is True
