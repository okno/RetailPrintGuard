"""CLI entry point for one-shot historical import."""

from __future__ import annotations

import os
from collections.abc import Sequence

from retailprintguard.ingestion.main import run_cli


def cli(argv: Sequence[str] | None = None) -> int:
    os.umask(0o027)
    return run_cli(argv, historical=True)


if __name__ == "__main__":
    raise SystemExit(cli())
