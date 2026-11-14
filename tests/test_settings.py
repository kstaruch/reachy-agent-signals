import logging
from datetime import datetime, time
from pathlib import Path

import pytest

from reachy_signals import settings as s
from reachy_signals.settings import (
    Settings,
    SettingsError,
    TimeRange,
    load,
    parse,
    parse_duration,
    parse_time_range,
    quiet_active,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_match_example_file(caplog):
    with caplog.at_level(logging.WARNING):
        assert load(EXAMPLE) == Settings()
    assert caplog.records == [], "example file has keys the code doesn't know"


def test_missing_file_means_defaults(tmp_path):
    assert load(tmp_path / "nope.toml") == Settings()


def test_default_values():
    d = Settings()
    assert d.enabled is True
    assert d.bridge.port == 8765
    assert d.bridge.max_event_age == 10
    assert d.source_mode("claude-code") == "drive"
    assert d.source_mode("herdr") == "off"
    assert d.source_mode("unheard-of") == "off"
    assert d.reactions.for_event("blocked") == ("attentive1",)
    assert d.escalation.blocked_after == 180
    assert d.escalation.blocked == ("toc-toc-toc",)


def test_partial_file_merges_over_defaults(tmp_path):
    p = write(
        tmp_path,
        """
enabled = false
[reactions]
done = ["success1", "proud1"]
[sources.herdr]
mode = "shadow"
""",
    )
    got = load(p)
    assert got.enabled is False
    assert got.reactions.done == ("success1", "proud1")
    assert got.reactions.started == ("yeah_nod",)  # default kept
    assert got.source_mode("herdr") == "shadow"
    assert got.source_mode("claude-code") == "drive"  # default kept


def test_empty_reaction_list_is_allowed():
    assert parse({"quiet": {"reactions": {"started": []}}}).quiet.reactions.started == ()


def test_daemon_url_trailing_slash_stripped():
    assert parse({"bridge": {"daemon_url": "http://localhost:8001/api/"}}).bridge.daemon_url == (
        "http://localhost:8001/api"
    )


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"enabled": "yes"}, "enabled"),
        ({"bridge": {"port": 0}}, "bridge.port"),
        ({"bridge": {"port": "8765"}}, "bridge.port"),
        ({"bridge": {"daemon_url": "localhost:8000"}}, "bridge.daemon_url"),
        ({"bridge": {"max_event_age": "soon"}}, "bridge.max_event_age"),
        ({"sources": {"claude-code": {"mode": "on"}}}, "sources.claude-code.mode"),
        ({"sources": {"claude-code": "drive"}}, "sources.claude-code"),
        ({"reactions": {"done": "cheerful1"}}, "reactions.done"),
        ({"reactions": {"done": [""]}}, "reactions.done"),
        ({"quiet": {"enabled": 1}}, "quiet.enabled"),
        ({"quiet": {"schedule": "22:00-08:00"}}, "quiet.schedule"),
        ({"quiet": {"schedule": ["25:00-08:00"]}}, "quiet.schedule[0]"),
        ({"escalation": {"blocked_after": 0}}, "escalation.blocked_after"),
        ({"bridge": []}, "bridge"),
    ],
)
def test_invalid_values_name_the_key(data, key):
    with pytest.raises(SettingsError, match=rf"^{key.replace('[', r'\[').replace(']', r'\]')}:"):
        parse(data)


def test_invalid_toml(tmp_path):
    with pytest.raises(SettingsError, match="invalid TOML"):
        load(write(tmp_path, "enabled = = true"))


def test_unknown_keys_warn_but_load(caplog):
    with caplog.at_level(logging.WARNING):
        got = parse({"colour": "red", "quiet": {"allow": ["blocked"]}, "bridge": {"host": "x"}})
    assert got == Settings()
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "'colour'" in msgs and "'quiet.allow'" in msgs and "'bridge.host'" in msgs


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("90s", 90), ("3m", 180), ("1h", 3600), ("1.5m", 90), (" 2 m ", 120), (45, 45), (0.5, 0.5)],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value, "k") == seconds


@pytest.mark.parametrize("value", ["", "3", "3d", "-1s", "m", True, None, 0, "0s"])
def test_parse_duration_rejects(value):
    with pytest.raises(SettingsError):
        parse_duration(value, "k")


def test_time_range_same_day():
    r = parse_time_range("09:00-17:30", "k")
    assert r == TimeRange(time(9), time(17, 30))
    assert r.contains(time(9)) and r.contains(time(17, 29))
    assert not r.contains(time(17, 30)) and not r.contains(time(8, 59))


def test_time_range_past_midnight():
    r = parse_time_range("22:00-08:00", "k")
    assert r.contains(time(22)) and r.contains(time(23, 59)) and r.contains(time(0))
    assert r.contains(time(7, 59))
    assert not r.contains(time(8)) and not r.contains(time(12)) and not r.contains(time(21, 59))


@pytest.mark.parametrize("value", ["22:00", "22:00-22:00", "7:00-08:00", "22:60-08:00", 2200])
def test_time_range_rejects(value):
    with pytest.raises(SettingsError):
        parse_time_range(value, "k")


def test_quiet_active_manual_and_schedule():
    manual = parse({"quiet": {"enabled": True}})
    scheduled = parse({"quiet": {"schedule": ["22:00-08:00"]}})
    noon, night = datetime(2026, 11, 14, 12), datetime(2026, 11, 14, 23)
    assert quiet_active(manual, noon)
    assert not quiet_active(scheduled, noon)
    assert quiet_active(scheduled, night)
    assert not quiet_active(Settings(), night)


def test_config_path_uses_platformdirs(monkeypatch, tmp_path):
    calls = []

    def fake_user_config_dir(appname, appauthor=None, roaming=False):
        calls.append((appname, appauthor, roaming))
        return str(tmp_path / appname)

    monkeypatch.delenv(s.CONFIG_ENV, raising=False)
    monkeypatch.setattr(s, "user_config_dir", fake_user_config_dir)
    assert s.config_path() == tmp_path / "reachy-signals" / "config.toml"
    # roaming=True puts it under %APPDATA% on Windows, as the spec says
    assert calls == [("reachy-signals", False, True)]


def test_config_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(s.CONFIG_ENV, str(tmp_path / "x.toml"))
    assert s.config_path() == tmp_path / "x.toml"
