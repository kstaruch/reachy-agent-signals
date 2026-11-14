import pytest

from reachy_signals.events import Event, EventError, from_hook, hook_summary

PERMISSION = {
    "session_id": "abc123",
    "transcript_path": "/tmp/t.jsonl",
    "cwd": "/work/proj",
    "permission_mode": "default",
    "hook_event_name": "Notification",
    "message": "Claude needs your permission to use Bash",
    "title": "Permission needed",
    "notification_type": "permission_prompt",
}


def test_blocked_from_notification():
    ev = from_hook("blocked", PERMISSION)
    assert (ev.source, ev.event, ev.session, ev.cwd) == (
        "claude-code",
        "blocked",
        "abc123",
        "/work/proj",
    )
    assert ev.detail == "permission_prompt"
    assert ev.hook["hook_event_name"] == "Notification"
    assert "message" not in ev.hook  # free text isn't logged
    assert ev.ts.endswith("Z")


def test_prompt_text_is_never_kept():
    h = hook_summary({"hook_event_name": "UserPromptSubmit", "prompt": "fix the secret bug"})
    assert h["prompt"] == {"length": 18, "slash": None}
    h = hook_summary({"prompt": "  /reachy-signals:reachy quiet on"})
    assert h["prompt"]["slash"] == "/reachy-signals:reachy"


def test_observe_detail_names_hook_and_tool():
    ev = from_hook(
        "observe", {"hook_event_name": "PreToolUse", "tool_name": "Bash", "agent_id": "x"}
    )
    assert ev.detail == "PreToolUse:Bash"
    assert ev.hook["subagent"] is True
    assert "tool_input" not in ev.hook


def test_garbage_payload_types_are_ignored():
    ev = from_hook("done", {"session_id": 5, "cwd": None, "tool_name": {"x": 1}})
    assert ev.session == "" and ev.cwd == "" and "tool_name" not in ev.hook


def test_round_trip():
    ev = from_hook("blocked", PERMISSION)
    assert Event.from_dict(ev.to_dict()) == ev
    assert ev.timestamp() is not None


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"event": "done"},
        {"source": "", "event": "done"},
        {"source": "x", "event": "finished"},
        {"source": "x", "event": "done", "session": 3},
        {"source": "x", "event": "done", "hook": []},
    ],
)
def test_from_dict_rejects(data):
    with pytest.raises(EventError):
        Event.from_dict(data)
