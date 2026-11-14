# reachy-agent-signals

My Reachy Mini Lite physically signals when any of my coding-agent sessions starts a task, finishes it, or is blocked waiting for me, so I can look away from the terminal without missing anything.

> **Status:** specification stage. Nothing to install yet. The design is in [`docs/spec.md`](docs/spec.md).

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

- Reachy Mini Lite connected by USB, with its daemon running (Reachy Mini Control desktop app, or `reachy-mini-daemon`). Developed against daemon **1.14.0**.
- macOS or Windows.
- Claude Code (first event source). [herdr](https://herdr.dev) support is planned.

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
