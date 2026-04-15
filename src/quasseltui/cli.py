"""Command-line entry point for quasseltui."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from quasseltui import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quasseltui",
        description="Terminal client for Quassel IRC cores.",
    )
    parser.add_argument("--version", action="version", version=f"quasseltui {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    print(f"quasseltui {__version__} — under construction")
    return 0
