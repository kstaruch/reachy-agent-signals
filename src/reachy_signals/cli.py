"""``reachy-signals`` command line. Subcommands arrive with milestones 2 and 5."""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version


def _version() -> str:
    try:
        return version("reachy-agent-signals")
    except PackageNotFoundError:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reachy-signals",
        description="Make a Reachy Mini react to coding-agent sessions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
