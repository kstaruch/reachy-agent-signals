import logging
import os
import time
from pathlib import Path

import pytest

from reachy_signals.settings import Settings, SettingsError
from reachy_signals.watch import SettingsWatcher


def write(path: Path, text: str, bump: int = 0) -> None:
    """Write and move mtime forward explicitly, so coarse filesystem clocks can't hide a change."""
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump * 1_000_000_000))


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.toml"
    write(p, "enabled = true\n")
    return p


def test_reloads_on_change(cfg):
    seen = []
    w = SettingsWatcher(cfg, on_change=seen.append)
    assert w.current().enabled is True
    assert w.check() is False  # unchanged
    write(cfg, "enabled = false\n", bump=1)
    assert w.check() is True
    assert w.current().enabled is False
    assert seen == [w.current()]


def test_invalid_edit_keeps_last_good_settings(cfg, caplog):
    w = SettingsWatcher(cfg)
    write(cfg, "enabled = false\n[bridge]\nport = 99999\n", bump=1)
    with caplog.at_level(logging.ERROR):
        assert w.check() is False
    assert w.current().enabled is True
    assert "bridge.port" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert w.check() is False  # same bad file: no repeated error
    assert caplog.text == ""
    write(cfg, "enabled = false\n", bump=2)  # fixed
    assert w.check() is True
    assert w.current().enabled is False


def test_removed_file_means_defaults(cfg):
    write(cfg, "enabled = false\n")
    w = SettingsWatcher(cfg)
    cfg.unlink()
    assert w.check() is True
    assert w.current() == Settings()


def test_invalid_file_at_startup_raises(cfg):
    write(cfg, "enabled = 'maybe'\n")
    with pytest.raises(SettingsError):
        SettingsWatcher(cfg)


def test_background_thread_picks_up_change(cfg):
    with SettingsWatcher(cfg, interval=0.05) as w:
        write(cfg, "enabled = false\n", bump=1)
        deadline = time.monotonic() + 3
        while w.current().enabled and time.monotonic() < deadline:
            time.sleep(0.02)
        assert w.current().enabled is False
