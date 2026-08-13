"""Canonical identities for append-only fraud-rule configuration versions."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from retailprintguard.common.hashchain import canonical_json


def _safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def rule_configuration_fingerprint(
    *,
    implementation_version: str,
    enabled: bool,
    severity: str,
    weight: Decimal,
    configuration: dict[str, Any],
) -> str:
    payload = {
        "implementation_version": implementation_version,
        "enabled": enabled,
        "severity": severity,
        "weight": str(weight),
        "configuration": _safe(configuration),
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


__all__ = ["rule_configuration_fingerprint"]
