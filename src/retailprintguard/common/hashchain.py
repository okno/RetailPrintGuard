"""Canonical hash-chain helpers for tamper-evident records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

ZERO_HASH = "0" * 64


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def chained_hash(record: Mapping[str, Any], previous_hash: str | None) -> str:
    parent = (previous_hash or ZERO_HASH).lower()
    if len(parent) != 64 or any(char not in "0123456789abcdef" for char in parent):
        raise ValueError("previous_hash is not a SHA-256 digest")
    return hashlib.sha256(bytes.fromhex(parent) + canonical_json(dict(record))).hexdigest()


def verify_chain(records: list[Mapping[str, Any]]) -> bool:
    previous: str | None = None
    for record in records:
        stored_previous = record.get("previous_hash")
        stored_hash = record.get("record_hash")
        if stored_previous != (previous or ZERO_HASH):
            return False
        payload = {key: value for key, value in record.items() if key not in {"record_hash"}}
        if chained_hash(payload, previous) != stored_hash:
            return False
        previous = str(stored_hash)
    return True
