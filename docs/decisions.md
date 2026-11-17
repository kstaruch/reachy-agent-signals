# Decisions

Newest last. Each entry: what was decided, and why.

## D1 — Claude Code first, herdr second, then compare

Claude Code is the main agent, and its hooks are a stable, exact signal fired by the agent itself. herdr covers every agent but is young, its API may still shift, and its state detection is heuristic unless an agent integration is installed. The bridge makes the two non-exclusive: herdr is added as a second adapter in **shadow mode** (logged, not acted on), and after a week or two of real use the logs decide which source drives the robot — or both, with deduplication.

## D2 — Settings control behaviour, not plugin install state

The Claude Code plugin and the herdr plugin stay installed and always forward events. Enabling, disabling, quiet mode and reaction choice all live in one settings file owned by the bridge. Reason: toggling should be instant and reversible from any surface (CLI, slash command, herdr keybinding) without reinstalling anything.

## D3 — One bridge process owns the robot

Adapters never call the daemon directly. Several sessions finishing at once would otherwise fight over the motors, and the daemon may also be serving an app. The bridge serialises gestures, applies priority and checks the daemon's app lock.

## D4 — macOS and Windows

Python (the robot's own SDK is Python), localhost TCP instead of a Unix socket, settings path from `platformdirs`, hooks that call the CLI rather than shell scripts. The Reachy Mini daemon, SDK and desktop app officially support both platforms.

## D5 — Reactions come from the built-in move libraries

The daemon can play any move from Pollen Robotics' emotions library (~80 moves) and dances library (~20) with one call. v1 uses those by name instead of custom gestures. Picks, after watching 14 candidates on the robot:

| Event | Move | Why |
|---|---|---|
| `started` | `yeah_nod` | ~2 s and silent: fires on every prompt, so it must be short and unobtrusive |
| `done` | `cheerful1` | ~4.5 s, clearly positive |
| `blocked` | `attentive1` | ~6 s, reads as "look at me" |
| `blocked`, still waiting | `toc-toc-toc` | ~15 s: too long as a first alert, right as a reminder |

## D6 — Quiet mode has its own reaction list

Some emotion moves play their own sound, and the play endpoint has no mute option. Quiet mode therefore gets a separate per-event list rather than "same moves, muted". Until silent moves are picked, the quiet list repeats the normal one — so quiet mode is not yet quiet.

## D7 — A standalone tool, for now

This stays its own small tool rather than becoming a module of a larger assistant app running on the same robot. The daemon's app lock already keeps two controllers from fighting over the robot. Folding it into something bigger stays possible later: the bridge's event endpoint is the seam where another system would plug in.

## D8 — `blocked` means a prompt or a question, not idle

Claude Code's `Notification` hook fires for several types. Only `permission_prompt` and `elicitation_dialog` mean the agent can't continue. `idle_prompt` fires ~60 s after *every* finished response the user hasn't answered. Counting it would turn each `done` into `cheerful1` → `attentive1` a minute later → `toc-toc-toc` three minutes after that. The plugin filters with the hook `matcher`, so other types never reach the bridge.

## D9 — Minimal runtime dependencies

`emit` starts a fresh Python process on every hook, so import time is latency on every prompt. Runtime dependencies are `platformdirs` (settings path) and `tomlkit` (editing the settings file without losing comments). Reading TOML uses stdlib `tomllib`, and HTTP to the bridge and the daemon uses stdlib `urllib`. No web framework, no HTTP client library, no file-watcher library: hot reload polls the file's modification time.

## D10 — The bridge can run on another machine; the tailnet is the boundary

The robot moved to a dedicated always-on host next to the desk (a 64-bit Raspberry Pi), so the agents run on other machines. Reversing the v1 scope line "event sources on a different machine from the robot":

- **The bridge still binds `127.0.0.1` only.** It's published with `tailscale serve`, which proxies HTTPS from the tailnet to localhost and never reaches the public internet (that would be Funnel). Tailnet membership is the authentication, so there is no token to distribute or rotate. The JSON content-type rule still stops browser pages from posting without a CORS preflight.
- **Clients name the bridge with `bridge.url`.** Empty (the default) keeps everything local, exactly as before. Set, `emit`, `status` and `try` go there.
- **Settings stay in one file, on the bridge machine.** With `bridge.url` set, `on`, `off`, `quiet` and `source` send the change to `POST /settings`, and the bridge applies it to its own file with the same validated, atomic edit (`edit.update`). A client's own settings file holds only `bridge.url`; nothing else in it is used.
- **`emit` keeps its 0.5 s budget.** Over a direct tailnet path the round trip is a few milliseconds plus a TLS handshake. When the bridge machine is off, hooks give up in 0.5 s and exit 0, as with a stopped local bridge.

Rejected: binding the bridge to the tailnet address with a shared token (a secret on every client, and the bridge would need to know which interface to bind); an SSH tunnel per client (no code change, but `on`/`off`/`quiet` would silently edit the client's own file).
