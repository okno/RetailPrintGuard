"""Bounded ESC/POS text extraction without touching authoritative RAW bytes."""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BufferedReader
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from retailprintguard.common.domain import (
    DocumentLine,
    DocumentType,
    EvidenceLevel,
    NormalizedDocument,
    SourceSpan,
)

PARSER_NAME = "retailprintguard-escpos"
PARSER_VERSION = "1.2.0"
_MAX_INPUT_BYTES = 16 * 1024 * 1024
_MAX_OUTPUT_CHARS = 4_000_000
_MAX_DOCUMENTS = 1_024
_MAX_LINES = 32_768
_MAX_RASTER_IMAGES = 4
_MAX_RASTER_PIXELS = 4_000_000
_OCR_MINIMUM_CONFIDENCE = 80.0
_OCR_TIMEOUT_SECONDS = 5.0
_OCR_STDOUT_LIMIT = 1_000_000
_OCR_STDERR_LIMIT = 64 * 1024
_CODEPAGES = {0: "cp437", 2: "cp850", 16: "cp1252", 19: "cp858"}
_ALLOWED_ENCODINGS = frozenset({"cp437", "cp850", "cp858", "cp1252", "latin-1", "utf-8"})
_MONEY_RE = re.compile(r"(?<!\d)(?P<value>[+-]?\d{1,9}(?:\.\d{3})*,\d{2})(?!\d)")
_TABLE_RE = re.compile(r"\bTAVOLO\s*[:#-]?\s*(?P<value>[A-Z0-9._/-]+)", re.IGNORECASE)
_ORDER_RE = re.compile(r"\b(?:ORDINE|COMANDA)\s*[:#-]?\s*(?P<value>[A-Z0-9._/-]+)", re.IGNORECASE)
_OPERATOR_LINE_RE = re.compile(
    r"\bOPERATORE\s*[:#-]?\s*(?P<value>[^\r\n]*)",
    re.IGNORECASE,
)
_LOCAL_TIMESTAMP_RE = re.compile(
    r"(?P<date>\d{2}[/-]\d{2}[/-]\d{2,4})\s+(?P<time>\d{2}:\d{2}(?::\d{2})?)"
)
_ITEM_RE = re.compile(
    r"^\s*(?P<quantity>[+-]?\d+(?:[.,]\d+)?)\s*[xX]\s+(?P<description>\S.*)$"
)
_COURSE_RE = re.compile(r"^\s*PORTATA\s*:\s*(?P<value>[A-Z0-9._/-]+)\s*$", re.IGNORECASE)
_COVERS_RE = re.compile(r"^\s*COPERTI\s*:\s*(?P<value>\d+)\s*$", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^[\s\-_=.]{3,}$")
_DOCUMENT_HEADER_RE = re.compile(
    r"^(?:OPERATORE|TAVOLO|ORDINE|COMANDA|PORTATA|COPERTI|DOCUMENTO|PRECONTO)\b",
    re.IGNORECASE,
)
_SPACE_BEFORE_CONTINUATION = frozenset(
    {
        "a",
        "ai",
        "al",
        "alla",
        "alle",
        "agli",
        "con",
        "da",
        "dal",
        "dalla",
        "delle",
        "di",
        "e",
        "in",
        "per",
        "senza",
        "su",
    }
)
_UNIT_CONTINUATIONS = frozenset({"cl", "dl", "g", "gr", "kg", "l", "ml"})
_DOCUMENT_RE = re.compile(
    r"\b(?:DOCUMENTO|DOC\.?|PRECONTO|COMANDA)\s*(?:N\.?|#)?\s*[:#-]?\s*(?P<value>[A-Z0-9._/-]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _TextLine:
    text: str
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class _Segment:
    payload: bytes
    base_offset: int
    cut_observed: bool


@dataclass(frozen=True, slots=True)
class _RasterImage:
    """Bounded one-bit raster reconstructed from consecutive ESC ``*`` strips."""

    width: int
    height: int
    packed_bits: bytes
    source_offset: int
    source_length: int
    strip_count: int

    @property
    def row_stride(self) -> int:
        return (self.width + 7) // 8

    def pixel_is_black(self, x: int, y: int) -> bool:
        value = self.packed_bits[y * self.row_stride + (x // 8)]
        return bool(value & (0x80 >> (x % 8)))


@dataclass(frozen=True, slots=True)
class RasterOcrResult:
    text: str
    confidence: float
    bounding_box: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(slots=True)
class _PendingLine:
    description: str
    quantity: Decimal
    amount: Decimal | None
    course: str | None
    raw_fragments: list[str]
    source_start: int
    source_end: int
    space_before_next: bool = False


RasterOcrEngine = Callable[[_RasterImage], RasterOcrResult | None]


def _money(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None


def _decode_esc_star_strip(
    width: int,
    height: int,
    bytes_per_column: int,
    payload: bytes,
) -> bytes:
    stride = (width + 7) // 8
    packed = bytearray(stride * height)
    for x in range(width):
        for band in range(bytes_per_column):
            value = payload[x * bytes_per_column + band]
            for bit in range(8):
                y = band * 8 + bit
                if y < height and value & (0x80 >> bit):
                    packed[y * stride + (x // 8)] |= 0x80 >> (x % 8)
    return bytes(packed)


def _esc_star_strip_at(
    payload: bytes,
    cursor: int,
) -> tuple[int, int, int, bytes, int] | None:
    if cursor + 5 > len(payload) or payload[cursor : cursor + 2] != b"\x1b*":
        return None
    mode = payload[cursor + 2]
    if mode not in {0, 1, 32, 33}:
        return None
    width = payload[cursor + 3] + (payload[cursor + 4] << 8)
    bytes_per_column = 3 if mode in {32, 33} else 1
    height = bytes_per_column * 8
    data_length = width * bytes_per_column
    end = cursor + 5 + data_length
    if width < 1 or width * height > _MAX_RASTER_PIXELS or end > len(payload):
        return None
    packed = _decode_esc_star_strip(
        width,
        height,
        bytes_per_column,
        payload[cursor + 5 : end],
    )
    return mode, width, height, packed, end


def _reconstructed_esc_star_rasters(
    payload: bytes,
    *,
    base_offset: int,
) -> tuple[_RasterImage, ...]:
    """Reconstruct bounded strip groups without changing their source bytes.

    The production driver emits a table banner as four equal 24-dot ``ESC *``
    bands separated by ``ESC J 48``.  A single strip is deliberately not sent
    to OCR: this avoids treating arbitrary logos or binary decoration as text.
    """

    images: list[_RasterImage] = []
    cursor = 0
    while cursor < len(payload) and len(images) < _MAX_RASTER_IMAGES:
        start = payload.find(b"\x1b*", cursor)
        if start < 0:
            break
        first = _esc_star_strip_at(payload, start)
        if first is None:
            cursor = start + 2
            continue
        mode, width, height, packed, end = first
        strips = [packed]
        group_end = end
        while group_end + 3 <= len(payload):
            if payload[group_end : group_end + 2] != b"\x1bJ":
                break
            candidate_start = group_end + 3
            candidate = _esc_star_strip_at(payload, candidate_start)
            if candidate is None:
                break
            next_mode, next_width, next_height, next_packed, next_end = candidate
            if (next_mode, next_width, next_height) != (mode, width, height):
                break
            if width * height * (len(strips) + 1) > _MAX_RASTER_PIXELS:
                break
            strips.append(next_packed)
            group_end = next_end
        if len(strips) >= 2:
            images.append(
                _RasterImage(
                    width=width,
                    height=height * len(strips),
                    packed_bits=b"".join(strips),
                    source_offset=base_offset + start,
                    source_length=group_end - start,
                    strip_count=len(strips),
                )
            )
            cursor = group_end
        else:
            cursor = end
    return tuple(images)


def _bitmap_pgm_for_ocr(image: _RasterImage) -> tuple[bytes, int, int]:
    source_pixels = image.width * image.height
    scale = min(4, max(1, int(math.sqrt(_MAX_RASTER_PIXELS / max(1, source_pixels)))))
    padding = 4
    output_width = (image.width + padding * 2) * scale
    rows = bytearray()
    white_row = b"\xff" * output_width
    for _ in range(padding * scale):
        rows.extend(white_row)
    side = b"\xff" * (padding * scale)
    for y in range(image.height):
        row = bytearray(side)
        for x in range(image.width):
            row.extend((b"\x00" if image.pixel_is_black(x, y) else b"\xff") * scale)
        row.extend(side)
        for _ in range(scale):
            rows.extend(row)
    for _ in range(padding * scale):
        rows.extend(white_row)
    output_height = (image.height + padding * 2) * scale
    header = f"P5\n{output_width} {output_height}\n255\n".encode("ascii")
    return header + bytes(rows), scale, padding


def _normalize_ocr_word(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(character for character in normalized if character.isprintable())
    return " ".join(normalized.split())[:256]


def _parse_tesseract_tsv(
    payload: bytes,
    *,
    scale: int,
    padding: int,
) -> RasterOcrResult | None:
    if len(payload) > _OCR_STDOUT_LIMIT:
        return None
    text = payload.decode("utf-8", errors="replace")
    grouped: dict[tuple[int, int, int, int], list[str]] = {}
    confidences: list[tuple[float, int]] = []
    boxes: list[tuple[int, int, int, int]] = []
    for row in text.splitlines()[1:]:
        fields = row.split("\t", 11)
        if len(fields) != 12:
            continue
        try:
            level = int(fields[0])
            line_key = tuple(int(fields[index]) for index in (1, 2, 3, 4))
            left, top, width, height = (int(fields[index]) for index in (6, 7, 8, 9))
            confidence = float(fields[10])
        except (TypeError, ValueError):
            continue
        word = _normalize_ocr_word(fields[11])
        if level != 5 or not word or confidence < 0 or not math.isfinite(confidence):
            continue
        grouped.setdefault(line_key, []).append(word)
        confidences.append((min(100.0, confidence), max(1, len(word))))
        boxes.append((left, top, left + max(0, width), top + max(0, height)))
    lines = [" ".join(words).strip(" |[]{}") for words in grouped.values()]
    lines = [line for line in lines if line and any(character.isalnum() for character in line)]
    if not lines or not confidences:
        return None
    weight = sum(size for _score, size in confidences)
    confidence = sum(score * size for score, size in confidences) / max(1, weight)
    bounding_box = None
    if boxes:
        bounding_box = (
            max(0, math.floor(min(box[0] for box in boxes) / scale) - padding),
            max(0, math.floor(min(box[1] for box in boxes) / scale) - padding),
            max(0, math.ceil(max(box[2] for box in boxes) / scale) - padding),
            max(0, math.ceil(max(box[3] for box in boxes) / scale) - padding),
        )
    return RasterOcrResult("\n".join(lines)[:2048], confidence, bounding_box)


def _run_bounded_process(
    command: list[str],
    input_data: bytes,
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> _BoundedProcessResult | None:
    """Run a fixed executable without a shell and cap both output streams."""

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_limited = threading.Event()
    reader_failed = threading.Event()
    timed_out = False
    with tempfile.TemporaryFile() as input_file:
        input_file.write(input_data)
        input_file.seek(0)
        process = subprocess.Popen(  # noqa: S603 - fixed argv, shell disabled
            command,
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        stdout = process.stdout
        stderr = process.stderr
        if stdout is None or stderr is None:
            process.kill()
            raise OSError("OCR child pipes were not created")

        def kill_child() -> None:
            with contextlib.suppress(OSError):
                process.kill()

        def read_bounded(stream: BufferedReader, target: bytearray, limit: int) -> None:
            try:
                read_chunk = getattr(stream, "read1", stream.read)
                while True:
                    chunk = read_chunk(64 * 1024)
                    if not chunk:
                        return
                    remaining = max(0, limit - len(target))
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        output_limited.set()
                        kill_child()
                        return
            except (OSError, ValueError):
                reader_failed.set()
                kill_child()

        readers = (
            threading.Thread(
                target=read_bounded,
                args=(stdout, stdout_buffer, stdout_limit),
                name="retailprintguard-ocr-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=read_bounded,
                args=(stderr, stderr_buffer, stderr_limit),
                name="retailprintguard-ocr-stderr",
                daemon=True,
            ),
        )
        for reader in readers:
            reader.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_child()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1.0)
            returncode = process.returncode if process.returncode is not None else -1
        finally:
            if process.poll() is None:
                kill_child()
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    process.wait(timeout=1.0)
            for reader in readers:
                reader.join(timeout=1.0)
            stdout.close()
            stderr.close()
    if (
        timed_out
        or output_limited.is_set()
        or reader_failed.is_set()
        or any(reader.is_alive() for reader in readers)
    ):
        return None
    return _BoundedProcessResult(returncode, bytes(stdout_buffer), bytes(stderr_buffer))


def _ocr_language() -> str:
    value = os.environ.get("RPG_POS_OCR_LANG", "ita+eng")
    return value if re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", value) else "ita+eng"


@lru_cache(maxsize=1)
def _tesseract_identity() -> str:
    executable = shutil.which("tesseract")
    if executable is None:
        return "tesseract:none"
    try:
        result = _run_bounded_process(
            [executable, "--version"],
            b"",
            timeout=2.0,
            stdout_limit=4096,
            stderr_limit=4096,
        )
    except (OSError, subprocess.SubprocessError):
        return "tesseract:unavailable"
    if result is None or result.returncode != 0:
        return "tesseract:unavailable"
    first_line = (result.stdout or result.stderr).decode("utf-8", errors="replace").splitlines()
    version = first_line[0][:128] if first_line else "unknown"
    return f"{version};lang={_ocr_language()}"


def runtime_build_fingerprint() -> str:
    """Return external OCR identity used by the versioned parser build digest."""

    return _tesseract_identity()


# The repository deliberately discovers this callable by its stable public
# name.  Keeping the alias next to the implementation makes the external OCR
# runtime part of the immutable ParserVersion build identity.
PARSER_RUNTIME_FINGERPRINT = runtime_build_fingerprint


def _run_tesseract_ocr(image: _RasterImage) -> RasterOcrResult | None:
    executable = shutil.which("tesseract")
    if executable is None:
        return None
    pgm, scale, padding = _bitmap_pgm_for_ocr(image)
    command = [
        executable,
        "stdin",
        "stdout",
        "--dpi",
        str(180 * scale),
        "--psm",
        "6",
        "-l",
        _ocr_language(),
        "tsv",
    ]
    try:
        result = _run_bounded_process(
            command,
            pgm,
            timeout=_OCR_TIMEOUT_SECONDS,
            stdout_limit=_OCR_STDOUT_LIMIT,
            stderr_limit=_OCR_STDERR_LIMIT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result is None or result.returncode != 0:
        return None
    return _parse_tesseract_tsv(result.stdout, scale=scale, padding=padding)


def _raster_table_evidence(
    payload: bytes,
    *,
    base_offset: int,
    ocr_engine: RasterOcrEngine | None,
) -> tuple[str | None, tuple[dict[str, object], ...], tuple[str, ...], tuple[str, ...]]:
    images = _reconstructed_esc_star_rasters(payload, base_offset=base_offset)
    if not images:
        return None, (), (), ()
    engine = ocr_engine or _run_tesseract_ocr
    backend = "injected" if ocr_engine is not None else runtime_build_fingerprint()
    observations: list[dict[str, object]] = []
    accepted: list[tuple[str, float]] = []
    rendered: list[str] = []
    warnings: list[str] = []
    for image in images:
        observation: dict[str, object] = {
            "backend": backend,
            "width": image.width,
            "height": image.height,
            "strip_count": image.strip_count,
            "source_offset": image.source_offset,
            "source_length": image.source_length,
            "bitmap_sha256": hashlib.sha256(
                image.packed_bits,
                usedforsecurity=True,
            ).hexdigest(),
        }
        try:
            result = engine(image)
        except Exception as exc:  # noqa: BLE001 - optional OCR must degrade without losing RAW
            observation.update(
                {
                    "status": "BACKEND_ERROR",
                    "error_type": type(exc).__name__,
                }
            )
            observations.append(observation)
            warnings.append("raster_ocr_backend_error")
            continue
        if result is None:
            observation["status"] = "NO_RESULT"
            observations.append(observation)
            continue
        normalized = "\n".join(" ".join(line.split()) for line in result.text.splitlines())[:2048]
        observation.update(
            {
                "status": "OBSERVED",
                "text": normalized,
                "confidence": round(result.confidence, 4),
                "bounding_box": result.bounding_box,
            }
        )
        observations.append(observation)
        rendered.append(
            f"<OCR:ESC_STAR:{result.confidence:.2f}>"
            f"{normalized}</OCR:ESC_STAR>"
        )
        match = _TABLE_RE.search(normalized)
        if match is None:
            continue
        if result.confidence < _OCR_MINIMUM_CONFIDENCE:
            warnings.append("raster_table_ocr_below_confidence_threshold")
            continue
        accepted.append((match.group("value").upper(), result.confidence))
    distinct = {value for value, _confidence in accepted}
    if len(distinct) > 1:
        warnings.append("raster_table_ocr_conflict")
        return None, tuple(observations), tuple(dict.fromkeys(warnings)), tuple(rendered)
    table_code = max(accepted, key=lambda item: item[1])[0] if accepted else None
    return table_code, tuple(observations), tuple(dict.fromkeys(warnings)), tuple(rendered)


def _segments(payload: bytes) -> tuple[_Segment, ...]:
    result: list[_Segment] = []
    start = 0
    cursor = 0
    while cursor + 2 < len(payload) and len(result) < _MAX_DOCUMENTS - 1:
        gs_marker = payload.find(b"\x1d\x56", cursor)
        esc_marker = payload.find(b"\x1b\x6d", cursor)
        markers = [value for value in (gs_marker, esc_marker) if value >= 0]
        if not markers:
            break
        marker = min(markers)
        if payload[marker : marker + 2] == b"\x1b\x6d":
            cut_end = marker + 2
            next_init = payload.find(b"\x1b@", cut_end)
            end = len(payload) if next_init < 0 else next_init
            result.append(_Segment(payload[start:end], start, True))
            start = end
            cursor = end
            continue
        if marker + 2 >= len(payload):
            break
        mode = payload[marker + 2]
        if mode in {0, 1, 48, 49}:
            end = marker + 3
        elif mode in {65, 66} and marker + 3 < len(payload):
            end = marker + 4
        else:
            cursor = marker + 2
            continue
        next_init = payload.find(b"\x1b@", end)
        segment_end = len(payload) if next_init < 0 else next_init
        result.append(_Segment(payload[start:segment_end], start, True))
        start = segment_end
        cursor = segment_end
    if start < len(payload) or not result:
        result.append(_Segment(payload[start:], start, False))
    return tuple(result[:_MAX_DOCUMENTS])


def _extract_lines(
    payload: bytes,
    *,
    base_offset: int,
    default_encoding: str,
) -> tuple[tuple[_TextLine, ...], str, tuple[str, ...]]:
    if default_encoding not in _ALLOWED_ENCODINGS:
        raise ValueError(f"unsupported configured ESC/POS encoding: {default_encoding}")
    encoding = default_encoding
    buffer = bytearray()
    buffer_offset = base_offset
    lines: list[_TextLine] = []
    rendered: list[str] = []
    warnings: list[str] = []
    cursor = 0
    chars = 0

    def emit_text() -> None:
        nonlocal chars
        if not buffer:
            return
        try:
            text = bytes(buffer).decode(encoding, errors="strict")
        except (LookupError, UnicodeDecodeError) as exc:
            text = "".join(
                chr(value) if 32 <= value < 127 else f"<0x{value:02X}>" for value in buffer
            )
            warnings.append(f"text_decode_error:{type(exc).__name__}:{encoding}")
        remaining = _MAX_OUTPUT_CHARS - chars
        if remaining <= 0:
            warnings.append("normalized_text_limit_exceeded")
            buffer.clear()
            return
        text = text[:remaining]
        rendered.append(text)
        chars += len(text)
        if text and len(lines) < _MAX_LINES:
            lines.append(_TextLine(text, buffer_offset, len(buffer)))
        elif text:
            warnings.append("line_limit_exceeded")
        buffer.clear()

    def control(label: str, length: int) -> None:
        nonlocal chars
        emit_text()
        if chars < _MAX_OUTPUT_CHARS:
            rendered.append(label)
            chars += len(label)
        else:
            warnings.append("normalized_text_limit_exceeded")
        del length

    while cursor < len(payload) and chars < _MAX_OUTPUT_CHARS:
        byte = payload[cursor]
        absolute = base_offset + cursor
        if byte == 0x0A:
            emit_text()
            rendered.append("\n")
            chars += 1
            cursor += 1
        elif byte == 0x0D:
            emit_text()
            cursor += 1
        elif byte == 0x09:
            if not buffer:
                buffer_offset = absolute
            buffer.extend(b"    ")
            cursor += 1
        elif byte == 0x1B and cursor + 1 < len(payload):
            command = payload[cursor + 1]
            if command == 0x40:
                control("<ESC/POS:INIT>", 2)
                cursor += 2
            elif command == 0x74 and cursor + 2 < len(payload):
                emit_text()
                code = payload[cursor + 2]
                encoding = _CODEPAGES.get(code, encoding)
                control(f"<ESC/POS:CODEPAGE:{code}:{encoding}>", 3)
                cursor += 3
            elif command in {0x21, 0x45, 0x61, 0x64, 0x4A, 0x33, 0x20} and cursor + 2 < len(
                payload
            ):
                control(f"<ESC/POS:ESC:{command:02X}:{payload[cursor + 2]}>", 3)
                cursor += 3
            elif command == 0x70 and cursor + 4 < len(payload):
                control("<ESC/POS:CASH_DRAWER>", 5)
                cursor += 5
            elif command == 0x6D:
                control("<ESC/POS:CUT>", 2)
                cursor += 2
            elif command == 0x2A and cursor + 4 < len(payload):
                width = payload[cursor + 3] + (payload[cursor + 4] << 8)
                multiplier = 3 if payload[cursor + 2] in {32, 33} else 1
                available = min(width * multiplier, max(0, len(payload) - cursor - 5))
                control(f"<ESC/POS:BIT_IMAGE:{available}>", 5 + available)
                cursor += 5 + available
            else:
                control(f"<ESC/POS:ESC:{command:02X}>", 2)
                cursor += 2
        elif byte == 0x1D and cursor + 1 < len(payload):
            command = payload[cursor + 1]
            if command == 0x56 and cursor + 2 < len(payload):
                mode = payload[cursor + 2]
                size = 4 if mode in {65, 66} and cursor + 3 < len(payload) else 3
                control("<ESC/POS:CUT>", size)
                cursor += size
            elif command == 0x76 and cursor + 7 < len(payload) and payload[cursor + 2] == 0x30:
                width = payload[cursor + 4] + (payload[cursor + 5] << 8)
                height = payload[cursor + 6] + (payload[cursor + 7] << 8)
                available = min(width * height, max(0, len(payload) - cursor - 8))
                control(f"<ESC/POS:RASTER_IMAGE:{available}>", 8 + available)
                cursor += 8 + available
            elif command == 0x6B and cursor + 2 < len(payload):
                mode = payload[cursor + 2]
                if mode >= 65 and cursor + 3 < len(payload):
                    available = min(payload[cursor + 3], max(0, len(payload) - cursor - 4))
                    size = 4 + available
                else:
                    terminator = payload.find(b"\x00", cursor + 3)
                    size = len(payload) - cursor if terminator < 0 else terminator + 1 - cursor
                    available = max(0, size - 3)
                control(f"<ESC/POS:BARCODE:{available}>", size)
                cursor += size
            elif command == 0x28 and cursor + 4 < len(payload):
                declared = payload[cursor + 3] + (payload[cursor + 4] << 8)
                available = min(declared, max(0, len(payload) - cursor - 5))
                control(f"<ESC/POS:GS_PAREN:{available}>", 5 + available)
                cursor += 5 + available
            elif command == 0x21 and cursor + 2 < len(payload):
                control(f"<ESC/POS:CHAR_SIZE:{payload[cursor + 2]}>", 3)
                cursor += 3
            else:
                control(f"<ESC/POS:GS:{command:02X}>", 2)
                cursor += 2
        elif byte == 0x10 and cursor + 2 < len(payload):
            control(f"<ESC/POS:REALTIME:{payload[cursor + 1]:02X}:{payload[cursor + 2]}>", 3)
            cursor += 3
        elif byte < 0x20 or byte == 0x7F:
            control(f"<BYTE:0x{byte:02X}>", 1)
            cursor += 1
        else:
            if not buffer:
                buffer_offset = absolute
            buffer.append(byte)
            cursor += 1
    emit_text()
    if cursor < len(payload):
        warnings.append("normalized_text_limit_exceeded")
    return tuple(lines), "".join(rendered).strip(), tuple(dict.fromkeys(warnings))


def _classify(text: str) -> tuple[DocumentType, str]:
    upper = text.upper()
    if "COPIA CONFORME" in upper:
        return DocumentType.CONFORMING_COPY, "COPIA_CONFORME_LITERAL"
    if "RISTAMPA" in upper:
        return DocumentType.REPRINT, "RISTAMPA_LITERAL"
    if "ANNULL" in upper or "STORNO" in upper:
        return DocumentType.CANCELLATION, "ANNULLAMENTO_LITERAL"
    if "PRECONTO" in upper or "PRE-CONTO" in upper:
        return DocumentType.PRE_BILL, "PRECONTO_LITERAL"
    if re.search(r"(?m)^[ \t]*-\d+(?:[.,]\d+)?[ \t]*[xX][ \t]+", text):
        return DocumentType.ORDER_CHANGE, "VARIAZIONE_QUANTITA_POS_INFERRED"
    if "COMANDA" in upper or "CUCINA" in upper:
        return DocumentType.KITCHEN_ORDER, "COMANDA_LITERAL"
    if "ORDINE" in upper:
        return DocumentType.ORDER, "ORDINE_LITERAL"
    if "PORTATA:" in upper or (
        "COPERTI:" in upper and re.search(r"\b\d+\s*[xX]\s+", text)
    ):
        return DocumentType.KITCHEN_ORDER, "TICKET_POS_INFERRED"
    if "DOCUMENTO GESTIONALE" in upper:
        return DocumentType.MANAGEMENT_DOCUMENT, "GESTIONALE_LITERAL"
    return DocumentType.UNKNOWN, "NESSUN_MARCATORE_CONFERMATO"


def _field(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group("value") if match else None


def _operator_and_timestamp(
    text: str,
    *,
    captured_at: datetime,
    timezone_name: str,
) -> tuple[str | None, datetime | None, tuple[str, ...]]:
    match = _OPERATOR_LINE_RE.search(text)
    if match is None:
        return None, None, ()
    value = " ".join(match.group("value").split())
    timestamp_match = _LOCAL_TIMESTAMP_RE.search(value)
    operator = value
    document_timestamp = None
    warnings: list[str] = []
    if timestamp_match is not None:
        operator = (value[: timestamp_match.start()] + value[timestamp_match.end() :]).strip(" -:;")
        local_value = f"{timestamp_match.group('date')} {timestamp_match.group('time')}"
        formats = (
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d/%m/%y %H:%M:%S",
            "%d/%m/%y %H:%M",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M",
            "%d-%m-%y %H:%M:%S",
            "%d-%m-%y %H:%M",
        )
        naive = None
        for candidate in formats:
            with contextlib.suppress(ValueError):
                naive = datetime.strptime(local_value, candidate)
                break
        if naive is not None:
            try:
                zone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                warnings.append("document_timezone_unknown")
            else:
                candidates: list[datetime] = []
                for fold in (0, 1):
                    aware = naive.replace(tzinfo=zone, fold=fold)
                    roundtrip = aware.astimezone(UTC).astimezone(zone)
                    if roundtrip.replace(tzinfo=None) == naive:
                        candidates.append(aware)
                if candidates:
                    document_timestamp = min(
                        candidates,
                        key=lambda item: abs((item - captured_at.astimezone(zone)).total_seconds()),
                    )
                else:
                    warnings.append("document_local_time_nonexistent")
        else:
            warnings.append("document_timestamp_invalid")
    return operator[:128] or None, document_timestamp, tuple(warnings)


def _join_wrapped_description(left: str, right: str, *, force_space: bool = False) -> str:
    fragment = " ".join(right.split())
    if not fragment:
        return left
    first = fragment.split(maxsplit=1)[0].lower()
    previous = left.rsplit(maxsplit=1)[-1].lower() if left.split() else ""
    first_is_function_word = first in _SPACE_BEFORE_CONTINUATION and (
        len(first) > 1 or len(fragment.split()) > 1
    )
    needs_space = (
        force_space
        or first_is_function_word
        or fragment.lower() in _UNIT_CONTINUATIONS
        or previous in _SPACE_BEFORE_CONTINUATION
    )
    return f"{left}{' ' if needs_space else ''}{fragment}"


def _is_non_item_line(text: str) -> bool:
    stripped = text.strip()
    upper = stripped.upper()
    return (
        not stripped
        or _SEPARATOR_RE.fullmatch(stripped) is not None
        or _DOCUMENT_HEADER_RE.match(stripped) is not None
        or upper.startswith(("TOTALE", "TOT ", "IMPORTO", "SUBTOTALE", "SUB-TOTALE"))
    )


def _semantic_lines(
    lines: tuple[_TextLine, ...],
    base_offset: int,
) -> tuple[tuple[DocumentLine, ...], dict[str, object]]:
    result: list[DocumentLine] = []
    line_courses: dict[str, str] = {}
    joined_line_fragments: list[dict[str, object]] = []
    current_course: str | None = None
    covers: int | None = None
    active: _PendingLine | None = None

    def finish_active() -> None:
        nonlocal active
        if active is None or len(result) >= _MAX_LINES:
            active = None
            return
        raw_fragments = tuple(active.raw_fragments)
        state = "QUANTITY_DECREASE" if active.quantity < 0 else "ACTIVE"
        result.append(
            DocumentLine(
                sequence=len(result) + 1,
                description=active.description.strip()[:512],
                quantity=active.quantity,
                unit_price=active.amount,
                line_total=active.amount,
                state=state,
                removed=False,
                raw_text="\n".join(raw_fragments),
                course_code=active.course,
                source=SourceSpan(
                    direction="CLIENT_TO_DEVICE",
                    offset=max(base_offset, active.source_start),
                    length=max(0, active.source_end - active.source_start),
                ),
            )
        )
        if active.course is not None:
            line_courses[str(len(result))] = active.course
        if len(raw_fragments) > 1:
            joined_line_fragments.append(
                {
                    "sequence": len(result),
                    "fragments": list(raw_fragments),
                    "source_offset": active.source_start,
                    "source_length": max(0, active.source_end - active.source_start),
                }
            )
        active = None

    for line in lines:
        collapsed = " ".join(line.text.split())
        course_match = _COURSE_RE.match(collapsed)
        if course_match is not None:
            finish_active()
            current_course = course_match.group("value").upper()
            continue
        covers_match = _COVERS_RE.match(collapsed)
        if covers_match is not None:
            finish_active()
            covers = int(covers_match.group("value"))
            continue
        item_match = _ITEM_RE.match(line.text)
        if item_match is not None:
            finish_active()
            quantity = Decimal(item_match.group("quantity").replace(",", "."))
            raw_description = item_match.group("description")
            amounts = list(_MONEY_RE.finditer(raw_description))
            amount = _money(amounts[-1].group("value")) if amounts else None
            description = (
                raw_description[: amounts[-1].start()].strip(" -:;")
                if amounts
                else raw_description.strip()
            )
            active = _PendingLine(
                description=description,
                quantity=quantity,
                amount=amount,
                course=current_course,
                raw_fragments=[line.text.rstrip()],
                source_start=line.offset,
                source_end=line.offset + line.length,
                space_before_next=raw_description.endswith(" "),
            )
            continue
        if active is not None and line.text[:1].isspace() and not _is_non_item_line(collapsed):
            active.description = _join_wrapped_description(
                active.description,
                line.text,
                force_space=active.space_before_next,
            )
            active.space_before_next = line.text.rstrip("\r\n").endswith(" ")
            active.raw_fragments.append(line.text.rstrip())
            active.source_end = line.offset + line.length
            continue
        finish_active()
        if _is_non_item_line(collapsed):
            continue
        amounts = list(_MONEY_RE.finditer(collapsed))
        if not amounts:
            continue
        amount = _money(amounts[-1].group("value"))
        description = collapsed[: amounts[-1].start()].strip(" -:;")
        if not description:
            continue
        active = _PendingLine(
            description=description,
            quantity=Decimal("1"),
            amount=amount,
            course=current_course,
            raw_fragments=[line.text.rstrip()],
            source_start=line.offset,
            source_end=line.offset + line.length,
        )
    finish_active()
    metadata: dict[str, object] = {}
    if covers is not None:
        metadata["covers"] = covers
    if line_courses:
        metadata["line_courses"] = line_courses
    if joined_line_fragments:
        metadata["joined_line_fragments"] = joined_line_fragments
    return tuple(result), metadata


def _document_total(lines: tuple[_TextLine, ...]) -> Decimal | None:
    values: list[Decimal] = []
    for line in lines:
        text = " ".join(line.text.split())
        if not text.upper().startswith(
            ("TOTALE", "TOT ", "IMPORTO", "SUBTOTALE", "SUB-TOTALE")
        ):
            continue
        amounts = list(_MONEY_RE.finditer(text))
        if amounts:
            value = _money(amounts[-1].group("value"))
            if value is not None:
                values.append(value)
    return values[-1] if values else None


def parse_escpos(
    payload: bytes,
    *,
    device_id: str,
    session_id: str | None,
    job_id: str,
    captured_at: datetime,
    manifest_sha256: str,
    source_path: str | Path,
    encoding: str = "cp858",
    timezone_name: str = "Europe/Rome",
    ocr_engine: RasterOcrEngine | None = None,
) -> tuple[NormalizedDocument, ...]:
    """Return versionable documents; byte framing never depends on TCP recv calls."""

    source_hash = hashlib.sha256(payload, usedforsecurity=True).hexdigest()
    if len(payload) > _MAX_INPUT_BYTES:
        payload = payload[:_MAX_INPUT_BYTES]
        global_warning = ("parser_input_limit_exceeded",)
    else:
        global_warning = ()
    documents: list[NormalizedDocument] = []
    for segment in _segments(payload):
        lines, text, warnings = _extract_lines(
            segment.payload,
            base_offset=segment.base_offset,
            default_encoding=encoding,
        )
        if not segment.payload:
            continue
        semantic_lines, line_metadata = _semantic_lines(lines, segment.base_offset)
        doc_type, subtype = _classify(text)
        if any(line.quantity is not None and line.quantity < 0 for line in semantic_lines):
            doc_type = DocumentType.ORDER_CHANGE
            subtype = "VARIAZIONE_QUANTITA_POS_INFERRED"
        gross_total = _document_total(lines)
        operator_code, document_timestamp, timestamp_warnings = _operator_and_timestamp(
            text,
            captured_at=captured_at,
            timezone_name=timezone_name,
        )
        raster_table, raster_observations, raster_warnings, raster_text = (
            _raster_table_evidence(
                segment.payload,
                base_offset=segment.base_offset,
                ocr_engine=ocr_engine,
            )
        )
        plain_table = _field(_TABLE_RE, text)
        table_code = plain_table or raster_table
        if raster_text:
            text = f"{text}\n" + "\n".join(raster_text)
        # The source identity deliberately excludes the parser version.  A new
        # parser release must append a DocumentVersion to the same document,
        # never manufacture a second business document from unchanged bytes.
        document_id = uuid5(
            NAMESPACE_URL,
            f"retailprintguard:escpos:{job_id}:{segment.base_offset}:{source_hash}",
        )
        documents.append(
            NormalizedDocument(
                id=document_id,
                source_device_id=device_id,
                source_session_id=session_id,
                source_job_id=job_id,
                type=doc_type,
                subtype=subtype,
                external_document_code=_field(_DOCUMENT_RE, text),
                order_code=_field(_ORDER_RE, text),
                table_code=table_code,
                operator_code=operator_code,
                document_timestamp=document_timestamp,
                captured_at=captured_at,
                gross_total=gross_total,
                net_total=gross_total,
                status="COMPLETE" if segment.cut_observed else "PARTIAL",
                normalized_text=text,
                encoding=encoding,
                parser_name=PARSER_NAME,
                parser_version=PARSER_VERSION,
                parse_confidence=85 if doc_type is not DocumentType.UNKNOWN else 25,
                evidence=(
                    EvidenceLevel.CONFIRMED
                    if doc_type is not DocumentType.UNKNOWN
                    else EvidenceLevel.UNKNOWN
                ),
                source_manifest_sha256=manifest_sha256,
                source_payload_sha256=source_hash,
                source_path=str(source_path),
                complete=segment.cut_observed,
                warnings=tuple(
                    dict.fromkeys(
                        (
                            *global_warning,
                            *warnings,
                            *timestamp_warnings,
                            *raster_warnings,
                        )
                    )
                ),
                lines=semantic_lines,
                raw_metadata={
                    "source_start_offset": segment.base_offset,
                    "source_end_offset": segment.base_offset + len(segment.payload),
                    "cut_observed": segment.cut_observed,
                    "classification_evidence": subtype
                    if doc_type is not DocumentType.UNKNOWN
                    else "UNKNOWN",
                    "table_code_evidence": (
                        "ESC_POS_RASTER_OCR_INFERRED"
                        if raster_table is not None and plain_table is None
                        else "PLAIN_TEXT"
                        if table_code is not None
                        else "UNKNOWN"
                    ),
                    "raster_ocr": {
                        "runtime": (
                            "injected" if ocr_engine is not None else runtime_build_fingerprint()
                        ),
                        "minimum_confidence": _OCR_MINIMUM_CONFIDENCE,
                        "observations": list(raster_observations),
                    },
                    **line_metadata,
                },
            )
        )
    return tuple(documents)


__all__ = [
    "PARSER_NAME",
    "PARSER_RUNTIME_FINGERPRINT",
    "PARSER_VERSION",
    "RasterOcrEngine",
    "RasterOcrResult",
    "parse_escpos",
    "runtime_build_fingerprint",
]
