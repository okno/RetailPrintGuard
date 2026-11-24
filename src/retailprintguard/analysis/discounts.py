"""Observed discounts, independently of correlation and authorization decisions.

A printed employee label is evidence of the label, not proof of authorization.
Arithmetic alone never attributes a discount to employees. Cross-document
identity, deduplication and reporting periods belong to the calling repository.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from retailprintguard.api.schemas import DocumentView

DISCOUNT_ANALYSIS_VERSION = "rpg-observed-discounts-1.2.0"
_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_MAX_LINES = 32_768
_MAX_TEXT = 2_048
_MAX_SPAN = 1_000_000_000_000
_QUANTITY = re.compile(
    r"^\s*(?P<quantity>\d{1,6}(?:[.,]\d{1,4})?)\s*[xX×]\s*"
    r"(?P<unit>(?:\d{1,9}|\d{1,3}(?:\.\d{3}){1,3}),\d{2})\s*$"
)
_TOTAL = re.compile(r"^(?:TOT|TOTALE(?:\s+COMPLESSIVO)?)\s*[:.]?$", re.I)
_SUBTOTAL = re.compile(r"^SUB(?:-?\s*)?TOT(?:AL[EI]?)?\s*[:.]?$", re.I)
_DISCOUNT_WORD = re.compile(r"\bSCONT[OI]\b", re.I)
_RATE = re.compile(r"(?<!\d)(\d{1,3}(?:[.,]\d{1,4})?)\s*%")
_EMPLOYEE_LABEL = re.compile(
    r"SCONT[OI]\s+DIPENDENT[EI]\s+(?P<rate>\d{1,3}(?:[.,]\d{1,4})?)\s*%"
    r"(?:\s*(?:\[\s*-(?P<repeat_square>\d{1,3}(?:[.,]\d{1,4})?)\s*%\s*\]"
    r"|\(\s*-(?P<repeat_round>\d{1,3}(?:[.,]\d{1,4})?)\s*%\s*\)))?",
    re.I,
)
_GENERIC_LABEL = re.compile(
    r"SCONT[OI]\s+(?P<rate>\d{1,3}(?:[.,]\d{1,4})?)\s*%"
    r"(?:\s*(?:\[\s*-(?P<repeat_square>\d{1,3}(?:[.,]\d{1,4})?)\s*%\s*\]"
    r"|\(\s*-(?P<repeat_round>\d{1,3}(?:[.,]\d{1,4})?)\s*%\s*\)))?",
    re.I,
)
_MONEY_SUFFIX = re.compile(r"\s+(-\s*\d{1,9}(?:\.\d{3})*,\d{2})\s*$")
_TECHNICAL = re.compile(
    r"^(?:TOT(?:ALE)?\b|SUB(?:-?\s*)?TOT(?:AL[EI]?)?\b|IVA\b|DI\s+CUI\s+IVA\b|RESTO\b|CONTANTI\b|"
    r"CONTANTE\b|CARTA\b|BANCOMAT\b|ASSEGNO\b|PAGAMENTO\b|IMPORTO\s+PAGATO\b|"
    r"IMPONIBILE\b|ALIQUOTA\b|EURO\b|DOCUMENTO\b|ORDINE\s*:|TAVOLO\s*:|"
    r"SCONT[OI]\b|[A-Z]\s+\d+(?:[.,]\d+)?\s*%)",
    re.I,
)


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    encoded = str(value)
    if len(encoded) > 128:
        return None
    try:
        result = Decimal(encoded)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return (
        result
        if result.is_finite()
        and result.as_tuple().exponent >= -8
        and result.copy_abs() <= Decimal("999999999999999")
        else None
    )


def _amount(line: Any) -> Decimal | None:
    total = _decimal(_get(line, "line_total"))
    if total is not None:
        return total
    unit = _decimal(_get(line, "unit_price"))
    quantity = _decimal(_get(line, "quantity"))
    return unit * (quantity if quantity is not None else Decimal("1")) if unit is not None else None


def _label(line: Any) -> str:
    value = _get(line, "description") or _get(line, "raw_text") or ""
    value = str(value)
    return " ".join(value.split()) if len(value) <= _MAX_TEXT else ""


def _technical(label: str) -> bool:
    return not label or _TECHNICAL.match(label) is not None


def _active(line: Any) -> bool:
    return not (_get(line, "removed", False) or _get(line, "cancelled", False))


@dataclass(frozen=True, slots=True)
class QuantityContext:
    context_index: int
    product_index: int
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


def confirmed_quantity_contexts(lines: Sequence[Any]) -> tuple[QuantityContext, ...]:
    """Recognize an adjacent quantity/unit explanation only when it reconciles.

    No row is discarded: consumers retain its text and original source span.
    An orphan, mismatch, intervening row or technical next row remains untouched.
    """

    result: list[QuantityContext] = []
    for index in range(min(len(lines) - 1, _MAX_LINES - 1)):
        line, following = lines[index], lines[index + 1]
        raw = str(_get(line, "raw_text") or "")
        if len(raw) > 128 or not _active(line) or not _active(following):
            continue
        match = _QUANTITY.fullmatch(raw)
        if match is None:
            continue
        quantity = Decimal(match.group("quantity").replace(",", "."))
        unit = Decimal(match.group("unit").replace(".", "").replace(",", "."))
        total = _amount(following)
        label = _label(following)
        if (
            quantity <= _ZERO
            or unit <= _ZERO
            or total is None
            or total <= _ZERO
            or _technical(label)
            or _QUANTITY.fullmatch(str(_get(following, "raw_text") or ""))
            or not any(character.isalpha() for character in label)
            or (quantity * unit).quantize(_CENT, rounding=ROUND_HALF_UP) != total
        ):
            continue
        result.append(QuantityContext(index, index + 1, quantity, unit, total))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class DiscountObservation:
    sequence: int
    label: str
    amount: Decimal
    rate_percent: Decimal | None
    kind: Literal["employee_30", "generic_30", "other"]
    basis: Decimal | None
    arithmetic_verified: bool
    source_text: str


@dataclass(frozen=True, slots=True)
class DiscountAnalysis:
    observations: tuple[DiscountObservation, ...]
    employee_total: Decimal
    generic_30_total: Decimal
    other_discount_total: Decimal
    arithmetic_reconciled: bool
    reasons: tuple[str, ...]
    policy_version: str = DISCOUNT_ANALYSIS_VERSION


@dataclass(frozen=True, slots=True)
class DashboardDiscountTotals:
    observed_employee_discount_total: Decimal = _ZERO
    observed_employee_discount_count: int = 0
    observed_generic_30_discount_total: Decimal = _ZERO
    observed_generic_30_discount_count: int = 0
    arithmetic_reconciled_employee_discount_total: Decimal = _ZERO
    arithmetic_reconciled_employee_discount_count: int = 0


def aggregate_document_discounts(documents: Iterable[Any]) -> DashboardDiscountTotals:
    """Aggregate selected document versions, counting each document identity once.

    Counts describe documents, not discount rows or asserted unique sales.
    Callers select the reporting period and current interpretation. Conflicting
    versions of one document fail explicitly; identical amounts or timestamps
    never collapse distinct documents. Cross-document copies need independently
    proven identity and must be resolved by the caller.
    """

    seen: dict[str, DiscountAnalysis] = {}
    employee = generic = reconciled = _ZERO
    employee_count = generic_count = reconciled_count = 0
    for document in documents:
        analysis = analyze_document_discounts(document)
        identifier = _get(document, "id")
        if identifier is not None:
            key = str(identifier)
            if key in seen:
                if seen[key] != analysis:
                    raise ValueError(
                        "Conflicting selected discount interpretations for one document"
                    )
                continue
            seen[key] = analysis
        if analysis.employee_total > _ZERO:
            employee += analysis.employee_total
            employee_count += 1
            if analysis.arithmetic_reconciled:
                reconciled += analysis.employee_total
                reconciled_count += 1
        if analysis.generic_30_total > _ZERO:
            generic += analysis.generic_30_total
            generic_count += 1
    return DashboardDiscountTotals(
        observed_employee_discount_total=employee,
        observed_employee_discount_count=employee_count,
        observed_generic_30_discount_total=generic,
        observed_generic_30_discount_count=generic_count,
        arithmetic_reconciled_employee_discount_total=reconciled,
        arithmetic_reconciled_employee_discount_count=reconciled_count,
    )


def _discount_label(line: Any) -> tuple[str, Decimal | None, str]:
    label = _label(line)
    # Some read models retain only a literal row. The amount suffix is removed
    # for label recognition, but its sign/amount must still agree with the row.
    label = _MONEY_SUFFIX.sub("", label)
    employee = _EMPLOYEE_LABEL.fullmatch(label)
    match = employee or _GENERIC_LABEL.fullmatch(label)
    if match is not None:
        rate = Decimal(match.group("rate").replace(",", "."))
        repeat = match.group("repeat_square") or match.group("repeat_round")
        if repeat is not None and Decimal(repeat.replace(",", ".")) != rate:
            return label, None, "other"
        kind = "employee_30" if employee else "generic_30"
        return label, rate, kind if rate == 30 else "other"
    rates = {Decimal(m.group(1).replace(",", ".")) for m in _RATE.finditer(label)}
    return label, next(iter(rates)) if len(rates) == 1 else None, "other"


def _span_within(span: Any, direction: str, envelope: Any | None = None) -> bool:
    offset, length = _get(span, "offset"), _get(span, "length")
    if (
        _get(span, "direction") != direction
        or type(offset) is not int
        or type(length) is not int
        or offset < 0
        or length <= 0
        or offset > _MAX_SPAN
        or length > _MAX_SPAN
    ):
        return False
    if envelope is None:
        return True
    start, size = _get(envelope, "offset"), _get(envelope, "length")
    return (
        type(start) is int
        and type(size) is int
        and 0 <= start <= _MAX_SPAN
        and 0 < size <= _MAX_SPAN
        and start <= offset
        and offset + length <= start + size
    )


def _epson_soap_receipt(document: Any) -> bool:
    """Identify the parser's fiscal sale, not arbitrary Epson command output."""

    metadata = _get(document, "raw_metadata")
    document_type = _get(document, "type")
    return (
        _get(document, "parser_name") == "retailprintguard-epson-observed"
        and _get(document, "subtype") == "EPSON_COMMERCIAL_SALE_SOAP_OBSERVED"
        and getattr(document_type, "value", document_type) == "COMMERCIAL_DOCUMENT"
        and (
            (
                isinstance(metadata, Mapping)
                and metadata.get("protocol") == "EPSON_FPMATE_SOAP_HTTP"
                and metadata.get("request_root") == "printerFiscalReceipt"
            )
            or isinstance(document, DocumentView)
        )
    )


def _epson_response_total_reconciled(
    document: Any,
    rows: Sequence[Any],
    positive_total: Decimal,
    negative_total: Decimal,
    observations: Sequence[DiscountObservation],
) -> bool:
    """Use Epson's fiscal response as total evidence only with full provenance.

    Epson SOAP sends sale and adjustment commands, then reports the fiscal
    total in the acknowledged device response. It has no printed total row in
    the request. A stored boolean alone is insufficient: both the request
    rows and the response amount must independently reconcile here.
    """

    if not _epson_soap_receipt(document):
        return False
    metadata = _get(document, "raw_metadata")
    projection = isinstance(document, DocumentView)
    receipt_number = str(
        (_get(document, "external_document_code_suffix") if projection else
         metadata.get("fiscal_receipt_number")) or ""
    )
    if (
        _get(document, "complete") is not True
        or _get(document, "status") != "ACKNOWLEDGED"
        or _get(document, "parser_version") != "1.1.0"
        or re.fullmatch(r"[0-9]{1,12}", receipt_number) is None
        or int(receipt_number) <= 0
    ):
        return False
    request_span = None
    if projection:
        sha = _get(document, "sha256")
        if (
            _get(document, "confidence") != 95
            or not isinstance(sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha) is None
            or _get(document, "document_timestamp_evidence")
            not in {"EPSON_SOAP_RESPONSE_PRINTER_CLOCK", "EPSON_SOAP_LOCAL_CLOCK_DST_UNRESOLVED"}
            or not str(_get(document, "normalized_text") or "").startswith(
                "DOCUMENTO COMMERCIALE EPSON (DATI SOAP)\n"
            )
        ):
            return False
    else:
        if (
            metadata.get("response_paired") is not True
            or metadata.get("command_acknowledged") is not True
            or metadata.get("arithmetic_reconciled") is not True
            or metadata.get("payment_reconciled") is not True
            or metadata.get("fiscal_identity_evidence") != "EPSON_SOAP_RESPONSE_FIELDS"
            or metadata.get("text_projection") != "RECONSTRUCTED_FROM_SOAP_NOT_PRINTED_TEXT"
            or re.fullmatch(r"[0-9]{1,12}", str(metadata.get("z_report_number") or ""))
            is None
        ):
            return False
        request_span = {
            "direction": metadata.get("direction"),
            "offset": metadata.get("raw_offset"),
            "length": metadata.get("raw_length"),
        }
        response_span = metadata.get("response_span")
        if (
            not _span_within(request_span, "CLIENT_TO_DEVICE")
            or not _span_within(response_span, "DEVICE_TO_CLIENT")
            or not _span_within(
                metadata.get("fiscal_total_source_span"),
                "DEVICE_TO_CLIENT", response_span,
            )
            or _get(metadata.get("fiscal_total_source_span"), "length") > 128
        ):
            return False
    total = _decimal(_get(document, "gross_total"))
    net = _decimal(_get(document, "net_total"))
    discount = _decimal(_get(document, "discount_total"))
    if total is None or total <= _ZERO or net != total or discount is None:
        return False
    sale_total = surcharge_total = discount_total = _ZERO
    modifiers = None if projection else metadata.get("subtotal_modifiers")
    if not projection and not isinstance(modifiers, list):
        return False
    observed_modifiers: list[tuple[str, str, Decimal]] = []
    discount_rows = 0
    for line in rows:
        if not _active(line) or (
            not projection and not _span_within(
                _get(line, "source"), "CLIENT_TO_DEVICE", request_span
            )
        ):
            return False
        amount = _decimal(_get(line, "line_total"))
        state = _get(line, "state")
        if amount is None:
            return False
        if state == "SALE_ITEM":
            quantity = _decimal(_get(line, "quantity"))
            unit = _decimal(_get(line, "unit_price"))
            if (
                amount <= _ZERO or quantity is None or quantity <= _ZERO
                or unit is None or unit < _ZERO
                or (quantity * unit).quantize(_CENT) != amount
            ):
                return False
            sale_total += amount
        elif state == "SUBTOTAL_DISCOUNT":
            row_discount = _decimal(_get(line, "discount"))
            if amount >= _ZERO or row_discount is None or row_discount != -amount:
                return False
            discount_total += row_discount
            discount_rows += 1
            observed_modifiers.append(("-", _label(line), row_discount))
        elif state == "SUBTOTAL_SURCHARGE":
            row_surcharge = _decimal(_get(line, "surcharge"))
            if amount <= _ZERO or row_surcharge is None or row_surcharge != amount:
                return False
            surcharge_total += row_surcharge
            observed_modifiers.append(("+", _label(line), row_surcharge))
        else:
            return False
    declared_modifiers: list[tuple[str, str, Decimal]] = []
    for modifier in modifiers or ():
        if not isinstance(modifier, Mapping):
            return False
        amount = _decimal(modifier.get("amount"))
        sign, label = modifier.get("sign"), modifier.get("description")
        if sign not in ("+", "-") or not isinstance(label, str) or amount is None:
            return False
        declared_modifiers.append((sign, " ".join(label.split()), amount))
    payments = _get(document, "payments") or ()
    payment_amounts = [_decimal(_get(payment, "amount")) for payment in payments]
    return (
        sale_total > _ZERO
        and (
            projection
            or sale_total == _decimal(metadata.get("observed_item_subtotal"))
        )
        and discount_total == discount == negative_total
        and discount_rows == 1
        and surcharge_total == _ZERO
        and (projection or declared_modifiers == observed_modifiers)
        and discount_rows == len(observations)
        and all(item.arithmetic_verified for item in observations)
        and sale_total + surcharge_total - discount_total == total
        and positive_total == sale_total
        and positive_total - negative_total == total
        and bool(payment_amounts)
        and all(amount is not None and amount >= _ZERO for amount in payment_amounts)
        and sum(payment_amounts, _ZERO) == total
    )


def analyze_document_discounts(document: Any) -> DiscountAnalysis:
    """Read explicit, negative sale rows; never infer an employee entitlement.

    Totals describe observed rows even if arithmetic is unresolved. Consumers
    must inspect ``arithmetic_reconciled`` before treating them as reconciled
    economic evidence. Missing or partial receipts remain distinguishable.
    """

    rows = list(_get(document, "lines") or ())
    reasons: list[str] = []
    if len(rows) > _MAX_LINES:
        return DiscountAnalysis((), _ZERO, _ZERO, _ZERO, False, ("line_limit",))
    if any(
        len(str(_get(line, field) or "")) > _MAX_TEXT
        for line in rows
        for field in ("description", "raw_text")
    ):
        reasons.append("line_text_limit")
    context_indexes = {item.context_index for item in confirmed_quantity_contexts(rows)}
    total_index = next(
        (
            index
            for index, line in enumerate(rows)
            if _TOTAL.fullmatch(_label(line)) and _amount(line) is not None
        ),
        None,
    )
    section = rows if total_index is None else rows[:total_index]
    epson_soap = total_index is None and _epson_soap_receipt(document)
    observations: list[DiscountObservation] = []
    positive_total = _ZERO
    negative_total = _ZERO
    epson_sale_total = _ZERO
    previous_basis: Decimal | None = None
    for index, line in enumerate(section):
        if index in context_indexes:
            previous_basis = None
            continue
        label = _label(line)
        amount = _amount(line)
        if not _active(line):
            reasons.append("inactive_sale_row")
            previous_basis = None
            continue
        if _DISCOUNT_WORD.search(label):
            if amount is None or amount >= _ZERO:
                reasons.append("discount_without_negative_amount")
                previous_basis = None
                continue
            normalized, rate, kind = _discount_label(line)
            observed = -amount
            basis = (
                epson_sale_total
                if epson_soap and _get(line, "state") == "SUBTOTAL_DISCOUNT"
                else previous_basis
            )
            verified = bool(
                basis is not None
                and rate is not None
                and _ZERO < rate <= 100
                and (basis * rate / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
                == observed
            )
            observations.append(
                DiscountObservation(
                    sequence=int(_get(line, "sequence", index + 1)),
                    label=normalized,
                    amount=observed,
                    rate_percent=rate,
                    kind=kind,
                    basis=basis,
                    arithmetic_verified=verified,
                    source_text=str(_get(line, "raw_text") or "")[:_MAX_TEXT],
                )
            )
            negative_total += observed
            if not verified:
                reasons.append("discount_basis_unverified")
            previous_basis = None
        elif _SUBTOTAL.fullmatch(label):
            # A printed subtotal is a control total, not an additional sale.
            # It can serve as the basis of a following document-wide discount
            # only when it equals the observed positive product rows.
            if amount is not None and amount > _ZERO and amount == positive_total:
                previous_basis = amount
            else:
                reasons.append("subtotal_not_reconciled")
                previous_basis = None
        elif amount is not None and amount > _ZERO and not _technical(label):
            positive_total += amount
            if epson_soap and _get(line, "state") == "SALE_ITEM":
                epson_sale_total += amount
            previous_basis = amount
        else:
            if amount is not None and amount < _ZERO:
                reasons.append("unclassified_negative_sale_row")
            previous_basis = None
    if total_index is None:
        if not _epson_response_total_reconciled(
            document, rows, positive_total, negative_total, observations
        ):
            reasons.append("printed_total_missing")
    else:
        if any(_DISCOUNT_WORD.search(_label(line)) for line in rows[total_index + 1 :]):
            reasons.append("discount_after_total")
        total = _decimal(_get(document, "gross_total"))
        if total is None:
            total = _decimal(_get(document, "net_total"))
        printed_total = _amount(rows[total_index])
        if total is None or printed_total != total or positive_total - negative_total != total:
            reasons.append("document_total_not_reconciled")
    if not _get(document, "complete", False):
        reasons.append("document_incomplete")
    unique_reasons = tuple(dict.fromkeys(reasons))
    return DiscountAnalysis(
        observations=tuple(observations),
        employee_total=sum(
            (item.amount for item in observations if item.kind == "employee_30"), _ZERO
        ),
        generic_30_total=sum(
            (item.amount for item in observations if item.kind == "generic_30"), _ZERO
        ),
        other_discount_total=sum(
            (item.amount for item in observations if item.kind == "other"), _ZERO
        ),
        arithmetic_reconciled=bool(observations) and not unique_reasons,
        reasons=unique_reasons,
    )
