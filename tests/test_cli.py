import pytest

from reachy_signals.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.startswith("reachy-signals 0.1.0")


def test_no_arguments_prints_help(capsys):
    assert main([]) == 0
    assert "usage: reachy-signals" in capsys.readouterr().out
