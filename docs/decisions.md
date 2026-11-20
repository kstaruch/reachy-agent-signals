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

## D11 — The robot sleeps when the phone isn't at the desk

The robot is awake only while its owner is within about 2 m: the bridge host scans for the owner's paired phone over Bluetooth LE and judges distance from its signal strength (RSSI). Away → the robot goes to its sleep pose with motors off. Back → it wakes up. Measured first, before deciding ([presence.md](presence.md)): medians fall from −56 dBm at 1 m to −63 at 2 m and −71 at 3 m, a phone in a trouser pocket at 2 m loses only about 1 dB, and another room reads −84. Single readings swing ±12 dB, so only a smoothed value is used.

- **Detection is its own process, `reachy-signals presence-watch`, on the bridge host.** It posts `near` / `away` to the bridge like any other adapter. The bridge stays platform-neutral (D4), the robot still has one owner (D3), and the detector is Linux-only without making the bridge so.
- **No new dependency (D9).** The detector reads `bluetoothctl`'s output instead of talking D-Bus. Its output streams line by line through a pipe (checked on the Pi: readings arrive within 0.2 s).
- **Pair the phone once.** iPhones broadcast from a rotating LE address; pairing gives the host the key to resolve it, and BlueZ then reports every broadcast under the phone's fixed address. No key handling in this code.
- **Two thresholds on a median.** Near when the median of the last few seconds is ≥ `near_rssi`, away when ≤ `far_rssi` or nothing is heard for `lost_after`. The gap between them stops flicker at the boundary.
- **Asleep, events don't queue.** Each reaction event is logged with decision `asleep` and never played later (a late reaction is misleading — same reason as `max_event_age`). If any of them is in `presence.missed_events`, one `missed` reaction plays after the wake-up. Blocked sessions don't escalate while asleep.
- **Leaving is slow, arriving is fast.** Waking happens on the first `near`; sleeping only after `presence.sleep_after` of `away`, so a trip to the kitchen doesn't put the robot to sleep.
- **`presence.mode` is a setting (D2):** `off` (default, as before), `auto`, `awake`, `asleep`.
- **The `missed` reaction is `curious1`.** Picked by the owner after the first walk-away test, which played the placeholder `inquiring1`.

Rejected: Wi-Fi presence (an iPhone's Wi-Fi sleeps for minutes and says nothing about the desk); iOS Shortcuts automations (home-level, not desk-level); Tailscale online status (says the phone has internet, not where it is); queueing events while asleep and replaying them on wake.
