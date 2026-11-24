# bambuddy-dragonbreath

A small sidecar that lets [Bambuddy](https://github.com/maziggy/bambuddy) drive a
BIGTREETECH Panda Breath chamber heater running
[DragonBreath](https://github.com/plastikman/DragonBreath) firmware.

When a print starts, the sidecar works out which filaments the print actually
uses, looks up a chamber temperature, and turns the heater on. When the print
ends (completed, failed or cancelled), it turns the heater off.

## Why

DragonBreath's built-in "Bambu LAN" AUTO mode reads the printer's MQTT reports
itself, and it often gets the filament wrong. It finds the loaded tray by
counting `tray_type` strings instead of matching tray ids, and it only follows
the tray loaded right now. That tray is often empty during PREPARE.

Bambuddy already knows the right answer. The sidecar uses Bambuddy's view:

1. **The print's `ams_mapping`** (which AMS slots or external spool the job
   uses), read against the printer's live AMS state. The sidecar takes the
   highest target across those trays, so PETG with PLA support heats for PETG.
   An ASA spool that is loaded but unused has no effect.
2. **Slicer metadata** (the archive's `filament_type`), used when the mapping
   isn't available, e.g. after a sidecar restart mid-print.
3. If neither is available within about 3 minutes, it **doesn't heat** and logs a warning.

Temperatures come from Bambuddy's own preheat table: **Settings → Workflow →
Preheat** (`preheat_filament_targets`). Edit them there. The defaults are
PLA/PETG/TPU/PVA 0 (no heat), PETG-CF 40, ABS/ASA 45, PA/PC 50, PA-CF 55 °C.
Filled variants use their base material's value (ASA-GF → ASA), and nylon
spellings map to PA. Sidecar `overrides:` win over Bambuddy's values.
DragonBreath accepts 30–70 °C, so non-zero targets below 30 are raised to 30.

This uses the same lookup as Bambuddy's chamber preheat. That preheat only
acts on printers with a built-in chamber heater (H2D/H2S/…), not the X1C, so the
two don't conflict.

## How it controls the heater

The sidecar uses DragonBreath's API v2:

- `power_on` with a target. This creates a lease.
- A heartbeat every `comms_ms/3`, about 100 s by default.
- `off` at the end.

If the sidecar dies without sending `off`, the lease expires within `comms_ms`
(5 min by default). The firmware then cuts the heater and latches a fault.
Safety doesn't depend on the sidecar staying up.

The sidecar also:

- reconciles every 30 s against Bambuddy and the heater. This catches prints
  that started while it was down and print-end events it missed, and clears a
  leftover lease from a previous run.
- turns the heater off if Bambuddy has no printer status for 10 min
  (`bambuddy_lost_off_s`).
- turns it off after 24 h of continuous heating (`max_engaged_hours`).
- backs off for the rest of the print if you turn the heater off or change its
  mode yourself on the device or its web UI.
- refuses to heat while the device has a latched fault, unless you set
  `auto_clear_fault: true`.
- sends `off` on SIGTERM, so `docker compose stop`/`restart` leaves nothing running.

## Setup

1. **DragonBreath.** Open its web UI and go to **Setup**. Set the control
   source to **None** and save; the device restarts. This turns off its own
   Bambu AUTO logic so the two don't both drive the heater. Optionally set a
   control token:
   ```sh
   curl -X POST http://<panda-ip>/api/v2/token \
     -H 'X-DragonBreath-Auth: x' -H 'Content-Type: application/json' \
     -d '{"token":"<long random string>"}'
   ```
   Without a token, any LAN client can command the heater.
2. **Bambuddy.** Go to **Settings → API Keys → Create API Key**. Enable only
   **Read status**, which covers status, settings, archives and the WebSocket.
   If Bambuddy auth is disabled, you can leave the key empty.
3. **Find the printer id.** It's the number in the printer's URL in Bambuddy,
   or the `id` from `GET /api/v1/printers/`.
4. **Add the service** from `compose.snippet.yaml` to the compose file that
   runs Bambuddy. Put the secrets in the stack's `.env`:
   ```sh
   DRAGONBREATH_BAMBUDDY_API_KEY=bb_...
   DRAGONBREATH_URL=http://<panda-ip-or-name>
   ```
   Then `docker compose up -d bambuddy-dragonbreath`.

   If Bambuddy uses `network_mode: host` (its default), the sidecar can't
   reach `http://bambuddy:8000`. Set `BAMBUDDY_URL` to the host's address or
   to Bambuddy's public URL instead.

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `BAMBUDDY_URL` | yes | Bambuddy base URL |
| `BAMBUDDY_API_KEY` | if Bambuddy auth is on | API key with only "Read status" |
| `PRINTER_ID` | yes | Bambuddy printer id (`GET /api/v1/printers/`) |
| `PRINTER_NAME` | no | Label used in the logs |
| `DRAGONBREATH_URL` | yes | e.g. `http://192.168.1.50` |
| `DRAGONBREATH_TOKEN` | if one is set on the device | DragonBreath control token |
| `OVERRIDES` | no | e.g. `ABS=55,PETG=0`; wins over Bambuddy's table |
| `OFF_DELAY_S` | no | Seconds to keep heating after a print ends (default 0) |
| `DRY_RUN` | no | `true` to log heater commands without sending them |
| `AUTO_CLEAR_FAULT`, `BAMBUDDY_LOST_OFF_S`, `MAX_ENGAGED_HOURS`, `RECONCILE_INTERVAL_S`, `LOG_LEVEL` | no | See `config.example.yaml` |

For more than one printer, mount a YAML config at `/config/config.yaml`
(see `config.example.yaml`). When that file exists it is used instead of
the variables.

## Trying it safely

```sh
# Log what it would do, send nothing to the heater:
docker compose run --rm -e DRY_RUN=true bambuddy-dragonbreath

# Check the heater and the decision for the current print:
docker compose run --rm bambuddy-dragonbreath status
docker compose run --rm bambuddy-dragonbreath resolve

# Heat to 40 °C for 60 s with heartbeats, then turn off:
docker compose run --rm bambuddy-dragonbreath test --target 40 --hold 60

# Turn the heater off:
docker compose run --rm bambuddy-dragonbreath off
```

Every decision is logged, for example:

```
[X1C] Print started: benchy (ams_mapping=[1])
[X1C] Filament decision: target 45°C from ams_mapping [tray1=ASA→45°C]
[X1C] Heater engaged at 45°C
[X1C] Print ended (completed)
[X1C] Heater off (print completed)
```

## Releases

Pushing a `v*` tag builds and publishes `ghcr.io/rmonk/bambuddy-dragonbreath`
(`latest`, `X.Y.Z`, `X.Y`) for amd64 and arm64.

Versions follow SemVer. Releases that only refresh dependencies or the base
image, with no code change, bump the patch version.

## License

AGPL-3.0-or-later. The filament matching in `filament.py` is ported from
Bambuddy, which is AGPL-3.0.

## Development

```sh
python3 -m venv .venv
PATH="$PWD/.venv/bin:$PATH" safe-chain pip install -e '.[dev]'
.venv/bin/pytest
```

The Docker image installs from `requirements.lock`, which pins exact versions
by hash. Regenerate it with `scripts/lock.sh`, or `scripts/lock.sh --upgrade`
to move to the latest releases first.
