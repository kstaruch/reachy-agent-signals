"""``reachy-signals emit``: hand one event to the bridge, fast, and never fail.

Runs on every hook, so it must:
- import little (stdlib + settings, no tomlkit),
- give up quickly when the bridge is down,
- exit 0 whatever happens — a Stop hook exiting 2 would keep Claude working.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.request
from typing import Any

from .events import EVENT_NAMES, Event, from_hook

POST_TIMEOUT = 0.5  # seconds
STDIN_TIMEOUT = 2.0  # hooks close stdin at once; this only guards a stray open terminal
MAX_STDIN = 1_000_000


def _read_stdin(timeout: float = STDIN_TIMEOUT) -> dict[str, Any]:
    stream = sys.stdin
    if stream is None or stream.isatty():
        return {}
    box: list[str] = []

    def reader() -> None:
        box.append(stream.read(MAX_STDIN))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    t.join(timeout)
    if not box or not box[0].strip():
        return {}
    data = json.loads(box[0])
    return data if isinstance(data, dict) else {}


DEFAULT_URL = "http://127.0.0.1:8765"


def _bridge_url() -> str:
    """``bridge.url`` if set (bridge on another machine), else localhost on ``bridge.port``."""
    try:
        from .settings import load

        return load().bridge.client_url
    except Exception:
        return DEFAULT_URL


def post(event: Event, url: str, timeout: float = POST_TIMEOUT) -> bool:
    req = urllib.request.Request(
        f"{url}/events",
        data=json.dumps(event.to_dict()).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reachy-signals emit", add_help=True)
    p.add_argument("event", choices=EVENT_NAMES)
    p.add_argument("--source", default="claude-code")
    p.add_argument("--session", help="session id (default: from hook JSON)")
    p.add_argument("--cwd", help="working directory (default: from hook JSON, else current)")
    p.add_argument("--detail", help="detail (default: from hook JSON)")
    p.add_argument("--no-stdin", action="store_true", help="don't read hook JSON from stdin")
    return p


def run(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    try:
        payload = {} if args.no_stdin else _read_stdin()
    except (ValueError, OSError):
        payload = {}  # garbage on stdin still produces an event
    ev = from_hook(args.event, payload, source=args.source)
    overrides = {
        "session": args.session,
        "cwd": args.cwd or (None if ev.cwd else os.getcwd()),
        "detail": args.detail,
    }
    ev = Event(**{**ev.to_dict(), **{k: v for k, v in overrides.items() if v is not None}})
    post(ev, _bridge_url())


def main(argv: list[str]) -> int:
    """Always returns 0. Set REACHY_SIGNALS_DEBUG=1 to see why an emit failed."""
    try:
        run(argv)
    except SystemExit as e:  # argparse: --help or bad arguments
        if e.code not in (0, None) and os.environ.get("REACHY_SIGNALS_DEBUG"):
            print(f"reachy-signals emit: bad arguments: {argv}", file=sys.stderr)
    except BaseException as e:  # bridge down, timeout, anything: never block the agent
        if os.environ.get("REACHY_SIGNALS_DEBUG"):
            print(f"reachy-signals emit: {type(e).__name__}: {e}", file=sys.stderr)
    return 0
