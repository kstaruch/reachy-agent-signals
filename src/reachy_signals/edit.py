"""Change the settings file from the CLI, keeping its comments and layout (tomlkit).

Every change is validated with the same parser the bridge uses before it's written,
and written atomically, so the bridge's hot reload never sees a half-written or
invalid file. The bridge picks the change up within about a second.
"""

from __future__ import annotations

import os
import tempfile
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from .settings import MODES, Settings, SettingsError, config_path, parse

HEADER = (
    "reachy-agent-signals settings. Created by the reachy-signals CLI.\n"
    "Every key is described in config.example.toml in the repository.\n"
    "The bridge reloads this file on change."
)


def update(change: Callable[[TOMLDocument], None], path: Path | None = None) -> Settings:
    """Apply ``change`` to the settings document and save it. Returns the new settings.

    Refuses (SettingsError) to touch a file that's already invalid, and to save a
    change that would make it invalid.
    """
    path = path or config_path()
    try:
        before = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        before = None
    except OSError as e:
        raise SettingsError(f"{path}: can't read: {e}") from e

    if before is None:
        doc = tomlkit.document()
        for line in HEADER.splitlines():
            doc.add(tomlkit.comment(line))
        doc.add(tomlkit.nl())
    else:
        try:
            doc = tomlkit.parse(before)
            parse(tomllib.loads(before))
        except (TOMLKitError, tomllib.TOMLDecodeError) as e:
            raise SettingsError(f"{path}: invalid TOML, fix it by hand first: {e}") from e

    change(doc)
    after = tomlkit.dumps(doc)
    try:
        settings = parse(tomllib.loads(after))
    except tomllib.TOMLDecodeError as e:  # pragma: no cover - tomlkit wrote bad TOML
        raise SettingsError(f"refusing to save invalid TOML: {e}") from e
    if after != before:
        _write_atomic(path, after)
    return settings


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: the bridge may have the file open for a moment while reloading.
                if attempt == 9:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _table(doc: TOMLDocument | Table, name: str, super_table: bool = False) -> Table:
    if name not in doc:
        t = tomlkit.table(is_super_table=super_table)
        doc.add(name, t)
    return doc[name]


# --- the changes the CLI offers ----------------------------------------------


def set_enabled(value: bool) -> Callable[[TOMLDocument], None]:
    def change(doc: TOMLDocument) -> None:
        doc["enabled"] = value  # tomlkit puts a new top-level key before the first table

    return change


def set_quiet(value: bool) -> Callable[[TOMLDocument], None]:
    def change(doc: TOMLDocument) -> None:
        _table(doc, "quiet")["enabled"] = value

    return change


def set_source(name: str, mode: str) -> Callable[[TOMLDocument], None]:
    if mode not in MODES:
        raise SettingsError(f"source mode must be one of {', '.join(MODES)}, got {mode!r}")
    if not name or any(c in name for c in " .[]\"'"):
        raise SettingsError(f"invalid source name {name!r}")

    def change(doc: TOMLDocument) -> None:
        sources = _table(doc, "sources", super_table=True)
        if name in sources:
            sources[name]["mode"] = mode
        else:
            t = tomlkit.table()
            t["mode"] = mode
            t.add(tomlkit.nl())
            sources.add(name, t)

    return change
