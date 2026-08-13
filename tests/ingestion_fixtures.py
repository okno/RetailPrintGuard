from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path
from typing import Any

from retailprintguard.ingestion.printproxy import canonical_json

RCH_TARGET = ("192.0.2.251", 23)
POS_TARGET = ("192.0.2.200", 9100)
TEST_HMAC_KEY = b"synthetic-printproxy-integrity-key-0001"
ZERO_HASH = "0" * 64


def digest(data: bytes) -> str:
    return hashlib.sha256(data, usedforsecurity=True).hexdigest()


def pretty_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def write_rch_job(
    root: Path,
    *,
    parsed: bool = False,
    offline: bool = False,
) -> Path:
    job_dir = root / RCH_TARGET[0] / "2042" / "02" / "03" / "0001"
    job_dir.mkdir(parents=True)
    request = b"\x02synthetic commercial document\x03"
    response = b"\x06"
    job_id = uuid.uuid4().hex
    session_id = f"offline-{digest(request)[:32]}" if offline else uuid.uuid4().hex
    connection_id = f"offline-{digest(response)[:32]}" if offline else uuid.uuid4().hex
    request_name = "file_2274996153.123000000.raw"
    response_name = "response_2274996153.124000000.raw"
    timeline_name = "timeline_2274996153.123000000.jsonl"

    timeline_events = []
    for sequence, direction, payload, received_ns in (
        (1, "CLIENT -> RCH", request, 2_274_996_153_123_000_000),
        (2, "RCH -> CLIENT", response, 2_274_996_153_124_000_000),
    ):
        timeline_events.append(
            {
                "byte_count": len(payload),
                "connection_id": connection_id,
                "direction": direction,
                "error": None,
                "forward_status": "local_write_drain_completed",
                "forwarded_unix_ns": received_ns + 100,
                "job_offset": 0,
                "local_write_drain_completed": True,
                "local_write_drain_unix_ns": received_ns + 200,
                "monotonic_ns": sequence * 1000,
                "received_at": f"2042-02-03T10:22:3{sequence}+00:00",
                "received_unix_ns": received_ns,
                "remote_arrival": None,
                "sequence": sequence,
                "session_id": session_id,
                "session_offset": 0,
                "sha256": digest(payload),
            }
        )
    timeline = b"".join(canonical_json(event) + b"\n" for event in timeline_events)
    (job_dir / request_name).write_bytes(request)
    (job_dir / response_name).write_bytes(response)
    (job_dir / timeline_name).write_bytes(timeline)

    manifest: dict[str, Any] = {
        "schema": "commercialrchproxy.capture.v1",
        "project": "commercialRCHproxy",
        "codice_doc": "0001",
        "job_id": job_id,
        "session_id": session_id,
        "connection_id": connection_id,
        "printer_ip": RCH_TARGET[0],
        "printer_port": RCH_TARGET[1],
        "listen_ip": "192.0.2.231",
        "listen_port": 23,
        "client_ip": "192.0.2.10",
        "client_port": None if offline else 41000,
        "opened_at": "2042-02-03T10:22:31+00:00",
        "closed_at": "2042-02-03T10:22:32+00:00",
        "opened_unix_ns": 2_274_996_151_000_000_000,
        "request_size": len(request),
        "response_size": len(response),
        "raw_complete": True,
        "timeline_complete": True,
        "timeline_event_count": 2,
        "job_boundary_source": "connection_lifecycle",
        "job_boundary_confidence": 0.8,
        "delivery_evidence": "UNCONFIRMED_WITHOUT_PCAP",
        "capture_error": None,
        "timeline_error": None,
        "status": "ready",
        "files": {
            "request_raw": request_name,
            "response_raw": response_name,
            "timeline": timeline_name,
        },
        "sha256": {
            "request_raw": digest(request),
            "response_raw": digest(response),
            "timeline": digest(timeline),
        },
        "timezone": "Europe/Rome",
    }
    manifest_raw = pretty_json(manifest)
    (job_dir / "manifest.json").write_bytes(manifest_raw)
    (job_dir / ".ready").write_bytes(
        pretty_json(
            {
                "schema": "commercialrchproxy.capture.v1",
                "codice_doc": "0001",
                "manifest_sha256": digest(manifest_raw),
                "published_at": "2042-02-03T10:22:33+00:00",
            }
        )
    )

    if parsed:
        output_dir = job_dir / "PHARSED"
        output_dir.mkdir()
        txt_name = "0001_C_11.22.31.000.txt"
        txt = b"RICOSTRUZIONE SINTETICA\nTOTALE 1,00\n"
        (output_dir / txt_name).write_bytes(txt)
        parsed_document = {
            "ordinal": 1,
            "document_id": "synthetic-commercial-1",
            "type": "C",
            "canonical_type": "commerciale",
            "subtype": "observed_commercial",
            "subtype_evidence": "synthetic-test-fixture",
            "complete": True,
            "classification_evidence": "observed_protocol_marker",
            "capture_time_local": "2042-02-03T11:22:31.000+0100",
            "timezone": "Europe/Rome",
            "source": {
                "start_offset": 0,
                "end_offset": len(request),
                "frame_ids": [1],
                "timeline_request_offset": 0,
                "timeline_received_unix_ns": 2_274_996_153_123_000_000,
            },
            "outputs": {
                "txt": {"name": txt_name, "sha256": digest(txt)},
                "pdf": None,
            },
            "semantic": {
                "document_type": "commerciale",
                "lines": [{"description": "Articolo sintetico", "total": "1.00"}],
                "issues": [],
            },
        }
        parsed_metadata = {
            "schema": "commercialrchproxy.pharsed.v1",
            "project": "commercialRCHproxy",
            "parser_version": "0.3.0",
            "codice_doc": "0001",
            "capture_manifest": "../manifest.json",
            "capture_manifest_sha256": digest(manifest_raw),
            "parser_status": "semantic_documents_reconstructed",
            "document_count": 1,
            "documents": [parsed_document],
            "protocol_issues": [],
            "correlations": [],
            "evidence_policy": {"synthetic": True},
        }
        parsed_raw = pretty_json(parsed_metadata)
        (output_dir / "parsed.json").write_bytes(parsed_raw)
        (job_dir / ".parsed").write_bytes(
            pretty_json(
                {
                    "schema": "commercialrchproxy.pharsed.v1",
                    "status": "parsed",
                    "codice_doc": "0001",
                    "document_count": 1,
                    "metadata": "PHARSED/parsed.json",
                    "metadata_sha256": digest(parsed_raw),
                    "completed_at": "2042-02-03T10:22:34+00:00",
                    "parser_version": "0.3.0",
                }
            )
        )
    return job_dir


def write_printproxy_job(
    root: Path,
    *,
    hmac_key: bytes | None = TEST_HMAC_KEY,
    state_schema_version: int = 2,
) -> Path:
    route = root / "pos-synthetic"
    route.mkdir(parents=True)
    job_id = str(uuid.uuid4())
    raw_name = f"2042-02-03_10-22-31_000000_{job_id}.raw"
    metadata_name = Path(raw_name).with_suffix(".json").name
    raw = b"\x1b@Synthetic POS order\n\x1dV\x00"
    raw_hash = digest(raw)
    (route / raw_name).write_bytes(raw)
    metadata = {
        "schema_version": 2,
        "job_id": job_id,
        "session_id": str(uuid.uuid4()),
        "state": "SEALED",
        "timestamp_start": "2042-02-03T10:22:31+00:00",
        "timestamp_last_byte": "2042-02-03T10:22:31.100000+00:00",
        "timestamp_archive_complete": "2042-02-03T10:22:31.200000+00:00",
        "source_ip": "192.0.2.10",
        "source_port": 42000,
        "proxy_ip": "192.0.2.220",
        "proxy_port": 9100,
        "printer_ip": POS_TARGET[0],
        "printer_port": POS_TARGET[1],
        "raw_filename": raw_name,
        "metadata_filename": metadata_name,
        "raw_sha256": raw_hash,
        "bytes_archived": len(raw),
        "bytes_submitted_to_socket": 0,
        "boundary_reason": "client_eof",
        "complete_by_policy": True,
        "forward_status": "sealed_archived",
        "physical_print_confirmed": False,
        "last_error": None,
        "sidecar_errors": [],
        "printer_response_hex": "",
        "printer_response_truncated": False,
        "printer_response_sha256": digest(b""),
        "clean_filename": None,
        "clean_sha256": None,
        "pdf_filename": None,
        "pdf_sha256": None,
    }
    metadata_raw = canonical_json(metadata) + b"\n"
    (route / metadata_name).write_bytes(metadata_raw)
    payload = {
        "event_id": str(uuid.uuid4()),
        "event": "ARCHIVED",
        "job_id": job_id,
        "status": "SEALED",
        "raw_filename": raw_name,
        "raw_sha256": raw_hash,
        "raw_size": len(raw),
        "metadata_filename": metadata_name,
        "metadata_sha256": digest(metadata_raw),
        "source": "192.0.2.10:42000",
        "destination": f"{POS_TARGET[0]}:{POS_TARGET[1]}",
        "attempt_id": None,
        "error": None,
        "operator_action": None,
        "state_schema_version": state_schema_version,
        "delivery_mode": "store_forward",
        "clean_filename": None,
        "clean_sha256": None,
        "pdf_filename": None,
        "pdf_sha256": None,
        "render_status": "disabled",
        "bytes_client_to_printer": 0,
        "bytes_printer_received": 0,
        "bytes_printer_to_client": 0,
        "realtime_status_queries": 0,
        "parsed_realtime_status_queries": 0,
        "printer_response_sha256": digest(b""),
        "printer_response_delivered_sha256": digest(b""),
        "retry_allowed": True,
        "schema_version": 1,
        "sequence": 1,
        "timestamp": "2042-02-03T10:22:31.200000+00:00",
    }
    encoded = canonical_json(payload)
    chain = hashlib.sha256(usedforsecurity=True)
    chain.update(bytes.fromhex(ZERO_HASH))
    chain.update(len(encoded).to_bytes(8, "big"))
    chain.update(encoded)
    chain_hash = chain.hexdigest()
    record = {
        **payload,
        "previous_chain_hash": ZERO_HASH,
        "chain_hash": chain_hash,
        "hmac_sha256": (
            hmac.new(hmac_key, bytes.fromhex(chain_hash), hashlib.sha256).hexdigest()
            if hmac_key is not None
            else None
        ),
    }
    (route / "manifest.jsonl").write_bytes(canonical_json(record) + b"\n")
    head_payload = {"record_count": 1, "last_chain_hash": chain_hash}
    head = {
        "schema_version": 1,
        **head_payload,
        "hmac_sha256": (
            hmac.new(hmac_key, canonical_json(head_payload), hashlib.sha256).hexdigest()
            if hmac_key is not None
            else None
        ),
    }
    (route / "manifest.head.json").write_bytes(canonical_json(head) + b"\n")
    return route


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): digest(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
