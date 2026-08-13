"""Read-only adapter for the observed printproxy v3 archive contract.

The source contract is deliberately explicit: canonical JSONL ledger schema 1,
operational metadata schema 2, a validated hash-chain/head, and an immutable
``ARCHIVED`` event that authenticates the client-to-printer RAW artifact.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from retailprintguard.ingestion.adapters import (
    endpoint,
    optional_endpoint,
    parse_datetime,
    parse_optional_datetime,
    require_bool,
    require_int,
    require_optional_int,
    require_optional_string,
    require_sha256,
    require_string,
    require_uuid,
    resolve_device,
    validate_source_instance_id,
)
from retailprintguard.ingestion.dto import (
    ArtifactRole,
    ArtifactSnapshot,
    Endpoint,
    ImportCandidate,
    NormalizedEnvelope,
    SourceKind,
    StreamChunk,
    StreamDirection,
)
from retailprintguard.ingestion.errors import SourceBusyError, SourceValidationError
from retailprintguard.ingestion.safeio import (
    iter_files_no_symlinks,
    parse_json_bytes,
    read_json_object,
    read_regular_file,
    safe_child,
    validate_root,
)

_ZERO_HASH = "0" * 64
_LEDGER_SCHEMA = 1
_METADATA_SCHEMA = 2
_MAX_HEAD_BYTES = 65_536
_MAX_LEDGER_LINE_BYTES = 1_048_576
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_SAFE_STATE = {
    "SEALED",
    "DUPLEX_ACTIVE",
    "DUPLEX_ABORTED",
    "QUEUED",
    "SEND_ARMED",
    "SENDING",
    "SENT_UNCONFIRMED",
    "FAILED_BEFORE_SEND",
    "UNKNOWN_PRINT_STATE",
    "PARTIAL",
    "QUARANTINED",
}
_EVENT_STATUS = {
    "ARCHIVED": "SEALED",
    "DUPLEX_ACTIVE": "DUPLEX_ACTIVE",
    "DUPLEX_ABORTED": "DUPLEX_ABORTED",
    "QUEUED": "QUEUED",
    "PARTIAL_ARCHIVED": "PARTIAL",
    "SEND_ARMED": "SEND_ARMED",
    "SENDING": "SENDING",
    "SENT_UNCONFIRMED": "SENT_UNCONFIRMED",
    "FAILED_BEFORE_SEND": "FAILED_BEFORE_SEND",
    "FAILED_BEFORE_SEND_RECOVERY": "FAILED_BEFORE_SEND",
    "UNKNOWN_PRINT_STATE": "UNKNOWN_PRINT_STATE",
    "UNKNOWN_PRINT_STATE_RECOVERY": "UNKNOWN_PRINT_STATE",
    "MANUAL_RETRY_QUEUED": "QUEUED",
    "RETENTION_DELETE_STARTED": "RETENTION_DELETING",
    "RETENTION_DELETED": "RETENTION_DELETED",
    "QUARANTINED": "QUARANTINED",
}
_EVENT_PREDECESSORS: dict[str, set[str | None]] = {
    "ARCHIVED": {None, "DUPLEX_ACTIVE"},
    "DUPLEX_ACTIVE": {None},
    "DUPLEX_ABORTED": {"ARCHIVED"},
    "QUEUED": {"ARCHIVED"},
    "PARTIAL_ARCHIVED": {"ARCHIVED"},
    "SEND_ARMED": {
        "QUEUED",
        "MANUAL_RETRY_QUEUED",
        "FAILED_BEFORE_SEND",
        "FAILED_BEFORE_SEND_RECOVERY",
    },
    "SENDING": {"SEND_ARMED"},
    "SENT_UNCONFIRMED": {"SENDING", "ARCHIVED"},
    "FAILED_BEFORE_SEND": {"SEND_ARMED", "ARCHIVED"},
    "FAILED_BEFORE_SEND_RECOVERY": {"SEND_ARMED"},
    "UNKNOWN_PRINT_STATE": {"SENDING", "ARCHIVED", "SENT_UNCONFIRMED"},
    "UNKNOWN_PRINT_STATE_RECOVERY": {"SENDING", "ARCHIVED"},
    "MANUAL_RETRY_QUEUED": {
        "QUEUED",
        "MANUAL_RETRY_QUEUED",
        "FAILED_BEFORE_SEND",
        "FAILED_BEFORE_SEND_RECOVERY",
        "UNKNOWN_PRINT_STATE",
        "UNKNOWN_PRINT_STATE_RECOVERY",
    },
    "RETENTION_DELETE_STARTED": {"SENT_UNCONFIRMED"},
    "RETENTION_DELETED": {"RETENTION_DELETE_STARTED"},
}


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SourceValidationError(f"printproxy record is not canonicalizable: {exc}") from exc


def _digest(data: bytes) -> str:
    return hashlib.sha256(data, usedforsecurity=True).hexdigest()


def _split_destination(value: object, label: str) -> Endpoint:
    text = require_string(value, label, maximum=128)
    if text.count(":") != 1:
        raise SourceValidationError(f"{label} must be an IPv4:port endpoint")
    ip_text, port_text = text.rsplit(":", 1)
    if not port_text.isdecimal():
        raise SourceValidationError(f"{label} has an invalid port")
    return endpoint(ip_text, int(port_text), label)


def _require_plain_filename(value: object, label: str) -> str:
    name = require_string(value, label, maximum=255)
    if Path(name).name != name or "\x00" in name:
        raise SourceValidationError(f"{label} is an unsafe filename")
    return name


class PrintProxyV3Adapter:
    def __init__(
        self,
        root: Path,
        *,
        source_instance_id: str,
        devices_by_target: Mapping[tuple[str, int], str],
        hmac_key: bytes | None = None,
        require_hmac: bool = True,
        max_payload_bytes: int = 128 * 1024 * 1024,
        max_ledger_bytes: int = 256 * 1024 * 1024,
        max_records: int = 1_000_000,
    ) -> None:
        if max_payload_bytes < 1 or max_ledger_bytes < 1 or max_records < 1:
            raise ValueError("printproxy ingestion limits must be positive")
        self.root = validate_root(root)
        self.source_instance_id = validate_source_instance_id(source_instance_id)
        self.devices_by_target = dict(devices_by_target)
        self.hmac_key = bytes(hmac_key) if hmac_key is not None else None
        if self.hmac_key is not None and len(self.hmac_key) < 32:
            raise ValueError("printproxy HMAC key must contain at least 32 bytes")
        self.require_hmac = require_hmac
        self.max_payload_bytes = max_payload_bytes
        self.max_ledger_bytes = max_ledger_bytes
        self.max_records = max_records
        self._discovery_cursor = 0

    def discover(self, *, maximum: int) -> Sequence[ImportCandidate]:
        if maximum < 1:
            raise ValueError("maximum must be positive")
        ledgers = iter_files_no_symlinks(self.root, "manifest.jsonl", maximum=4096)
        discovered: list[ImportCandidate] = []
        for ledger_path in ledgers:
            route = ledger_path.parent
            route_key = route.relative_to(self.root).as_posix()
            try:
                discovered.extend(self._discover_route(route))
            except SourceBusyError as exc:
                discovered.append(
                    ImportCandidate(
                        SourceKind.PRINTPROXY_V3,
                        self.source_instance_id,
                        f"printproxy-v3:{self.source_instance_id}:busy:{route_key}",
                        route,
                        context={"busy_error": str(exc)},
                    )
                )
                continue
            except SourceValidationError as exc:
                discovered.append(
                    ImportCandidate(
                        SourceKind.PRINTPROXY_V3,
                        self.source_instance_id,
                        f"printproxy-v3:{self.source_instance_id}:invalid:{route_key}",
                        route,
                        context={"validation_error": str(exc)},
                    )
                )
        if len(discovered) <= maximum:
            self._discovery_cursor = 0
            return tuple(discovered)
        start = self._discovery_cursor % len(discovered)
        rotated = discovered[start:] + discovered[:start]
        self._discovery_cursor = (start + maximum) % len(discovered)
        return tuple(rotated[:maximum])

    def _discover_route(self, route: Path) -> list[ImportCandidate]:
        snapshot = self._read_ledger(route)
        events_by_job: dict[str, list[dict[str, Any]]] = {}
        for record in snapshot["records"]:
            job_value = record.get("job_id")
            if not isinstance(job_value, str):
                raise SourceValidationError("printproxy v3 ledger event has no job UUID")
            job_id = require_uuid(job_value, "printproxy ledger job_id")
            events_by_job.setdefault(job_id, []).append(record)

        candidates: list[ImportCandidate] = []
        route_key = route.relative_to(self.root).as_posix()
        for job_id, job_events in sorted(events_by_job.items()):
            try:
                candidate = self._candidate_from_events(route, job_id, job_events, snapshot)
            except SourceValidationError as exc:
                candidates.append(
                    ImportCandidate(
                        SourceKind.PRINTPROXY_V3,
                        self.source_instance_id,
                        (f"printproxy-v3:{self.source_instance_id}:invalid:{route_key}:{job_id}"),
                        route,
                        context={"validation_error": str(exc)},
                    )
                )
                continue
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _candidate_from_events(
        self,
        route: Path,
        job_id: str,
        events: list[dict[str, Any]],
        snapshot: dict[str, Any],
    ) -> ImportCandidate | None:
        archive: dict[str, Any] | None = None
        previous_event: str | None = None
        deleting = False
        deleted = False
        for record in events:
            if record.get("state_schema_version") != _METADATA_SCHEMA:
                raise SourceValidationError(
                    f"unsupported printproxy v3 state schema for job {job_id}: "
                    f"{record.get('state_schema_version')!r}"
                )
            event = require_string(record.get("event"), "ledger.event", maximum=64)
            status = require_string(record.get("status"), "ledger.status", maximum=64)
            if _EVENT_STATUS.get(event) != status:
                raise SourceValidationError(
                    f"invalid printproxy v3 event/status for job {job_id}: {event}/{status}"
                )
            if event == "QUARANTINED":
                if previous_event == "RETENTION_DELETED":
                    raise SourceValidationError("deleted printproxy job was later quarantined")
            elif previous_event not in _EVENT_PREDECESSORS[event]:
                raise SourceValidationError(
                    f"invalid printproxy v3 transition for job {job_id}: "
                    f"{previous_event!r} -> {event}"
                )
            previous_event = event
            if event == "ARCHIVED":
                if archive is not None:
                    raise SourceValidationError(
                        f"duplicate ARCHIVED event for printproxy job {job_id}"
                    )
                archive = record
            elif event == "RETENTION_DELETE_STARTED":
                deleting = True
            elif event == "RETENTION_DELETED":
                deleted = True

        if archive is None or deleting or deleted:
            return None
        destination = _split_destination(archive.get("destination"), "ledger.destination")
        if any(
            _split_destination(record.get("destination"), "ledger.destination") != destination
            for record in events
        ):
            raise SourceValidationError(f"printproxy job {job_id} crosses physical-printer routes")
        device_id = resolve_device(self.devices_by_target, destination)
        latest = events[-1]
        archive_chain_hash = require_sha256(archive.get("chain_hash"), "archive.chain_hash")
        latest_chain_hash = require_sha256(latest.get("chain_hash"), "latest.chain_hash")
        return ImportCandidate(
            SourceKind.PRINTPROXY_V3,
            self.source_instance_id,
            (
                f"printproxy-v3:{self.source_instance_id}:{job_id}:"
                f"{archive_chain_hash}:{latest_chain_hash}"
            ),
            route,
            device_id,
            {
                "archive": archive,
                "latest": latest,
                "events": events,
                "integrity": snapshot["integrity"],
                "head_raw": snapshot["head_raw"],
            },
        )

    def _line_hmac(self, chain_hash: str) -> str:
        if self.hmac_key is None:
            raise SourceValidationError("printproxy HMAC key is unavailable")
        return hmac.new(self.hmac_key, bytes.fromhex(chain_hash), hashlib.sha256).hexdigest()

    def _head_hmac(self, count: int, chain_hash: str) -> str:
        if self.hmac_key is None:
            raise SourceValidationError("printproxy HMAC key is unavailable")
        payload = canonical_json({"record_count": count, "last_chain_hash": chain_hash})
        return hmac.new(self.hmac_key, payload, hashlib.sha256).hexdigest()

    def _read_ledger(self, route: Path) -> dict[str, Any]:
        ledger_path = safe_child(self.root, route, "manifest.jsonl")
        head_path = safe_child(self.root, route, "manifest.head.json")
        head_before = read_regular_file(self.root, head_path, max_bytes=_MAX_HEAD_BYTES)
        ledger_raw = read_regular_file(self.root, ledger_path, max_bytes=self.max_ledger_bytes)
        head_after = read_regular_file(self.root, head_path, max_bytes=_MAX_HEAD_BYTES)
        if head_before != head_after:
            raise SourceBusyError(f"printproxy manifest head changed during read: {head_path}")
        if ledger_raw and not ledger_raw.endswith(b"\n"):
            raise SourceBusyError(f"printproxy ledger has an uncommitted tail: {ledger_path}")

        records: list[dict[str, Any]] = []
        previous = _ZERO_HASH
        for line_number, raw_line in enumerate(ledger_raw.splitlines(keepends=True), 1):
            if len(raw_line) > _MAX_LEDGER_LINE_BYTES:
                raise SourceValidationError(
                    f"printproxy ledger line {line_number} exceeds size limit"
                )
            record = parse_json_bytes(raw_line, label=f"printproxy ledger line {line_number}")
            if not isinstance(record, dict):
                raise SourceValidationError(
                    f"printproxy ledger line {line_number} is not an object"
                )
            if canonical_json(record) + b"\n" != raw_line:
                raise SourceValidationError(
                    f"printproxy ledger line {line_number} is not canonical JSON"
                )
            if record.get("schema_version") != _LEDGER_SCHEMA:
                raise SourceValidationError(
                    f"unsupported printproxy ledger schema at line {line_number}: "
                    f"{record.get('schema_version')!r}"
                )
            if (
                require_int(record.get("sequence"), f"ledger[{line_number}].sequence", minimum=1)
                != line_number
            ):
                raise SourceValidationError(
                    f"printproxy ledger line {line_number} has invalid sequence"
                )
            require_uuid(record.get("event_id"), f"ledger[{line_number}].event_id")
            parse_datetime(record.get("timestamp"), f"ledger[{line_number}].timestamp")
            if record.get("previous_chain_hash") != previous:
                raise SourceValidationError(
                    f"printproxy ledger line {line_number} previous hash mismatch"
                )
            payload = {
                key: value
                for key, value in record.items()
                if key not in {"previous_chain_hash", "chain_hash", "hmac_sha256"}
            }
            encoded = canonical_json(payload)
            digest = hashlib.sha256(usedforsecurity=True)
            digest.update(bytes.fromhex(previous))
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            calculated = digest.hexdigest()
            actual = require_sha256(record.get("chain_hash"), f"ledger[{line_number}].chain_hash")
            if not hmac.compare_digest(calculated, actual):
                raise SourceValidationError(
                    f"printproxy ledger line {line_number} chain hash mismatch"
                )
            mac = record.get("hmac_sha256")
            if self.hmac_key is not None:
                if not isinstance(mac, str) or not hmac.compare_digest(
                    mac, self._line_hmac(actual)
                ):
                    raise SourceValidationError(
                        f"printproxy ledger line {line_number} HMAC mismatch"
                    )
            elif self.require_hmac or mac is not None:
                raise SourceValidationError(
                    "printproxy ledger requires an HMAC key for trusted ingestion"
                )
            previous = actual
            records.append(record)
            if len(records) > self.max_records:
                raise SourceValidationError("printproxy ledger record limit exceeded")

        head_value = parse_json_bytes(head_after, label="printproxy manifest head")
        if not isinstance(head_value, dict) or canonical_json(head_value) + b"\n" != head_after:
            raise SourceValidationError("printproxy manifest head is not canonical JSON")
        if head_value.get("schema_version") != _LEDGER_SCHEMA:
            raise SourceValidationError(
                f"unsupported printproxy head schema: {head_value.get('schema_version')!r}"
            )
        head_count = require_int(head_value.get("record_count"), "head.record_count")
        head_hash = require_sha256(head_value.get("last_chain_hash"), "head.last_chain_hash")
        if head_count != len(records) or head_hash != previous:
            raise SourceBusyError("printproxy manifest head does not match ledger snapshot")
        head_mac = head_value.get("hmac_sha256")
        if self.hmac_key is not None:
            if not isinstance(head_mac, str) or not hmac.compare_digest(
                head_mac, self._head_hmac(head_count, head_hash)
            ):
                raise SourceValidationError("printproxy manifest head HMAC mismatch")
            integrity = "HMAC_SHA256_AND_HASH_CHAIN_VALIDATED"
        elif self.require_hmac or head_mac is not None:
            raise SourceValidationError("printproxy manifest head requires an HMAC key")
        else:
            integrity = "SHA256_HASH_CHAIN_VALIDATED_NO_HMAC_CONFIGURED"
        return {"records": records, "integrity": integrity, "head_raw": head_after}

    def load(self, candidate: ImportCandidate) -> NormalizedEnvelope:
        if candidate.source_kind is not SourceKind.PRINTPROXY_V3:
            raise SourceValidationError("candidate kind does not match printproxy v3 adapter")
        busy_error = candidate.context.get("busy_error")
        if isinstance(busy_error, str):
            raise SourceBusyError(busy_error)
        validation_error = candidate.context.get("validation_error")
        if isinstance(validation_error, str):
            raise SourceValidationError(validation_error)
        archive = candidate.context.get("archive")
        latest = candidate.context.get("latest")
        events = candidate.context.get("events")
        if (
            not isinstance(archive, dict)
            or not isinstance(latest, dict)
            or not isinstance(events, list)
        ):
            raise SourceValidationError("printproxy candidate has no validated ledger context")
        if archive.get("event") != "ARCHIVED":
            raise SourceValidationError("printproxy candidate is not committed by ARCHIVED")
        if archive.get("state_schema_version") != _METADATA_SCHEMA:
            raise SourceValidationError(
                f"unsupported printproxy v3 state schema: {archive.get('state_schema_version')!r}"
            )
        job_id = require_uuid(archive.get("job_id"), "archive.job_id")
        if latest.get("job_id") != job_id:
            raise SourceValidationError("printproxy latest record belongs to another job")
        if latest.get("state_schema_version") != _METADATA_SCHEMA:
            raise SourceValidationError("printproxy latest state schema is unsupported")
        route = candidate.source_path
        raw_name = _require_plain_filename(archive.get("raw_filename"), "archive.raw_filename")
        if latest.get("raw_filename") != raw_name:
            raise SourceValidationError("printproxy latest record RAW binding mismatch")
        if not raw_name.endswith(f"_{job_id}.raw"):
            raise SourceValidationError("printproxy RAW filename is not bound to job UUID")
        raw_path = safe_child(self.root, route, raw_name)
        raw = read_regular_file(self.root, raw_path, max_bytes=self.max_payload_bytes)
        raw_hash = require_sha256(archive.get("raw_sha256"), "archive.raw_sha256")
        if not hmac.compare_digest(_digest(raw), raw_hash):
            raise SourceValidationError("printproxy RAW SHA-256 mismatch")
        if len(raw) != require_int(
            archive.get("raw_size"), "archive.raw_size", maximum=self.max_payload_bytes
        ):
            raise SourceValidationError("printproxy RAW size mismatch")

        metadata_name = _require_plain_filename(
            latest.get("metadata_filename"), "latest.metadata_filename"
        )
        expected_metadata = Path(raw_name).with_suffix(".json").name
        if metadata_name != expected_metadata:
            raise SourceValidationError("printproxy metadata filename is not bound to RAW")
        metadata_path = safe_child(self.root, route, metadata_name)
        metadata_raw, metadata = read_json_object(
            self.root,
            metadata_path,
            max_bytes=_MAX_METADATA_BYTES,
            label="printproxy operational metadata",
        )
        metadata_hash = require_sha256(latest.get("metadata_sha256"), "latest.metadata_sha256")
        if not hmac.compare_digest(_digest(metadata_raw), metadata_hash):
            raise SourceValidationError("printproxy metadata SHA-256 mismatch")
        if metadata.get("schema_version") != _METADATA_SCHEMA:
            raise SourceValidationError(
                f"unsupported printproxy v3 metadata schema: {metadata.get('schema_version')!r}"
            )
        if metadata.get("job_id") != job_id:
            raise SourceValidationError("printproxy metadata job ID mismatch")
        if metadata.get("raw_filename") != raw_name or metadata.get("raw_sha256") != raw_hash:
            raise SourceValidationError("printproxy metadata RAW binding mismatch")
        if metadata.get("bytes_archived") != len(raw):
            raise SourceValidationError("printproxy metadata archived-byte count mismatch")
        if metadata.get("metadata_filename") != metadata_name:
            raise SourceValidationError("printproxy metadata self-filename binding mismatch")

        device_endpoint = endpoint(
            metadata.get("printer_ip"), metadata.get("printer_port"), "printer"
        )
        ledger_destination = _split_destination(archive.get("destination"), "archive.destination")
        if ledger_destination != device_endpoint:
            raise SourceValidationError("printproxy ledger and metadata destination mismatch")
        device_id = resolve_device(self.devices_by_target, device_endpoint)
        if candidate.device_id is not None and candidate.device_id != device_id:
            raise SourceValidationError("printproxy candidate device mapping changed")
        source_endpoint = optional_endpoint(
            metadata.get("source_ip"), metadata.get("source_port"), "client"
        )
        proxy_endpoint = endpoint(metadata.get("proxy_ip"), metadata.get("proxy_port"), "proxy")

        artifacts: list[ArtifactSnapshot] = [
            ArtifactSnapshot(ArtifactRole.REQUEST_RAW, raw_path, raw_hash, len(raw), raw, True),
            ArtifactSnapshot(
                ArtifactRole.SOURCE_METADATA,
                metadata_path,
                metadata_hash,
                len(metadata_raw),
                metadata_raw,
                True,
                "application/json",
            ),
        ]
        archive_record = canonical_json(archive) + b"\n"
        artifacts.append(
            ArtifactSnapshot(
                ArtifactRole.INTEGRITY_LEDGER_RECORD,
                route / "manifest.jsonl",
                _digest(archive_record),
                len(archive_record),
                archive_record,
                True,
                "application/x-ndjson",
            )
        )
        latest_record = canonical_json(latest) + b"\n"
        if latest_record != archive_record:
            artifacts.append(
                ArtifactSnapshot(
                    ArtifactRole.INTEGRITY_LEDGER_LATEST_RECORD,
                    route / "manifest.jsonl",
                    _digest(latest_record),
                    len(latest_record),
                    latest_record,
                    True,
                    "application/x-ndjson",
                )
            )
        head_raw = candidate.context.get("head_raw")
        if not isinstance(head_raw, bytes):
            raise SourceValidationError("printproxy candidate has no authenticated ledger head")
        artifacts.append(
            ArtifactSnapshot(
                ArtifactRole.INTEGRITY_LEDGER_HEAD,
                route / "manifest.head.json",
                _digest(head_raw),
                len(head_raw),
                head_raw,
                True,
                "application/json",
            )
        )

        warnings: list[str] = []
        for filename_key, hash_key, role, suffix, media_type in (
            (
                "clean_filename",
                "clean_sha256",
                ArtifactRole.NORMALIZED_TEXT,
                ".PULITO.txt",
                "text/plain; charset=utf-8",
            ),
            ("pdf_filename", "pdf_sha256", ArtifactRole.RECEIPT_PDF, ".pdf", "application/pdf"),
        ):
            filename = latest.get(filename_key, metadata.get(filename_key))
            expected_hash = latest.get(hash_key, metadata.get(hash_key))
            if filename is None:
                if expected_hash is not None:
                    raise SourceValidationError(
                        f"printproxy {hash_key} exists without its filename"
                    )
                continue
            name = _require_plain_filename(filename, f"latest.{filename_key}")
            if name != Path(raw_name).with_suffix(suffix).name:
                raise SourceValidationError(f"printproxy {filename_key} is not bound to RAW")
            content_path = safe_child(self.root, route, name)
            content = read_regular_file(self.root, content_path, max_bytes=self.max_payload_bytes)
            content_hash = require_sha256(expected_hash, f"latest.{hash_key}")
            if not hmac.compare_digest(_digest(content), content_hash):
                raise SourceValidationError(f"printproxy {filename_key} SHA-256 mismatch")
            artifacts.append(
                ArtifactSnapshot(
                    role, content_path, content_hash, len(content), content, True, media_type
                )
            )

        response_hex = metadata.get("printer_response_hex")
        if not isinstance(response_hex, str):
            raise SourceValidationError("printproxy printer response preview must be a hex string")
        response_truncated = require_bool(
            metadata.get("printer_response_truncated"), "metadata.printer_response_truncated"
        )
        declared_response_hash = require_sha256(
            metadata.get("printer_response_sha256"), "metadata.printer_response_sha256"
        )
        if response_hex:
            if len(response_hex) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", response_hex):
                raise SourceValidationError("printproxy printer response preview is not valid hex")
            if len(response_hex) > self.max_payload_bytes * 2:
                raise SourceValidationError("printproxy printer response preview exceeds limit")
            response_preview = bytes.fromhex(response_hex)
            response_hash = _digest(response_preview)
            if not response_truncated:
                if not hmac.compare_digest(response_hash, declared_response_hash):
                    raise SourceValidationError(
                        "complete printproxy response preview hash mismatch"
                    )
            else:
                warnings.append("printer response evidence is a truncated preview")
            artifacts.append(
                ArtifactSnapshot(
                    ArtifactRole.RESPONSE_PREVIEW,
                    metadata_path,
                    response_hash,
                    len(response_preview),
                    response_preview,
                    not response_truncated,
                )
            )
        elif not response_truncated and not hmac.compare_digest(
            _digest(b""), declared_response_hash
        ):
            raise SourceValidationError("empty printproxy response SHA-256 mismatch")
        elif response_truncated:
            warnings.append("printer response evidence is a truncated preview")

        sidecar_errors = metadata.get("sidecar_errors")
        if not isinstance(sidecar_errors, list) or any(
            not isinstance(item, str) for item in sidecar_errors
        ):
            raise SourceValidationError("printproxy sidecar_errors must be a string list")
        warnings.extend(item[:4096] for item in sidecar_errors if item)
        physical_confirmed = require_bool(
            metadata.get("physical_print_confirmed"), "metadata.physical_print_confirmed"
        )
        if not physical_confirmed:
            warnings.append("physical print outcome is unconfirmed")
        state = require_string(latest.get("status"), "latest.status", maximum=64)
        if state not in _SAFE_STATE:
            raise SourceValidationError(
                f"unsupported printproxy v3 terminal/current state: {state!r}"
            )
        if metadata.get("state") != state:
            raise SourceValidationError("printproxy metadata and latest ledger state differ")
        complete_by_policy = require_bool(
            metadata.get("complete_by_policy"), "metadata.complete_by_policy"
        )
        bytes_submitted = require_optional_int(
            metadata.get("bytes_submitted_to_socket"),
            "metadata.bytes_submitted_to_socket",
            maximum=self.max_payload_bytes,
        )
        opened_at = parse_datetime(metadata.get("timestamp_start"), "metadata.timestamp_start")
        closed_at = parse_optional_datetime(
            metadata.get("timestamp_archive_complete") or metadata.get("timestamp_last_byte"),
            "metadata.capture_end",
        )
        if closed_at is not None and closed_at < opened_at:
            raise SourceValidationError("printproxy capture end precedes its start")
        aggregate_chunk = StreamChunk(
            sequence=1,
            direction=StreamDirection.CLIENT_TO_DEVICE,
            received_at=opened_at,
            received_unix_ns=None,
            forwarded_unix_ns=None,
            local_write_drain_unix_ns=None,
            monotonic_ns=None,
            job_offset=0,
            session_offset=0,
            byte_count=len(raw),
            sha256=raw_hash,
            local_write_drain_completed=(
                bytes_submitted == len(raw) if bytes_submitted is not None else None
            ),
            forward_status=require_optional_string(
                metadata.get("forward_status"), "metadata.forward_status", maximum=128
            ),
            error=require_optional_string(metadata.get("last_error"), "metadata.last_error"),
        )
        events_copy = [dict(event) for event in events]
        normalized_metadata = {
            "source_contract": {
                "ledger_schema_version": _LEDGER_SCHEMA,
                "metadata_schema_version": _METADATA_SCHEMA,
                "timeline_fidelity": "AGGREGATE_ONLY_NO_RECV_CHUNK_TIMELINE_IN_ARCHIVE_V3",
                "integrity": candidate.context.get("integrity"),
                "archive_chain_hash": archive.get("chain_hash"),
                "latest_chain_hash": latest.get("chain_hash"),
            },
            "archive_record": dict(archive),
            "latest_record": dict(latest),
            "job_events": events_copy,
            "metadata_document": metadata,
        }
        chain_hash = require_sha256(archive.get("chain_hash"), "archive.chain_hash")
        latest_chain_hash = require_sha256(latest.get("chain_hash"), "latest.chain_hash")
        return NormalizedEnvelope(
            source_key=(
                f"printproxy-v3:{self.source_instance_id}:{job_id}:{chain_hash}:{latest_chain_hash}"
            ),
            source_kind=SourceKind.PRINTPROXY_V3,
            source_instance_id=self.source_instance_id,
            device_id=device_id,
            source_job_id=job_id,
            source_session_id=require_optional_string(
                metadata.get("session_id"), "metadata.session_id", maximum=64
            ),
            connection_id=None,
            opened_at=opened_at,
            closed_at=closed_at,
            source_endpoint=source_endpoint,
            proxy_endpoint=proxy_endpoint,
            device_endpoint=device_endpoint,
            status=state,
            complete=complete_by_policy and state not in {"PARTIAL", "DUPLEX_ABORTED"},
            boundary_source=require_optional_string(
                metadata.get("boundary_reason"), "metadata.boundary_reason", maximum=128
            ),
            boundary_confidence=None,
            delivery_evidence="LOCAL_SOCKET_PROGRESS_ONLY_PHYSICAL_PRINT_UNCONFIRMED",
            manifest_sha256=chain_hash,
            parser_version=None,
            artifacts=tuple(artifacts),
            chunks=(aggregate_chunk,) if raw else (),
            documents=(),
            metadata=normalized_metadata,
            warnings=tuple(warnings),
        )
