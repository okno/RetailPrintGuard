"""Conservative service-mode classification from observed order labels.

An ``Ordine`` label denotes takeaway and a ``Tavolo`` label denotes service
on site.  Neither absence nor conflicting labels support a forced choice.
The values used here must come from the captured print, not configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ServiceMode = Literal["DINE_IN", "TAKEAWAY", "UNDETERMINED"]


@dataclass(frozen=True)
class ServiceModeDecision:
    mode: ServiceMode
    evidence: str


def classify_service_mode(
    *,
    order_code: str | None,
    table_code: str | None,
    table_evidence: str | None = None,
) -> ServiceModeDecision:
    """Classify only explicit, nonempty printed order/table references."""

    order_seen = bool(order_code and order_code.strip())
    table_seen = bool(table_code and table_code.strip())
    if order_seen and table_seen:
        return ServiceModeDecision("UNDETERMINED", "CONFLICTING_ORDER_AND_TABLE_MARKERS")
    if table_seen:
        return ServiceModeDecision("DINE_IN", table_evidence or "OBSERVED_TABLE_MARKER")
    if order_seen:
        return ServiceModeDecision("TAKEAWAY", "OBSERVED_ORDER_MARKER")
    return ServiceModeDecision("UNDETERMINED", "NO_ORDER_OR_TABLE_MARKER")
