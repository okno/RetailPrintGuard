"""Common adapter contracts and validation helpers."""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Protocol

from retailprintguard.ingestion.dto import Endpoint, ImportCandidate, NormalizedEnvelope
from retailprintguard.ingestion.errors import SourceValidationError

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class SourceAdapter(Protocol):
    source_instance_id: str
    root: Path

    def discover(self, *, maximum: int) -> Sequence[ImportCandidate]: ...

    def load(self, candidate: ImportCandidate) -> NormalizedEnvelope: ...


def validate_source_instance_id(value: str) -> str:
    if not SOURCE_INSTANCE_RE.fullmatch(value):
        raise ValueError(
            "source_instance_id must contain only letters, digits, dot, underscore or hyphen"
        )
    return value


def require_string(value: object, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SourceValidationError(
            f"{label} must be a non-empty string up to {maximum} characters"
        )
    return value


def require_optional_string(value: object, label: str, *, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    return require_string(value, label, maximum=maximum)


def require_int(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SourceValidationError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def require_optional_int(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = 2**63 - 1,
) -> int | None:
    if value is None:
        return None
    return require_int(value, label, minimum=minimum, maximum=maximum)


def require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise SourceValidationError(f"{label} must be a boolean")
    return value


def require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise SourceValidationError(f"{label} must be a lowercase SHA-256 digest")
    return value


def require_uuid(value: object, label: str) -> str:
    text = require_string(value, label, maximum=64)
    try:
        uuid.UUID(text)
    except ValueError as exc:
        raise SourceValidationError(f"{label} must be a UUID") from exc
    return text


def parse_datetime(value: object, label: str) -> datetime:
    text = require_string(value, label, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SourceValidationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def parse_optional_datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return parse_datetime(value, label)


def endpoint(ip_value: object, port_value: object, label: str) -> Endpoint:
    ip_text = require_string(ip_value, f"{label}.ip", maximum=45)
    try:
        ip = IPv4Address(ip_text)
    except ValueError as exc:
        raise SourceValidationError(f"{label}.ip must be an IPv4 address") from exc
    port = require_int(port_value, f"{label}.port", minimum=1, maximum=65535)
    return Endpoint(str(ip), port)


def optional_endpoint(ip_value: object, port_value: object, label: str) -> Endpoint | None:
    """Return a real observed endpoint, or ``None`` for an explicitly unknown port.

    Both supported historical formats use ``null`` (commercialRCHproxy replay)
    or ``0`` (printproxy legacy recovery) to mean that the source port was not
    observed.  Those sentinels must not be promoted to a real network endpoint.
    The IP is still validated when present so malformed metadata cannot hide
    behind the unknown-port convention.
    """

    if port_value is None or port_value == 0:
        ip_text = require_string(ip_value, f"{label}.ip", maximum=45)
        try:
            IPv4Address(ip_text)
        except ValueError as exc:
            raise SourceValidationError(f"{label}.ip must be an IPv4 address") from exc
        return None
    return endpoint(ip_value, port_value, label)


def resolve_device(
    mapping: Mapping[tuple[str, int], str],
    device_endpoint: Endpoint,
) -> str:
    device_id = mapping.get((device_endpoint.ip, device_endpoint.port))
    if device_id is None:
        raise SourceValidationError(
            "captured target endpoint is not mapped to an enabled configured device: "
            f"{device_endpoint.ip}:{device_endpoint.port}"
        )
    return device_id
