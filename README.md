# reachy-agent-signals

My Reachy Mini Lite physically signals when any of my coding-agent sessions starts a task, finishes it, or is blocked waiting for me, so I can look away from the terminal without missing anything.

> **Status:** in development. Milestone 5: Claude Code events make the robot react, and the control commands and `/reachy` work. Windows verification comes next. The design is in [`docs/spec.md`](docs/spec.md); progress is tracked as one [GitHub issue per milestone](https://github.com/kstaruch/reachy-agent-signals/issues).

## How it works

```
Claude Code hooks ──┐
                    ├─► bridge (queue · priority · settings) ─► Reachy Mini daemon (REST, localhost:8000)
herdr (later)    ───┘
```

- **Event sources** are thin adapters. They report three events (`started`, `done`, `blocked`) to a local bridge and never talk to the robot themselves.
- **The bridge** owns the robot. It reads a settings file, picks a reaction for the event, and plays it, one gesture at a time, with `blocked` ahead of everything else.
- **Reactions** are named moves from Pollen Robotics' built-in emotions and dances libraries, so no custom choreography is needed.

| Event | Default reaction |
|---|---|
| `started` | `yeah_nod` |
| `done` | `cheerful1` |
| `blocked` | `attentive1`, then `toc-toc-toc` if still blocked after 3 minutes |

Everything is controlled from settings rather than by uninstalling anything: enable or disable per source, quiet mode, reactions per event.

## Requirements

| What | Why | Install |
|---|---|---|
| macOS or Windows | | |
| Reachy Mini Lite, plugged into power and USB | the robot | |
| Reachy Mini daemon **1.14.0** | the robot's REST API, which the bridge drives | [Reachy Mini Control](https://hf.co/reachy-mini/#/download) desktop app (bundles the daemon), or `uv tool install "reachy-mini==1.14.0"` |
| [uv](https://docs.astral.sh/uv/) **≥ 0.13.4** | installs and runs this tool. Older versions trust revoked certificate authorities ([Pollen notice](https://huggingface.co/docs/reachy_mini/SDK/installation)) | macOS: `brew install uv`. Windows: `winget install astral-sh.uv`, or `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Python **3.12** | runtime | `uv python install 3.12` (uv manages it) |
| [Claude Code](https://code.claude.com) | first event source | see its docs |

[herdr](https://herdr.dev) support is planned.

How to start, stop and check the daemon, real or simulated: [`docs/reachy-daemon.md` → Running the daemon](docs/reachy-daemon.md#running-the-daemon). In short: open Reachy Mini Control with the robot plugged in, and quit it to stop. Or run `reachy-mini-daemon` and stop it with Ctrl+C. Both wake the robot on start and put it to sleep on stop.

## Try it

```bash
# 1. Install the CLI from a checkout. --editable: code changes apply without reinstalling
uv tool install --editable .
reachy-signals paths            # where the settings file and event log live

# 2. Run the bridge in its own terminal (Ctrl+C to stop)
reachy-signals serve

# 3. Install the Claude Code plugin from this repo
claude plugin marketplace add /path/to/reachy-agent-signals
claude plugin install reachy-signals@reachy-agent-signals
```

Start a new Claude Code session from a terminal where `reachy-signals` is on `PATH`; hooks use that terminal's `PATH`. Each prompt, finish and permission prompt then makes the robot react, and shows up in the `serve` output and in the event log. The robot only moves when the daemon is running and no app holds it. To try it without moving the robot, point `daemon_url` in the settings file at the simulated daemon (`http://127.0.0.1:8001/api`, see Development).

### Control

```bash
reachy-signals status              # bridge, settings in effect, robot, blocked sessions
reachy-signals off                 # robot stays still; events are still logged
reachy-signals on
reachy-signals quiet on            # use [quiet.reactions]; `quiet` alone shows the state
reachy-signals source herdr shadow # drive | shadow (log only) | off
reachy-signals try done            # play the done reaction now (started, done, blocked, escalation)
```

In Claude Code: `/reachy on`, `/reachy off`, `/reachy quiet on`, `/reachy status` (full name `/reachy-signals:reachy`). A `/reachy` turn doesn't make the robot react.

Commands that change settings edit the settings file, keeping your comments, and the running bridge applies the change within about a second. `try` works while reactions are off, uses the quiet reactions in quiet mode, and doesn't move a robot another app holds.

After pulling plugin changes, update the installed copy: `claude plugin marketplace update reachy-agent-signals`, then `claude plugin update reachy-signals@reachy-agent-signals`, and start a new session. Claude Code installs the plugin from the repo's last commit, not the working tree.

- While working on the plugin itself: `claude --plugin-dir ./plugin`, then `/reload-plugins` after edits. A marketplace install runs from a cached copy of the last commit.
- `/hooks` in Claude Code lists the plugin's hooks. `claude --debug` shows each hook run with its exit status.
- `REACHY_SIGNALS_DEBUG=1` makes `emit` print why it failed. Normally it is silent and always exits 0.
- `REACHY_SIGNALS_CONFIG` and `REACHY_SIGNALS_LOG_DIR` override the settings file and log directory, e.g. for a test bridge.

## Development

Extra tools: `git`, and [`gh`](https://cli.github.com) for issues. Git LFS isn't needed for this project, only for Pollen's own apps and model assets.

```bash
uv sync                      # create .venv with runtime + dev dependencies
uv run pytest                # tests
uv run ruff check            # lint
uv run reachy-signals ...    # run the CLI from the checkout
```

Runtime dependencies are deliberately few: `platformdirs` and `tomlkit` ([D9](docs/decisions.md#d9--minimal-runtime-dependencies)).

Simulated robot for development, without hardware, on port 8001 (stop with Ctrl+C):

```bash
uvx --python 3.12 --from "reachy-mini==1.14.0" reachy-mini-daemon --mockup-sim --headless --no-media --fastapi-port 8001
```

With it running, `uv run pytest` also runs the daemon tests in `tests/test_daemon_sim.py` (`-m sim` for only those). Without it they're skipped. They refuse to run against a daemon that isn't a simulation.

## Documentation

| File | Contents |
|---|---|
| [`docs/spec.md`](docs/spec.md) | v1 scope, architecture, events, settings, CLI, Claude Code plugin |
| [`docs/decisions.md`](docs/decisions.md) | Design decisions and why |
| [`docs/reachy-daemon.md`](docs/reachy-daemon.md) | Verified daemon API facts, the move libraries, measured reaction lengths |
| [`docs/herdr.md`](docs/herdr.md) | herdr plugin and socket API notes, for the second event source |
| [`config.example.toml`](config.example.toml) | Settings file sketch |
| [`docs/reference/`](docs/reference/) | Daemon OpenAPI spec, captured from a live daemon |

## License

MIT — see [LICENSE](LICENSE).
