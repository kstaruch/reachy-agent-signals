# Specification — v1

## Goal

A coding-agent session reaching one of three moments makes the robot react, so the person at the desk notices without watching the terminal:

| Event | Meaning |
|---|---|
| `started` | The agent began working on a task |
| `done` | The agent finished and is waiting for the next prompt |
| `blocked` | The agent cannot continue without the user (permission prompt, question) |

## Scope

**In v1**

- Bridge process that owns the robot.
- Settings file, reloaded on change. Quiet mode by manual toggle and by time schedule.
- `reachy-signals` CLI.
- Claude Code plugin (hooks and a `/reachy` slash command).
- macOS first, then verified on Windows.

**Not in v1** (designed for, not built)

- Starting the bridge automatically at login (launchd / Task Scheduler). In v1 you run `reachy-signals serve` yourself.
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
| `started` | `UserPromptSubmit` | Fires for every prompt, including `/loop` iterations, background subagent reports and cross-session messages. Reaction should be short |
| `done` | `Stop` | Main agent only; `SubagentStop` ignored. Does **not** fire when the user interrupts (Esc) |
| `blocked` | `Notification` with `notification_type` `permission_prompt` or `elicitation_dialog` | Both fire after ~6 s without a response, so a user already looking at the terminal isn't pinged. `idle_prompt` (~60 s after a response with no typing) is **not** `blocked`: it follows every `done` ([D8](decisions.md#d8--blocked-means-a-prompt-or-a-question-not-idle)) |

Verified against the [hooks reference](https://code.claude.com/docs/en/hooks.md) on 2026-11-14:

- Every hook gets JSON on stdin with `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name` (plus `prompt_id`, `effort`, …). `UserPromptSubmit` adds `prompt`; `Stop` adds `stop_hook_active`, `last_assistant_message`; `Notification` adds `message`, `title`, `notification_type`.
- The `Notification` hook `matcher` filters on `notification_type`, so the plugin registers only the types it wants.
- A `Stop` hook exiting 2 **keeps Claude working**, and `UserPromptSubmit` exiting 2 blocks the prompt. `emit` must always exit 0.
- Hooks are registered with `"async": true`: they run in the background and never delay the session, whatever `emit` does.
- No hook fires when a permission is granted. The closest "agent resumed" signals are `PreToolUse` / `PostToolUse`.

### Bridge behaviour

1. **Drop or pass.** Master switch off, source `off` or `shadow` → log the event and stop. `shadow` exists to compare a new source against a trusted one without moving the robot.
2. **Choose the reaction.** `[quiet.reactions]` if quiet mode is on (manual toggle, or the current time is inside a `quiet.schedule` range), else `[reactions]`. Several names → random pick. Empty list → no reaction.
3. **Check the robot is ours.** `GET /api/daemon/robot-app-lock-status`; anything other than `free` (`local_app`, `remote_session`) means something else owns the robot → skip (log it).
4. **Make sure it can move.** If the motor mode is anything other than `enabled` (`disabled`, `gravity_compensation`), enable and wake up first. The daemon accepts moves silently while motors are off, so this check is not optional.
5. **Serialise.** One gesture at a time. Priority `blocked` > `done` > `started`. A `blocked` arriving during a lower-priority gesture interrupts it (`POST /api/move/stop`). Several `started` events in a burst collapse into one.
6. **Drop stale events.** A reaction that comes late is misleading. An event older than `bridge.max_event_age` when its turn comes is logged and dropped, never replayed. That covers the daemon being down, the robot being locked, or a long queue. A queued event for a session that has since sent a newer event is dropped too.
7. **Track blocked sessions.** A session stays blocked until its next event. If still blocked after `escalation.blocked_after`, play the escalation reaction once.

The bridge is also the only path to the robot for the CLI: `try` asks the bridge to play a reaction, and `status` asks the bridge for robot state. Neither calls the daemon itself.

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
reachy-signals status                settings, robot state, blocked sessions (asks the bridge)
reachy-signals emit <event> ...      used by adapters; reads hook JSON from stdin
reachy-signals try <event>           play the configured reaction now (through the bridge)
```

## Claude Code plugin

- Hooks for `UserPromptSubmit`, `Stop`, `Notification` (matcher `permission_prompt|elicitation_dialog`), each calling `reachy-signals emit` directly with `"async": true`. No bash or `curl`, so the same hook works in zsh and PowerShell. On Windows, shell-form hooks run in Git Bash, or in PowerShell when Git Bash isn't installed. The uv-installed launcher is a real `.exe`, so it runs in either.
- The plugin is laid out as `.claude-plugin/plugin.json`, `hooks/hooks.json` and `skills/reachy/SKILL.md` ([plugin components](https://code.claude.com/docs/en/plugins/components.md)).
- Slash command: a skill, so its full name is `/reachy-signals:reachy` with arguments `on`, `off`, `quiet`, `status`, passed through to the CLI. To verify: whether the short `/reachy` also works.
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

Answer these by watching real sessions (the event log from milestone 2 onwards), not by guessing.

- What clears `blocked` most reliably: the next `UserPromptSubmit`, the next tool run (`PreToolUse` / `PostToolUse`, which would need an internal clear-only event with no reaction), or `Stop`.
- What happens when the user presses Esc at a permission prompt. `Stop` doesn't fire, so the session may stay blocked and escalate for nothing.
- Whether `started` on every prompt is too much even with a 2-second nod; possibly debounce per session, and ignore `UserPromptSubmit` that isn't a human prompt (e.g. `/reachy` itself, `/loop` iterations).
- Whether to put the robot back to sleep after a gesture if the bridge woke it.
