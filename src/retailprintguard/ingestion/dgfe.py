"""Read-only adapter for official RCH/AE fiscal-memory XML exports.

The adapter deliberately consumes files already exported by the fiscal device.
It never opens a socket, discovers a printer, or sends a protocol command.  The
original XML is persisted byte-for-byte; all structured values are append-only
interpretations with explicit ``RCH_DGFE_EXPORT`` provenance.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from retailprintguard.common.domain import DocumentType
from retailprintguard.ingestion.adapters import validate_source_instance_id
from retailprintguard.ingestion.dto import (
    ArtifactRole,
    ArtifactSnapshot,
    Endpoint,
    ImportCandidate,
    NormalizedDocument,
    NormalizedEnvelope,
    SourceKind,
)
from retailprintguard.ingestion.errors import SourceBusyError, SourceValidationError
from retailprintguard.ingestion.safeio import contained_path, read_regular_file, validate_root

DETAIL_NAMESPACE = (
    "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/"
    "memoriadettaglio/export/v2.0"
)
SUMMARY_NAMESPACE = (
    "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/"
    "memoriariepilogo/export/v2.0"
)
XMLDSIG_NAMESPACE = "http://www.w3.org/2000/09/xmldsig#"
_ALLOWED_NAMESPACES = frozenset(
    {
        DETAIL_NAMESPACE,
        SUMMARY_NAMESPACE,
        XMLDSIG_NAMESPACE,
        "http://www.w3.org/XML/1998/namespace",
        "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/corrispettivi/v1.0",
        "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/corrispettivi/dati/v1.0",
        "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/doccommercialilotteria/v1.0",
        (
            "http://ivaservizi.agenziaentrate.gov.it/docs/xsd/"
            "doccommercialilotteria/esito/v1.0"
        ),
    }
)

_MAX_XML_BYTES = 64 * 1024 * 1024
_MAX_ROOT_PREFIX_BYTES = 64 * 1024
_MAX_NODES = 250_000
_MAX_DEPTH = 64
_MAX_ATTRIBUTES = 32
_MAX_TEXT_NODE = 8 * 1024 * 1024
_MAX_TOTAL_TEXT = 48 * 1024 * 1024
_MAX_DOCUMENTS = 50_000
_MAX_LINES_PER_DOCUMENT = 10_000
_MAX_PAYMENTS_PER_DOCUMENT = 1_000
_SERIAL_RE = re.compile(r"^[A-Za-z0-9]{10,32}$")
_DECIMAL_RE = re.compile(r"^-?[0-9]{1,15}(?:\.[0-9]{1,4})?$")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data, usedforsecurity=True).hexdigest()


def _split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, local_name = tag[1:].split("}", 1)
        return namespace, local_name
    return "", tag


def _local(element: ET.Element) -> str:
    return _split_tag(element.tag)[1]


def _direct(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child) == name]


def _one(
    element: ET.Element,
    name: str,
    *,
    required: bool = True,
) -> ET.Element | None:
    values = _direct(element, name)
    if len(values) > 1 or (required and not values):
        qualifier = "exactly one" if required else "at most one"
        raise SourceValidationError(f"{_local(element)} must contain {qualifier} {name}")
    return values[0] if values else None


def _descendants(element: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in element.iter():
        if child is not element and _local(child) == name:
            yield child


def _text(
    element: ET.Element | None,
    label: str,
    *,
    required: bool = True,
    maximum: int = 4096,
) -> str | None:
    if element is None:
        if required:
            raise SourceValidationError(f"missing {label}")
        return None
    value = (element.text or "").strip()
    if not value:
        if required:
            raise SourceValidationError(f"{label} must not be empty")
        return None
    if len(value) > maximum:
        raise SourceValidationError(f"{label} exceeds {maximum} characters")
    return value


def _child_text(
    element: ET.Element,
    name: str,
    *,
    required: bool = True,
    maximum: int = 4096,
) -> str | None:
    return _text(
        _one(element, name, required=required),
        f"{_local(element)}.{name}",
        required=required,
        maximum=maximum,
    )


def _integer(value: str | None, label: str, *, positive: bool = False) -> int:
    if value is None or not value.isascii() or not value.isdigit():
        raise SourceValidationError(f"{label} must be an integer")
    parsed = int(value)
    if parsed > 999_999_999_999_999 or (positive and parsed < 1):
        raise SourceValidationError(f"{label} is outside the supported range")
    return parsed


def _decimal(value: str | None, label: str) -> Decimal:
    if value is None or not _DECIMAL_RE.fullmatch(value):
        raise SourceValidationError(f"{label} must be a bounded XML decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise SourceValidationError(f"{label} is not a decimal") from exc
    if not parsed.is_finite() or abs(parsed) > Decimal("999999999999999.9999"):
        raise SourceValidationError(f"{label} is outside the supported range")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _parse_bool(value: str | None, label: str) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise SourceValidationError(f"{label} must be an XML boolean")


def _parse_timestamp(value: str | None, label: str, timezone: ZoneInfo) -> datetime:
    if value is None or len(value) > 64:
        raise SourceValidationError(f"{label} must be a bounded xs:dateTime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceValidationError(f"{label} must be an xs:dateTime") from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)

    # A fiscal clock normally exports local wall time.  Reject ambiguous or
    # nonexistent DST values rather than silently selecting a fold.
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = parsed.replace(tzinfo=timezone, fold=fold)
        roundtrip = candidate.astimezone(UTC).astimezone(timezone)
        if roundtrip.replace(tzinfo=None) == parsed and roundtrip.fold == fold:
            candidates.append(candidate)
    unique_offsets = {candidate.utcoffset() for candidate in candidates}
    if not candidates:
        raise SourceValidationError(f"{label} is a nonexistent local time in {timezone.key}")
    if len(unique_offsets) > 1:
        raise SourceValidationError(f"{label} is ambiguous in {timezone.key}; offset required")
    return candidates[0].astimezone(UTC)


def _validate_tree(root: ET.Element) -> None:
    count = 0
    total_text = 0
    stack: list[tuple[ET.Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > _MAX_NODES:
            raise SourceValidationError(f"XML exceeds {_MAX_NODES} elements")
        if depth > _MAX_DEPTH:
            raise SourceValidationError(f"XML exceeds depth {_MAX_DEPTH}")
        if not isinstance(element.tag, str):
            raise SourceValidationError("XML contains an unsupported node type")
        namespace, local_name = _split_tag(element.tag)
        if namespace not in _ALLOWED_NAMESPACES:
            raise SourceValidationError(f"unsupported XML namespace: {namespace!r}")
        if not local_name or len(local_name) > 191:
            raise SourceValidationError("XML element name is invalid")
        if len(element.attrib) > _MAX_ATTRIBUTES:
            raise SourceValidationError("XML element has too many attributes")
        for attribute_name, attribute_value in element.attrib.items():
            attribute_namespace, _ = _split_tag(attribute_name)
            if attribute_namespace and attribute_namespace not in _ALLOWED_NAMESPACES:
                raise SourceValidationError(
                    f"unsupported XML attribute namespace: {attribute_namespace!r}"
                )
            if len(attribute_name) > 191 or len(attribute_value) > 8192:
                raise SourceValidationError("XML attribute exceeds the supported bound")
        for value in (element.text, element.tail):
            if value:
                encoded_size = len(value.encode("utf-8", errors="strict"))
                if encoded_size > _MAX_TEXT_NODE:
                    raise SourceValidationError("XML text node exceeds the supported bound")
                total_text += encoded_size
                if total_text > _MAX_TOTAL_TEXT:
                    raise SourceValidationError("XML text exceeds the aggregate bound")
        stack.extend((child, depth + 1) for child in reversed(element))


def _classify_root(root: ET.Element) -> SourceKind:
    namespace, local_name = _split_tag(root.tag)
    if local_name == "MemoriaDettaglio" and namespace == DETAIL_NAMESPACE:
        return SourceKind.RCH_XTOOLS_DGFE_V1
    if local_name == "MemoriaRiepilogo" and namespace == SUMMARY_NAMESPACE:
        return SourceKind.RCH_XTOOLS_FISCAL_SUMMARY_V1
    raise SourceValidationError(
        f"unsupported fiscal-memory XML root: {{{namespace}}}{local_name}"
    )


def _root_kind_prefix(root: Path, path: Path) -> SourceKind:
    resolved = contained_path(root, path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        raise SourceValidationError(f"cannot inspect XML export: {resolved}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_XML_BYTES:
            raise SourceValidationError("XML export is not a bounded regular file")
        parser = ET.XMLPullParser(events=("start",))
        total = 0
        while total < _MAX_ROOT_PREFIX_BYTES:
            chunk = os.read(fd, min(8192, _MAX_ROOT_PREFIX_BYTES - total))
            if not chunk:
                break
            total += len(chunk)
            if b"<!doctype" in chunk.lower() or b"<!entity" in chunk.lower():
                raise SourceValidationError("DTD and entity declarations are forbidden")
            try:
                parser.feed(chunk)
            except ET.ParseError as exc:
                raise SourceValidationError(f"invalid XML prologue: {exc}") from exc
            for _event, element in parser.read_events():
                return _classify_root(element)
        raise SourceValidationError(
            f"XML root was not found within {_MAX_ROOT_PREFIX_BYTES} bytes"
        )
    except OSError as exc:
        raise SourceBusyError(f"cannot inspect XML export: {resolved}: {exc}") from exc
    finally:
        os.close(fd)


def _discover_xml(root: Path, maximum: int) -> tuple[tuple[Path, SourceKind], ...]:
    if maximum < 1:
        raise ValueError("maximum must be positive")
    root_resolved = validate_root(root)
    found: list[tuple[Path, SourceKind]] = []

    def walk_error(error: OSError) -> None:
        raise SourceValidationError(f"cannot traverse DGFE source tree: {error}") from error

    for base, directories, files in os.walk(
        root_resolved, followlinks=False, onerror=walk_error
    ):
        base_path = Path(base)
        directories[:] = [
            name for name in directories if not (base_path / name).is_symlink()
        ]
        for name in sorted(files):
            if not name.lower().endswith(".xml"):
                continue
            path = base_path / name
            if path.is_symlink():
                continue
            kind = _root_kind_prefix(root_resolved, path)
            found.append((path, kind))
            if len(found) > maximum:
                raise SourceValidationError(
                    f"DGFE discovery exceeds the {maximum}-candidate limit"
                )
    return tuple(sorted(found, key=lambda item: str(item[0])))


def _period(
    root: ET.Element,
    *,
    timezone: ZoneInfo,
) -> tuple[str, datetime, dict[str, object], bool, tuple[str, ...]]:
    period = _one(root, "Periodo")
    if period is None:  # Defensive for type narrowing; _one(required=True) already rejects it.
        raise SourceValidationError("missing Periodo")
    serial = _child_text(period, "MatricolaRT", maximum=32)
    if serial is None:
        raise SourceValidationError("missing Periodo.MatricolaRT")
    serial = serial.upper()
    if not _SERIAL_RE.fullmatch(serial):
        raise SourceValidationError("Periodo.MatricolaRT has an invalid shape")
    read_text = _child_text(period, "LetturaDel", maximum=64)
    read_at = _parse_timestamp(read_text, "Periodo.LetturaDel", timezone)

    part_text = _child_text(period, "Parte", required=False, maximum=16)
    total_text = _child_text(period, "NumeroTotaleParti", required=False, maximum=16)
    terminal_text = _child_text(period, "seTerminale", required=False, maximum=8)
    chain_key = _child_text(period, "ChiaveConcatenazione", required=False, maximum=128)
    part = _integer(part_text, "Periodo.Parte", positive=True) if part_text else None
    total = (
        _integer(total_text, "Periodo.NumeroTotaleParti", positive=True)
        if total_text
        else None
    )
    terminal = _parse_bool(terminal_text, "Periodo.seTerminale")
    if total is not None and part is None:
        raise SourceValidationError("NumeroTotaleParti requires Parte")
    if total is not None and part is not None and part > total:
        raise SourceValidationError("Periodo.Parte exceeds NumeroTotaleParti")
    chain_valid: bool | None = None
    if chain_key is not None:
        if part is None:
            raise SourceValidationError("ChiaveConcatenazione requires Parte")
        expected = _digest(f"{read_text}{part - 1}{serial}".encode())
        chain_valid = expected.lower() == chain_key.lower()
        if not chain_valid:
            raise SourceValidationError("multipart ChiaveConcatenazione is invalid")

    is_multipart = part is not None and (total is None or total > 1 or part > 1)
    complete = not is_multipart or (part == 1 and total == 1)
    warnings: list[str] = []
    if is_multipart:
        warnings.append(
            "multipart fiscal-memory export part imported independently; "
            "set completeness requires reconciliation of every part"
        )
    integral_raw = period.attrib.get("integrale")
    if integral_raw is None:
        raise SourceValidationError("Periodo.integrale is required")
    metadata: dict[str, object] = {
        "dal": _child_text(period, "Dal", required=False, maximum=64),
        "al": _child_text(period, "Al", required=False, maximum=64),
        "da_chiusura_numero": _child_text(
            period, "DaChiusuraNumero", required=False, maximum=32
        ),
        "a_chiusura_numero": _child_text(
            period, "AChiusuraNumero", required=False, maximum=32
        ),
        "lettura_del_raw": read_text,
        "parte": part,
        "numero_totale_parti": total,
        "se_terminale": terminal,
        "chiave_concatenazione": chain_key,
        "chiave_concatenazione_verificata": chain_valid,
        "integrale": _parse_bool(integral_raw, "Periodo.integrale"),
    }
    return serial, read_at, metadata, complete, tuple(warnings)


def _signature_metadata(root: ET.Element) -> dict[str, object]:
    signatures = [
        element
        for element in root.iter()
        if _split_tag(element.tag) == (XMLDSIG_NAMESPACE, "Signature")
    ]
    if len(signatures) > 1:
        raise SourceValidationError("XML contains more than one XMLDSIG Signature")
    if not signatures:
        return {
            "signature_present": False,
            "signature_validation": "NOT_PRESENT_OPTIONAL",
        }
    signature = signatures[0]
    signature_methods = [
        element.attrib.get("Algorithm")
        for element in signature.iter()
        if _split_tag(element.tag) == (XMLDSIG_NAMESPACE, "SignatureMethod")
    ]
    digest_methods = [
        element.attrib.get("Algorithm")
        for element in signature.iter()
        if _split_tag(element.tag) == (XMLDSIG_NAMESPACE, "DigestMethod")
    ]
    references = [
        element.attrib.get("URI", "")
        for element in signature.iter()
        if _split_tag(element.tag) == (XMLDSIG_NAMESPACE, "Reference")
    ]
    return {
        "signature_present": True,
        "signature_validation": "NOT_PERFORMED_NO_TRUST_CHAIN",
        "signature_methods": [value for value in signature_methods if value],
        "digest_methods": [value for value in digest_methods if value],
        "reference_uris": references[:64],
    }


def _headers(root: ET.Element, *, timezone: ZoneInfo) -> list[dict[str, object]]:
    containers = _direct(root, "RecordIntestazioni")
    if len(containers) > 1:
        raise SourceValidationError("duplicate RecordIntestazioni")
    if not containers:
        return []
    result: list[dict[str, object]] = []
    for index, header in enumerate(_direct(containers[0], "Intestazione"), start=1):
        observed_text = _child_text(header, "DataOra", maximum=64)
        observed_at = _parse_timestamp(
            observed_text, f"RecordIntestazioni.Intestazione[{index}].DataOra", timezone
        )
        denomination = _child_text(
            header, "Denominazione", required=False, maximum=191
        )
        user_data = _child_text(header, "DatiUtente", required=False, maximum=191)
        vat_number = _child_text(header, "PartitaIva", required=False, maximum=32)
        address = _one(header, "Indirizzo", required=False)
        address_lines: list[str] = []
        address_fields: dict[str, str | None] = {}
        if address is not None:
            street = _child_text(address, "Indirizzo", required=False, maximum=191)
            postal_code = _child_text(address, "Cap", required=False, maximum=16)
            municipality = _child_text(address, "Comune", required=False, maximum=128)
            province = _child_text(address, "Provincia", required=False, maximum=16)
            if street:
                address_lines.append(street)
            locality = " ".join(
                part
                for part in (
                    postal_code,
                    municipality,
                    f"({province})" if province else None,
                )
                if part
            )
            if locality:
                address_lines.append(locality)
            address_fields = {
                "street": street,
                "postal_code": postal_code,
                "municipality": municipality,
                "province": province,
            }
        receipt_header = {
            "schema_version": 1,
            "merchant_name": denomination,
            "legal_name": None,
            "address_lines": address_lines,
            "phone": None,
            "tax_code": None,
            "vat_number": vat_number,
            "evidence": "RCH_DGFE_EXPORT",
        }
        result.append(
            {
                "observed_at": observed_at,
                "observed_at_raw": observed_text,
                "receipt_header": receipt_header,
                "user_data": user_data,
                "address": address_fields,
            }
        )
    return sorted(result, key=lambda value: value["observed_at"])  # type: ignore[arg-type]


def _header_for(
    headers: Sequence[Mapping[str, object]], document_time: datetime
) -> Mapping[str, object] | None:
    selected: Mapping[str, object] | None = None
    for header in headers:
        observed_at = header.get("observed_at")
        if isinstance(observed_at, datetime) and observed_at <= document_time:
            selected = header
    return selected


def _reference_code(value: str | None) -> str | None:
    if value is None:
        return None
    compact = value.replace("-", "")
    if compact.isdigit() and len(compact) == 8:
        return f"{compact[:4]}-{compact[4:]}"
    if value.isdigit():
        return value
    if len(value) > 128:
        raise SourceValidationError("commercial reference exceeds 128 characters")
    return value


def _tax(element: ET.Element) -> tuple[str | None, Decimal | None, Decimal | None]:
    iva = _one(element, "IVA", required=False)
    if iva is not None:
        rate_value = _child_text(iva, "AliquotaIVA", maximum=32)
        rate = _decimal(rate_value, "IVA.AliquotaIVA")
        tax_text = _child_text(iva, "Imposta", required=False, maximum=32)
        tax = _decimal(tax_text, "IVA.Imposta") if tax_text else None
        return "IVA", rate, tax
    nature = _child_text(element, "Natura", required=False, maximum=32)
    if nature is not None:
        return f"NATURA:{nature}", None, None
    ventilation = _child_text(element, "VentilazioneIVA", required=False, maximum=32)
    if ventilation is not None:
        return f"VENTILAZIONE:{ventilation}", None, None
    raise SourceValidationError("line or modifier has no IVA/Natura/VentilazioneIVA")


def _modifier(element: ET.Element, label: str) -> dict[str, object]:
    amount = _decimal(_child_text(element, "Importo", maximum=32), f"{label}.Importo")
    sign = _child_text(element, "Segno", maximum=1)
    if sign not in {"+", "-"}:
        raise SourceValidationError(f"{label}.Segno must be + or -")
    description = _child_text(element, "Descrizione", maximum=512)
    tax_kind, tax_rate, tax_amount = _tax(element)
    return {
        "amount": _decimal_text(amount),
        "sign": sign,
        "description": description,
        "tax_kind": tax_kind,
        "tax_rate": _decimal_text(tax_rate),
        "tax_amount": _decimal_text(tax_amount),
    }


def _lines(document: ET.Element) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    detail = _one(document, "Dettaglio")
    if detail is None:
        raise SourceValidationError("missing Dettaglio")
    lines: list[dict[str, object]] = []
    subtotal_modifiers: list[dict[str, object]] = []
    for generic in _direct(detail, "RigaGenerica"):
        rows = _direct(generic, "Riga")
        subtotal_rows = _direct(generic, "RigaSubtotale")
        if bool(rows) == bool(subtotal_rows):
            raise SourceValidationError("RigaGenerica must contain Riga xor RigaSubtotale")
        for row in rows:
            base = _one(row, "Elemento")
            if base is None:
                raise SourceValidationError("missing Riga.Elemento")
            amount = _decimal(_child_text(base, "Importo", maximum=32), "Elemento.Importo")
            description = _child_text(
                base, "Descrizione", required=False, maximum=512
            )
            quantity_text = _child_text(base, "Quantita", required=False, maximum=32)
            quantity = (
                Decimal(_integer(quantity_text, "Elemento.Quantita", positive=True))
                if quantity_text
                else None
            )
            unit_text = _child_text(base, "PrezzoUnitario", required=False, maximum=32)
            unit_price = _decimal(unit_text, "Elemento.PrezzoUnitario") if unit_text else None
            tax_kind, tax_rate, tax_amount = _tax(base)
            modifiers = [
                _modifier(value, "ModificatoreElemento")
                for value in _direct(row, "ModificatoreElemento")
            ]
            discount = sum(
                (abs(Decimal(str(value["amount"]))) for value in modifiers if value["sign"] == "-"),
                Decimal("0"),
            )
            surcharge = sum(
                (abs(Decimal(str(value["amount"]))) for value in modifiers if value["sign"] == "+"),
                Decimal("0"),
            )
            lines.append(
                {
                    "sequence": len(lines) + 1,
                    "description": description,
                    "quantity": _decimal_text(quantity),
                    "unit_price": _decimal_text(unit_price),
                    "line_total": _decimal_text(amount),
                    "discount": _decimal_text(discount) if discount else None,
                    "surcharge": _decimal_text(surcharge) if surcharge else None,
                    "tax_rate": _decimal_text(tax_rate),
                    "tax_amount": _decimal_text(tax_amount),
                    "tax_kind": tax_kind,
                    "modifiers": modifiers,
                    "attributes": dict(base.attrib),
                    "state": "FISCAL_MEMORY_RECORDED",
                }
            )
            if len(lines) > _MAX_LINES_PER_DOCUMENT:
                raise SourceValidationError("document exceeds the line limit")
        for subtotal in subtotal_rows:
            subtotal_value = _child_text(subtotal, "SubTotale", maximum=32)
            modifiers = []
            for value in _direct(subtotal, "ModificatoreSubTotale"):
                amount_text = _child_text(value, "Importi", maximum=32)
                sign = _child_text(value, "Segno", maximum=1)
                if sign not in {"+", "-"}:
                    raise SourceValidationError("ModificatoreSubTotale.Segno is invalid")
                modifiers.append(
                    {
                        "subtotal": subtotal_value,
                        "amount": _decimal_text(
                            _decimal(amount_text, "ModificatoreSubTotale.Importi")
                        ),
                        "sign": sign,
                        "description": _child_text(value, "Descrizione", maximum=512),
                    }
                )
            subtotal_modifiers.extend(modifiers)
    total_cancellation = _one(detail, "AnnullamentoTotale", required=False)
    if total_cancellation is not None:
        amount_text = _child_text(total_cancellation, "Importi", maximum=32)
        subtotal_modifiers.append(
            {
                "scope": "TOTAL_CANCELLATION",
                "amount": _decimal_text(
                    _decimal(amount_text, "AnnullamentoTotale.Importi")
                ),
                "sign": _child_text(total_cancellation, "Segno", maximum=1),
                "description": _child_text(
                    total_cancellation, "Descrizione", maximum=512
                ),
            }
        )
    return lines, subtotal_modifiers


def _payments(document: ET.Element) -> tuple[list[dict[str, object]], str | None]:
    container = _one(document, "Pagamenti")
    if container is None:
        raise SourceValidationError("missing Pagamenti")
    payments: list[dict[str, object]] = []
    for index, payment in enumerate(_direct(container, "Pagamento"), start=1):
        amount = _decimal(
            _child_text(payment, "Importo", maximum=32), f"Pagamento[{index}].Importo"
        )
        method = _child_text(payment, "Tipo", maximum=64)
        ticket = _child_text(payment, "NumeroTicket", required=False, maximum=32)
        pos = _one(payment, "RifPos", required=False)
        pos_data: dict[str, object] | None = None
        if pos is not None:
            pos_data = {
                "data_ora": _child_text(pos, "DataOra", required=False, maximum=64),
                "modalita_operazione": _child_text(
                    pos, "ModalitaOperazione", maximum=128
                ),
                "importo": _child_text(pos, "Importo", required=False, maximum=32),
                "codice_autorizzativo": _child_text(
                    pos, "CodiceAutorizzativo", maximum=128
                ),
            }
        payments.append(
            {
                "sequence": index,
                "amount": _decimal_text(amount),
                "method": method,
                "ticket_count": ticket,
                "pos_reference": pos_data,
                "currency": "EUR",
                "status": "RECORDED",
                "evidence": "CONFIRMED",
            }
        )
        if len(payments) > _MAX_PAYMENTS_PER_DOCUMENT:
            raise SourceValidationError("document exceeds the payment limit")
    if not payments:
        raise SourceValidationError("Pagamenti contains no Pagamento")
    remainder = _child_text(container, "Resto", required=False, maximum=32)
    if remainder is not None:
        _decimal(remainder, "Pagamenti.Resto")
    return payments, remainder


def _normalized_text(
    *,
    kind: str,
    code: str,
    timestamp: datetime,
    serial: str,
    lines: Sequence[Mapping[str, object]],
    total: Decimal,
    payments: Sequence[Mapping[str, object]],
) -> str:
    output = [f"DOCUMENTO COMMERCIALE - {kind}"]
    for line in lines:
        description = line.get("description") or "Riga senza descrizione"
        quantity = line.get("quantity") or "1"
        line_total = line.get("line_total") or ""
        output.append(f"{quantity} x {description}  {line_total}")
        for modifier in line.get("modifiers") or []:  # type: ignore[union-attr]
            if isinstance(modifier, Mapping):
                output.append(
                    f"  {modifier.get('sign', '')}{modifier.get('amount', '')} "
                    f"{modifier.get('description', '')}".rstrip()
                )
    output.append(f"TOTALE {format(total, 'f')}")
    for payment in payments:
        output.append(f"PAGAMENTO {payment.get('method')} {payment.get('amount')}")
    output.extend(
        (
            timestamp.isoformat().replace("+00:00", "Z"),
            f"DOCUMENTO N. {code}",
            f"RT {serial}",
            "",
            "Testo ricostruito da export DGFE; XML originale conservato.",
        )
    )
    return "\n".join(output)


def _detail_documents(
    root: ET.Element,
    *,
    serial: str,
    timezone: ZoneInfo,
    headers: Sequence[Mapping[str, object]],
) -> tuple[NormalizedDocument, ...]:
    documents: list[NormalizedDocument] = []
    identities: set[str] = set()
    containers = list(_descendants(root, "DocumentoCommerciale"))
    containers.extend(_descendants(root, "Scontrino"))
    for container_index, container in enumerate(containers, start=1):
        candidates = [
            child
            for child in container
            if _local(child) in {"Vendita", "Annullo", "Reso", "NotadiCredido"}
        ]
        if len(candidates) != 1:
            raise SourceValidationError(
                "DocumentoCommerciale/Scontrino must contain exactly one fiscal event"
            )
        value = candidates[0]
        kind = _local(value)
        timestamp_raw = _child_text(value, "DataOra", maximum=64)
        timestamp = _parse_timestamp(timestamp_raw, f"{kind}.DataOra", timezone)
        document_number_raw = _child_text(value, "NumeroDocumento", maximum=32)
        closure_number_raw = _child_text(value, "NumeroAzzeramento", maximum=32)
        document_number = _integer(
            document_number_raw, f"{kind}.NumeroDocumento", positive=True
        )
        closure_number = _integer(
            closure_number_raw, f"{kind}.NumeroAzzeramento", positive=True
        )
        code = f"{closure_number:04d}-{document_number:04d}"
        document_type = {
            "Vendita": DocumentType.COMMERCIAL_DOCUMENT,
            "Annullo": DocumentType.CANCELLATION,
            "Reso": DocumentType.REFUND,
            "NotadiCredido": DocumentType.REFUND,
        }[kind]
        subtype = {
            "Vendita": "RCH_DGFE_VENDITA",
            "Annullo": "RCH_DGFE_ANNULLO",
            "Reso": "RCH_DGFE_RESO",
            "NotadiCredido": "RCH_DGFE_NOTA_CREDITO",
        }[kind]
        identity = f"{serial}:{code}:{document_type.value}"
        if identity in identities:
            raise SourceValidationError(f"duplicate fiscal identity inside XML: {identity}")
        identities.add(identity)

        lines, subtotal_modifiers = _lines(value)
        payments, remainder = _payments(value)
        total = _decimal(_child_text(value, "ImportoTotale", maximum=32), "ImportoTotale")
        reference_raw = _child_text(
            value, "NumeroProgressivoRiferimento", required=False, maximum=128
        )
        reference_code = _reference_code(reference_raw)
        reference_timestamp_raw = _child_text(
            value, "DataRiferimento", required=False, maximum=64
        ) or _child_text(value, "DataOraRiferimento", required=False, maximum=64)
        reference_timestamp = (
            _parse_timestamp(reference_timestamp_raw, f"{kind}.DataRiferimento", timezone)
            if reference_timestamp_raw
            else None
        )
        selected_header = _header_for(headers, timestamp)
        receipt_header = selected_header.get("receipt_header") if selected_header else None
        tax_total = sum(
            (
                Decimal(str(line["tax_amount"]))
                for line in lines
                if line.get("tax_amount") is not None
            ),
            Decimal("0"),
        )
        semantic: dict[str, object] = {
            "provenance": "RCH_DGFE_EXPORT",
            "source_export_kind": "DGFE_DETAIL",
            "external_document_code": code,
            "commercial_reference_code": reference_code,
            "rch_serial_number": serial,
            "rch_serial_number_evidence": "RCH_DGFE_EXPORT",
            "fiscal_document_timestamp": timestamp.isoformat(),
            "document_timestamp": timestamp.isoformat(),
            "document_timestamp_precision": "SECOND",
            "document_timestamp_evidence": "RCH_DGFE_EXPORT",
            "receipt_header": receipt_header,
            "receipt_header_history_record": (
                {
                    key: value
                    for key, value in selected_header.items()
                    if key != "observed_at"
                }
                if selected_header
                else None
            ),
            "numero_azzeramento_raw": closure_number_raw,
            "numero_documento_raw": document_number_raw,
            "fiscal_identity": {
                "matricola_rt": serial,
                "numero_azzeramento": closure_number,
                "numero_documento": document_number,
                "kind": kind,
            },
            "fiscal_reference": (
                {
                    "numero_progressivo_raw": reference_raw,
                    "normalized_code": reference_code,
                    "timestamp_raw": reference_timestamp_raw,
                    "timestamp": reference_timestamp.isoformat()
                    if reference_timestamp
                    else None,
                    "punto_cassa": _child_text(
                        value, "PuntoCassaRiferimento", required=False, maximum=64
                    ),
                    "tipo": _child_text(
                        value, "TipoRiferimento", required=False, maximum=32
                    ),
                    "matricola": _child_text(
                        value, "MatricolaRiferimento", required=False, maximum=32
                    ),
                }
                if reference_raw or reference_timestamp_raw
                else None
            ),
            "gross_total": _decimal_text(total),
            "tax_total": _decimal_text(tax_total) if tax_total else None,
            "payment_method": payments[0].get("method") if payments else None,
            "payments": payments,
            "change_amount": remainder,
            "lines": lines,
            "subtotal_modifiers": subtotal_modifiers,
            "normalized_text": _normalized_text(
                kind=kind,
                code=code,
                timestamp=timestamp,
                serial=serial,
                lines=lines,
                total=total,
                payments=payments,
            ),
            "parse_confidence": 100,
            "xml_path": (
                "SessioniGiornaliere/SessioneGiornaliera/DocumentiEmessi/"
                f"{_local(container)}/{kind}[{container_index}]"
            ),
        }
        documents.append(
            NormalizedDocument(
                external_id=f"dgfe:{identity}",
                document_type=document_type,
                subtype=subtype,
                complete=True,
                evidence="CONFIRMED",
                capture_time=timestamp,
                timezone=timezone.key,
                source_start_offset=None,
                source_end_offset=None,
                source_frame_ids=(),
                semantic=semantic,
            )
        )
        if len(documents) > _MAX_DOCUMENTS:
            raise SourceValidationError("DGFE XML exceeds the document limit")
    return tuple(documents)


def _summary_documents(
    root: ET.Element,
    *,
    serial: str,
    timezone: ZoneInfo,
    headers: Sequence[Mapping[str, object]],
) -> tuple[NormalizedDocument, ...]:
    documents: list[NormalizedDocument] = []
    seen: set[int] = set()
    for index, record in enumerate(_descendants(root, "RecordAzzeramento"), start=1):
        timestamp_raw = _child_text(record, "DataOra", maximum=64)
        timestamp = _parse_timestamp(timestamp_raw, "RecordAzzeramento.DataOra", timezone)
        number_raw = _child_text(record, "Numero", maximum=32)
        number = _integer(number_raw, "RecordAzzeramento.Numero", positive=True)
        if number in seen:
            raise SourceValidationError(f"duplicate fiscal closure inside XML: {number}")
        seen.add(number)
        gross = _decimal(
            _child_text(record, "Corrispettivo", maximum=32),
            "RecordAzzeramento.Corrispettivo",
        )
        cancellations = _decimal(
            _child_text(record, "TotaleAnnullo", maximum=32),
            "RecordAzzeramento.TotaleAnnullo",
        )
        refunds = _decimal(
            _child_text(record, "TotaleReso", maximum=32),
            "RecordAzzeramento.TotaleReso",
        )
        selected_header = _header_for(headers, timestamp)
        code = f"Z{number:04d}"
        lines = [
            {
                "sequence": 1,
                "description": "Corrispettivo",
                "line_total": _decimal_text(gross),
                "state": "FISCAL_MEMORY_SUMMARY",
            },
            {
                "sequence": 2,
                "description": "Totale annulli",
                "line_total": _decimal_text(cancellations),
                "state": "FISCAL_MEMORY_SUMMARY",
            },
            {
                "sequence": 3,
                "description": "Totale resi",
                "line_total": _decimal_text(refunds),
                "state": "FISCAL_MEMORY_SUMMARY",
            },
        ]
        semantic: dict[str, object] = {
            "provenance": "RCH_FISCAL_SUMMARY_EXPORT",
            "source_export_kind": "FISCAL_SUMMARY",
            "external_document_code": code,
            "rch_serial_number": serial,
            "rch_serial_number_evidence": "RCH_FISCAL_SUMMARY_EXPORT",
            "fiscal_document_timestamp": timestamp.isoformat(),
            "document_timestamp": timestamp.isoformat(),
            "document_timestamp_precision": "SECOND",
            "document_timestamp_evidence": "RCH_FISCAL_SUMMARY_EXPORT",
            "receipt_header": (
                selected_header.get("receipt_header") if selected_header else None
            ),
            "closure_number_raw": number_raw,
            "gross_total": _decimal_text(gross),
            "total_cancellations": _decimal_text(cancellations),
            "total_refunds": _decimal_text(refunds),
            "document_count": _child_text(
                record, "NumeroDocumentiCommerciali", required=False, maximum=32
            )
            or _child_text(
                record, "NumeroScontriniFiscali", required=False, maximum=32
            ),
            "lines": lines,
            "normalized_text": "\n".join(
                (
                    "REPORT MEMORIA FISCALE - CHIUSURA",
                    f"Numero chiusura {number}",
                    f"Corrispettivo {format(gross, 'f')}",
                    f"Totale annulli {format(cancellations, 'f')}",
                    f"Totale resi {format(refunds, 'f')}",
                    timestamp.isoformat().replace("+00:00", "Z"),
                    f"RT {serial}",
                    "",
                    "Testo ricostruito da memoria riepilogo; XML originale conservato.",
                )
            ),
            "parse_confidence": 100,
            "xml_path": f"RecordDati/RecordAzzeramento[{index}]",
        }
        documents.append(
            NormalizedDocument(
                external_id=f"fiscal-summary:{serial}:{number}",
                document_type=DocumentType.SHIFT_END_REPORT,
                subtype="RCH_FISCAL_MEMORY_CLOSURE",
                complete=True,
                evidence="CONFIRMED",
                capture_time=timestamp,
                timezone=timezone.key,
                source_start_offset=None,
                source_end_offset=None,
                source_frame_ids=(),
                semantic=semantic,
            )
        )
    return tuple(documents)


class RCHFiscalMemoryXMLAdapter:
    """Adapter for immutable XTools/RT fiscal-memory XML export directories."""

    def __init__(
        self,
        root: Path,
        *,
        source_instance_id: str,
        device_id: str,
        device_endpoint: Endpoint,
        timezone: str,
    ) -> None:
        self.root = Path(root)
        self.source_instance_id = validate_source_instance_id(source_instance_id)
        self.device_id = device_id
        self.device_endpoint = device_endpoint
        self.timezone = ZoneInfo(timezone)

    def discover(self, *, maximum: int) -> Sequence[ImportCandidate]:
        root = validate_root(self.root)
        candidates: list[ImportCandidate] = []
        for path, kind in _discover_xml(root, maximum):
            relative = path.relative_to(root).as_posix()
            candidates.append(
                ImportCandidate(
                    source_kind=kind,
                    source_instance_id=self.source_instance_id,
                    candidate_key=(
                        f"rch-fiscal-memory:{self.source_instance_id}:{relative}"
                    ),
                    source_path=path,
                    device_id=self.device_id,
                    context={"discovered_kind": kind.value},
                )
            )
        return tuple(candidates)

    def load(self, candidate: ImportCandidate) -> NormalizedEnvelope:
        if candidate.source_instance_id != self.source_instance_id:
            raise SourceValidationError("candidate belongs to another source instance")
        if candidate.device_id != self.device_id:
            raise SourceValidationError("candidate belongs to another configured device")
        raw = read_regular_file(self.root, candidate.source_path, max_bytes=_MAX_XML_BYTES)
        lowered = raw.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise SourceValidationError("DTD and entity declarations are forbidden")
        try:
            # The file is bounded, DTD/entity declarations were rejected and
            # the resulting tree is subjected to namespace/node/depth/text
            # limits immediately below. stdlib ET performs no network fetches.
            root = ET.fromstring(raw)  # noqa: S314
        except (ET.ParseError, RecursionError) as exc:
            raise SourceValidationError(f"invalid fiscal-memory XML: {exc}") from exc
        _validate_tree(root)
        source_kind = _classify_root(root)
        if candidate.source_kind is not source_kind:
            raise SourceValidationError("XML root changed after discovery")
        declared_version = root.attrib.get("versione")
        # The current AE V3.x XSD family intentionally keeps this payload
        # attribute fixed at 3.0.  It is not the release number of the XSD zip.
        if declared_version != "3.0":
            raise SourceValidationError(
                f"unsupported fiscal-memory payload version: {declared_version!r}"
            )

        serial, read_at, period, set_complete, period_warnings = _period(
            root, timezone=self.timezone
        )
        headers = _headers(root, timezone=self.timezone)
        signature = _signature_metadata(root)
        if source_kind is SourceKind.RCH_XTOOLS_DGFE_V1:
            documents = _detail_documents(
                root,
                serial=serial,
                timezone=self.timezone,
                headers=headers,
            )
            artifact_role = ArtifactRole.DGFE_XML
            export_kind = "DGFE_DETAIL"
        else:
            documents = _summary_documents(
                root,
                serial=serial,
                timezone=self.timezone,
                headers=headers,
            )
            artifact_role = ArtifactRole.FISCAL_SUMMARY_XML
            export_kind = "FISCAL_SUMMARY"

        digest = _digest(raw)
        source_job_id = f"fiscal-memory-{digest[:32]}"
        warnings = list(period_warnings)
        if signature.get("signature_present"):
            warnings.append(
                "XMLDSIG preserved but not cryptographically validated without "
                "a configured trust chain"
            )
        metadata = {
            "source_path": str(candidate.source_path),
            "source_export_kind": export_kind,
            "provenance": "RCH_DGFE_EXPORT"
            if source_kind is SourceKind.RCH_XTOOLS_DGFE_V1
            else "RCH_FISCAL_SUMMARY_EXPORT",
            "matricola_rt": serial,
            "payload_version_attribute": declared_version,
            "schema_release_claim": "AE_FISCAL_MEMORY_V3_X",
            "schema_validation": "BOUNDED_STRUCTURAL_ALLOWLIST_NO_XSD_ENGINE",
            "period": period,
            "signature": signature,
            "header_history_count": len(headers),
            "source_contract": {
                "original_bytes": "PRESERVED",
                "integrity": "SHA256_SNAPSHOT",
                "network_access": "NONE",
                "xml_dtd_entities": "FORBIDDEN",
            },
        }
        return NormalizedEnvelope(
            source_key=f"rch-fiscal-memory:{self.source_instance_id}:{digest}",
            source_kind=source_kind,
            source_instance_id=self.source_instance_id,
            device_id=self.device_id,
            source_job_id=source_job_id,
            source_session_id=f"memory-read:{serial}:{digest[:16]}",
            connection_id=None,
            opened_at=read_at,
            closed_at=read_at,
            source_endpoint=None,
            proxy_endpoint=None,
            device_endpoint=self.device_endpoint,
            status="COMPLETE" if set_complete else "PARTIAL_MULTIPART",
            complete=set_complete,
            boundary_source="FISCAL_MEMORY_XML_EXPORT",
            boundary_confidence=1.0,
            delivery_evidence="FISCAL_MEMORY_EXPORT_PHYSICAL_PRINT_NOT_APPLICABLE",
            manifest_sha256=digest,
            parser_version="rch-fiscal-memory-xml-1.0.0",
            artifacts=(
                ArtifactSnapshot(
                    role=artifact_role,
                    original_path=candidate.source_path,
                    sha256=digest,
                    size=len(raw),
                    content=raw,
                    complete=True,
                    media_type="application/xml",
                ),
            ),
            chunks=(),
            documents=documents,
            metadata=metadata,
            warnings=tuple(warnings),
        )


__all__ = [
    "DETAIL_NAMESPACE",
    "SUMMARY_NAMESPACE",
    "RCHFiscalMemoryXMLAdapter",
]
