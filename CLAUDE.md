# HHOF Goal Detector — Claude context

## What this project is

Camera-based goal detection for the Hockey Hall of Fame goalie simulator.
A Windows 11 PC with a top-down Lucid Vision GigE camera watches the net.
A Python script detects when a foam puck crosses the goal line and fires a
WebSocket event to a Node.js game server. One Python process per net.

## Key files

| File | Purpose |
|---|---|
| `goal_detector.py` | Entire detector — calibration, CV pipeline, tracker, goal logic, WS client |
| `admin_server.js` | Always-on admin panel: HTTP/WS on port 8090, detector WS on port 8766 |
| `admin.html` | Admin panel UI — live stats, config editor, restart + calibrate buttons |
| `install.ps1` | One-shot Windows installer — deps, NSSM services, scheduled tasks |
| `config.json` | Runtime config + calibration data. Generated/updated by `--calibrate`. Never hand-edit calibration coordinates. |
| `server.js` | Dev-only WebSocket broker + HTTP server for the dev dashboard |
| `receiver.html` | Dev-only browser dashboard (served by `server.js`) |

## Deployment

Windows 11 Pro. Install via `install.ps1` (run as Administrator). It registers:

- NSSM service `goal-detector-N` — the Python detector (auto-start, restarts on crash)
- NSSM service `goal-detector-admin-N` — the Node admin panel (always on, port 8090)
- Scheduled task `hhof-calibrate-N` — runs calibration as the current interactive user
- Scheduled task `hhof-log-cleanup-N` — weekly Sunday 3am, deletes rotated log files >30 days

Both services run as SYSTEM (Session 0). The calibration task runs as the interactive
user because Session 0 has no access to the user's display — OpenCV `imshow` calls
are invisible from a SYSTEM service. The scheduled task bridges this gap.

NSSM log rotation is set at 10 MB per file. The cleanup task prevents rotated files
from accumulating over years of continuous operation.

## Architecture

- Single Python process per net.
- CV pipeline runs in the main thread (capture → MOG2 / dark threshold → morph → contours → tracker → goal logic).
- Two WSClient threads: one for the game server (port 8765), one for the local admin server (port 8766). Each runs its own asyncio loop. Communication with the main thread is via `queue.Queue`.
- The detector is a WS **client** on both connections; it connects out. This keeps firewall rules simple.
- A Windows named mutex (`hhof-goal-detector-mutex`) prevents two detector instances from running simultaneously. The second instance exits with a clear error before opening the camera.

## Admin panel (admin_server.js)

- HTTP + browser WebSocket on port 8090 (same port, HTTP upgrade)
- Detector WebSocket on port 8766 (dedicated port, accepts a single connection from goal_detector.py)
- REST API: `GET /api/config`, `POST /api/config`, `POST /api/restart`, `POST /api/calibrate`, `GET /api/status`
- Config save writes directly to config.json. Fields marked `[restart]` in the UI need a service restart to apply.
- Restart: calls `nssm restart <service>` async, broadcasts status to browser via WebSocket.
- Calibrate: calls `schtasks /run /tn <task>`, polls `schtasks /query` every 2s to detect completion, wraps with service stop/start.
- NSSM/UV/schtasks paths are passed as env vars (`NSSM_PATH`, `UV_PATH`, `CALIBRATE_TASK_NAME`) so SYSTEM account can find them despite having a minimal PATH.

## Calibration flow (`--calibrate`)

Four interactive steps, all in one OpenCV window (resizable):

1. Capture a reference frame (SPACE)
2. Click goal line — left post then right post (2 clicks, ENTER)
3. Click net interior polygon — where pucks count as IN (4+ clicks, ENTER)
4. Click approach zone polygon — area in front of the goal (4+ clicks, ENTER)

Results saved to `config.json` as `goal_line`, `net_roi`, and `approach_roi`.

The approach zone ensures the tracker sees the puck before it crosses the line —
without it the first detection is already past the line and the goal is silently missed.
Old configs without `approach_roi` fall back to auto-mirroring the net polygon.

## Detection pipeline

1. Downscale frame by `process_scale` (default 0.5 — runs CV at 360p from 720p capture)
2. MOG2 background subtraction and/or brightness threshold (depending on `detection_method`)
3. Apply combined ROI mask (net interior + approach zone)
4. Morphological open (remove speckle) then close (merge puck fragments)
5. Find contours, filter by area, compute centroids → detections
6. Greedy nearest-neighbor tracker updates tracks
7. Goal logic: if armed/always-on, check whether any confirmed track's last two positions straddle the goal line moving net-ward

## Config keys to know

- `detection_method`: `"motion"` (MOG2 only), `"dark"` (brightness only), `"combined"` (both — best for dark pucks with shadow noise)
- `puck_min_area` / `puck_max_area`: full-res pixel area thresholds. Tune with `verbose_contour_logging: true`
- `always_on_default`: `true` to score goals without waiting for `shot_incoming`
- `process_scale`: lower = faster CV, coarser detection. 0.5 is the sweet spot for 720×540 @ 120fps
- `lucid_serial`: serial number on the camera body. Required when two cameras share a GigE network — without it device selection is non-deterministic.
- `admin_server_url`: defaults to `ws://localhost:8766`. Do not put this key in config.json as null/empty — it overrides the default and breaks the admin panel connection. (The admin panel config editor intentionally doesn't show this field.)

## WebSocket wire format

**Outbound:** `hello`, `heartbeat`, `goal`, `no_goal`
**Inbound:** `shot_incoming`, `set_mode`

All messages are JSON with `net_id` and `ts` fields. The detector echoes `shot_id` back in `goal`/`no_goal` so the Node app can correlate events.

Heartbeat is sent every ~1s even when the camera is failing (fps=0, armed=false) so the admin panel always shows current status.

## Lucid Vision GigE camera (PHX004S)

Set `capture_backend: "lucid"` in config.json.

**One-time setup:**
1. Install Arena SDK from Lucid's downloads hub (C++ DLLs)
2. Install arena_api Python wheel (bundled with SDK, not on PyPI); handled by install.ps1
3. After `uv sync`, run `uv pip install pip` — arena_api runs `pip show` at import time and fails if pip is absent from the venv
4. Assign the camera a static IP on the same subnet as the GigE NIC (use `IpConfigUtility`)
5. Enable jumbo frames (MTU 9000) on the NIC
6. Set `frame_width: 720` and `frame_height: 540` in config.json (PHX004S native res)

**Exposure:** use `lucid_exposure_us` (microseconds) instead of `manual_exposure` (OpenCV log2-seconds, ignored for Lucid). 7800 µs ≈ 1/128 s.

**FPS cap:** `AcquisitionFrameRateEnable` must be set `True` before setting `AcquisitionFrameRate`; otherwise `target_fps` is silently ignored and the camera runs at ~284fps (GigE bandwidth ceiling), causing SC_ERR_TIMEOUT -1011 dropouts.

**Multi-camera:** set `lucid_serial` in each net's config.json to the camera's serial number. Without it, device ordering is non-deterministic and both detectors may try to open the same camera.

**Pixel format:** PHX004S outputs Mono8. `LucidCapture.read()` converts to BGR before returning, so the CV pipeline is unchanged.

## Common gotchas

- **SYSTEM services can't show GUI windows:** Windows services run in Session 0, isolated from the interactive desktop. Any subprocess they spawn also runs in Session 0. Calibration is routed through a scheduled task (registered as the interactive user) to get a visible window.
- **NSSM PATH for SYSTEM account:** NSSM, uv, and Node.js are not on SYSTEM's PATH. Pass their full paths via NSSM AppEnvironmentExtra (`NSSM_PATH`, `UV_PATH`) so admin_server.js can find them.
- **arena_api needs pip:** uv sync strips pip from the venv. install.ps1 re-installs it with `uv pip install pip` after sync.
- **Windows named mutex:** prevents two detector instances from running simultaneously (e.g. after a crash where NSSM restarts before the previous process fully exits, which causes SC_ERR_ACCESS_DENIED on camera open). Second instance logs an error and exits.
- **NSSM SERVICE_PAUSED bug:** NSSM 2.24 (winget) has a race condition on Windows 11 that leaves a service in SERVICE_PAUSED if it exits quickly multiple times. Workaround: `sc.exe start goal-detector-1` or kill orphaned python.exe processes first.
- **Windows 60fps USB cap:** Windows 11 24H2+ MEP silently limits USB cameras. Disable in Settings → Cameras → Basic camera mode. Not applicable for GigE.
- **Approach zone fallback:** if the debug overlay shows a mirrored (auto-computed) approach zone, re-run `--calibrate` and complete step 4.
- **admin_server_url null in config:** if a user saves config.json via the admin panel and `admin_server_url` is empty in the form (because it wasn't in config.json to begin with), the panel writes `null` to the file, overriding the default and breaking the admin WS connection. Fix: remove the key from config.json or set it to `"ws://localhost:8766"`. The admin panel schema no longer shows this field (removed to prevent this).
- **Camera reinit on failure:** after 10 consecutive frame-read failures the detector releases the camera, sleeps 2s, and reopens it. MOG2 model and tracker are recreated. This handles transient GigE packet loss without requiring a full service restart.
