# Reachy Mini daemon — verified facts

Checked against a live Reachy Mini Lite on macOS, daemon **1.14.0** run by the Reachy Mini Control desktop app. Full API: [`reference/reachy-daemon-openapi-1.14.0.json`](reference/reachy-daemon-openapi-1.14.0.json), captured from `http://localhost:8000/openapi.json`. Interactive docs at `http://localhost:8000/docs` while the daemon runs.

## Connection

- Lite: daemon runs on the computer the robot is plugged into, at `http://localhost:8000/api`.
- It listens on **127.0.0.1 only**. Anything on another machine has to go through the bridge.
- Only one controller at a time: while an app holds the robot, scripts can't drive it.

## Endpoints the bridge uses

| Endpoint | Use |
|---|---|
| `GET /api/daemon/status` | Health. `state` (`running`), `backend_status.motor_control_mode`, `backend_status.error`, control loop stats on real hardware (~20 ms period is healthy). `backend_status.ready` appeared on the real robot but not in `--mockup-sim`: don't rely on it |
| `GET /api/motors/status` | `{"mode": ...}`: `enabled`, `disabled` or `gravity_compensation`. Anything but `enabled` = can't play moves |
| `GET /api/daemon/robot-app-lock-status` | `{"state": "free", "holder_name": null}` when nothing holds the robot. Other states: `local_app`, `remote_session`, with `holder_name` set |
| `POST /api/move/play/recorded-move-dataset/{dataset}/{move}` | Play a library move; returns `{"uuid": ...}`. `dataset` is URL-encoded (`pollen-robotics%2Freachy-mini-emotions-library`). No body, no mute option. Unknown move → **404** `{"detail": "Move X not found in recorded moves library ..."}`. First use of a dataset may download it from Hugging Face |
| `GET /api/move/recorded-move-datasets/list/{dataset}` | List move names in a dataset |
| `GET /api/move/running` | `[{"uuid": ...}]`, empty when no move is playing. Poll to serialise |
| `POST /api/move/stop` | Stop **one** move: body `{"uuid": "<move uuid>"}` (required, 422 without it). A uuid that isn't running (e.g. already finished) → **500** |
| `POST /api/move/goto` | Direct pose. Body `GotoModelRequest`: `duration` (required), `antennas` `[left, right]` in radians, `body_yaw`, `head_pose` `{x, y, z, roll, pitch, yaw}`, `interpolation` (default `minjerk`). Returns a move `uuid` |
| `POST /api/move/play/wake_up`, `/api/move/play/goto_sleep` | Wake / sleep poses. Return a move `uuid` like any move, so wait on `/api/move/running` before the next gesture |
| `POST /api/motors/set_mode/{mode}` | Enable motors before moving (`mode` = `enabled`) |
| `POST /api/media/play_sound`, `/api/media/sounds/upload` | Sound on the robot's speaker — candidate for sound-only reactions and later voice |
| `POST /api/volume/set` | Speaker volume |

> [!WARNING]
> With motors disabled (robot asleep after an app stops, or just after the daemon starts) the daemon **accepts move commands, reports success and does nothing**. Check `motor_control_mode` before every gesture.

> [!WARNING]
> **The daemon doesn't queue moves.** A play request while another move runs is accepted and gets a uuid, but it is then **silently dropped**: it never appears in `/api/move/running` and never plays. Wait until `/api/move/running` is empty before every play. Verified on 1.14.0 mockup-sim, 2026-11-14 (`yeah_nod` then `simple_nod` straight after: only `yeah_nod` ran).

Readiness sequence that works (mockup-sim): `POST /api/motors/set_mode/enabled`, then `POST /api/move/play/wake_up`, then wait for its uuid to leave `/api/move/running` (~1 s in sim). The client is `src/reachy_signals/daemon.py`; its tests in `tests/test_daemon_sim.py` run against mockup-sim and skip unless the daemon reports a simulation mode.

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

## Running the daemon

Exactly one daemon can own the robot. The bridge only needs *a* daemon at `bridge.daemon_url`; it never starts or stops one.

> [!WARNING]
> Starting and stopping the real daemon **moves the robot**: by default it wakes the robot on start and puts it to sleep on stop (`--wake-up-on-start`, `--goto-sleep-on-stop`). Pass `--no-wake-up-on-start` / `--no-goto-sleep-on-stop` to avoid that.

### Real robot: desktop app (normal use)

1. Plug the robot into power and USB.
2. Open **Reachy Mini Control**. When the robot is detected the app starts its own daemon on `127.0.0.1:8000`. In the app it shows "Ready".
3. Quit the app to stop the daemon. The robot goes to sleep.

To verify on this machine: the exact button or step that starts the daemon, and that quitting the app stops it. On 2026-11-14 the app was running with no daemon listening.

The app ships its own daemon and Python environment. On macOS they are under `~/Library/Application Support/com.pollen-robotics.reachy-mini/apps_venv/` (the daemon is `bin/reachy-mini-daemon`, version 1.14.0, without MuJoCo).

### Real robot: command line

Quit the desktop app first. Then either:

```bash
# the app's bundled daemon (macOS)
~/Library/Application\ Support/com.pollen-robotics.reachy-mini/apps_venv/bin/reachy-mini-daemon

# or a standalone install (any OS)
uv tool install "reachy-mini==1.14.0"
reachy-mini-daemon
```

Stop it with **Ctrl+C**. It shuts down in ~3 s, putting the robot to sleep first.

### Simulation (no robot needed)

`--mockup-sim` simulates the robot without MuJoCo or a display, and never touches USB. Run it on port **8001** so it can never be confused with the real daemon on 8000:

```bash
uvx --python 3.12 --from "reachy-mini==1.14.0" reachy-mini-daemon --mockup-sim --headless --no-media --fastapi-port 8001
```

Stop it with **Ctrl+C**. Checked on 2026-11-14:
- it starts in a few seconds;
- motors report `enabled` and the lock reports `free`;
- the dataset list works;
- `yeah_nod` plays and `/api/move/running` empties after 1.9 s (real robot: 2.1 s).

That makes it good enough for the daemon-client and bridge tests. Point the bridge at it with `daemon_url = "http://localhost:8001/api"`.

The full physics simulation is `--sim`, which needs `reachy-mini[mujoco]`; on macOS it runs as `mjpython -m reachy_mini.daemon.app.main --sim` ([quickstart](https://huggingface.co/docs/reachy_mini/SDK/quickstart)). This project doesn't need it.

### Is it running?

```bash
curl http://localhost:8000/api/daemon/status     # JSON with "state": "running"
```

The interactive API docs are at `http://localhost:8000/docs`. "Connection refused" means no daemon is running. Note that the daemon takes a few seconds to start listening.

### Useful flags (`reachy-mini-daemon --help`, 1.14.0)

| Flag | Effect |
|---|---|
| `--mockup-sim` / `--sim` | Simulated robot without / with MuJoCo |
| `--fastapi-port N` | HTTP port (default 8000) |
| `--fastapi-host H` | Bind address (default 127.0.0.1 on Lite) |
| `--no-wake-up-on-start`, `--no-goto-sleep-on-stop` | Don't move the robot on start / stop |
| `--headless`, `--no-media` | No GUI; no camera, audio or WebRTC |
| `--preload-datasets` | Download the emotions and dances libraries at startup instead of on first play |
| `--log-level`, `--log-file` | Logging |

## Platform notes (Windows)

From the official troubleshooting guide:

- PowerShell needs `Set-ExecutionPolicy RemoteSigned` once to activate a virtual environment.
- App installs can fail on symlink permissions; set `HF_HUB_DISABLE_SYMLINKS_WARNING=1`.

## Sources

- [Reachy Mini documentation](https://huggingface.co/docs/reachy_mini/index)
- [Installation](https://huggingface.co/docs/reachy_mini/SDK/installation) (October 2026 certificate notice: uv ≥ 0.13.4, pip ≥ 26.2.2, certifi ≥ 2026.10.19)
- [Quickstart](https://huggingface.co/docs/reachy_mini/SDK/quickstart) (starting the daemon, simulation)
- [Troubleshooting & FAQ](https://huggingface.co/docs/reachy_mini/troubleshooting)
