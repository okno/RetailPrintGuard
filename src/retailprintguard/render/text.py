"""Human-readable receipt projection derived from normalized parser text.

The normalized text remains available as technical evidence.  This module only
removes explicit parser annotations from the presentation copy used by the web
application and PDF renderer; it never reads or changes the immutable RAW.
"""

from __future__ import annotations

import re

_OCR_BLOCK = re.compile(r"<OCR:[^>]+>(.*?)</OCR:[^>]+>", re.DOTALL)
_TECHNICAL_TOKEN = re.compile(r"<(?:ESC/POS|BYTE):[^>]*>")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def receipt_text(normalized_text: str, *, maximum_characters: int = 250_000) -> str:
    """Return a bounded, readable projection while preserving printable text.

    OCR payload text is kept, whereas the surrounding OCR provenance tags and
    ESC/POS control annotations are removed.  At most one consecutive blank
    line is retained so a command-heavy ticket does not become an empty page.
    """

    if maximum_characters < 1:
        raise ValueError("maximum_characters must be positive")
    source = normalized_text[:maximum_characters]
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    source = _OCR_BLOCK.sub(lambda match: match.group(1), source)
    source = _TECHNICAL_TOKEN.sub("", source)
    source = _CONTROL_CHARACTER.sub("", source)

    output: list[str] = []
    previous_blank = True
    for value in source.split("\n"):
        line = value.rstrip()
        blank = not line.strip()
        if blank and previous_blank:
            continue
        output.append("" if blank else line)
        previous_blank = blank
    while output and not output[-1]:
        output.pop()
    return "\n".join(output).strip("\n")


__all__ = ["receipt_text"]
