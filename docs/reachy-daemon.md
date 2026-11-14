# Reachy Mini daemon — verified facts

Checked against a live Reachy Mini Lite on macOS, daemon **1.14.0** run by the Reachy Mini Control desktop app. Full API: [`reference/reachy-daemon-openapi-1.14.0.json`](reference/reachy-daemon-openapi-1.14.0.json), captured from `http://localhost:8000/openapi.json`. Interactive docs at `http://localhost:8000/docs` while the daemon runs.

## Connection

- Lite: daemon runs on the computer the robot is plugged into, at `http://localhost:8000/api`.
- It listens on **127.0.0.1 only**. Anything on another machine has to go through the bridge.
- Only one controller at a time: while an app holds the robot, scripts can't drive it.

## Endpoints the bridge uses

| Endpoint | Use |
|---|---|
| `GET /api/daemon/status` | Health. `backend_status.ready`, `backend_status.motor_control_mode` (`enabled` / disabled), control loop stats (~20 ms period is healthy) |
| `GET /api/daemon/robot-app-lock-status` | `{"state": "free", "holder_name": null}` when nothing holds the robot; otherwise the app's name |
| `POST /api/move/play/recorded-move-dataset/{dataset}/{move}` | Play a library move. `dataset` is URL-encoded (`pollen-robotics%2Freachy-mini-emotions-library`). No body, no mute option |
| `GET /api/move/recorded-move-datasets/list/{dataset}` | List move names in a dataset |
| `GET /api/move/running` | Empty when no move is playing — poll to serialise |
| `POST /api/move/stop` | Interrupt the current move |
| `POST /api/move/goto` | Direct pose. Body `GotoModelRequest`: `duration` (required), `antennas` `[left, right]` in radians, `body_yaw`, `head_pose` `{x, y, z, roll, pitch, yaw}`, `interpolation` (default `minjerk`). Returns a move `uuid` |
| `POST /api/move/play/wake_up`, `/api/move/play/goto_sleep` | Wake / sleep poses |
| `POST /api/motors/set_mode/{mode}` | Enable motors before moving |
| `POST /api/media/play_sound`, `/api/media/sounds/upload` | Sound on the robot's speaker — candidate for sound-only reactions and later voice |
| `POST /api/volume/set` | Speaker volume |

> [!WARNING]
> With motors disabled (robot asleep after an app stops, or just after the daemon starts) the daemon **accepts move commands, reports success and does nothing**. Check `motor_control_mode` before every gesture.

## Move libraries

- `pollen-robotics/reachy-mini-emotions-library`: ~80 moves, e.g. `success1`, `success2`, `cheerful1`, `proud1`, `enthusiastic1`, `attentive1`, `inquiring1`, `impatient1`, `waiting`, `toc-toc-toc`, `oops1`, `understanding1`. Some include sound.
- `pollen-robotics/reachy-mini-dances-library`: ~20 moves, e.g. `simple_nod`, `yeah_nod`, `uh_huh_tilt`, `side_glance_flick`. The ones tried so far were silent.

## Measured lengths

Wall-clock from play request until `/api/move/running` was empty.

| Move | Library | Length | Sound |
|---|---|---|---|
| `success1` | emotions | 4.4 s | |
| `success2` | emotions | 4.1 s | |
| `cheerful1` | emotions | 4.1–4.7 s | yes |
| `proud1` | emotions | 5.3 s | |
| `enthusiastic1` | emotions | 4.6 s | |
| `attentive1` | emotions | 5.5–6.3 s | yes |
| `toc-toc-toc` | emotions | 14.9 s | |
| `inquiring1` | emotions | 4.3 s | |
| `impatient1` | emotions | 5.5 s | |
| `waiting` | emotions | 10.9 s | |
| `understanding1` | emotions | 5.7 s | |
| `simple_nod` | dances | 2.1 s | |
| `yeah_nod` | dances | 2.1–2.3 s | no |
| `uh_huh_tilt` | dances | 2.0 s | |

Blank sound column = not checked yet.

## Platform notes (Windows)

From the official troubleshooting guide:

- PowerShell needs `Set-ExecutionPolicy RemoteSigned` once to activate a virtual environment.
- App installs can fail on symlink permissions; set `HF_HUB_DISABLE_SYMLINKS_WARNING=1`.

## Sources

- [Reachy Mini documentation](https://huggingface.co/docs/reachy_mini/index)
- [Installation](https://huggingface.co/docs/reachy_mini/SDK/installation)
- [Troubleshooting & FAQ](https://huggingface.co/docs/reachy_mini/troubleshooting)
