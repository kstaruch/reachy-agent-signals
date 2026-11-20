# Presence: awake only while you're at the desk

The robot wakes up when your phone comes within about 2 m of the bridge host and goes to sleep when it leaves. Why and what was rejected: [D11](decisions.md#d11--the-robot-sleeps-when-the-phone-isnt-at-the-desk). Bridge behaviour: [spec.md → Presence](spec.md#presence-m6).

Linux only (BlueZ). Developed on a Raspberry Pi 4 (BCM43455, Bluetooth 5.0) running Raspberry Pi OS 13 with an iPhone.

## How it works

```
phone ──BLE adverts──► presence-watch ──POST /events near|away──► bridge ──► daemon
                       (bluetoothctl scan,                        (wake_up / goto_sleep,
                        median + 2 thresholds)                     missed reaction)
```

1. `reachy-signals presence-watch` runs `bluetoothctl`, starts an LE scan with duplicate reporting on, and reads its output.
2. Every `RSSI` line for the configured phone is a reading. Any other change line for the phone (e.g. manufacturer data) counts as "heard" without a reading.
3. It keeps the readings of the last `presence.detector.window` seconds and takes their median (at least 3 readings).
4. **Near** when the median is ≥ `near_rssi`. **Away** when the median is ≤ `far_rssi`, or nothing has been heard for `lost_after`. Between the two thresholds the state doesn't change.
5. Each change is posted to the bridge as an event from source `presence`, and the current state is re-sent every 60 s so a restarted bridge learns it.

## Measurements

Raspberry Pi 4 on the desk, iPhone locked, 30 s per position, LE scan with duplicate data on. 2026-11-20.

| Position | Readings/s | Median (dBm) | p10 … p90 | Min … max |
|---|---|---|---|---|
| ~0.8 m, on the desk | ~1.6 | ≈ −54 | | −80 … −47 |
| 1 m, in hand | 1.3 | −56 | −70 … −46 | −80 … −43 |
| 2 m, in hand | 1.4 | −63 | −71 … −54 | −73 … −47 |
| 2 m, trouser pocket | 1.3 | −64 | −70 … −58 | −79 … −56 |
| 3 m, in hand | 1.2 | −71 | −77 … −67 | −84 … −64 |
| Another room | 0.8 | −84 | −87 … −82 | −87 … −81 |

What follows from it:

- About 7–8 dB per metre between 1 and 3 m: 2 m is distinguishable from 3 m on a median, never on one reading.
- A pocket facing the host costs ~1 dB at 2 m.
- A locked iPhone keeps advertising (~1.3/s near the desk). It's never silent from the next room, just much weaker.
- Defaults: `near_rssi = -65`, `far_rssi = -70`.

Not measured yet: back turned to the host with the phone in a pocket, phone in a bag, Low Power Mode, a phone locked for hours. Use `presence-watch --calibrate` to check your own setup.

## Setup on the bridge host

### 1. Turn Bluetooth on

Raspberry Pi OS ships with the radio rfkill-blocked (`bluetoothctl show` says `PowerState: off-blocked`):

```bash
sudo rfkill unblock bluetooth
bluetoothctl show | grep -E 'Powered|PowerState'   # Powered: yes, PowerState: on
```

`bluetoothctl power on` straight after the unblock may answer `org.bluez.Error.Busy`: BlueZ is already powering the adapter up by itself. Check again a second later.

### 2. Pair the phone (once)

Pairing is what lets the host recognise the phone through its rotating LE address. The host has no audio or keyboard profile, so **it doesn't appear in the iPhone's Bluetooth list**: start the pairing from the host instead.

1. On the phone, open **Settings → Bluetooth** and keep the screen on: the phone is discoverable while that screen is open.
2. On the host, in an interactive terminal:

   ```
   bluetoothctl
   agent KeyboardDisplay
   default-agent
   menu scan
   transport bredr
   back
   scan on
   ```

   Wait for `[NEW] Device XX:XX:XX:XX:XX:XX <phone name>` (10–20 s). `transport bredr` keeps the many anonymous LE devices out of the list.

3. Pair:

   ```
   scan off
   pair XX:XX:XX:XX:XX:XX
   ```

   The phone shows a pairing request with a 6-digit code. Check it matches, tap **Pair** on the phone and answer `yes` in `bluetoothctl`.

4. Trust it and check:

   ```
   trust XX:XX:XX:XX:XX:XX
   info XX:XX:XX:XX:XX:XX
   quit
   ```

   Want `Paired: yes`, `Bonded: yes`, `Trusted: yes`. `Connected: no` is normal: iOS drops a link with nothing on it.

5. **Check LE resolution works.** After a minute near the host, `bluetoothctl info XX:XX:XX:XX:XX:XX` should list `AdvertisingFlags` and `ManufacturerData.Key: 0x004c`. Those come only from the phone's LE broadcasts, so BlueZ is resolving its rotating address to the paired device. If they never appear, the pairing gave the host no LE identity key and presence can't work: remove the device (`remove XX:XX:XX:XX:XX:XX`) and pair again.

### 3. Configure

In the bridge host's settings file (never commit your phone's address):

```toml
[presence]
mode = "auto"

[presence.detector]
device = "XX:XX:XX:XX:XX:XX"    # from `bluetoothctl devices Paired`
```

Calibrate: stand where "near" should end, phone where it usually is.

```bash
reachy-signals presence-watch --calibrate     # live RSSI, median, and what it would decide
```

### 4. Run it as a service

`~/.config/systemd/user/reachy-presence.service`:

```ini
[Unit]
Description=reachy-signals presence detector
After=reachy-signals.service bluetooth.target
Wants=reachy-signals.service

[Service]
ExecStart=%h/signals-venv/bin/reachy-signals presence-watch
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now reachy-presence
journalctl _SYSTEMD_USER_UNIT=reachy-presence.service -f
reachy-signals status          # presence: auto, phone near, robot awake
```
