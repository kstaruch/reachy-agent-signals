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
  "ts": "2026-11-14T10:00:00Z",
  "hook": {"hook_event_name": "Notification", "notification_type": "permission_prompt", "permission_mode": "default"}
}
```

`session` and `cwd` identify which session it is: needed for clearing state, escalation and, later, deduplication between sources.

- `event` is `started`, `done`, `blocked`, or `observe`. `observe` is logged and never reacts. The plugin sends it for extra hooks (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `SubagentStop`, `StopFailure`, every `Notification` type) while the open questions below are being settled. `detail` names the hook and tool or type, e.g. `PreToolUse:Bash`.
- `hook` holds selected fields of the hook payload for the event log. Never the prompt text, tool input or messages: a prompt is reduced to its length and leading slash command (e.g. `/loop`).

Bridge HTTP endpoints on `127.0.0.1:<bridge.port>`:
- `POST /events`: one event as JSON. Requires `Content-Type: application/json`, which keeps web pages from posting cross-origin without a preflight. Answers `202 {"decision": ...}`.
- `GET /health`: liveness, counts per decision, event log path, what's playing and pending, blocked sessions.
- `GET /status`: `/health` plus the settings in effect and robot state (daemon reachable, motor mode, app lock). Used by `reachy-signals status`.
- `POST /try`: `{"event": "started" | "done" | "blocked" | "escalation"}`. Plays that reaction and answers with the outcome when it's done. Used by `reachy-signals try`.

Event log: one JSON line per event received, with the bridge's `decision`. macOS `~/Library/Logs/reachy-signals/events.jsonl`, Windows `%LOCALAPPDATA%\reachy-signals\Logs\events.jsonl`. Rotated at 10 MB, keeping one backup.

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
7. **Track blocked sessions.** A session stays blocked until its next event. If still blocked after `escalation.blocked_after`, play the escalation reaction once. In quiet mode the escalation is skipped when `[quiet.reactions] blocked` is empty, so silencing `blocked` silences its reminder too.
   - **Which events clear it (provisional):** `started`, `done`, a new `blocked`, and the main agent's tool hooks (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`) or `StopFailure`. A tool can't run while the agent waits for the user, so a tool hook after a permission prompt means it was answered. These don't clear it: `PermissionRequest` and `Notification`, which arrive around the block itself, a subagent's tool hooks, and other sessions. Clearing also drops anything still queued for that session. To confirm against real blocked sessions (open question below).

Every outcome is written to the event log as a `"kind": "reaction"` record:
- `played`, `interrupted` (with the move and `seconds`);
- `no-reaction`, `locked` (with the holder), `daemon-unavailable`, `unknown-move`, `error`;
- `stale`, `superseded`, `collapsed`, `cleared`;
- or the drop/pass decision when settings changed while the event was queued.

At startup `serve` checks that every configured move exists and warns about unknown names.

After a gesture the robot is left awake, even if the bridge woke it (open question below).

**A `/reachy` command's own turn doesn't react.** A `started` whose prompt is `/reachy` or `/reachy-signals:reachy`, and the `done` that ends that session's turn, are logged with decision `control` and don't move the robot.

**`try`** plays the reaction a real event would get right now: quiet reactions in quiet mode, a random pick from the list, the robot left alone if another app holds it. It ignores the master switch and source modes, because it's an explicit request, and it doesn't start a blocked episode or supersede real events. It waits up to 30 s behind other gestures.

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
reachy-signals status [--json]       settings, robot state, blocked sessions (asks the bridge)
reachy-signals emit <event> ...      used by adapters; reads hook JSON from stdin
reachy-signals try <event>           play the configured reaction now (through the bridge);
                                     started | done | blocked | escalation
reachy-signals paths                 settings file and event log locations
```

`on`, `off`, `quiet on|off` and `source` edit the settings file with `tomlkit`, keeping comments and layout. A change is validated with the bridge's own parser before it's saved, and saved atomically, so hot reload never sees a broken file. An already invalid file is never edited: fix it by hand first. If the file doesn't exist, it's created with just the changed key. `status` and `try` exit 1 when the bridge isn't running.

## Claude Code plugin

- Hooks for `UserPromptSubmit`, `Stop`, `Notification` (matcher `permission_prompt|elicitation_dialog`), each calling `reachy-signals emit` directly with `"async": true`. No bash or `curl`, so the same hook works in zsh and PowerShell. On Windows, shell-form hooks run in Git Bash, or in PowerShell when Git Bash isn't installed. The uv-installed launcher is a real `.exe`, so it runs in either.
- The plugin is laid out as `.claude-plugin/plugin.json`, `hooks/hooks.json` and `skills/reachy/SKILL.md` ([plugin components](https://code.claude.com/docs/en/plugins/components.md)).
- Slash command: the skill `skills/reachy/SKILL.md`, full name `/reachy-signals:reachy`, with arguments `on`, `off`, `quiet [on|off]`, `status`, passed through to the CLI. Because its frontmatter sets `name: reachy`, the bare `/reachy` works too, unless another command already uses that name ([skills](https://code.claude.com/docs/en/skills.md), verified 2026-11-14). `disable-model-invocation: true`: Claude never toggles the robot by itself. `allowed-tools: Bash(reachy-signals *)` runs the command without a permission prompt.
- Claude Code installs a plugin from the marketplace repo's last commit into a cache. A plugin change takes effect after it's committed, the version in `plugin.json` is bumped, and the plugin is updated (`claude plugin update`).
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

- What clears `blocked` most reliably: the next `UserPromptSubmit`, the next tool run (`PreToolUse` / `PostToolUse`), or `Stop`. Implemented provisionally as "any of these" (Bridge behaviour, step 7); confirm from real blocked sessions.
- What happens when the user presses Esc at a permission prompt. `Stop` doesn't fire, so the session may stay blocked and escalate for nothing.
- How often `blocked` fires at all in `auto` permission mode, which the main user runs in. Permission prompts are rare there, so `elicitation_dialog` may be the main source.

### Observed so far

From a real session on 2026-11-14 (Claude Code 2.1.325, macOS, `permission_mode: auto`):

- **Payload fields match the docs** for `UserPromptSubmit` (`session_id`, `cwd`, `hook_event_name`, `permission_mode`, `prompt`), `Stop` (`stop_hook_active`) and `PreToolUse` / `PostToolUse` (`tool_name`). `Notification` and `PermissionRequest` have not been observed yet, so the `blocked` path is still unverified against real payloads.
- **Esc mid-task sends nothing.** An interrupted turn shows as `started` followed by the next prompt's `started`, with no `done` and no other hook in between. So an interrupt is invisible until the next prompt. That's harmless for `started`/`done`; for `blocked` it's the open question above.
- **Latency:** an event reaches the bridge ~40 ms after the hook fires.
- Whether `started` on every prompt is too much even with a 2-second nod; possibly debounce per session, and ignore `UserPromptSubmit` that isn't a human prompt (e.g. `/loop` iterations). `/reachy` itself is already ignored.
- Whether to put the robot back to sleep after a gesture if the bridge woke it.
