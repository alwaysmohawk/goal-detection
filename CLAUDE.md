# HHOF Goal Detector — Claude context

## What this project is

Camera-based goal detection for the Hockey Hall of Fame goalie simulator.
A Raspberry Pi with a top-down USB/CSI camera watches the net. A Python
script detects when a foam puck crosses the goal line and fires a WebSocket
event to a Node.js server. One Python process per net.

## Key files

| File | Purpose |
|---|---|
| `goal_detector.py` | Entire detector — calibration, CV pipeline, tracker, goal logic, WS client |
| `config.json` | Runtime config + calibration data. Generated/updated by `--calibrate`. Never hand-edit calibration coordinates. |
| `server.js` | Dev-only WebSocket broker + HTTP server for the dashboard |
| `receiver.html` | Dev-only browser dashboard (served by `server.js`) |

## Running it

```bash
# One-time calibration (needs a display)
python goal_detector.py --calibrate

# Run headless
python goal_detector.py

# Run with overlay windows for tuning
python goal_detector.py --debug

# Common overrides
python goal_detector.py --source 1 --debug
python goal_detector.py --server ws://hostname:8765
```

## Architecture

- Single Python process per net.
- CV pipeline runs in the main thread (capture → MOG2 / dark threshold → morph → contours → tracker → goal logic).
- WebSocket client runs in a daemon thread on its own asyncio loop. Communication with the main thread is via `queue.Queue` — no shared state.
- The detector is a WS **client**; it connects out to the Node server. This keeps firewall rules simple.

## Calibration flow (`--calibrate`)

Four interactive steps, all in one OpenCV window:

1. Capture a reference frame (SPACE)
2. Click goal line — left post then right post (2 clicks, ENTER)
3. Click net interior polygon — where pucks count as IN (4+ clicks, ENTER)
4. Click approach zone polygon — area in front of the goal (4+ clicks, ENTER)

Results saved to `config.json` as `goal_line`, `net_roi`, and `approach_roi`.

The approach zone matters because the goal-crossing check compares a track's *previous* position to its *current* position. If the puck's first detection is already inside the net (because detection only starts at the goal line), there is no "before" position and the goal is silently missed. The approach zone ensures detection starts before the line.

If `approach_roi` is absent in config (old calibration), the detector falls back to auto-mirroring the net polygon across the goal line and logs a reminder to re-calibrate.

## Detection pipeline

1. Downscale frame by `process_scale` (default 0.5 — runs CV at 640×360)
2. MOG2 background subtraction and/or brightness threshold depending on `detection_method`
3. Apply combined detection mask (net interior + approach zone)
4. Morphological open (remove speckle) then close (merge puck fragments)
5. Find contours, filter by area, compute centroids → detections
6. Greedy nearest-neighbor tracker updates tracks
7. Goal logic: if armed/always-on, check whether any confirmed track's last two positions straddle the goal line moving net-ward

## Config keys to know

- `detection_method`: `"motion"` (MOG2 only), `"dark"` (brightness only), `"combined"` (both — best for dark pucks with shadow noise)
- `puck_min_area` / `puck_max_area`: full-res pixel area thresholds. Tune with `verbose_contour_logging: true`
- `always_on_default`: `true` to score goals without waiting for `shot_incoming`
- `process_scale`: lower = faster CV, coarser detection. 0.5 is the sweet spot for 1280×720

## WebSocket wire format

**Outbound:** `hello`, `heartbeat`, `goal`, `no_goal`
**Inbound:** `shot_incoming`, `set_mode`

All messages are JSON with `net_id` and `ts` fields. The detector echoes `shot_id` back in `goal`/`no_goal` so the Node app can correlate events.

## Common gotchas

- **Windows 60fps cap:** Windows 11 24H2+ MEP silently limits USB cameras. Disable in Settings → Cameras → Basic camera mode.
- **Approach zone fallback:** if the debug overlay shows a mirrored (auto-computed) approach zone instead of the correct one, re-run `--calibrate` and complete step 4.
- **Puck not detected:** run `--debug` to watch the foreground mask. Enable `verbose_contour_logging` to see per-contour area/aspect logs and find the threshold to tune.
- **False positives:** raise `bg_var_threshold`, switch to `"combined"` detection, or raise `min_track_hits`.
