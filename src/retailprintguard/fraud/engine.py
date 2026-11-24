"""Versioned deterministic fraud rules with human-readable evidence."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from retailprintguard.analysis.discounts import analyze_document_discounts
from retailprintguard.common.domain import (
    NON_SALE_DOCUMENT_TYPES,
    AlertSeverity,
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    FraudFinding,
    NormalizedDocument,
    OrderEvent,
    OrderEventType,
)
from retailprintguard.common.hashchain import ZERO_HASH, canonical_json, chained_hash
from retailprintguard.correlation.engine import (
    CorrelatedTransaction,
    LineChangeType,
    is_dgfe_fiscal_evidence,
    numeric_suffix,
)

ZERO = Decimal("0.0000")
CENT = Decimal("0.0100")
FISCAL_TYPES = {DocumentType.COMMERCIAL_DOCUMENT}
SOURCE_TYPES = {
    DocumentType.ORDER,
    DocumentType.ORDER_CHANGE,
    DocumentType.KITCHEN_ORDER,
    DocumentType.PRE_BILL,
    DocumentType.MANAGEMENT_DOCUMENT,
}
OPENING_TYPES = {DocumentType.ORDER, DocumentType.PRE_BILL}
_SECONDARY_ECONOMIC_RULES = {
    "PREBILL_FISCAL_AMOUNT_DROP",
    "ITEM_REMOVED_AFTER_PREBILL",
    "PRICE_REDUCED_AFTER_PREBILL",
    "EXTREME_PRICE_CHANGE",
    "SAME_REFERENCE_DIFFERENT_AMOUNT",
}
_AUTHORIZED_EMPLOYEE_DISCOUNT_RULES = {
    "MODIFICA_POST_PRECONTO",
    *_SECONDARY_ECONOMIC_RULES,
}
_EMPLOYEE_DISCOUNT_RATE = Decimal("30.0000")
_EMPLOYEE_DISCOUNT_FACTOR = Decimal("0.30")
_MONEY_QUANTUM = Decimal("0.01")
_RCH_CAPTURE_PARSER = "retailprintguard-rch-observed"
_EMPLOYEE_DISCOUNT_LABEL_RE = re.compile(
    r"^SCONTO(?:\s+DIPENDENT(?:E|I))?\s+30(?:[,.]0+)?\s*%"
    r"(?:\s*(?:\[\s*-30(?:[,.]0+)?\s*%\s*\]"
    r"|\(\s*-30(?:[,.]0+)?\s*%\s*\)))?$",
    re.IGNORECASE,
)
_GENERIC_STRUCTURED_DISCOUNT_RE = re.compile(r"^SCONTO$", re.IGNORECASE)
_EMPLOYEE_DISCOUNT_LITERAL_RE = re.compile(
    r"^\s*(?P<label>SCONTO(?:\s+DIPENDENT(?:E|I))?\s+30(?:[,.]0+)?\s*%)"
    r"(?P<structured_suffix>\s*(?:\[\s*-30(?:[,.]0+)?\s*%\s*\]"
    r"|\(\s*-30(?:[,.]0+)?\s*%\s*\)))?"
    r"\s+(?P<amount>-\s*(?:\d{1,3}(?:[. ]\d{3})+|\d+)[,.]\d{2})\s*$",
    re.IGNORECASE,
)


class RuleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    version: int = 1
    enabled: bool = True
    severity: AlertSeverity
    weight: Decimal = Decimal("1.0000")
    parameters: dict[str, Any] = Field(default_factory=dict)


class OperatorPatternStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operator_code: str
    transaction_count: int
    anomalous_transaction_count: int
    void_or_cancellation_count: int = 0


class AuthorizedEmployeeDiscount(BaseModel):
    """Fail-closed proof that one sale reduction is the authorized 30% benefit.

    This is a derived interpretation, never a mutation of the captured fiscal
    evidence.  ``observations`` records whether each amount came from an exact
    printed literal or from a DGFE line modifier and retains the source
    document/line coordinates needed for an audit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rate_percent: Decimal = _EMPLOYEE_DISCOUNT_RATE
    gross_basis: Decimal
    discounted_basis: Decimal
    discount_amount: Decimal
    final_amount: Decimal
    document_ids: tuple[UUID, ...]
    line_references: tuple[tuple[UUID, int], ...] = ()
    observations: tuple[dict[str, Any], ...]


class WhitelistScope(StrEnum):
    GLOBAL = "GLOBAL"
    TRANSACTION = "TRANSACTION"
    DEVICE = "DEVICE"
    OPERATOR = "OPERATOR"
    DOCUMENT = "DOCUMENT"
    REFERENCE = "REFERENCE"


class WhitelistEntry(BaseModel):
    """A documented and time-bounded reason to suppress matching findings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    rule_code: str | None = None
    scope: WhitelistScope
    scope_value: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    valid_from: datetime
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def valid_window(self) -> WhitelistEntry:
        if self.valid_from.tzinfo is None or self.valid_from.utcoffset() is None:
            raise ValueError("valid_from must be timezone-aware")
        if self.valid_until is not None:
            if self.valid_until.tzinfo is None or self.valid_until.utcoffset() is None:
                raise ValueError("valid_until must be timezone-aware")
            if self.valid_until < self.valid_from:
                raise ValueError("valid_until must not precede valid_from")
        return self


class FraudContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction: CorrelatedTransaction
    order_events: tuple[OrderEvent, ...] = ()
    comparison_documents: tuple[NormalizedDocument, ...] = ()
    operator_stats: OperatorPatternStats | None = None
    whitelist_entries: tuple[WhitelistEntry, ...] = ()
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("evaluated_at")
    @classmethod
    def evaluated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        return value.astimezone(UTC)


class _FindingData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str
    explanation: str
    evidence: tuple[dict[str, Any], ...]
    score: int
    confidence: int
    document_ids: tuple[Any, ...] = ()


class SuppressedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    finding: FraudFinding
    whitelist_id: UUID
    reason: str


class FraudEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[FraudFinding, ...]
    suppressed: tuple[SuppressedFinding, ...]


def _rule(
    code: str,
    severity: AlertSeverity,
    **parameters: Any,
) -> RuleDefinition:
    return RuleDefinition(code=code, severity=severity, parameters=parameters)


DEFAULT_RULES: tuple[RuleDefinition, ...] = (
    _rule(
        "MODIFICA_POST_PRECONTO",
        AlertSeverity.HIGH,
        minimum_percent=Decimal("20"),
        minimum_amount=Decimal("1.00"),
    ),
    _rule(
        "PREBILL_FISCAL_AMOUNT_DROP",
        AlertSeverity.HIGH,
        minimum_percent=Decimal("20"),
        minimum_amount=Decimal("1.00"),
    ),
    _rule("ITEM_REMOVED_AFTER_PREBILL", AlertSeverity.HIGH),
    _rule("PRICE_REDUCED_AFTER_PREBILL", AlertSeverity.HIGH),
    _rule(
        "EXTREME_PRICE_CHANGE",
        AlertSeverity.CRITICAL,
        minimum_percent=Decimal("70"),
    ),
    _rule(
        "EXTREME_PRINTED_DISCOUNT",
        AlertSeverity.CRITICAL,
        minimum_percent=Decimal("90"),
    ),
    _rule(
        "SAME_REFERENCE_DIFFERENT_AMOUNT",
        AlertSeverity.HIGH,
        minimum_amount=Decimal("1.00"),
    ),
    _rule(
        "ORDER_WITHOUT_FISCAL_CLOSE",
        AlertSeverity.MEDIUM,
        close_minutes=120,
    ),
    _rule("FISCAL_WITHOUT_SOURCE_ORDER", AlertSeverity.MEDIUM),
    _rule(
        "EXCESSIVE_VOID_OR_CANCELLATION",
        AlertSeverity.HIGH,
        minimum_count=3,
    ),
    _rule("REPRINT_OR_COPY_ANOMALY", AlertSeverity.MEDIUM, maximum_count=2),
    _rule("DOCUMENT_SEQUENCE_GAP", AlertSeverity.HIGH),
    _rule("DUPLICATE_DOCUMENT", AlertSeverity.HIGH),
    _rule("LATE_ORDER_MODIFICATION", AlertSeverity.HIGH, window_minutes=5),
    _rule("NEGATIVE_OR_ZERO_VALUE_ITEM", AlertSeverity.HIGH),
    _rule("TOTAL_LINE_MISMATCH", AlertSeverity.HIGH, tolerance=Decimal("0.01")),
    _rule("PAYMENT_TOTAL_MISMATCH", AlertSeverity.CRITICAL, tolerance=Decimal("0.01")),
    _rule(
        "UNUSUAL_OPERATOR_PATTERN",
        AlertSeverity.MEDIUM,
        minimum_transactions=20,
        minimum_anomaly_rate=Decimal("0.30"),
    ),
)


def _document_time(document: NormalizedDocument) -> datetime:
    return document.document_timestamp or document.captured_at


def _document_total(document: NormalizedDocument) -> Decimal | None:
    return document.gross_total if document.gross_total is not None else document.net_total


def _valid_fiscal_document(document: NormalizedDocument) -> bool:
    return (
        document.type in FISCAL_TYPES
        and document.complete
        and _document_total(document) is not None
    )


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return value if isinstance(value, Decimal) else Decimal(str(value))


def technical_non_sale_line(description: str) -> bool:
    """Identify fiscal/payment projection rows that are not sold articles."""

    normalized = " ".join(
        "".join(
            character if character.isalnum() else " " for character in description.upper()
        ).split()
    )
    labels = {
        "IVA",
        "TOT",
        "TOTALE",
        "TOTALE COMPLESSIVO",
        "RESTO",
        "CONTANTI",
        "PAGAMENTO CONTANTE",
        "IMPORTO PAGATO",
        "IMPONIBILE",
        "ALIQUOTA IVA",
    }
    prefixes = (
        "IVA ",
        "TOT ",
        "TOTALE ",
        "RESTO ",
        "CONTANTI ",
        "PAGAMENTO ",
        "IMPORTO PAGATO ",
        "IMPONIBILE ",
        "ALIQUOTA IVA ",
    )
    return normalized in labels or normalized.startswith(prefixes)


def _money_amount(value: str) -> Decimal | None:
    """Parse a bounded printed amount without accepting a signless discount."""

    compact = value.replace(" ", "")
    if not compact.startswith("-"):
        return None
    unsigned = compact[1:]
    if "," in unsigned and "." in unsigned:
        if unsigned.rfind(",") > unsigned.rfind("."):
            unsigned = unsigned.replace(".", "").replace(",", ".")
        else:
            unsigned = unsigned.replace(",", "")
    else:
        unsigned = unsigned.replace(",", ".")
    try:
        parsed = Decimal(unsigned)
    except InvalidOperation:
        return None
    return parsed.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP) if parsed > ZERO else None


def _money_round(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _employee_discount_label(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.strip().upper().split())
    return _EMPLOYEE_DISCOUNT_LABEL_RE.fullmatch(normalized) is not None


def _dgfe_discount_observations(
    document: NormalizedDocument,
) -> tuple[tuple[dict[str, Any], ...], tuple[tuple[UUID, int], ...]] | None:
    """Read only exact, line-scoped DGFE modifiers whose amount is exactly 30%."""

    metadata = document.raw_metadata
    if (
        not is_dgfe_fiscal_evidence(document)
        or metadata.get("provenance") != "RCH_DGFE_EXPORT"
        or metadata.get("source_export_kind") != "DGFE_DETAIL"
    ):
        return None
    if metadata.get("subtotal_modifiers") not in (None, [], ()):
        return None
    raw_lines = metadata.get("lines")
    if not isinstance(raw_lines, list):
        return None
    observations: list[dict[str, Any]] = []
    references: list[tuple[UUID, int]] = []
    for fallback_sequence, raw_line in enumerate(raw_lines, start=1):
        if not isinstance(raw_line, Mapping):
            return None
        modifiers = raw_line.get("modifiers")
        if modifiers in (None, [], ()):
            continue
        # Multiple stacked modifiers make the effective basis ambiguous.  They
        # remain visible to antifraud rather than being guessed as authorized.
        if not isinstance(modifiers, list) or len(modifiers) != 1:
            return None
        modifier = modifiers[0]
        if not isinstance(modifier, Mapping) or modifier.get("sign") != "-":
            return None
        description = modifier.get("description")
        normalized_description = (
            " ".join(description.strip().upper().split())
            if isinstance(description, str)
            else ""
        )
        if not (
            _EMPLOYEE_DISCOUNT_LABEL_RE.fullmatch(normalized_description)
            or _GENERIC_STRUCTURED_DISCOUNT_RE.fullmatch(normalized_description)
        ):
            return None
        try:
            basis = Decimal(str(raw_line.get("line_total")))
            amount = Decimal(str(modifier.get("amount")))
        except (InvalidOperation, TypeError, ValueError):
            return None
        basis = _money_round(basis)
        amount = _money_round(abs(amount))
        if basis <= ZERO or amount != _money_round(basis * _EMPLOYEE_DISCOUNT_FACTOR):
            return None
        sequence_value = raw_line.get("sequence", fallback_sequence)
        try:
            sequence = int(str(sequence_value))
        except (TypeError, ValueError):
            return None
        if sequence < 0:
            return None
        references.append((document.id, sequence))
        observations.append(
            {
                "kind": "authorized_employee_discount_observation",
                "provenance": "RCH_DGFE_EXPORT",
                "evidence": "DGFE_LINE_MODIFIER",
                "document_id": str(document.id),
                "line_sequence": sequence,
                "description": str(description),
                "gross_basis": str(basis),
                "discount_amount": str(amount),
                "rate_percent": str(_EMPLOYEE_DISCOUNT_RATE),
                "xml_path": metadata.get("xml_path"),
            }
        )
    return tuple(observations), tuple(references)


def _literal_discount_observations(
    document: NormalizedDocument,
) -> tuple[tuple[dict[str, Any], ...], tuple[tuple[UUID, int], ...]] | None:
    """Extract exact printed ``SCONTO 30% -x,xx`` literals from live RCH evidence."""

    metadata = document.raw_metadata
    if (
        document.parser_name != _RCH_CAPTURE_PARSER
        or document.parse_confidence < 70
        or document.evidence is EvidenceLevel.UNKNOWN
        or metadata.get("semantic_evidence") != "INFERRED_FROM_CAPTURE"
        or not isinstance(metadata.get("source_start_offset"), int)
        or not isinstance(metadata.get("source_end_offset"), int)
    ):
        return None
    observations: list[dict[str, Any]] = []
    for line_index, raw_line in enumerate(document.normalized_text.splitlines(), start=1):
        normalized = " ".join(raw_line.strip().upper().split())
        match = _EMPLOYEE_DISCOUNT_LITERAL_RE.fullmatch(raw_line)
        if normalized.startswith("SCONTO") and match is None:
            # An additional or malformed discount is never silently covered by
            # the authorized employee benefit.
            return None
        if match is None:
            continue
        amount = _money_amount(match.group("amount"))
        if amount is None:
            return None
        observations.append(
            {
                "kind": "authorized_employee_discount_observation",
                "provenance": "RCH_CAPTURED_PRINT_LITERAL",
                "evidence": "EXACT_SCONTO_30_PERCENT_LITERAL",
                "document_id": str(document.id),
                "normalized_text_line": line_index,
                "label": " ".join(
                    (
                        match.group("label")
                        + (match.group("structured_suffix") or "")
                    )
                    .upper()
                    .split()
                ),
                "discount_amount": str(amount),
                "rate_percent": str(_EMPLOYEE_DISCOUNT_RATE),
                "source_start_offset": metadata.get("source_start_offset"),
                "source_end_offset": metadata.get("source_end_offset"),
            }
        )
    references = tuple(
        (document.id, line.sequence)
        for line in document.lines
        if _employee_discount_label(line.description)
        and line.line_total is not None
        and line.line_total < ZERO
    )
    return tuple(observations), references


def _normalized_line_label(value: str | None) -> str:
    if value is None:
        return ""
    return " ".join(
        "".join(character if character.isalnum() else " " for character in value.upper()).split()
    )


def _management_sale_section(
    document: NormalizedDocument,
) -> tuple[tuple[DocumentLine, ...], DocumentLine] | None:
    """Return observed rows before the first printed document-total marker."""

    ordered = tuple(sorted(document.lines, key=lambda line: line.sequence))
    if len({line.sequence for line in ordered}) != len(ordered):
        return None
    for index, line in enumerate(ordered):
        if (
            line.line_total is not None
            and _normalized_line_label(line.description)
            in {"TOT", "TOTALE", "TOTALE COMPLESSIVO"}
        ):
            return ordered[:index], line
    return None


def _line_identities(line: DocumentLine) -> set[str]:
    identities: set[str] = set()
    if line.item_code:
        identities.add(f"CODE:{' '.join(str(line.item_code).upper().split())}")
    description = _normalized_line_label(line.description)
    if description:
        identities.add(f"DESC:{description}")
    return identities


def _counter_suffix(value: str | None) -> str | None:
    if value is None:
        return None
    match = re.search(r"(?:^|[-/])(?P<value>\d{1,8})$", value.strip())
    return match.group("value") if match is not None else None


def _strong_management_companion(
    management: NormalizedDocument,
    fiscal: NormalizedDocument,
    sale_lines: tuple[DocumentLine, ...],
) -> bool:
    """Reproduce the correlator's bounded full/suffix fiscal resolution."""

    if (
        management.type is not DocumentType.MANAGEMENT_DOCUMENT
        or not management.complete
        or management.commercial_reference_code is None
        or management.raw_metadata.get("commercial_reference_code_evidence") is None
        or not str(
            management.raw_metadata.get("commercial_reference_code_evidence")
        ).startswith("RCH_PRINTED_")
        or management.source_device_id != fiscal.source_device_id
        or _document_total(management) is None
        or _document_total(fiscal) is None
        or _money_round(_document_total(management) or ZERO)
        != _money_round(_document_total(fiscal) or ZERO)
    ):
        return False
    reference = management.commercial_reference_code.strip()
    exact_reference = (
        fiscal.external_document_code is not None
        and reference == fiscal.external_document_code.strip()
    )
    reference_suffix = _counter_suffix(reference)
    fiscal_suffix = _counter_suffix(fiscal.external_document_code_suffix)
    suffix_reference = (
        reference_suffix is not None
        and fiscal_suffix is not None
        and reference_suffix == fiscal_suffix
        and fiscal.raw_metadata.get("external_document_code_suffix_evidence")
        == "RCH_STATUS_RESPONSE_SUFFIX_SEQUENCE_CONFIRMED"
    )
    if not exact_reference and not suffix_reference:
        return False
    management_order = _normalized_line_label(management.order_code)
    fiscal_order = _normalized_line_label(fiscal.order_code)
    if not management_order or management_order != fiscal_order:
        return False
    management_table = _normalized_line_label(management.table_code)
    fiscal_table = _normalized_line_label(fiscal.table_code)
    if bool(management_table) != bool(fiscal_table):
        return False
    if management_table and management_table != fiscal_table:
        return False
    if abs((_document_time(management) - _document_time(fiscal)).total_seconds()) > 300:
        return False
    management_items = {
        identity
        for line in sale_lines
        if line.line_total is not None
        and line.line_total > ZERO
        for identity in _line_identities(line)
    }
    fiscal_items = {
        identity
        for line in fiscal.lines
        if line.line_total is not None
        and line.line_total > ZERO
        for identity in _line_identities(line)
    }
    return bool(management_items & fiscal_items)


def _management_employee_discount(
    management: NormalizedDocument,
    fiscal: NormalizedDocument,
    sale_lines: tuple[DocumentLine, ...],
    total_line: DocumentLine,
) -> tuple[
    Decimal,
    Decimal,
    Decimal,
    tuple[dict[str, Any], ...],
    tuple[tuple[UUID, int], ...],
] | None:
    """Prove line-paired 30% discounts in a strongly resolved management copy."""

    extracted = _literal_discount_observations(management)
    if extracted is None:
        return None
    literal_observations, literal_references = extracted
    if not literal_observations:
        return None
    declared_total = _document_total(management)
    if (
        declared_total is None
        or total_line.line_total is None
        or _money_round(total_line.line_total) != _money_round(declared_total)
    ):
        return None

    gross_basis = ZERO
    discounted_basis = ZERO
    discount_amount = ZERO
    paired_references: list[tuple[UUID, int]] = []
    observations: list[dict[str, Any]] = []
    for index, line in enumerate(sale_lines):
        value = line.line_total
        if value is None:
            continue
        value = _money_round(value)
        if _employee_discount_label(line.description):
            if value >= ZERO or index == 0:
                return None
            previous = sale_lines[index - 1]
            previous_value = previous.line_total
            if (
                previous_value is None
                or previous_value <= ZERO
                or _employee_discount_label(previous.description)
                or technical_non_sale_line(previous.description or "")
                or previous.source is None
                or line.source is None
                or line.raw_text is None
            ):
                return None
            literal = _EMPLOYEE_DISCOUNT_LITERAL_RE.fullmatch(line.raw_text)
            literal_amount = (
                _money_amount(literal.group("amount")) if literal is not None else None
            )
            amount = _money_round(abs(value))
            basis = _money_round(previous_value)
            if (
                literal_amount != amount
                or amount != _money_round(basis * _EMPLOYEE_DISCOUNT_FACTOR)
            ):
                return None
            discounted_basis += basis
            discount_amount += amount
            paired_references.append((management.id, line.sequence))
            observations.append(
                {
                    "kind": "authorized_employee_discount_observation",
                    "provenance": "RCH_MANAGEMENT_COPY_PRINT_LITERAL",
                    "evidence": "IMMEDIATE_PRECEDING_ITEM_PAIR",
                    "document_id": str(management.id),
                    "fiscal_document_id": str(fiscal.id),
                    "commercial_reference_code": management.commercial_reference_code,
                    "fiscal_external_document_code": fiscal.external_document_code,
                    "fiscal_external_document_code_suffix": (
                        fiscal.external_document_code_suffix
                    ),
                    "fiscal_identity_resolution": (
                        "FULL_COMMERCIAL_REFERENCE"
                        if fiscal.external_document_code is not None
                        and management.commercial_reference_code
                        == fiscal.external_document_code
                        else "SEQUENCE_CONFIRMED_SUFFIX"
                    ),
                    "fiscal_suffix_evidence": fiscal.raw_metadata.get(
                        "external_document_code_suffix_evidence"
                    ),
                    "table_code": management.table_code,
                    "fiscal_time_delta_seconds": abs(
                        int(
                            (
                                _document_time(management)
                                - _document_time(fiscal)
                            ).total_seconds()
                        )
                    ),
                    "line_sequence": line.sequence,
                    "paired_item_sequence": previous.sequence,
                    "paired_item_description": previous.description,
                    "gross_basis": str(basis),
                    "discount_amount": str(amount),
                    "rate_percent": str(_EMPLOYEE_DISCOUNT_RATE),
                    "source": line.source.model_dump(mode="json"),
                }
            )
            continue
        if value <= ZERO or technical_non_sale_line(line.description or ""):
            return None
        gross_basis += value

    gross_basis = _money_round(gross_basis)
    discounted_basis = _money_round(discounted_basis)
    discount_amount = _money_round(discount_amount)
    # Every printed discount must be a one-to-one paired row and vice versa.
    if (
        not observations
        or len(observations) != len(literal_observations)
        or set(paired_references) != set(literal_references)
        or discount_amount
        != _money_round(
            sum(
                (Decimal(str(item["discount_amount"])) for item in literal_observations),
                ZERO,
            )
        )
        or discount_amount != _money_round(discounted_basis * _EMPLOYEE_DISCOUNT_FACTOR)
        or _money_round(gross_basis - discount_amount) != _money_round(declared_total)
    ):
        return None
    return (
        gross_basis,
        discounted_basis,
        discount_amount,
        tuple(observations),
        tuple(paired_references),
    )


def _document_employee_discount(
    document: NormalizedDocument,
) -> tuple[
    Decimal,
    Decimal,
    Decimal,
    tuple[dict[str, Any], ...],
    tuple[tuple[UUID, int], ...],
] | None:
    """Return basis, discounted basis, discount and provenance for one fiscal close."""

    if not _valid_fiscal_document(document):
        return None
    extracted = (
        _dgfe_discount_observations(document)
        if is_dgfe_fiscal_evidence(document)
        else _literal_discount_observations(document)
    )
    if extracted is None:
        return None
    observations, references = extracted
    if not observations:
        return None
    discount = sum(
        (Decimal(str(item["discount_amount"])) for item in observations), ZERO
    )
    discount = _money_round(discount)
    product_totals: list[Decimal] = []
    for line in document.lines:
        if line.line_total is None:
            if line.description and not technical_non_sale_line(line.description):
                return None
            continue
        if _employee_discount_label(line.description):
            if line.line_total >= ZERO:
                return None
            continue
        if line.line_total < ZERO:
            return None
        if line.description and technical_non_sale_line(line.description):
            continue
        product_totals.append(line.line_total)
    if not product_totals:
        return None
    gross_basis = _money_round(sum(product_totals, ZERO))
    discounted_basis = (
        _money_round(
            sum((Decimal(str(item["gross_basis"])) for item in observations), ZERO)
        )
        if is_dgfe_fiscal_evidence(document)
        else gross_basis
    )
    if discount != _money_round(discounted_basis * _EMPLOYEE_DISCOUNT_FACTOR):
        return None
    declared_total = _document_total(document)
    if declared_total is None or _money_round(declared_total) != _money_round(
        gross_basis - discount
    ):
        return None
    if document.discount_total is not None and _money_round(
        abs(document.discount_total)
    ) != discount:
        return None
    return gross_basis, discounted_basis, discount, observations, references


def authorized_employee_discount(
    transaction: CorrelatedTransaction,
) -> AuthorizedEmployeeDiscount | None:
    """Recognize only a fully explained, authorized 30% employee discount.

    The predicate deliberately fails closed.  It requires complete authoritative
    fiscal closes, exact printed or DGFE evidence, per-document arithmetic,
    transaction-level arithmetic, and the absence of item removals, quantity
    changes, arbitrary price changes, or DGFE/stream conflicts.
    """

    if (
        transaction.prebill_total is None
        or transaction.prebill_total <= ZERO
        or transaction.difference_amount is None
        or transaction.difference_amount <= ZERO
        or transaction.fiscal_evidence_mismatches
        or not transaction.fiscal_total_document_ids
    ):
        return None
    authoritative_ids = set(transaction.fiscal_total_document_ids)
    documents = [
        document for document in transaction.documents if document.id in authoritative_ids
    ]
    if len(documents) != len(authoritative_ids):
        return None

    gross_basis = ZERO
    discounted_basis = ZERO
    discount_amount = ZERO
    final_amount = ZERO
    observations: list[dict[str, Any]] = []
    references: list[tuple[UUID, int]] = []
    evidence_document_ids: set[UUID] = set(authoritative_ids)
    used_companions: set[UUID] = set()
    for document in documents:
        result = _document_employee_discount(document)
        if result is None:
            companions: list[
                tuple[NormalizedDocument, tuple[DocumentLine, ...], DocumentLine]
            ] = []
            for candidate in transaction.documents:
                if candidate.id in used_companions:
                    continue
                section = _management_sale_section(candidate)
                if section is None:
                    continue
                sale_lines, total_line = section
                if _strong_management_companion(candidate, document, sale_lines):
                    companions.append((candidate, sale_lines, total_line))
            if len(companions) != 1:
                return None
            companion, sale_lines, total_line = companions[0]
            result = _management_employee_discount(
                companion,
                document,
                sale_lines,
                total_line,
            )
            if result is None:
                return None
            used_companions.add(companion.id)
            evidence_document_ids.add(companion.id)
        (
            basis,
            document_discounted_basis,
            discount,
            document_observations,
            document_references,
        ) = result
        gross_basis += basis
        discounted_basis += document_discounted_basis
        discount_amount += discount
        final_amount += _document_total(document) or ZERO
        observations.extend(document_observations)
        references.extend(document_references)

    gross_basis = _money_round(gross_basis)
    discounted_basis = _money_round(discounted_basis)
    discount_amount = _money_round(discount_amount)
    final_amount = _money_round(final_amount)
    expected_discount = _money_round(discounted_basis * _EMPLOYEE_DISCOUNT_FACTOR)
    if (
        gross_basis != _money_round(transaction.prebill_total)
        or discounted_basis <= ZERO
        or discounted_basis > gross_basis
        or discount_amount != expected_discount
        or discount_amount != _money_round(transaction.difference_amount)
        or final_amount != _money_round(transaction.observed_final_total)
        or final_amount != _money_round(gross_basis - discount_amount)
    ):
        return None

    allowed_changes = {LineChangeType.UNCHANGED, LineChangeType.DISCOUNT_CHANGED}
    discount_change_amount = ZERO
    saw_discount_change = False
    for change in transaction.line_changes:
        if change.change_type in allowed_changes:
            if change.change_type is LineChangeType.DISCOUNT_CHANGED:
                saw_discount_change = True
                before = change.before_discount or ZERO
                after = change.after_discount or ZERO
                if after <= before:
                    return None
                discount_change_amount += after - before
            continue
        if (
            change.change_type is LineChangeType.ADDED
            and _employee_discount_label(change.description)
            and change.after_total is not None
            and change.after_total < ZERO
        ):
            continue
        return None
    if saw_discount_change and _money_round(discount_change_amount) != discount_amount:
        return None

    return AuthorizedEmployeeDiscount(
        gross_basis=gross_basis,
        discounted_basis=discounted_basis,
        discount_amount=discount_amount,
        final_amount=final_amount,
        document_ids=tuple(sorted(evidence_document_ids, key=str)),
        line_references=tuple(sorted(set(references), key=lambda item: (str(item[0]), item[1]))),
        observations=tuple(observations),
    )


def _line_total_candidates(
    document: NormalizedDocument, line_sum: Decimal
) -> tuple[Decimal, ...] | None:
    """Return defensible totals without guessing unknown global adjustments."""

    candidates = {line_sum}
    discount = document.discount_total
    if discount is not None:
        candidates.add(line_sum - discount)

    adjustments: list[Decimal] = []
    metadata = document.raw_metadata
    for key in ("adjustment_total", "surcharge_total", "rounding_adjustment"):
        if key not in metadata:
            continue
        try:
            if metadata[key] is None:
                return None
            adjustments.append(_decimal(metadata[key]))
        except (ArithmeticError, TypeError, ValueError):
            return None
    if "adjustments" in metadata:
        raw_adjustments = metadata["adjustments"]
        if not isinstance(raw_adjustments, (list, tuple)):
            return None
        try:
            for item in raw_adjustments:
                value = item.get("amount") if isinstance(item, dict) else item
                if value is None:
                    return None
                adjustments.append(_decimal(value))
        except (ArithmeticError, TypeError, ValueError):
            return None
    if adjustments:
        adjustment_total = sum(adjustments, ZERO)
        candidates.add(line_sum + adjustment_total)
        if discount is not None:
            candidates.add(line_sum - discount + adjustment_total)
    return tuple(sorted(candidates))


def _amount_evidence(context: FraudContext) -> dict[str, Any]:
    transaction = context.transaction
    return {
        "kind": "amount_comparison",
        "prebill_total": str(transaction.prebill_total),
        "fiscal_aggregate": str(transaction.fiscal_total),
        "difference_amount": str(transaction.difference_amount),
        "difference_percent": str(transaction.difference_percent),
        "split_payment": transaction.split_payment,
    }


def _normalise_scope_value(value: Any) -> str:
    return " ".join(str(value).strip().upper().split())


def _whitelist_matches(
    entry: WhitelistEntry,
    finding: FraudFinding,
    context: FraudContext,
) -> bool:
    evaluated_at = context.evaluated_at
    valid_from = entry.valid_from.astimezone(UTC)
    valid_until = None if entry.valid_until is None else entry.valid_until.astimezone(UTC)
    if evaluated_at < valid_from or (valid_until is not None and evaluated_at > valid_until):
        return False
    if entry.rule_code is not None and entry.rule_code != finding.rule_code:
        return False

    expected = _normalise_scope_value(entry.scope_value)
    documents = context.transaction.documents
    if entry.scope is WhitelistScope.GLOBAL:
        return expected == "*"
    if entry.scope is WhitelistScope.TRANSACTION:
        return expected == _normalise_scope_value(context.transaction.transaction_id)
    if entry.scope is WhitelistScope.DEVICE:
        return expected in {
            _normalise_scope_value(document.source_device_id) for document in documents
        }
    if entry.scope is WhitelistScope.OPERATOR:
        return expected in {
            _normalise_scope_value(document.operator_code)
            for document in documents
            if document.operator_code
        }
    if entry.scope is WhitelistScope.DOCUMENT:
        return expected in {_normalise_scope_value(document.id) for document in documents}
    references = {
        _normalise_scope_value(value)
        for document in documents
        for value in (document.order_code, document.external_document_code)
        if value
    }
    return entry.scope is WhitelistScope.REFERENCE and expected in references


class FraudEngine:
    """Evaluate all configured rules without hidden statistical state."""

    def __init__(self, rules: tuple[RuleDefinition, ...] = DEFAULT_RULES) -> None:
        codes = [rule.code for rule in rules]
        if len(codes) != len(set(codes)):
            raise ValueError("fraud rule codes must be unique")
        self.rules = rules
        self._handlers: dict[str, Callable[[FraudContext, RuleDefinition], _FindingData | None]] = {
            "MODIFICA_POST_PRECONTO": self._post_prebill_change,
            "PREBILL_FISCAL_AMOUNT_DROP": self._prebill_amount_drop,
            "ITEM_REMOVED_AFTER_PREBILL": self._item_removed,
            "PRICE_REDUCED_AFTER_PREBILL": self._price_reduced,
            "EXTREME_PRICE_CHANGE": self._extreme_price_change,
            "EXTREME_PRINTED_DISCOUNT": self._extreme_printed_discount,
            "SAME_REFERENCE_DIFFERENT_AMOUNT": self._same_reference_different_amount,
            "ORDER_WITHOUT_FISCAL_CLOSE": self._order_without_fiscal_close,
            "FISCAL_WITHOUT_SOURCE_ORDER": self._fiscal_without_source_order,
            "EXCESSIVE_VOID_OR_CANCELLATION": self._excessive_void,
            "REPRINT_OR_COPY_ANOMALY": self._reprint_anomaly,
            "DOCUMENT_SEQUENCE_GAP": self._sequence_gap,
            "DUPLICATE_DOCUMENT": self._duplicate_document,
            "LATE_ORDER_MODIFICATION": self._late_modification,
            "NEGATIVE_OR_ZERO_VALUE_ITEM": self._non_positive_item,
            "TOTAL_LINE_MISMATCH": self._total_line_mismatch,
            "PAYMENT_TOTAL_MISMATCH": self._payment_total_mismatch,
            "UNUSUAL_OPERATOR_PATTERN": self._operator_pattern,
        }
        unknown = set(codes) - self._handlers.keys()
        if unknown:
            raise ValueError(f"unsupported fraud rules: {', '.join(sorted(unknown))}")

    def evaluate(self, context: FraudContext) -> tuple[FraudFinding, ...]:
        return self.evaluate_with_suppressions(context).findings

    def evaluate_with_suppressions(self, context: FraudContext) -> FraudEvaluation:
        candidates = self._evaluate_candidates(context)
        visible: list[FraudFinding] = []
        suppressed: list[SuppressedFinding] = []
        for finding in candidates:
            whitelist = next(
                (
                    entry
                    for entry in sorted(context.whitelist_entries, key=lambda item: str(item.id))
                    if _whitelist_matches(entry, finding, context)
                ),
                None,
            )
            if whitelist is None:
                visible.append(finding)
            else:
                suppressed.append(
                    SuppressedFinding(
                        finding=finding,
                        whitelist_id=whitelist.id,
                        reason=whitelist.reason,
                    )
                )
        return FraudEvaluation(findings=tuple(visible), suppressed=tuple(suppressed))

    def _evaluate_candidates(self, context: FraudContext) -> tuple[FraudFinding, ...]:
        if context.transaction.documents and all(
            document.type in NON_SALE_DOCUMENT_TYPES
            for document in context.transaction.documents
        ):
            return ()
        employee_discount = authorized_employee_discount(context.transaction)
        findings: list[FraudFinding] = []
        for rule in self.rules:
            if not rule.enabled:
                continue
            data = self._handlers[rule.code](context, rule)
            if data is None:
                continue
            weighted_score = min(
                100,
                max(0, int((Decimal(data.score) * rule.weight).to_integral_value())),
            )
            document_ids = data.document_ids or tuple(
                document.id for document in context.transaction.documents
            )
            findings.append(
                FraudFinding(
                    rule_code=rule.code,
                    rule_version=rule.version,
                    severity=rule.severity,
                    score=weighted_score,
                    transaction_id=context.transaction.transaction_id,
                    document_ids=document_ids,
                    description=data.description,
                    explanation=data.explanation,
                    evidence=data.evidence,
                    confidence=data.confidence,
                    opened_at=context.evaluated_at,
                )
            )
        # With the primary post-prebill rule enabled, one sale-value reduction
        # is one operational incident.  Detailed removals and price changes are
        # embedded as evidence in that primary finding.  Keeping the legacy
        # symptom rules visible when the materiality threshold is not met made
        # harmless cent-level corrections look more serious than the configured
        # loss policy and inflated both dashboard totals and operator rates.
        #
        # An administrator can still opt into the legacy behaviour by disabling
        # MODIFICA_POST_PRECONTO explicitly; this preserves the documented rule
        # controls without creating auxiliary alerts in the default profile.
        primary_rule_enabled = any(
            rule.code == "MODIFICA_POST_PRECONTO" and rule.enabled for rule in self.rules
        )
        if primary_rule_enabled and context.transaction.prebill_total is not None:
            findings = [
                item for item in findings if item.rule_code not in _SECONDARY_ECONOMIC_RULES
            ]
        if employee_discount is not None:
            # The authorization is arithmetic and evidence based; it covers
            # only the sale-value reduction it fully explains.  Independent
            # sequence, duplicate, payment, cancellation and operator findings
            # remain visible.
            findings = [
                item
                for item in findings
                if item.rule_code not in _AUTHORIZED_EMPLOYEE_DISCOUNT_RULES
            ]
        return tuple(findings)

    def _post_prebill_change(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        """Flag a material post-prebill reduction even without a valid fiscal close.

        A cancelled/partial fiscal attempt or an explicitly marked non-fiscal
        settlement is still economically relevant evidence.  It must not be
        misrepresented as a successful fiscal total, hence this rule uses the
        correlation engine's separately provenance-checked
        ``observed_final_total``.
        """

        tx = context.transaction
        if tx.prebill_total is None or tx.prebill_total <= ZERO:
            return None
        closures = [
            document
            for document in tx.documents
            if (
                _valid_fiscal_document(document)
                or document.type is DocumentType.CANCELLATION
                or (
                    document.type is DocumentType.MANAGEMENT_DOCUMENT
                    and _document_total(document) is not None
                    and (
                        bool(document.raw_metadata.get("economic_close"))
                        or document.raw_metadata.get("settlement_kind") == "ROOM_CHARGE"
                    )
                )
            )
        ]
        if not closures:
            return None
        difference = tx.difference_amount or ZERO
        percent = tx.difference_percent or ZERO
        if difference < _decimal(rule.parameters.get("minimum_amount"), "1") or percent < _decimal(
            rule.parameters.get("minimum_percent"), "20"
        ):
            return None
        line_evidence = tuple(
            {
                "kind": "post_prebill_line_change",
                "change_type": change.change_type.value,
                "item_key": change.item_key,
                "description": change.description,
                "quantity_before": (
                    None if change.before_quantity is None else str(change.before_quantity)
                ),
                "quantity_after": (
                    None if change.after_quantity is None else str(change.after_quantity)
                ),
                "unit_price_before": (
                    None if change.before_unit_price is None else str(change.before_unit_price)
                ),
                "unit_price_after": (
                    None if change.after_unit_price is None else str(change.after_unit_price)
                ),
            }
            for change in tx.line_changes
            if change.change_type is not LineChangeType.UNCHANGED
        )
        status_evidence = {
            "kind": "post_prebill_economic_outcome",
            "prebill_total": str(tx.prebill_total),
            "observed_final_total": str(tx.observed_final_total),
            "fiscal_aggregate": str(tx.fiscal_total),
            "difference_amount": str(difference),
            "difference_percent": str(percent),
            "fiscal_conclusive": any(
                _valid_fiscal_document(document) for document in closures
            ),
            "cancelled_or_partial": any(
                document.type is DocumentType.CANCELLATION or not document.complete
                for document in closures
            ),
            "room_settlement": any(
                document.raw_metadata.get("settlement_kind") == "ROOM_CHARGE"
                for document in closures
            ),
        }
        score = min(100, 70 + int(percent * Decimal("0.3")))
        return _FindingData(
            description="Riduzione del valore di vendita dopo il preconto",
            explanation=(
                f"Il preconto era {tx.prebill_total} EUR; l'esito economico osservato "
                f"e' {tx.observed_final_total} EUR, con differenza {difference} EUR "
                f"({percent}%). L'esito fiscale puo' essere annullato, parziale o non "
                "conclusivo e richiede revisione umana."
            ),
            evidence=(status_evidence, *line_evidence),
            score=score,
            confidence=95 if tx.correlation else 70,
            document_ids=tuple(document.id for document in tx.documents),
        )

    def _prebill_amount_drop(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        tx = context.transaction
        has_fiscal = any(_valid_fiscal_document(document) for document in tx.documents)
        if tx.prebill_total is None or tx.prebill_total <= ZERO or not has_fiscal:
            return None
        difference = tx.difference_amount or ZERO
        percent = tx.difference_percent or ZERO
        if difference < _decimal(rule.parameters.get("minimum_amount"), "1") or percent < _decimal(
            rule.parameters.get("minimum_percent"), "20"
        ):
            return None
        score = min(100, 60 + int(percent * Decimal("0.6")))
        return _FindingData(
            description="Riduzione significativa tra preconto e totale fiscale",
            explanation=(
                f"Il preconto è {tx.prebill_total} EUR e i documenti fiscali correlati "
                f"sommano {tx.fiscal_total} EUR: riduzione {difference} EUR ({percent}%)."
            ),
            evidence=(_amount_evidence(context),),
            score=score,
            confidence=95 if tx.correlation else 65,
        )

    def _item_removed(self, context: FraudContext, rule: RuleDefinition) -> _FindingData | None:
        del rule
        removed = [
            change
            for change in context.transaction.line_changes
            if change.change_type is LineChangeType.REMOVED
        ]
        if not removed:
            return None
        evidence = tuple(
            {
                "kind": "line_removed",
                "item_key": item.item_key,
                "description": item.description,
                "quantity_before": str(item.before_quantity),
                "total_before": str(item.before_total),
                "source": item.before_source,
            }
            for item in removed
        )
        return _FindingData(
            description="Articoli rimossi dopo il preconto",
            explanation=(
                f"Rilevate {len(removed)} righe presenti nel preconto ma assenti nel fiscale."
            ),
            evidence=evidence,
            score=min(100, 65 + len(removed) * 8),
            confidence=90,
        )

    def _price_reduced(self, context: FraudContext, rule: RuleDefinition) -> _FindingData | None:
        del rule
        changed = [
            item
            for item in context.transaction.line_changes
            if item.change_type not in {LineChangeType.ADDED, LineChangeType.REMOVED}
            and item.before_unit_price is not None
            and item.after_unit_price is not None
            and item.after_unit_price < item.before_unit_price
        ]
        if not changed:
            return None
        evidence = tuple(
            {
                "kind": "price_reduction",
                "item_key": item.item_key,
                "description": item.description,
                "before": str(item.before_unit_price),
                "after": str(item.after_unit_price),
                "difference": str(item.before_unit_price - item.after_unit_price),
                "before_source": item.before_source,
                "after_source": item.after_source,
            }
            for item in changed
        )
        return _FindingData(
            description="Prezzo ridotto dopo il preconto",
            explanation=f"Rilevate {len(changed)} riduzioni di prezzo unitario.",
            evidence=evidence,
            score=min(100, 65 + len(changed) * 10),
            confidence=90,
        )

    def _extreme_price_change(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        threshold = _decimal(rule.parameters.get("minimum_percent"), "70")
        extreme: list[dict[str, Any]] = []
        for item in context.transaction.line_changes:
            before, after = item.before_unit_price, item.after_unit_price
            if before is None or after is None or before <= ZERO or after >= before:
                continue
            percent = ((before - after) / before * Decimal("100")).quantize(Decimal("0.0001"))
            if percent >= threshold:
                extreme.append(
                    {
                        "kind": "extreme_price_reduction",
                        "item_key": item.item_key,
                        "description": item.description,
                        "before": str(before),
                        "after": str(after),
                        "reduction_percent": str(percent),
                    }
                )
        if not extreme:
            return None
        highest = max(_decimal(item["reduction_percent"]) for item in extreme)
        return _FindingData(
            description="Variazione estrema del prezzo",
            explanation=(
                f"Almeno un prezzo è stato ridotto del {highest}%, oltre la soglia {threshold}%."
            ),
            evidence=tuple(extreme),
            score=min(100, 75 + int(highest / Decimal("5"))),
            confidence=92,
        )

    def _extreme_printed_discount(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        """Flag a printed sale discount above the threshold without a prebill.

        A management receipt can be the only captured sale document. Require
        the printed percentage, negative discount amount, product basis and
        final total to reconcile before issuing an operational alert.
        """

        threshold = _decimal(rule.parameters.get("minimum_percent"), "90")
        evidence: list[dict[str, Any]] = []
        involved: set[UUID] = set()
        for document in context.transaction.documents:
            if document.type not in {
                DocumentType.PRE_BILL,
                DocumentType.MANAGEMENT_DOCUMENT,
                DocumentType.COMMERCIAL_DOCUMENT,
            }:
                continue
            analysis = analyze_document_discounts(document)
            if not analysis.arithmetic_reconciled:
                continue
            for observation in analysis.observations:
                if (
                    observation.rate_percent is None
                    or observation.rate_percent <= threshold
                    or not observation.arithmetic_verified
                ):
                    continue
                involved.add(document.id)
                evidence.append(
                    {
                        "kind": "extreme_printed_discount",
                        "document_id": str(document.id),
                        "document_type": document.type.value,
                        "line_sequence": observation.sequence,
                        "printed_rate_percent": str(observation.rate_percent),
                        "discount_basis": str(observation.basis),
                        "discount_amount": str(observation.amount),
                        "document_total": str(_document_total(document)),
                        "arithmetic_reconciled": True,
                    }
                )
        if not evidence:
            return None
        return _FindingData(
            description=f"Sconto stampato superiore al {threshold}%",
            explanation=(
                f"Rilevati {len(evidence)} sconti stampati oltre il {threshold}% "
                "con base e totale riconciliati; richiedono revisione contabile."
            ),
            evidence=tuple(evidence),
            score=min(100, 90 + 5 * len(evidence)),
            confidence=95,
            document_ids=tuple(sorted(involved, key=str)),
        )

    def _same_reference_different_amount(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        documents = context.transaction.documents
        reference_documents: dict[str, set[Any]] = {}
        metadata_keys = {
            "reference",
            "order_reference",
            "document_reference",
            "transaction_reference",
        }
        for document in documents:
            raw_references = [document.order_code, document.external_document_code]
            raw_references.extend(document.raw_metadata.get(key) for key in metadata_keys)
            for reference in raw_references:
                if reference is None:
                    continue
                normalized = " ".join(str(reference).upper().split())
                if normalized:
                    reference_documents.setdefault(normalized, set()).add(document.id)
        shared_references = {
            reference: document_ids
            for reference, document_ids in reference_documents.items()
            if len(document_ids) >= 2
        }
        difference = abs(context.transaction.difference_amount or ZERO)
        if (
            not shared_references
            or context.transaction.prebill_total is None
            or not any(_valid_fiscal_document(document) for document in documents)
            or difference < _decimal(rule.parameters.get("minimum_amount"), "1")
        ):
            return None
        return _FindingData(
            description="Stesso riferimento con importi differenti",
            explanation=(
                "Il riferimento condiviso collega il preconto al totale fiscale aggregato, "
                f"ma gli importi differiscono di {difference} EUR."
            ),
            evidence=(
                {
                    **_amount_evidence(context),
                    "shared_references": {
                        reference: sorted(str(document_id) for document_id in document_ids)
                        for reference, document_ids in sorted(shared_references.items())
                    },
                },
            ),
            score=min(100, 65 + int(difference)),
            confidence=90,
        )

    def _order_without_fiscal_close(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        documents = context.transaction.documents
        sources = [document for document in documents if document.type in OPENING_TYPES]
        economic_close = any(
            _valid_fiscal_document(document)
            or (
                document.type is DocumentType.MANAGEMENT_DOCUMENT
                and _document_total(document) is not None
                and (
                    bool(document.raw_metadata.get("economic_close"))
                    or document.raw_metadata.get("settlement_kind") == "ROOM_CHARGE"
                )
            )
            for document in documents
        )
        if not sources or economic_close:
            return None
        latest = max(_document_time(document) for document in sources)
        limit = timedelta(minutes=int(rule.parameters.get("close_minutes", 120)))
        if context.evaluated_at - latest < limit:
            return None
        return _FindingData(
            description="Ordine senza chiusura fiscale",
            explanation=(
                "Nessun documento fiscale osservato entro "
                f"{int(limit.total_seconds() / 60)} minuti."
            ),
            evidence=(
                {
                    "kind": "missing_fiscal_close",
                    "last_source_at": latest.isoformat(),
                    # ``evaluated_at`` is deliberately not evidence identity.  The
                    # finding stays the same while the order remains unclosed;
                    # including the worker clock here changed the fingerprint on
                    # every poll and generated an unbounded stream of duplicate
                    # alerts.  ``FraudAlert.opened_at`` records when the condition
                    # was first observed.
                    "threshold_minutes": int(limit.total_seconds() / 60),
                },
            ),
            score=70,
            confidence=75,
            document_ids=tuple(document.id for document in sources),
        )

    def _fiscal_without_source_order(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        del rule
        documents = context.transaction.documents
        fiscals = [document for document in documents if document.type in FISCAL_TYPES]
        if not fiscals or any(document.type in SOURCE_TYPES for document in documents):
            return None
        return _FindingData(
            description="Documento fiscale senza ordine sorgente",
            explanation="Il documento fiscale non è correlato a comanda, ordine o preconto.",
            evidence=tuple(
                {
                    "kind": "orphan_fiscal_document",
                    "document_id": str(document.id),
                    "external_code": document.external_document_code,
                    "total": str(_document_total(document)),
                }
                for document in fiscals
            ),
            score=65,
            confidence=70,
            document_ids=tuple(document.id for document in fiscals),
        )

    def _excessive_void(self, context: FraudContext, rule: RuleDefinition) -> _FindingData | None:
        cancellation_ids = {
            document.id
            for document in context.transaction.documents
            if document.type in {DocumentType.CANCELLATION, DocumentType.REFUND}
        }
        removal_events = [
            event
            for event in context.order_events
            if event.type is OrderEventType.ITEM_REMOVED
            and event.source_document_id not in cancellation_ids
        ]
        void_events = [
            event
            for event in context.order_events
            if event.type is OrderEventType.ORDER_VOIDED
        ]
        represented_cancellations = {
            event.source_document_id
            for event in void_events
            if event.source_document_id is not None
        }
        count = (
            len(removal_events)
            + len(represented_cancellations)
            + sum(event.source_document_id is None for event in void_events)
            + len(cancellation_ids - represented_cancellations)
        )
        threshold = int(rule.parameters.get("minimum_count", 3))
        if count < threshold:
            return None
        return _FindingData(
            description="Frequenza elevata di annulli o rimozioni",
            explanation=f"La transazione contiene {count} annulli/rimozioni; soglia {threshold}.",
            evidence=(
                {
                    "kind": "void_frequency",
                    "count": count,
                    "threshold": threshold,
                    "event_ids": [
                        str(event.id) for event in (*removal_events, *void_events)
                    ],
                    "cancellation_document_ids": sorted(
                        str(document_id) for document_id in cancellation_ids
                    ),
                },
            ),
            score=min(100, 60 + count * 8),
            confidence=85,
        )

    def _reprint_anomaly(self, context: FraudContext, rule: RuleDefinition) -> _FindingData | None:
        copies = [
            document
            for document in context.transaction.documents
            if document.type in {DocumentType.REPRINT, DocumentType.CONFORMING_COPY}
        ]
        maximum = int(rule.parameters.get("maximum_count", 2))
        if len(copies) <= maximum:
            return None
        return _FindingData(
            description="Numero anomalo di ristampe o copie conformi",
            explanation=f"Osservate {len(copies)} copie/ristampe; limite configurato {maximum}.",
            evidence=tuple(
                {
                    "kind": "copy_or_reprint",
                    "document_id": str(document.id),
                    "type": document.type.value,
                    "captured_at": document.captured_at.isoformat(),
                }
                for document in copies
            ),
            score=min(100, 55 + len(copies) * 8),
            confidence=90,
            document_ids=tuple(document.id for document in copies),
        )

    def _sequence_gap(self, context: FraudContext, rule: RuleDefinition) -> _FindingData | None:
        del rule
        transaction_ids = {document.id for document in context.transaction.documents}
        grouped: dict[tuple[str, str, str, str], list[tuple[int, NormalizedDocument]]] = {}
        candidates = (*context.comparison_documents, *context.transaction.documents)
        seen: set[Any] = set()
        for document in candidates:
            if document.id in seen:
                continue
            seen.add(document.id)
            parsed = numeric_suffix(document.external_document_code)
            if parsed is not None:
                series = (
                    document.source_device_id,
                    document.type.value,
                    _document_time(document).date().isoformat(),
                    parsed[0],
                )
                grouped.setdefault(series, []).append((parsed[1], document))
        gaps: list[dict[str, Any]] = []
        involved: set[Any] = set()
        for (device_id, document_type, business_date, prefix), values in grouped.items():
            ordered = sorted(values, key=lambda item: item[0])
            for (before, before_doc), (after, after_doc) in zip(ordered, ordered[1:], strict=False):
                pair_ids = {before_doc.id, after_doc.id}
                if after - before > 1 and pair_ids & transaction_ids:
                    involved.update((before_doc.id, after_doc.id))
                    gaps.append(
                        {
                            "kind": "sequence_gap",
                            "source_device_id": device_id,
                            "document_type": document_type,
                            "business_date": business_date,
                            "prefix": prefix,
                            "before": before,
                            "after": after,
                            "missing_count": after - before - 1,
                        }
                    )
        if not gaps:
            return None
        if min(involved, key=str) not in transaction_ids:
            # A cross-transaction series anomaly is emitted once, anchored to
            # the deterministic first involved document, never copied onto
            # every transaction in the comparison window.
            return None
        return _FindingData(
            description="Discontinuità nella sequenza dei documenti",
            explanation=f"Rilevate {len(gaps)} discontinuità numeriche nella stessa serie.",
            evidence=tuple(gaps),
            score=min(100, 65 + len(gaps) * 10),
            confidence=80,
            document_ids=tuple(sorted(involved, key=str)),
        )

    def _duplicate_document(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        del rule
        transaction_ids = {document.id for document in context.transaction.documents}
        candidates = (*context.comparison_documents, *context.transaction.documents)
        groups: dict[tuple[str, str, str, str], list[NormalizedDocument]] = {}
        seen: set[Any] = set()
        for document in candidates:
            if document.id in seen:
                continue
            seen.add(document.id)
            if document.external_document_code:
                identity = f"CODE:{document.external_document_code}"
            else:
                identity = f"HASH:{document.source_payload_sha256}"
            key = (
                document.source_device_id,
                document.type.value,
                _document_time(document).date().isoformat(),
                identity,
            )
            groups.setdefault(key, []).append(document)
        duplicate_groups = [
            documents
            for documents in groups.values()
            if len({document.source_job_id for document in documents}) > 1
            and len(documents) > 1
            and documents[0].type
            not in {
                DocumentType.REPRINT,
                DocumentType.CONFORMING_COPY,
                DocumentType.DEVICE_RESPONSE,
            }
            and not (
                documents[0].type is DocumentType.MANAGEMENT_DOCUMENT
                and documents[0].commercial_reference_code is not None
            )
            and {document.id for document in documents} & transaction_ids
        ]
        if not duplicate_groups:
            return None
        involved = tuple(document.id for documents in duplicate_groups for document in documents)
        if min(involved, key=str) not in transaction_ids:
            return None
        return _FindingData(
            description="Identificativo o contenuto documento duplicato",
            explanation=f"Rilevati {len(duplicate_groups)} gruppi duplicati su job distinti.",
            evidence=tuple(
                {
                    "kind": "duplicate_document",
                    "document_ids": [str(document.id) for document in documents],
                    "source_job_ids": [document.source_job_id for document in documents],
                    "external_code": documents[0].external_document_code,
                    "payload_sha256": documents[0].source_payload_sha256,
                }
                for documents in duplicate_groups
            ),
            score=min(100, 70 + len(duplicate_groups) * 10),
            confidence=90,
            document_ids=involved,
        )

    def _late_modification(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        fiscal_times = [
            _document_time(document)
            for document in context.transaction.documents
            if _valid_fiscal_document(document)
        ]
        if not fiscal_times:
            return None
        fiscal_time = min(fiscal_times)
        non_action_document_ids = {
            document.id
            for document in context.transaction.documents
            if document.type
            in {
                DocumentType.COMMERCIAL_DOCUMENT,
                DocumentType.MANAGEMENT_DOCUMENT,
                DocumentType.CONFORMING_COPY,
                DocumentType.REPRINT,
                DocumentType.DEVICE_RESPONSE,
            }
        }
        window = timedelta(minutes=int(rule.parameters.get("window_minutes", 5)))
        modification_types = {
            OrderEventType.ITEM_ADDED,
            OrderEventType.ITEM_REMOVED,
            OrderEventType.QUANTITY_CHANGED,
            OrderEventType.PRICE_CHANGED,
            OrderEventType.DISCOUNT_APPLIED,
            OrderEventType.DISCOUNT_REMOVED,
        }
        events = [
            event
            for event in context.order_events
            if event.type in modification_types
            and fiscal_time < event.occurred_at <= fiscal_time + window
            # Printed fiscal/management outputs and their copies are evidence of
            # an already completed operation, not a POS action.  Treating their
            # derived line differences as a late operator modification generated
            # a second false alert next to MODIFICA_POST_PRECONTO.
            and event.source_document_id not in non_action_document_ids
        ]
        if not events:
            return None
        return _FindingData(
            description="Modifica tardiva dell'ordine",
            explanation=(
                f"Rilevate {len(events)} modifiche dopo la chiusura ed entro "
                f"{int(window.total_seconds() / 60)} minuti."
            ),
            evidence=tuple(
                {
                    "kind": "late_order_event",
                    "event_id": str(event.id),
                    "event_type": event.type.value,
                    "occurred_at": event.occurred_at.isoformat(),
                    "fiscal_at": fiscal_time.isoformat(),
                    "details": event.details,
                }
                for event in events
            ),
            score=min(100, 65 + len(events) * 8),
            confidence=90,
        )

    def _non_positive_item(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        del rule
        employee_discount = authorized_employee_discount(context.transaction)
        authorized_lines = (
            set(employee_discount.line_references)
            if employee_discount is not None
            else set()
        )
        evidence: list[dict[str, Any]] = []
        involved: set[Any] = set()
        for document in context.transaction.documents:
            if document.type not in {
                DocumentType.ORDER,
                DocumentType.PRE_BILL,
                DocumentType.MANAGEMENT_DOCUMENT,
                DocumentType.COMMERCIAL_DOCUMENT,
            }:
                continue
            for line in document.lines:
                if (document.id, line.sequence) in authorized_lines:
                    continue
                effective_unit_price = (
                    line.modified_unit_price
                    if line.modified_unit_price is not None
                    else line.unit_price
                )
                values = [
                    value for value in (effective_unit_price, line.line_total) if value is not None
                ]
                # Zero-valued technical/footer lines (IVA, TOT, resto and
                # payment projections) are common and are not sold articles.
                # Zero-priced menu lines remain visible; only a conservatively
                # recognized technical label is excluded.
                if (
                    values
                    and any(value <= ZERO for value in values)
                    and not (
                        all(value >= ZERO for value in values)
                        and technical_non_sale_line(line.description)
                    )
                ):
                    involved.add(document.id)
                    evidence.append(
                        {
                            "kind": "non_positive_item",
                            "document_id": str(document.id),
                            "line_sequence": line.sequence,
                            "description": line.description,
                            "unit_price": str(effective_unit_price),
                            "line_total": str(line.line_total),
                        }
                    )
        if not evidence:
            return None
        return _FindingData(
            description="Articolo con valore nullo o negativo",
            explanation=(
                f"Rilevate {len(evidence)} righe non positive fuori da annulli, rimborsi "
                "e proiezioni tecniche riconosciute."
            ),
            evidence=tuple(evidence),
            score=min(100, 65 + len(evidence) * 8),
            confidence=90,
            document_ids=tuple(sorted(involved, key=str)),
        )

    def _total_line_mismatch(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        tolerance = _decimal(rule.parameters.get("tolerance"), "0.01")
        employee_discount = authorized_employee_discount(context.transaction)
        authorized_documents = (
            set(employee_discount.document_ids)
            if employee_discount is not None
            else set()
        )
        mismatches: list[dict[str, Any]] = []
        involved: list[Any] = []
        for document in context.transaction.documents:
            if document.id in authorized_documents:
                # The authorization predicate already reconstructed this exact
                # document from its product basis and observed 30% modifier.
                continue
            if document.type in {
                DocumentType.ORDER_CHANGE,
                DocumentType.KITCHEN_ORDER,
                DocumentType.CANCELLATION,
                DocumentType.REFUND,
                DocumentType.CONFORMING_COPY,
                DocumentType.REPRINT,
                DocumentType.DEVICE_RESPONSE,
            }:
                continue
            total = _document_total(document)
            if (
                total is None
                or not document.complete
                or not document.lines
                or any(line.line_total is None for line in document.lines)
            ):
                continue
            line_sum = sum((line.line_total or ZERO) for line in document.lines)
            expected_totals = _line_total_candidates(document, line_sum)
            if expected_totals is None:
                continue
            difference = min(abs(total - expected) for expected in expected_totals)
            if difference > tolerance:
                involved.append(document.id)
                mismatches.append(
                    {
                        "kind": "total_line_mismatch",
                        "document_id": str(document.id),
                        "declared_total": str(total),
                        "line_sum": str(line_sum),
                        "discount_total": (
                            None
                            if document.discount_total is None
                            else str(document.discount_total)
                        ),
                        "reconstructible_totals": [str(value) for value in expected_totals],
                        "difference": str(difference),
                    }
                )
        if not mismatches:
            return None
        return _FindingData(
            description="Totale incompatibile con la somma delle righe",
            explanation=(
                f"Rilevati {len(mismatches)} documenti oltre la tolleranza {tolerance} EUR."
            ),
            evidence=tuple(mismatches),
            score=min(100, 65 + len(mismatches) * 10),
            confidence=92,
            document_ids=tuple(involved),
        )

    def _payment_total_mismatch(
        self, context: FraudContext, rule: RuleDefinition
    ) -> _FindingData | None:
        payments = [
            payment for document in context.transaction.documents for payment in document.payments
        ]
        if not payments or not any(
            document.type in FISCAL_TYPES for document in context.transaction.documents
        ):
            return None
        paid = context.transaction.payment_total
        difference = abs(context.transaction.fiscal_total - paid)
        tolerance = _decimal(rule.parameters.get("tolerance"), "0.01")
        if difference <= tolerance:
            return None
        return _FindingData(
            description="Pagamenti incompatibili con il totale fiscale",
            explanation=(
                f"Totale fiscale {context.transaction.fiscal_total} EUR, pagamenti {paid} EUR, "
                f"differenza {difference} EUR."
            ),
            evidence=(
                {
                    "kind": "payment_total_mismatch",
                    "fiscal_total": str(context.transaction.fiscal_total),
                    "payment_total": str(paid),
                    "difference": str(difference),
                    "observed_payment_records": len(payments),
                },
            ),
            score=min(100, 70 + int(difference)),
            confidence=95,
        )

    def _operator_pattern(self, context: FraudContext, rule: RuleDefinition) -> _FindingData | None:
        stats = context.operator_stats
        if stats is None or not stats.operator_code or stats.transaction_count <= 0:
            return None
        minimum_transactions = int(rule.parameters.get("minimum_transactions", 20))
        rate = Decimal(stats.anomalous_transaction_count) / Decimal(stats.transaction_count)
        threshold = _decimal(rule.parameters.get("minimum_anomaly_rate"), "0.30")
        if stats.transaction_count < minimum_transactions or rate < threshold:
            return None
        return _FindingData(
            description="Concentrazione anomala su un operatore identificato",
            explanation=(
                f"Operatore {stats.operator_code}: {stats.anomalous_transaction_count}/"
                f"{stats.transaction_count} transazioni anomale ({rate:.2%})."
            ),
            evidence=(
                {
                    "kind": "operator_pattern",
                    "operator_code": stats.operator_code,
                    "transaction_count": stats.transaction_count,
                    "anomalous_transaction_count": stats.anomalous_transaction_count,
                    "void_or_cancellation_count": stats.void_or_cancellation_count,
                    "anomaly_rate": str(rate),
                },
            ),
            score=min(100, 50 + int(rate * Decimal("100"))),
            confidence=80,
        )


def finding_fingerprint(finding: FraudFinding) -> str:
    """Stable deduplication key for one rule/version/transaction/evidence set."""

    payload = finding.model_dump(mode="json", exclude={"opened_at"})
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def whitelist_entry_key(entry: WhitelistEntry) -> str:
    """Stable key used by ``fraud_whitelists.entry_key`` for idempotent writes."""

    payload = entry.model_dump(mode="json", exclude={"id"})
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def finding_chain_record(
    finding: FraudFinding,
    *,
    sequence: int,
    previous_hash: str | None,
) -> dict[str, Any]:
    """Return an append-ready canonical alert record with a chain hash."""

    if sequence < 1:
        raise ValueError("sequence must be positive")
    parent = previous_hash or ZERO_HASH
    payload: dict[str, Any] = {
        "sequence": sequence,
        "previous_hash": parent,
        "finding_key": finding_fingerprint(finding),
        "finding": finding.model_dump(mode="json"),
    }
    payload["record_hash"] = chained_hash(payload, parent)
    return payload


__all__ = [
    "AuthorizedEmployeeDiscount",
    "DEFAULT_RULES",
    "FraudContext",
    "FraudEngine",
    "FraudEvaluation",
    "OperatorPatternStats",
    "RuleDefinition",
    "SuppressedFinding",
    "WhitelistEntry",
    "WhitelistScope",
    "authorized_employee_discount",
    "finding_chain_record",
    "finding_fingerprint",
    "whitelist_entry_key",
]
