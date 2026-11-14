"""Settings changes from the CLI keep the file's comments (edit.py)."""

import shutil
import tomllib
from pathlib import Path

import pytest

from reachy_signals import edit
from reachy_signals.settings import SettingsError, load

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.toml"
    shutil.copy(EXAMPLE, p)
    return p


def comments(text):
    return [line.split("#", 1)[1] for line in text.splitlines() if "#" in line]


def test_round_trip_keeps_comments(cfg):
    before = cfg.read_text(encoding="utf-8")
    edit.update(edit.set_enabled(False), cfg)
    edit.update(edit.set_quiet(True), cfg)
    edit.update(edit.set_source("herdr", "shadow"), cfg)
    after = cfg.read_text(encoding="utf-8")
    assert comments(after) == comments(before)
    s = load(cfg)
    assert (s.enabled, s.quiet.enabled, s.source_mode("herdr")) == (False, True, "shadow")
    # everything else untouched
    assert s.reactions.done == ("cheerful1",) and s.quiet.schedule == ()


def test_only_the_changed_lines_differ(cfg):
    before = cfg.read_text(encoding="utf-8").splitlines()
    edit.update(edit.set_quiet(True), cfg)
    after = cfg.read_text(encoding="utf-8").splitlines()
    changed = [(a, b) for a, b in zip(before, after, strict=True) if a != b]
    assert len(changed) == 1 and changed[0][1].startswith("enabled = true")


def test_no_change_no_write(cfg):
    edit.update(edit.set_enabled(True), cfg)  # already true
    mtime = cfg.stat().st_mtime_ns
    edit.update(edit.set_enabled(True), cfg)
    assert cfg.stat().st_mtime_ns == mtime


def test_creates_missing_file(tmp_path):
    p = tmp_path / "sub" / "config.toml"
    edit.update(edit.set_enabled(False), p)
    edit.update(edit.set_quiet(True), p)
    edit.update(edit.set_source("codex", "shadow"), p)
    text = p.read_text(encoding="utf-8")
    assert text.startswith("# reachy-agent-signals settings")
    data = tomllib.loads(text)
    assert data == {
        "enabled": False,
        "quiet": {"enabled": True},
        "sources": {"codex": {"mode": "shadow"}},
    }


def test_new_top_level_key_goes_before_tables(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[quiet]\nenabled = true\n", encoding="utf-8")
    edit.update(edit.set_enabled(False), p)
    assert load(p).enabled is False and load(p).quiet.enabled is True


def test_new_source_in_existing_file(cfg):
    edit.update(edit.set_source("codex", "drive"), cfg)
    s = load(cfg)
    assert s.source_mode("codex") == "drive" and s.source_mode("claude-code") == "drive"


def test_refuses_to_edit_invalid_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("enabled = = broken", encoding="utf-8")
    with pytest.raises(SettingsError, match="fix it by hand"):
        edit.update(edit.set_enabled(False), p)
    assert p.read_text(encoding="utf-8") == "enabled = = broken"


def test_refuses_change_that_fails_validation(cfg):
    def bad(doc):
        doc["bridge"]["port"] = 0

    before = cfg.read_text(encoding="utf-8")
    with pytest.raises(SettingsError, match="bridge.port"):
        edit.update(bad, cfg)
    assert cfg.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(("name", "mode"), [("herdr", "loud"), ("a.b", "drive"), ("", "off")])
def test_bad_source_arguments(name, mode):
    with pytest.raises(SettingsError):
        edit.set_source(name, mode)


def test_no_temp_files_left(cfg):
    edit.update(edit.set_quiet(True), cfg)
    assert sorted(x.name for x in cfg.parent.iterdir()) == ["config.toml"]
