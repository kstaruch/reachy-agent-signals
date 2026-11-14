"""Static checks on the Claude Code plugin files (docs/spec.md → Claude Code plugin)."""

import json
import shlex
from pathlib import Path

from reachy_signals.events import EVENT_NAMES

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def all_handlers():
    for event, groups in load(PLUGIN / "hooks" / "hooks.json")["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                yield event, group.get("matcher"), handler


def test_marketplace_points_at_plugin():
    market = load(ROOT / ".claude-plugin" / "marketplace.json")
    manifest = load(PLUGIN / ".claude-plugin" / "plugin.json")
    (entry,) = market["plugins"]
    assert entry["source"] == "./plugin"
    assert entry["name"] == manifest["name"] == "reachy-signals"


def test_every_hook_is_async_emit_with_valid_event():
    handlers = list(all_handlers())
    assert handlers
    for event, _, h in handlers:
        assert h["type"] == "command" and h["async"] is True, event
        argv = shlex.split(h["command"])
        # the CLI directly: no shell scripts, pipes or curl (works in Git Bash and PowerShell)
        assert argv[:2] == ["reachy-signals", "emit"] and len(argv) == 3, h["command"]
        assert argv[2] in EVENT_NAMES


def test_reaction_mapping_matches_spec():
    mapping = {
        (e, m, shlex.split(h["command"])[2])
        for e, m, h in all_handlers()
        if not h["command"].endswith("observe")
    }
    assert mapping == {
        ("UserPromptSubmit", None, "started"),
        ("Stop", None, "done"),
        ("Notification", "permission_prompt|elicitation_dialog", "blocked"),  # D8: not idle
    }
