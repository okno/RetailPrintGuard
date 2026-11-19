"""Retain undecoded fiscal streams without inventing protocol or sale semantics.

Each nonempty direction gets one UNKNOWN evidence record. Its boundaries are
the captured TCP stream, never an inferred fiscal document boundary. No data
from this adapter is promoted to an order, amount, payment or printer status.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from retailprintguard.common.domain import DocumentType, EvidenceLevel, NormalizedDocument

PARSER_NAME = "retailprintguard-opaque-raw"
PARSER_VERSION = "1.0.0"


def parse_opaque(
    request_raw: bytes,
    response_raw: bytes = b"",
    *,
    device_id: str,
    session_id: str | None,
    job_id: str,
    captured_at: datetime,
    manifest_sha256: str,
    source_path: str | Path,
    response_source_path: str | Path | None = None,
) -> tuple[NormalizedDocument, ...]:
    records: list[NormalizedDocument] = []
    for direction, payload, path in (
        ("CLIENT_TO_DEVICE", request_raw, source_path),
        ("DEVICE_TO_CLIENT", response_raw, response_source_path or source_path),
    ):
        if not payload:
            continue
        digest = hashlib.sha256(payload, usedforsecurity=True).hexdigest()
        records.append(
            NormalizedDocument(
                id=uuid5(
                    NAMESPACE_URL,
                    f"retailprintguard:opaque:{device_id}:{job_id}:{direction}:{digest}",
                ),
                source_device_id=device_id,
                source_session_id=session_id,
                source_job_id=job_id,
                type=DocumentType.UNKNOWN,
                subtype="OPAQUE_RAW_STREAM",
                captured_at=captured_at,
                status="UNINTERPRETED",
                normalized_text="",
                encoding=None,
                parser_name=PARSER_NAME,
                parser_version=PARSER_VERSION,
                parse_confidence=0,
                evidence=EvidenceLevel.UNKNOWN,
                source_manifest_sha256=manifest_sha256,
                source_payload_sha256=digest,
                source_path=str(path),
                complete=False,
                warnings=("protocol_semantics_not_implemented",),
                raw_metadata={
                    "direction": direction,
                    "raw_offset": 0,
                    "raw_length": len(payload),
                    "document_boundaries": "UNKNOWN",
                    "fiscal_semantics": "UNKNOWN",
                },
            )
        )
    return tuple(records)


__all__ = ["PARSER_NAME", "PARSER_VERSION", "parse_opaque"]
