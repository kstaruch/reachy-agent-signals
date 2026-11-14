# Specification — v1

## Goal

A coding-agent session reaching one of three moments makes the robot react, so the person at the desk notices without watching the terminal:

| Event | Meaning |
|---|---|
| `started` | The agent began working on a task |
| `done` | The agent finished and is waiting for the next prompt |
| `blocked` | The agent cannot continue without the user (permission prompt, question, idle prompt) |

## Scope

**In v1**

- Bridge process that owns the robot.
- Settings file, reloaded on change.
- `reachy-signals` CLI.
- Claude Code plugin (hooks and a `/reachy` slash command).
- macOS first, then verified on Windows.

**Not in v1** (designed for, not built)

- herdr event source. Planned next, starting in shadow mode. See [herdr.md](herdr.md).
- Event sources on a different machine from the robot.
- Automatic quiet mode from OS focus / do-not-disturb state.
- Voice in either direction.

## Architecture

```
┌──────────────┐  reachy-signals emit   ┌───────────────────────────┐   HTTP    ┌────────────────┐
│ Claude Code  │ ─────────────────────► │ bridge                    │ ────────► │ Reachy Mini    │
│ hooks        │                        │  · settings (hot reload)  │           │ daemon         │
└──────────────┘                        │  · per-source mode        │           │ 127.0.0.1:8000 │
┌──────────────┐  (later)               │  · queue + priority       │           └────────────────┘
│ herdr        │ ─────────────────────► │  · escalation timers      │
└──────────────┘                        │  · event log              │
                                        └───────────────────────────┘
```

- **One process drives the robot.** Adapters never call the daemon. The daemon also serves other apps, and two controllers fight over the motors.
- **Bridge listens on localhost TCP.** Not a Unix socket: Windows support. Port is a setting.
- **Adapters fail fast and silently.** If the bridge is down, `emit` gives up after a short timeout and exits 0. A robot problem must never hold up an agent session.

## Events in, reactions out

### Event shape

```json
{
  "source": "claude-code",
  "event": "blocked",
  "session": "<session id>",
  "cwd": "/path/to/project",
  "detail": "permission_prompt",
  "ts": "2026-11-14T10:00:00Z"
}
```

`session` and `cwd` identify which session it is: needed for clearing state, escalation and, later, deduplication between sources.

### Claude Code mapping

| Event | Hook | Notes |
|---|---|---|
| `started` | `UserPromptSubmit` | Fires for every prompt. Reaction should be short |
| `done` | `Stop` | Main agent only; `SubagentStop` ignored |
| `blocked` | `Notification` | Permission prompt and idle prompt both count |

Hook commands receive JSON on stdin (`session_id`, `cwd`, `hook_event_name`, plus event-specific fields). Verify field names and `Notification` subtypes against the current Claude Code hooks documentation while implementing.

### Bridge behaviour

1. **Drop or pass.** Master switch off, source `off` or `shadow` → log the event and stop. `shadow` exists to compare a new source against a trusted one without moving the robot.
2. **Choose the reaction.** `[quiet.reactions]` if quiet mode is on, else `[reactions]`. Several names → random pick. Empty list → no reaction.
3. **Check the robot is ours.** `GET /api/daemon/robot-app-lock-status`; anything other than `free` means an app owns the robot → skip (log it).
4. **Make sure it can move.** If motors are disabled, enable and wake up first. The daemon accepts moves silently while motors are off, so this check is not optional.
5. **Serialise.** One gesture at a time. Priority `blocked` > `done` > `started`. A `blocked` arriving during a lower-priority gesture interrupts it (`POST /api/move/stop`). Several `started` events in a burst collapse into one.
6. **Track blocked sessions.** A session stays blocked until its next event. If still blocked after `escalation.blocked_after`, play the escalation reaction once.

## Settings

TOML, one file, location from `platformdirs`:

- macOS: `~/Library/Application Support/reachy-signals/config.toml`
- Windows: `%APPDATA%\reachy-signals\config.toml`

Full sketch: [`config.example.toml`](../config.example.toml). Sections: master `enabled`, `[bridge]`, `[sources.*]` with `mode = drive | shadow | off`, `[quiet]`, `[reactions]`, `[quiet.reactions]`, `[escalation]`.

The bridge reloads the file on change. All control surfaces edit the file through the CLI, so there is one source of truth.

## CLI

```
reachy-signals serve                 run the bridge
reachy-signals on | off              master switch
reachy-signals quiet [on|off]        quiet mode
reachy-signals source <name> <mode>  drive | shadow | off
reachy-signals status                settings, robot state, blocked sessions
reachy-signals emit <event> ...      used by adapters; reads hook JSON from stdin
reachy-signals try <event>           play the configured reaction now
```

## Claude Code plugin

- Hooks for `UserPromptSubmit`, `Stop`, `Notification`, each calling `reachy-signals emit` directly. No bash or `curl`, so the same hook works in zsh and PowerShell.
- `/reachy` slash command: `on`, `off`, `quiet`, `status`, passed through to the CLI.
- Installing the plugin never needs to be undone to silence the robot. That is what settings are for.

## Platforms

| Concern | Approach |
|---|---|
| Language | Python 3.12, one package, `uv tool install` |
| Settings path | `platformdirs` |
| Bridge transport | localhost TCP |
| Hook commands | the CLI, never a shell script |
| OS-specific features (focus state, later audio) | small per-platform interface with a no-op fallback |

## To settle during implementation

- What clears `blocked` most reliably: the next `UserPromptSubmit`, the next tool run, or `Stop`. Watch real sessions.
- Whether `started` on every prompt is too much even with a 2-second nod; possibly debounce per session.
- Whether to put the robot back to sleep after a gesture if the bridge woke it.
