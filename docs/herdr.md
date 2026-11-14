# herdr — second event source

Not in v1. Research notes for when the herdr adapter is built. Sources: [plugin docs](https://herdr.dev/docs/plugins/), [socket API](https://herdr.dev/docs/socket-api/), [repository](https://github.com/ogulcancelik/herdr). Verify against the installed version first: `herdr api schema --json`.

## Agent status

- Pane agent statuses: `idle`, `working`, `blocked`, `done` (idle and not yet seen), `unknown`.
- Mapping: `working` → `started`, `done` → `done`, `blocked` → `blocked`.
- Detection is heuristic (process name + terminal output) unless an agent integration is installed (`herdr integration install claude`).

## Socket API

- Default socket `~/.config/herdr/herdr.sock`; named sessions under `~/.config/herdr/sessions/<name>/herdr.sock`. Named pipe on Windows. `HERDR_SOCKET_PATH` overrides.
- Newline-delimited JSON, one request per line, responses echo `id`.
- Subscribe and keep the connection open:

  ```json
  {"id":"sub_1","method":"events.subscribe","params":{"subscriptions":[{"type":"pane.agent_status_changed"}]}}
  ```

- A lagging subscriber gets `events_lost` and is dropped: reconnect, then reconcile with `session.snapshot`.
- Payload fields of `pane.agent_status_changed` are not documented on the page — read them from the schema.

## Plugin

- Manifest `herdr-plugin.toml`. Required: `id`, `name`, `version`, `min_herdr_version`. Optional `platforms = ["macos", "windows"]`.
- Sections: `[[build]]`, `[[startup]]`, `[[actions]]`, `[[events]]`, `[[panes]]`, `[[link_handlers]]`, `[[keys.command]]`.
- Commands are argv arrays, no shell; working directory is the plugin directory. Event hooks get `HERDR_PLUGIN_EVENT` / `HERDR_PLUGIN_EVENT_JSON`, plus `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH` and others.
- Unconfirmed: whether status changes are available as `[[events]]` hooks — the docs only show `worktree.created`. Safe route: a `[[startup]]` command that runs a long-lived socket subscriber which calls `reachy-signals emit`.
- `[[actions]]` / `[[keys.command]]` give a herdr-side toggle that calls the CLI (`reachy-signals quiet`, etc.).

## Running both sources

A Claude Code session inside herdr produces each event twice. While herdr is in `shadow` mode that's fine — it's the comparison data. Before both drive the robot, the bridge needs one source per session (key on `cwd` + short time window), or the herdr adapter ignores panes where Claude Code is detected.
