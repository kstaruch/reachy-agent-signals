"""``reachy-signals`` command line.

``emit`` is dispatched before anything else is imported or parsed: it runs on every
agent hook and must start fast and always exit 0 (see emit.py).
"""

from __future__ import annotations

import sys


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("reachy-agent-signals")
    except PackageNotFoundError:
        return "unknown"


def build_parser():
    import argparse

    from .events import EVENT_NAMES

    parser = argparse.ArgumentParser(
        prog="reachy-signals",
        description="Make a Reachy Mini react to coding-agent sessions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    serve = sub.add_parser("serve", help="run the bridge (Ctrl+C to stop)")
    serve.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    emit = sub.add_parser(
        "emit", help="send an event to the bridge (used by hooks; reads hook JSON from stdin)"
    )
    emit.add_argument("event", choices=EVENT_NAMES)

    sub.add_parser("paths", help="show the settings file and event log locations")
    return parser


def _serve(verbose: bool) -> int:
    import logging

    from .bridge import serve
    from .eventlog import log_dir
    from .settings import config_path

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return serve(config_path(), log_dir() / "events.jsonl")


def _paths() -> int:
    from .eventlog import log_dir
    from .settings import config_path

    cfg = config_path()
    print(f"settings:  {cfg}{'' if cfg.exists() else '  (not created yet: defaults in use)'}")
    print(f"event log: {log_dir() / 'events.jsonl'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "emit":
        from .emit import main as emit_main

        return emit_main(argv[1:])

    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args.verbose)
    if args.command == "paths":
        return _paths()
    build_parser().print_help()
    return 0
