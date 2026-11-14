# AGENTS.md

Instructions for coding agents working in this repository.

## Read first

1. [`docs/spec.md`](docs/spec.md) — what v1 is and isn't.
2. [`docs/decisions.md`](docs/decisions.md) — don't re-open these without saying so.
3. [`docs/reachy-daemon.md`](docs/reachy-daemon.md) — verified daemon behaviour. Trust it over memory; the Reachy Mini SDK moves fast.

## Rules

- **Only the bridge talks to the robot.** Adapters (Claude Code hooks, herdr) call `reachy-signals emit` and nothing else.
- **Never block an agent session.** Hook commands time out fast and always exit 0.
- **Cross-platform from the first commit.** macOS and Windows: no shell scripts in hooks, no Unix-only sockets, paths via `platformdirs`.
- **Behaviour lives in settings.** New toggles go into the settings file and the CLI, not into install/uninstall steps.
- **Testing on the real robot moves hardware.** Keep test gestures short, restore the starting pose, and check `/api/daemon/robot-app-lock-status` is `free` first. Use the daemon's simulation mode (`--sim`) when the robot isn't needed.
- When the daemon API is unclear, read [`docs/reference/`](docs/reference/) or the live `/openapi.json`, and update `docs/reachy-daemon.md` with what you verified.
