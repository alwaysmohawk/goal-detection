# HHOF Goalie Sim — Goal Detection System

## Project Report

**Date:** May 2026
**Status:** Working PoC, ready for real-puck testing and Pi deployment work
**Project:** Computer-vision-based goal detection for the Hockey Hall of Fame goalie simulator (wonderMakr)

---

## Executive summary

A working camera-based goal detection system has been built and validated on Windows with a USB-connected OV9281 global-shutter camera. The detector reliably runs at the camera's full 120fps with ~30% CV headroom remaining, communicates with downstream systems via WebSocket, and supports both armed-window and always-on detection modes. Calibration is interactive and persistent. A standalone dev/test webpage and Node WebSocket broker are included for integration testing ahead of the real Node app.

The system is a one-process-per-net architecture: each goal is monitored by an independent Pi with its own camera and detector instance. This was a deliberate design choice for fault isolation and simplicity.

What remains is real-world tuning with foam pucks under representative lighting, the swap to the Raspberry Pi as the deployment target, and integration with the wonderMakr Node application once it's available.

---

## What was built

### Three deliverables

**1. `goal_detector.py`** — the detection engine
- USB camera capture via OpenCV (DirectShow on Windows, will swap to `picamera2` for CSI on the Pi)
- MOG2 background subtraction with configurable shadow rejection
- Morphological cleanup (open + close) to merge fragmented detections of fast-moving objects
- Bounding-box contour filtering by area
- Greedy nearest-neighbor tracker with hit-count confirmation (rejects single-frame flickers)
- Goal-line crossing detection with direction filtering (only counts crossings going *into* the net)
- Cooldown logic to prevent double-counting
- Two operating modes: armed-only (1s window after `shot_incoming` notification) and always-on
- WebSocket client with auto-reconnect and exponential backoff
- Per-stage timing instrumentation for performance diagnosis
- Interactive 3-step calibration GUI with persistent JSON config
- Debug overlay mode showing all contours (pass/fail color-coded), tracks (gray/green/orange by status), and live stats

**2. `server.js`** — dev/test WebSocket broker
- Single-file Node app, no framework dependencies beyond `ws`
- Routes messages between detectors and UI clients by sender role
- Serves the receiver page on port 8080
- Runs the WebSocket broker on port 8765
- Disposable: replace with the real wonderMakr Node app when ready, the wire format is documented

**3. `receiver.html`** — dev/test dashboard
- Real-time goal/no-goal indicator with clear visual states
- Manual "send shot_incoming" button for testing without the real game system
- Mode toggle (armed-only vs always-on) that broadcasts to all connected detectors
- Live detector status panel with FPS, mode, and track count per net
- Event log with timestamps and full message details
- Auto-reconnect WebSocket client

### Wire format (between detector and Node app)

All messages JSON over a single WebSocket connection. Detector connects out as a client.

**From detector → server:**
```json
{ "type": "hello", "net_id": "net_1", "ts": 1730000000.123 }

{ "type": "heartbeat", "net_id": "net_1", "ts": ...,
  "fps": 119.2, "mode": "armed", "armed": false, "tracks": 0 }

{ "type": "goal", "net_id": "net_1", "ts": ...,
  "track_id": 17, "speed_px_s": 1240.5,
  "shot_id": "shot_1730000000123", "mode": "armed" }

{ "type": "no_goal", "net_id": "net_1", "ts": ...,
  "shot_id": "shot_1730000000123", "reason": "window_expired" }
```

**From server → detector:**
```json
{ "type": "shot_incoming", "shot_id": "shot_1730000000123" }
{ "type": "set_mode", "mode": "always_on" }   // or "armed"
```

`shot_id` is opaque — the detector echoes it back on goal/no_goal so the Node app can correlate events.

---

## Key design decisions

### Top-down camera mount
By far the easiest geometric problem. The goal line is a literal line in the image, and "did it cross" reduces to a 2D side-of-line test. Side-on views suffer from perspective ambiguity; behind-the-net views suffer from mesh occlusion. Top-down was the right call.

### One process per net
Two physical sims, each with their own Pi, camera, and detector instance. This means:
- Either net failing doesn't affect the other.
- Scaling to more nets is a matter of plugging in another Pi.
- Each detector only has to think about one calibration, one goal line, one game state.

A single process driving both nets would have been more efficient on hardware but a much more brittle integration story. Not worth it.

### WebSocket, with detector as client
The Node app initiates `shot_incoming` notifications, which means it's the natural server in the relationship. The detector dialing out keeps the Node app's network-discovery problem trivial: detectors come and go, the Node app just listens. Both Pis can connect to a single Node endpoint without any coordination between them.

### Interactive click-to-calibrate
Two clicks define the goal line, four-plus clicks define the net interior polygon. Saved to `config.json`. Runs anywhere with a mouse + display attached. Re-run as needed.

The alternatives (auto-detection from fiducials, ARtag corners, etc.) are more "elegant" but add failure modes — markers fall off, get occluded by the goalie, get repainted by a janitor. Manual calibration is boring, takes 15 seconds, and works reliably.

### MOG2 background subtraction (with hooks for change later)
The camera is static, so background subtraction is the natural detection method. MOG2 was chosen over simple frame differencing because it's more robust to ambient lighting drift and has built-in shadow classification. The pipeline is structured so a different detector (frame diff, deep learning, whatever) could be dropped in without touching the tracker, goal logic, or wire protocol.

### Greedy nearest-neighbor tracker (not Kalman, not IoU)
A single foam puck per shot, sparse blobs, static camera. Anything fancier is overkill. The tracker has a `hits` counter — a track must be matched in N frames before it's trusted for goal logic, which efficiently rejects single-frame flicker without expensive math.

### Process at half-resolution by default
The CV pipeline runs at 640×360 by default while capture, calibration, and display use full 1280×720. This is a 4x speedup with no perceptible loss in detection quality (a foam puck spans 25+ pixels at 640×360, more than enough for robust contour detection). Configurable via `process_scale`.

---

## Performance results

On Windows, with the OV9281 over USB, after troubleshooting:

```
fps=119.2  capture=2.4ms  cv=3.1ms  display=2.9ms  total=8.4ms
fps=121.1  capture=6.1ms  cv=2.1ms  display=0.0ms  total=8.3ms
```

- **120fps sustained**, paced by the camera (8.33ms frame interval)
- **CV uses ~30% of the frame budget** — plenty of headroom for tuning
- **Display has measurable cost (~3ms) but is debug-only** — production runs headless
- **Backend: DirectShow + MJPG** at 1280×720

This should translate well to a Pi 5, and probably a Pi 4. The CSI path via `picamera2` will likely be lower-latency and lower-CPU than USB on the Pi.

---

## Tuning knobs

All in `config.json`. Most-impactful at top.

### Detection
| Knob | Default | Notes |
|------|---------|-------|
| `process_scale` | 0.5 | CV runs at this fraction of capture resolution. 4x perf at 0.5 vs 1.0. |
| `puck_min_area` / `puck_max_area` | 80 / 4000 | Pixel-area bounds for a "puck-sized" blob (full-res coords). |
| `bg_var_threshold` | 30 | MOG2 sensitivity. Higher = less sensitive = fewer false positives. |
| `morph_close_size` | 13 | Kernel size for fragment-merging. Bigger = more aggressive merging. Sized for *processed* image. |
| `morph_open_size` | 3 | Kernel size for noise removal. |
| `min_track_hits` | 2 | Frames a track must persist before it's eligible to score a goal. |
| `detect_shadows` | true | MOG2's built-in shadow classifier (marks shadow pixels at value 127). |
| `fg_threshold` | 200 | Drops everything below this in the foreground mask, including shadows. |

### Tracking
| Knob | Default | Notes |
|------|---------|-------|
| `tracker_max_dist_px` | 250 | Max pixel distance between frames for a detection to be matched to an existing track. |
| `tracker_max_age_s` | 0.4 | Track is dropped after this long with no detection. |

### Goal logic
| Knob | Default | Notes |
|------|---------|-------|
| `armed_window_seconds` | 1.0 | After `shot_incoming`, look for a goal for this long. |
| `goal_cooldown_seconds` | 0.5 | Min gap between goal events (prevents double-counts). |
| `always_on_default` | false | Start in armed-only mode (the production default). |

### Capture
| Knob | Default | Notes |
|------|---------|-------|
| `frame_width`, `frame_height` | 1280, 720 | Capture resolution. |
| `target_fps` | 120 | OV9281 native max. |
| `capture_backend` | "auto" | "auto", "dshow", "msmf", "v4l2", "any" |
| `auto_exposure` | true | Disabling forces manual exposure (useful for high-fps in dim light). |
| `manual_exposure` | -7 | log2(seconds), only applied when auto is off. |

---

## Lessons learned during development

A few rough edges worth documenting so they don't bite the next person:

### Windows Modern Effects Pipeline (MEP) breaks high-framerate capture
On Windows 11 24H2+, MEP intercepts UVC cameras to apply Studio Effects (background blur, eye contact, auto-framing). On the OV9281 it caused a 60fps cap that masqueraded as a CV/USB bottleneck, and got into stuck states that required physical replug to recover.

**Fix:** enable "Basic camera mode" for the camera in Windows Settings → Bluetooth & devices → Cameras. This is per-camera and persistent. With it on, the OV9281 immediately delivers 120fps.

This is Windows-specific and won't matter on the Pi. But it's worth noting for anyone else doing CV dev on Windows: **always check for MEP first when seeing inexplicable framerate caps.**

### "FPS reported in OpenCV" can be misleading
`cap.get(CAP_PROP_FPS)` reports what the driver was configured for, not what's actually arriving. Without per-stage timing instrumentation we'd never have known the camera was actually delivering 60fps despite reporting 120. The instrumentation is now baked in and prints every second.

### MOG2 fragments fast objects
Background subtraction operates per-pixel, so a fast puck shows up as a constellation of small blobs rather than one solid shape. This was the main source of flicker in early testing. The fix was a layered approach:
1. Bigger morphological close kernel to merge fragments before contour detection
2. A `min_track_hits` requirement so flickers don't get treated as objects
3. A wider tracker matching radius so all the fragments bind to one track

### USB cameras on Windows are flaky
Multiple USB ports were tried during development, with inconsistent results. Direct-to-motherboard USB 3.0 ports worked best. Hubs and front-panel ports caused enumeration issues. **For the install: short cable, direct connection, no hub.** This is moot for the Pi CSI path.

---

## Outstanding work

### Before real-world testing
- Tune `puck_min_area` / `puck_max_area` against actual foam puck throws (current defaults are placeholder; expect to revise).
- Consider re-enabling larger `morph_close_size` (17-21) once perf headroom is confirmed at 120fps.

### Before Pi deployment
- Swap the `open_capture` function to use `picamera2` for the CSI camera path (the rest of the pipeline is camera-agnostic and won't need changes).
- Build a systemd unit for autostart at boot.
- Decide on remote access strategy for recalibration (SSH + X forwarding, VNC, web-based recalibration tool, or "operator runs once on a laptop and copies the config over").
- Test on the actual Pi hardware. Pi 5 with 4GB RAM is the recommended target.

### Before installation
- Source IR illumination + IR-pass filter for the lens. This is the right answer for installation lighting — visible-spectrum shadows, projector content, ambient lighting changes all stop mattering.
- Add a `meters_per_pixel` calibration step for real-world speed estimation (currently speed is reported in pixels per second).
- Consider adding a rolling video buffer that dumps the last few seconds to disk on each goal event (~30 lines of code, hugely useful for "why did it call that a goal" debugging during install).
- Plan for fault telemetry beyond heartbeat. Camera dropouts, WebSocket disconnects, and detector restarts should surface as structured events the Node app can react to.
- Operator UI for recalibration without SSH access — possibly a small web tool that talks to the detector over WebSocket and triggers calibration mode remotely.

### Before launch
- Empirical sustained-load testing — run for 12+ hours straight with a puck launcher cycling shots, check for memory leaks, thermal throttling, frame drops.
- Decide whether to keep the existing accelerometer system as a redundant confirmation signal or retire it. The two signals could be combined ("camera says goal AND accelerometer says hit" = high confidence) for better reliability.

### Known edge cases
- **Two pucks crossing the goal line within a few frames.** With aggressive morph close, two close pucks can merge into one track. Unlikely in practice (one puck per launch) but worth documenting.
- **Goalie body or stick crossing the line during play.** Currently filtered by area thresholds and (mostly) by the net ROI mask. Will need real-world validation.
- **Pucks bouncing OUT of the net.** Direction filtering should reject these, but worth verifying.

---

## Recommended hardware (final installation)

- **Compute:** Raspberry Pi 5, 4GB RAM. 8GB if budget allows headroom for whatever else creeps onto these boxes.
- **Storage:** 64GB A2 SD card (32GB minimum, larger if rolling video buffer is added).
- **Camera:** OV9281 global-shutter, 1280×800 mono, 120fps. CSI ribbon to the Pi (preferred) or USB 3.0 with a known-good cable.
- **Lens:** filter for IR pass-through if using IR illumination.
- **Illumination:** 850nm IR LED panel pointed at the goal area. Diffused, not direct.
- **Mounting:** rigid, vibration-free. Any micro-vibration shifts the background and triggers MOG2 false positives.

---

## File inventory

```
goal-detector/
├── goal_detector.py    # Detection engine (Python). ~840 lines.
├── server.js           # Dev WebSocket broker (Node). ~70 lines.
├── receiver.html       # Dev/test dashboard (HTML+JS). ~340 lines.
├── package.json        # Node dependencies (just `ws`).
├── README.md           # User-facing setup and usage.
└── PROJECT_REPORT.md   # This document.
```

Generated at runtime, not in version control:
- `config.json` — written by `--calibrate`, contains goal-line and ROI coordinates.

---

## Quick reference: how to run

```bash
# One-time setup
pip install opencv-python numpy websockets
npm install

# Each session
node server.js                              # terminal 1
python goal_detector.py --calibrate         # terminal 2, first time only
python goal_detector.py --debug             # terminal 2, run detector
# Open http://localhost:8080 in a browser

# Useful overrides
python goal_detector.py --source 1          # specific webcam index
python goal_detector.py --server ws://...   # specific server URL
python goal_detector.py                     # run headless (production-like)
```

---

## Credits

Built collaboratively by Alexander Forster (wonderMakr) and Claude (Anthropic), May 2026. Camera-based detection design, code, and tuning iterations conducted in a single working session.
