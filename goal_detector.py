"""
HHOF Goalie Simulator - Goal Detection
======================================
Top-down camera, foam puck, line-crossing detection with optional armed mode.

Usage:
    python goal_detector.py --calibrate              # one-time setup of goal line + ROI
    python goal_detector.py                          # run detector
    python goal_detector.py --debug                  # run with overlay window
    python goal_detector.py --source 1               # specific webcam index
    python goal_detector.py --server http://host:3000  # override config

Architecture notes:
    - Python is a WebSocket *client*. It connects out to a server (the dev
      receiver page during testing, the real Node app in production).
    - Capture and detection run in the main thread. WebSocket runs in an
      asyncio task on a separate thread. They communicate via thread-safe
      queues. This keeps the CV loop tight and CPU-bound work off the
      asyncio loop.
    - Pi/picamera2 path is stubbed; for now uses cv2.VideoCapture on Windows.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import queue
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).with_name("config.json")

DEFAULT_CONFIG = {
    "net_id": "net_1",                 # which net this Pi is for
    "server_url": "http://localhost:3000",
    "admin_server_url": "ws://localhost:8766",  # local admin panel WS; set to null to disable
    "source": 0,                       # webcam index, or path to video file for testing
    "frame_width": 1280,
    "frame_height": 720,
    "target_fps": 120,                 # OV9281 will do 120 in good conditions
    "capture_backend": "auto",         # "auto", "dshow", "msmf", "v4l2", "any", "lucid"
                                       # Use "lucid" for Lucid Vision GigE cameras (arena_api).
    # Exposure control - mostly relevant for high-fps capture. If frames are coming
    # in at 60fps despite asking for 120, exposure is often the culprit (auto-exposure
    # silently uses long exposures in dim light, capping framerate).
    "auto_exposure": True,             # set False to use manual_exposure / lucid_exposure_us
    "manual_exposure": -7,             # OpenCV backends only: log2(seconds), -7 ≈ 1/128s.
    "lucid_sdk_path": None,            # Lucid/Arena backend only (Windows): path to Arena SDK
                                       # install dir so the native DLLs can be found. Defaults
                                       # to "C:\Program Files\Lucid Vision Labs\Arena SDK".
                                       # Only needed if you installed to a non-default location.
    "lucid_serial": None,              # Lucid/Arena backend only: select camera by serial number
                                       # (printed on the camera body). Required when more than one
                                       # Lucid camera is on the same GigE network — without this
                                       # the detector opens whichever camera is listed first, which
                                       # is non-deterministic. Leave null to use the first found
                                       # (fine for single-camera setups).
    "lucid_exposure_us": 7800,         # Lucid/Arena backend only: exposure in microseconds.
                                       # 7800 ≈ 1/128s. Only used when auto_exposure=False.
    "goal_line": None,                 # [[x1,y1],[x2,y2]] - set via --calibrate
    "net_roi": None,                   # [[x,y], ...] polygon - set via --calibrate
    "approach_roi": None,              # [[x,y], ...] polygon in front of goal - set via --calibrate
    "back_roi": None,                  # [[x,y], ...] polygon behind net - puck here means it went over, not in
    "goalie_roi": None,                # [[x,y], ...] polygon where goalie stands - new tracks born here are suppressed
    "goalie_roi_enabled": True,        # toggle birth suppression without recalibrating

    # ---- Detection ----
    "puck_min_area": 80,               # px^2 - applied to MERGED blob, not raw fragments
    "puck_max_area": 4000,             # px^2 - bump up if you see motion-blur streaks
    "process_scale": 0.5,              # downscale factor for CV pipeline (1.0=full res).
                                       # 0.5 means CV runs on 640x360 from a 1280x720
                                       # capture - 4x faster, plenty of detail for puck.
    "bg_history": 300,
    "bg_var_threshold": 30,
    "detect_shadows": True,            # let MOG2 classify shadow pixels (value=127)
    "fg_threshold": 200,               # 0-255; pixels above this are real fg.
                                       # 127 = shadow, 255 = hard fg. Threshold > 127
                                       # excludes shadows; raise toward 250 for stricter
                                       # rejection of marginal pixels.

    # Detection method - how to find puck candidates each frame:
    #   "motion"   - MOG2 background subtraction only (default, what we've been using)
    #   "dark"     - brightness threshold only; pixels darker than dark_threshold
    #   "combined" - both: pixel must be moving AND dark to count.
    #                Best for dark pucks against a brighter background; rejects
    #                shadows (which are moving but not dark enough).
    "detection_method": "motion",
    "dark_threshold": 60,              # 0-255; pixels darker than this are "dark"
                                       # for "dark" and "combined" methods.
                                       # 60 catches a black puck but not most shadows.
                                       # Lower = stricter (puck must be very dark).

    "morph_close_size": 13,            # kernel size for fragment-merging close op (odd).
                                       # Sized for the PROCESSED image - if you change
                                       # process_scale, the effective kernel changes too.
    "morph_open_size": 3,              # kernel size for noise-removal open op (odd)
    "min_track_hits": 2,               # detections needed before a track is "real"

    # ---- Tracker ----
    # max_dist_px: how far a track can move between frames and still be matched.
    # At 60fps a 25 m/s puck (foam, ~80mph max) moves ~0.4m / frame; in pixels
    # that depends on the camera mount but 200-300px is a reasonable default.
    "tracker_max_dist_px": 250,
    "tracker_max_age_s": 0.4,          # how long a track survives without a hit

    # ---- Goal logic ----
    "armed_window_seconds": 2.0,       # how long after shot_incoming to look for goal
    "goal_cooldown_seconds": 0.5,      # min gap between goal events
    "always_on_default": False,        # start in armed-mode by default
    "goal_confirm_ms": 200,            # ms after crossing to confirm puck stayed in net
    "goal_speed_confirm_fraction": 0.25,  # confirm early if speed drops to this fraction of crossing speed
    "require_approach_zone": True,     # if False, goals also fire when a puck appears only inside the net (5-hole with full occlusion)
    "log_level": "INFO",

    # ---- Debug display ----
    "debug_display_scale": 0.5,        # scale of the debug imshow window (1.0=full).
                                       # Lower = less GUI cost; helps on Pi where
                                       # imshow is the bottleneck under --debug.
    "debug_goal_flash_seconds": 0.5,   # how long the GOAL banner stays on screen

    # ---- Diagnostic logging ----
    # Verbose contour logging emits per-contour reject/accept info to the
    # console. Useful for tracking down false negatives ("why didn't that
    # puck get detected?"). To avoid drowning in noise, only contours within
    # `verbose_contour_log_margin` of the area thresholds are logged - so a
    # 2-pixel speckle won't spam the log, but a 2800px contou r with a
    # 3000px minimum will.
    "verbose_contour_logging": False,
    "verbose_contour_log_margin": 0.5, # log rejected contours whose area is
                                       # within this fraction of the threshold.
                                       # 0.5 = log anything 50%-200% of bounds.
    "verbose_log_throttle_hz": 5,      # max log lines per second (avoid spam)
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"WARNING: config.json is invalid ({e}); using defaults. "
                  f"Delete {CONFIG_PATH} and recalibrate.")
    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(level: str) -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    return logging.getLogger("goal_detector")


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

_BANNER_H = 70  # height of the calibration step banner in pixels


def _draw_banner(img, step_num: int, step_total: int, title: str, instruction: str,
                 y_start: int = 0) -> None:
    """Draw a solid black step banner in the `_BANNER_H`-pixel strip starting at `y_start`.

    Pass y_start = frame_height to draw below the frame content so nothing is obscured.
    """
    w = img.shape[1]
    y0, y1 = y_start, y_start + _BANNER_H
    cv2.rectangle(img, (0, y0), (w, y1), (0, 0, 0), -1)
    cv2.line(img, (0, y0), (w, y0), (0, 220, 255), 2)  # accent line at top of banner
    cv2.putText(img, f"STEP {step_num}/{step_total}", (12, y0 + 26),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 220, 255), 2)
    cv2.putText(img, title, (12, y0 + 56),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    (tw, _), _ = cv2.getTextSize(instruction, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
    cv2.putText(img, instruction, (max(12, w - tw - 12), y0 + 42),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)


# Single window name reused across phases — prevents window position weirdness
# and makes it visually obvious you're in one continuous flow.
CALIB_WINDOW = "Goal Detector Calibration"


def calibrate(cfg: dict, log: logging.Logger) -> None:
    """
    Interactive 5-step calibration:
      1. Capture a clean frame from the camera.
      2. Click 2 points to define the goal line.
      3. Click 4+ points to outline the net interior polygon.
      4. Click 4+ points to outline the approach zone in front of the goal.
      5. Click 4+ points to outline the back zone behind the net (where puck lands if it goes over).
    Reuses a single window across all phases for a clearer UX.
    """
    cap = open_capture(cfg, log)
    cv2.namedWindow(CALIB_WINDOW, cv2.WINDOW_NORMAL)

    # ============================================================
    # STEP 1/5 — capture a clean frame
    # ============================================================
    log.info("Step 1/5: position the camera, then press SPACE to capture a reference frame.")

    frame = None
    while True:
        ok, f = cap.read()
        if not ok:
            log.warning("Camera not ready yet, retrying...")
            time.sleep(0.05)
            continue
        fh, fw = f.shape[:2]
        canvas = np.zeros((fh + _BANNER_H, fw, 3), dtype=np.uint8)
        canvas[:fh] = f
        _draw_banner(canvas, 1, 6,"Capture reference frame",
                     "[SPACE] capture    [Q] quit", y_start=fh)
        cv2.imshow(CALIB_WINDOW, canvas)
        k = cv2.waitKey(1) & 0xFF
        if k == ord(' '):
            frame = f.copy()
            break
        if k == ord('q'):
            cap.release()
            cv2.destroyAllWindows()
            return
    cap.release()

    frame_h, frame_w = frame.shape[:2]

    def _canvas(base):
        """Frame in top rows, blank banner strip at bottom — nothing obscured."""
        c = np.zeros((frame_h + _BANNER_H, frame_w, 3), dtype=np.uint8)
        c[:frame_h] = base
        return c

    # ============================================================
    # STEP 2/5 — goal line (2 points)
    # ============================================================
    line_pts: list[tuple[int, int]] = []

    def on_line_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and y < frame_h and len(line_pts) < 2:
            line_pts.append((x, y))

    cv2.setMouseCallback(CALIB_WINDOW, on_line_click)
    log.info("Step 2/5: click LEFT POST, then RIGHT POST on the goal line. Enter to confirm.")

    while True:
        disp = _canvas(frame)
        for i, p in enumerate(line_pts):
            cv2.circle(disp, p, 7, (0, 0, 255), -1)
            label = "L" if i == 0 else "R"
            cv2.putText(disp, label, (p[0] + 10, p[1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        if len(line_pts) == 2:
            cv2.line(disp, line_pts[0], line_pts[1], (0, 0, 255), 2)

        instr = (f"clicks: {len(line_pts)}/2    "
                 f"[ENTER] confirm    [R] reset    [Q] quit")
        title = "Click LEFT POST, then RIGHT POST on the goal line"
        _draw_banner(disp, 2, 6, title, instr, y_start=frame_h)
        cv2.imshow(CALIB_WINDOW, disp)
        k = cv2.waitKey(20) & 0xFF
        if k == 13 and len(line_pts) == 2:  # Enter
            break
        if k == ord('r'):
            line_pts.clear()
        if k == ord('q'):
            cv2.destroyAllWindows()
            return

    # ============================================================
    # STEP 3/5 — net ROI polygon (4+ points)
    # ============================================================
    poly_pts: list[tuple[int, int]] = []

    def on_poly_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and y < frame_h:
            poly_pts.append((x, y))

    cv2.setMouseCallback(CALIB_WINDOW, on_poly_click)
    log.info("Step 3/5: click 4+ points around the inside of the net. Enter when done.")

    while True:
        disp = _canvas(frame)
        cv2.line(disp, tuple(line_pts[0]), tuple(line_pts[1]), (0, 0, 255), 2)
        for p in poly_pts:
            cv2.circle(disp, p, 5, (0, 255, 0), -1)
        if len(poly_pts) >= 2:
            cv2.polylines(disp, [np.array(poly_pts)], False, (0, 255, 0), 2)
        if len(poly_pts) >= 3:
            overlay = disp.copy()
            cv2.fillPoly(overlay, [np.array(poly_pts)], (0, 255, 0))
            disp = cv2.addWeighted(overlay, 0.2, disp, 0.8, 0)

        ready = len(poly_pts) >= 4
        instr = (f"points: {len(poly_pts)} (need 4+)    "
                 f"[ENTER]{'confirm' if ready else ' need more pts'}    "
                 f"[R] reset    [Q] quit")
        title = "Click around the inside of the net (where pucks count as IN)"
        _draw_banner(disp, 3, 6, title, instr, y_start=frame_h)
        cv2.imshow(CALIB_WINDOW, disp)
        k = cv2.waitKey(20) & 0xFF
        if k == 13 and ready:
            break
        if k == ord('r'):
            poly_pts.clear()
        if k == ord('q'):
            cv2.destroyAllWindows()
            return

    # ============================================================
    # STEP 4/5 — approach zone polygon (4+ points)
    # ============================================================
    approach_pts: list[tuple[int, int]] = []

    def on_approach_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and y < frame_h:
            approach_pts.append((x, y))

    cv2.setMouseCallback(CALIB_WINDOW, on_approach_click)
    log.info("Step 4/5: click 4+ points around the area in FRONT of the goal. Enter when done.")

    while True:
        disp = _canvas(frame)
        cv2.line(disp, tuple(line_pts[0]), tuple(line_pts[1]), (0, 0, 255), 2)
        overlay = disp.copy()
        cv2.fillPoly(overlay, [np.array(poly_pts)], (0, 255, 0))
        disp = cv2.addWeighted(overlay, 0.15, disp, 0.85, 0)
        cv2.polylines(disp, [np.array(poly_pts)], True, (0, 255, 0), 1)
        for p in approach_pts:
            cv2.circle(disp, p, 5, (0, 220, 220), -1)
        if len(approach_pts) >= 2:
            cv2.polylines(disp, [np.array(approach_pts)], False, (0, 220, 220), 2)
        if len(approach_pts) >= 3:
            overlay2 = disp.copy()
            cv2.fillPoly(overlay2, [np.array(approach_pts)], (0, 220, 220))
            disp = cv2.addWeighted(overlay2, 0.2, disp, 0.8, 0)

        ready = len(approach_pts) >= 4
        instr = (f"points: {len(approach_pts)} (need 4+)    "
                 f"[ENTER]{'confirm' if ready else ' need more pts'}    "
                 f"[R] reset    [Q] quit")
        title = "Click around the area IN FRONT of the goal (approach / shooting zone)"
        _draw_banner(disp, 4, 6, title, instr, y_start=frame_h)
        cv2.imshow(CALIB_WINDOW, disp)
        k = cv2.waitKey(20) & 0xFF
        if k == 13 and ready:
            break
        if k == ord('r'):
            approach_pts.clear()
        if k == ord('q'):
            cv2.destroyAllWindows()
            return

    # ============================================================
    # STEP 5/5 — back zone polygon (4+ points)
    # ============================================================
    back_pts: list[tuple[int, int]] = []

    def on_back_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and y < frame_h:
            back_pts.append((x, y))

    cv2.setMouseCallback(CALIB_WINDOW, on_back_click)
    log.info("Step 5/5: click 4+ points around the BACK ZONE (where puck lands if it goes over the net). Enter when done.")

    while True:
        disp = _canvas(frame)
        cv2.line(disp, tuple(line_pts[0]), tuple(line_pts[1]), (0, 0, 255), 2)
        overlay = disp.copy()
        cv2.fillPoly(overlay, [np.array(poly_pts)], (0, 255, 0))
        cv2.fillPoly(overlay, [np.array(approach_pts)], (0, 220, 220))
        disp = cv2.addWeighted(overlay, 0.15, disp, 0.85, 0)
        cv2.polylines(disp, [np.array(poly_pts)], True, (0, 255, 0), 1)
        cv2.polylines(disp, [np.array(approach_pts)], True, (0, 220, 220), 1)
        for p in back_pts:
            cv2.circle(disp, p, 5, (0, 128, 255), -1)
        if len(back_pts) >= 2:
            cv2.polylines(disp, [np.array(back_pts)], False, (0, 128, 255), 2)
        if len(back_pts) >= 3:
            overlay2 = disp.copy()
            cv2.fillPoly(overlay2, [np.array(back_pts)], (0, 128, 255))
            disp = cv2.addWeighted(overlay2, 0.2, disp, 0.8, 0)

        ready = len(back_pts) >= 4
        instr = (f"points: {len(back_pts)} (need 4+)    "
                 f"[ENTER]{'confirm' if ready else ' need more pts'}    "
                 f"[R] reset    [Q] quit")
        title = "Click around the BACK ZONE (behind the net — puck here = went over, not in)"
        _draw_banner(disp, 5, 6, title, instr, y_start=frame_h)
        cv2.imshow(CALIB_WINDOW, disp)
        k = cv2.waitKey(20) & 0xFF
        if k == 13 and ready:
            break
        if k == ord('r'):
            back_pts.clear()
        if k == ord('q'):
            cv2.destroyAllWindows()
            return

    # ---- Step 6: Goalie zone (optional) ----
    goalie_pts: list[tuple[int, int]] = []

    def on_goalie_click(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN and y < frame_h:
            goalie_pts.append((x, y))

    cv2.setMouseCallback(CALIB_WINDOW, on_goalie_click)
    log.info("Step 6/6 (optional): click 4+ points around the GOALIE ZONE "
             "(where the goalie stands — new blobs born here will be ignored). "
             "Press ENTER with no points to skip.")

    while True:
        disp = _canvas(frame)
        cv2.line(disp, tuple(line_pts[0]), tuple(line_pts[1]), (0, 0, 255), 2)
        overlay = disp.copy()
        cv2.fillPoly(overlay, [np.array(poly_pts)], (0, 255, 0))
        cv2.fillPoly(overlay, [np.array(approach_pts)], (0, 220, 220))
        cv2.fillPoly(overlay, [np.array(back_pts)], (0, 128, 255))
        disp = cv2.addWeighted(overlay, 0.15, disp, 0.85, 0)
        cv2.polylines(disp, [np.array(poly_pts)], True, (0, 255, 0), 1)
        cv2.polylines(disp, [np.array(approach_pts)], True, (0, 220, 220), 1)
        cv2.polylines(disp, [np.array(back_pts)], True, (0, 128, 255), 1)
        for p in goalie_pts:
            cv2.circle(disp, p, 5, (255, 0, 255), -1)
        if len(goalie_pts) >= 2:
            cv2.polylines(disp, [np.array(goalie_pts)], False, (255, 0, 255), 2)
        if len(goalie_pts) >= 3:
            overlay2 = disp.copy()
            cv2.fillPoly(overlay2, [np.array(goalie_pts)], (255, 0, 255))
            disp = cv2.addWeighted(overlay2, 0.2, disp, 0.8, 0)

        ready = len(goalie_pts) >= 4 or len(goalie_pts) == 0
        instr = (f"points: {len(goalie_pts)}    "
                 f"[ENTER] {'confirm' if len(goalie_pts) >= 4 else 'skip (no points)' if len(goalie_pts) == 0 else 'need 4+ pts'}    "
                 f"[R] reset    [Q] quit")
        title = "Click around the GOALIE ZONE (birth suppression) — or ENTER to skip"
        _draw_banner(disp, 6, 6, title, instr, y_start=frame_h)
        cv2.imshow(CALIB_WINDOW, disp)
        k = cv2.waitKey(20) & 0xFF
        if k == 13 and ready:
            break
        if k == ord('r'):
            goalie_pts.clear()
        if k == ord('q'):
            cv2.destroyAllWindows()
            return

    cv2.destroyAllWindows()

    cfg["goal_line"] = [list(p) for p in line_pts]
    cfg["net_roi"] = [list(p) for p in poly_pts]
    cfg["approach_roi"] = [list(p) for p in approach_pts]
    cfg["back_roi"] = [list(p) for p in back_pts]
    cfg["goalie_roi"] = [list(p) for p in goalie_pts] if goalie_pts else None
    save_config(cfg)
    log.info(f"Calibration saved to {CONFIG_PATH}")


# ---------------------------------------------------------------------------
# Lucid Vision GigE capture (arena_api)
# ---------------------------------------------------------------------------

class LucidCapture:
    """
    Wraps Lucid Vision's arena_api to present the same .read()/.release()
    interface as cv2.VideoCapture.

    Pixel format is forced to Mono8 (PHX004S is monochrome). Frames are
    returned as BGR (3-channel) so the rest of the pipeline is unchanged.

    Setup:
      1. Install Arena SDK from https://thinklucid.com/downloads-hub/
      2. pip install arena_api
      3. Set capture_backend: "lucid" in config.json
      4. Make sure the camera has a valid IP on the same subnet as the NIC
         (use IpConfigUtility from the Arena SDK install to assign one).
      5. Enable jumbo frames (MTU 9000) on the GigE NIC for full bandwidth.

    Resolution note: the PHX004S is 0.4 MP. Its native resolution is
    720x540. Set frame_width/frame_height in config.json accordingly —
    requesting an unsupported resolution will raise an error at startup.
    """

    def __init__(self, cfg: dict, log: logging.Logger):
        # On Windows, Python 3.8+ doesn't search PATH for DLLs. Add the SDK's
        # DLL directory explicitly before importing arena_api so it can load
        # ArenaC_v140.dll and friends. lucid_sdk_path in config.json overrides
        # the default install location.
        if sys.platform.startswith("win"):
            import os
            sdk_path = (cfg.get("lucid_sdk_path") or
                        r"C:\Program Files\Lucid Vision Labs\Arena SDK\x64Release")
            try:
                os.add_dll_directory(sdk_path)
                log.info(f"Added Arena SDK DLL directory: {sdk_path}")
            except (OSError, FileNotFoundError) as e:
                log.error(f"Could not add Arena SDK DLL directory '{sdk_path}': {e}")
                log.error("Set lucid_sdk_path in config.json to your Arena SDK install directory.")
                sys.exit(1)

        try:
            from arena_api.system import system as _arena_system
        except Exception as e:
            log.error(f"Failed to import arena_api: {e}")
            log.error("Make sure the Lucid Arena SDK is installed. "
                      "Download from https://thinklucid.com/downloads-hub/")
            sys.exit(1)

        self._log = log
        self._arena = _arena_system

        # Trigger device discovery.
        device_infos = self._arena.device_infos
        if not device_infos:
            log.error("No Lucid GigE devices found. "
                      "Check camera is connected, powered, and IP is configured.")
            sys.exit(2)

        log.info(f"Found {len(device_infos)} Lucid device(s): " +
                 ", ".join(f"{d.get('model','?')} serial={d.get('serial','?')} ip={d.get('ip','?')}"
                           for d in device_infos))

        target_serial = cfg.get("lucid_serial")
        if target_serial:
            target_str = str(target_serial).strip()
            # Short value (≤4 chars) = last-N-digits suffix supplied by installer prompt.
            # Full serial = exact match.
            if len(target_str) <= 4:
                match = [d for d in device_infos if str(d.get("serial", "")).endswith(target_str)]
            else:
                match = [d for d in device_infos if str(d.get("serial", "")) == target_str]
            if not match:
                serials = [d.get("serial", "?") for d in device_infos]
                log.error(f"lucid_serial={target_serial!r} not found. "
                          f"Available serials: {serials}. "
                          f"Check config.json or the serial number on the camera body.")
                sys.exit(2)
            chosen = match[0]
        else:
            chosen = device_infos[0]
            if len(device_infos) > 1:
                log.warning(f"Multiple Lucid cameras found and lucid_serial is not set — "
                            f"opening first found (serial={chosen.get('serial','?')}). "
                            f"Set lucid_serial in config.json to make this deterministic.")

        log.info(f"Opening Lucid device: model={chosen.get('model','?')} "
                 f"serial={chosen.get('serial','?')} ip={chosen.get('ip','?')}")
        self._device = self._arena.create_device(chosen)[0]
        nodemap = self._device.nodemap

        # --- Match the ArenaView settings that produced 288fps ---

        # Remove bandwidth cap (off by default in ArenaView). Optional —
        # skip if another instance still holds the camera or the node is locked.
        try:
            nodemap['DeviceLinkThroughputLimitMode'].value = 'Off'
        except Exception as e:
            log.warning(f"Could not set DeviceLinkThroughputLimitMode: {e}")

        # Set jumbo-frame packet size on the CAMERA side. This is the key
        # setting: with the default ~1500-byte packets and GevSCPD=80, the
        # inter-packet delay overhead alone limits fps to ~50-60fps. At 9000
        # bytes there are ~6x fewer packets per frame so that overhead drops
        # to nearly nothing and the camera can hit its hardware maximum.
        # Requires jumbo frames (MTU 9000) enabled on the GigE NIC. Falls back
        # to 1500 with a warning if the NIC doesn't support it yet.
        try:
            nodemap['GevSCPSPacketSize'].value = 9000
        except Exception as e:
            log.warning(
                f"Could not set GevSCPSPacketSize to 9000 (jumbo frames): {e}\n"
                "  FPS will be limited (~50-60fps). To fix: Device Manager → "
                "GigE NIC → Properties → Advanced → Jumbo Packet → 9014 bytes"
            )
            try:
                nodemap['GevSCPSPacketSize'].value = 1500
            except Exception:
                pass

        nodemap['PixelFormat'].value = 'Mono8'
        nodemap['Width'].value = cfg['frame_width']
        nodemap['Height'].value = cfg['frame_height']

        fps_limit = cfg.get('target_fps', 0)
        if fps_limit and fps_limit > 0:
            try:
                nodemap['AcquisitionFrameRateEnable'].value = True
                nodemap['AcquisitionFrameRate'].value = float(fps_limit)
            except Exception as e:
                log.warning(f"Could not set AcquisitionFrameRate to {fps_limit}: {e}; running at max fps")
                nodemap['AcquisitionFrameRateEnable'].value = False
        else:
            nodemap['AcquisitionFrameRateEnable'].value = False

        if cfg.get('auto_exposure', True):
            nodemap['ExposureAuto'].value = 'Continuous'
            # ExposureAutoLimitAuto=Continuous is the Lucid-specific node that
            # automatically caps auto-exposure time to fit the current frame
            # rate — cleaner than computing a manual limit.
            try:
                nodemap['ExposureAutoLimitAuto'].value = 'Continuous'
            except Exception:
                pass
        else:
            nodemap['ExposureAuto'].value = 'Off'
            nodemap['ExposureTime'].value = float(cfg.get('lucid_exposure_us', 2000))

        self._w = int(nodemap['Width'].value)
        self._h = int(nodemap['Height'].value)

        # --- TL stream nodemap (host-side transport settings) ---
        # Set BEFORE start_stream.
        tl = self._device.tl_stream_nodemap
        try:
            tl['StreamAutoNegotiatePacketSize'].value = True
        except Exception:
            log.warning("StreamAutoNegotiatePacketSize not available")
        try:
            tl['StreamPacketResendEnable'].value = True
        except Exception:
            log.warning("StreamPacketResendEnable not available")

        # Raise Windows timer resolution to 1ms before starting the stream.
        # The default Windows scheduler tick is 15.625ms (64Hz). arena_api's
        # get_buffer() uses a Windows wait internally, so without this it
        # rounds up to the nearest 15.625ms tick even when a frame is already
        # queued — capping effective fps at ~63 regardless of camera settings.
        # ArenaView sets this automatically as a native Windows app.
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
                self._timer_period_set = True
                log.info("Windows timer resolution set to 1ms")
            except Exception:
                self._timer_period_set = False
        else:
            self._timer_period_set = False

        # 10 buffers lets the camera pre-fill the queue so get_buffer()
        # returns immediately rather than blocking for the next frame.
        self._device.start_stream(10)

        # Log camera state so we can see what's actually configured.
        def _node_val(nm, name):
            try:
                return nm[name].value
            except Exception:
                return "N/A"

        actual_fps = _node_val(nodemap, 'AcquisitionFrameRate')
        log.info(f"Lucid Arena capture open: {self._w}x{self._h} "
                 f"device={_node_val(nodemap, 'DeviceModelName')}")
        log.info(f"  AcquisitionFrameRateEnable : {_node_val(nodemap, 'AcquisitionFrameRateEnable')}")
        log.info(f"  AcquisitionFrameRate       : {actual_fps}")
        log.info(f"  AcquisitionMode            : {_node_val(nodemap, 'AcquisitionMode')}")
        log.info(f"  ExposureAuto               : {_node_val(nodemap, 'ExposureAuto')}")
        log.info(f"  ExposureTime               : {_node_val(nodemap, 'ExposureTime')} µs")
        log.info(f"  GevSCPSPacketSize          : {_node_val(nodemap, 'GevSCPSPacketSize')}")
        log.info(f"  GevSCPD                    : {_node_val(nodemap, 'GevSCPD')}")
        log.info(f"  StreamAutoNegotiatePacketSize : {_node_val(tl, 'StreamAutoNegotiatePacketSize')}")
        log.info(f"  StreamPacketResendEnable   : {_node_val(tl, 'StreamPacketResendEnable')}")
        try:
            self._fps = float(actual_fps)
        except Exception:
            self._fps = float(cfg['target_fps'])

    def read(self) -> tuple[bool, np.ndarray | None]:
        try:
            buffer = self._device.get_buffer(timeout=1000)
            if buffer.is_incomplete:
                self._device.requeue_buffer(buffer)
                return False, None
            # buffer.pdata is a ctypes POINTER(uint8_t). buffer.data slices it
            # into a Python list of N ints before building the numpy array —
            # for a 720x540 frame that's 388,800 Python objects, which takes
            # ~15ms. ctypes.string_at does a direct memcpy instead (~0.3ms).
            import ctypes
            n = self._h * self._w
            raw = ctypes.string_at(buffer.pdata, n)
            self._device.requeue_buffer(buffer)
            mono = np.frombuffer(raw, dtype=np.uint8).reshape(self._h, self._w)
            bgr = cv2.cvtColor(mono, cv2.COLOR_GRAY2BGR)
            return True, bgr
        except Exception as e:
            self._log.warning(f"Lucid buffer read failed: {e}")
            return False, None

    def release(self):
        try:
            self._device.stop_stream()
            self._arena.destroy_device(self._device)
        except Exception:
            pass
        if getattr(self, '_timer_period_set', False):
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Capture abstraction
# ---------------------------------------------------------------------------

def open_capture(cfg: dict, log: logging.Logger):
    """
    Returns an object with .read() -> (ok, BGR frame) and .release().
    For USB/file sources: cv2.VideoCapture.
    For Lucid GigE cameras: LucidCapture (set capture_backend: "lucid").
    """
    src = cfg["source"]

    backend_name = (cfg.get("capture_backend") or "auto").lower()

    # Lucid GigE path — source index is ignored; camera is found via arena_api.
    if backend_name == "lucid":
        cap = LucidCapture(cfg, log)
        ok, test_frame = cap.read()
        if not ok or test_frame is None:
            log.error("Lucid camera opened but failed to deliver a frame. "
                      "Check IP config, cable, and jumbo frame MTU setting.")
            cap.release()
            sys.exit(2)
        return cap

    # Pick a backend. Auto picks the best native backend per platform:
    # DSHOW on Windows, V4L2 on Linux (avoids GStreamer pipeline confusion),
    # ANY elsewhere (lets OpenCV decide).

    if sys.platform.startswith("win"):
        auto_backend = cv2.CAP_DSHOW
    elif sys.platform.startswith("linux"):
        auto_backend = cv2.CAP_V4L2
    else:
        auto_backend = cv2.CAP_ANY
    backend_map = {
        "auto": auto_backend,
        "dshow": cv2.CAP_DSHOW,
        "msmf": cv2.CAP_MSMF,
        "v4l2": cv2.CAP_V4L2,
        "any": cv2.CAP_ANY,
    }
    backend = backend_map.get(backend_name, cv2.CAP_ANY)
    log.info(f"Opening capture source={src} backend={backend_name}")

    cap = cv2.VideoCapture(src, backend) if isinstance(src, int) else cv2.VideoCapture(src)
    if not cap.isOpened():
        log.error(f"Could not open capture source: {src} with backend={backend_name}. "
                  f"Try --source N for a different index, or set "
                  f"capture_backend in config.json to 'v4l2' (Linux) / 'dshow' or 'msmf' (Windows).")
        sys.exit(2)

    # Set FOURCC FIRST - some backends only honor framerate after the format
    # is locked in. MJPG is needed for >60fps over USB on most webcams.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["frame_width"])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["frame_height"])
    cap.set(cv2.CAP_PROP_FPS, cfg["target_fps"])

    # Exposure control. Auto-exposure is the #1 cause of "asked for 120fps,
    # got 60fps" on USB cameras: in dim light the driver silently uses long
    # exposures that cap the achievable framerate.
    #
    # The Windows DSHOW backend uses non-obvious magic values:
    #   CAP_PROP_AUTO_EXPOSURE: 0.75 = auto, 0.25 = manual
    #   CAP_PROP_EXPOSURE: log2(seconds), so -7 ≈ 1/128s
    # Linux v4l2 uses different values (1=manual, 3=auto for AUTO_EXPOSURE).
    if not cfg.get("auto_exposure", True):
        if sys.platform.startswith("win"):
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
        else:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
        cap.set(cv2.CAP_PROP_EXPOSURE, cfg.get("manual_exposure", -7))
        log.info(f"Manual exposure set to {cfg.get('manual_exposure', -7)} "
                 f"(log2 seconds; ~1/{int(2 ** -cfg.get('manual_exposure', -7))}s)")

    # Verify the capture is actually producing frames. Some backends "open"
    # but never deliver a frame - we want to fail loudly here, not later in
    # the main loop where it would look like a different bug.
    ok, test_frame = cap.read()
    if not ok or test_frame is None:
        log.error(f"Capture opened but failed to read first frame from source={src} "
                  f"backend={backend_name}. Camera may be in use by another process, "
                  f"or backend may be incompatible. Try a different backend in config.json.")
        cap.release()
        sys.exit(2)

    # Report what we actually got. The driver may have silently substituted.
    # Wrap in try because some backends don't implement getBackendName().
    actual_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    fourcc_str = "".join(chr((actual_fourcc >> 8 * i) & 0xFF) for i in range(4))
    try:
        backend_str = cap.getBackendName()
    except cv2.error:
        backend_str = backend_name
    log.info(f"Capture open: {cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x"
             f"{cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f} @ "
             f"{cap.get(cv2.CAP_PROP_FPS):.1f}fps "
             f"format={fourcc_str} backend={backend_str}")
    return cap  # cv2.VideoCapture


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------

@dataclass
class Track:
    """Lightweight nearest-neighbor tracker for a single moving blob."""
    id: int
    history: deque = field(default_factory=lambda: deque(maxlen=20))  # (t, x, y)
    last_seen: float = 0.0
    hits: int = 0          # number of detections matched to this track
    crossed: bool = False  # already counted, don't double-count

    @property
    def position(self) -> tuple[float, float]:
        return self.history[-1][1], self.history[-1][2]

    @property
    def velocity(self) -> tuple[float, float]:
        """Pixels per second, averaged over recent history."""
        if len(self.history) < 2:
            return (0.0, 0.0)
        t0, x0, y0 = self.history[0]
        t1, x1, y1 = self.history[-1]
        dt = t1 - t0
        if dt <= 0:
            return (0.0, 0.0)
        return ((x1 - x0) / dt, (y1 - y0) / dt)


class SimpleTracker:
    """
    Greedy nearest-neighbor tracker. For a static-background, sparse-blob
    scenario like this it's plenty. Anything fancier (Kalman, IoU tracker)
    is overkill until you actually need it.
    """

    def __init__(self, max_dist_px: float = 150.0, max_age_s: float = 0.3):
        self.tracks: dict[int, Track] = {}
        self.next_id = 1
        self.max_dist = max_dist_px
        self.max_age = max_age_s

    def update(self, detections: list[tuple[float, float]], t: float,
               birth_exclusion_np=None) -> list[Track]:
        # Prune dead tracks
        for tid in list(self.tracks):
            if t - self.tracks[tid].last_seen > self.max_age:
                del self.tracks[tid]

        # Greedy match: for each detection, find nearest track
        unmatched_dets = list(range(len(detections)))
        for tid, tr in list(self.tracks.items()):
            if not unmatched_dets:
                break
            tx, ty = tr.position
            best_idx, best_d = -1, float("inf")
            for i in unmatched_dets:
                dx = detections[i][0] - tx
                dy = detections[i][1] - ty
                d = (dx * dx + dy * dy) ** 0.5
                if d < best_d:
                    best_d, best_idx = d, i
            if best_idx >= 0 and best_d <= self.max_dist:
                x, y = detections[best_idx]
                tr.history.append((t, x, y))
                tr.last_seen = t
                tr.hits += 1
                unmatched_dets.remove(best_idx)

        # Spawn new tracks for leftover detections — skip any born inside the
        # goalie zone (those are goalie body movement, not pucks).
        # Existing tracks that travel INTO the goalie zone are unaffected.
        for i in unmatched_dets:
            x, y = detections[i]
            if birth_exclusion_np is not None:
                if cv2.pointPolygonTest(birth_exclusion_np, (float(x), float(y)), False) >= 0:
                    continue
            tr = Track(id=self.next_id)
            tr.history.append((t, x, y))
            tr.last_seen = t
            tr.hits = 1
            self.tracks[self.next_id] = tr
            self.next_id += 1

        return list(self.tracks.values())


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def side_of_line(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Signed area; >0 on one side, <0 on the other, 0 on the line."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def crossed_into_net(prev: tuple[float, float], curr: tuple[float, float],
                     line_a: tuple[float, float], line_b: tuple[float, float],
                     net_centroid: tuple[float, float]) -> bool:
    """
    Returns True iff the segment (prev -> curr) crosses the goal line
    moving toward the net side. The net_centroid disambiguates which side
    counts as 'into the net'.
    """
    s_prev = side_of_line(prev, line_a, line_b)
    s_curr = side_of_line(curr, line_a, line_b)
    if s_prev * s_curr >= 0:
        return False  # didn't cross (or grazed)
    s_net = side_of_line(net_centroid, line_a, line_b)
    # We crossed if curr ended up on the same side as the net centroid.
    return (s_curr > 0) == (s_net > 0)


def polygon_centroid(poly: list[list[int]]) -> tuple[float, float]:
    arr = np.array(poly, dtype=np.float64)
    return float(arr[:, 0].mean()), float(arr[:, 1].mean())


# ---------------------------------------------------------------------------
# WebSocket client (runs in its own thread)
# ---------------------------------------------------------------------------

class WSClient:
    """
    Connects out to the receiver/Node server. Auto-reconnects on drop.
    Outbound messages go through `send_queue` (thread-safe).
    Inbound messages (e.g. shot_incoming) go through `recv_queue`.
    """

    def __init__(self, url: str, net_id: str, log: logging.Logger):
        self.url = url
        self.net_id = net_id
        self.log = log
        self.send_queue: queue.Queue = queue.Queue()
        self.recv_queue: queue.Queue = queue.Queue()
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def send(self, msg: dict):
        msg.setdefault("net_id", self.net_id)
        msg.setdefault("ts", time.time())
        self.send_queue.put(msg)

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=10, ping_timeout=10) as ws:
                    self.log.info(f"WebSocket connected to {self.url}")
                    self.connected.set()
                    backoff = 1.0
                    # Identify ourselves
                    await ws.send(json.dumps({
                        "type": "hello", "net_id": self.net_id, "ts": time.time()
                    }))
                    await asyncio.gather(
                        self._sender(ws),
                        self._receiver(ws),
                    )
            except Exception as e:
                self.connected.clear()
                if self._stop.is_set():
                    break
                self.log.warning(f"WebSocket disconnected: {e}. Reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.7, 10.0)

    async def _sender(self, ws):
        loop = asyncio.get_event_loop()
        while not self._stop.is_set():
            try:
                msg = await loop.run_in_executor(None, self.send_queue.get, True, 0.2)
            except queue.Empty:
                continue
            if msg is None:
                continue
            await ws.send(json.dumps(msg))

    async def _receiver(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                self.log.warning(f"Bad JSON from server: {raw!r}")
                continue
            self.recv_queue.put(msg)


# ---------------------------------------------------------------------------
# Socket.IO client (game server — port 3000)
# ---------------------------------------------------------------------------

class SIOClient:
    """
    Socket.IO client for the game server.
    Receives shot:incoming events; emits shot:response for goal/no_goal.
    Same queue interface as WSClient so the detector loop is unchanged.
    """

    def __init__(self, url: str, net_id: str, log: logging.Logger):
        self.url = url
        self.net_id = net_id
        self.log = log
        self.send_queue: queue.Queue = queue.Queue()
        self.recv_queue: queue.Queue = queue.Queue()
        self.connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def send(self, msg: dict):
        self.send_queue.put(msg)

    def _run(self):
        asyncio.run(self._main())

    async def _main(self):
        try:
            import socketio as sio_module
        except ImportError:
            self.log.error("python-socketio not installed. Run: uv sync")
            return

        backoff = 1.0
        while not self._stop.is_set():
            sio = sio_module.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
            disconnected = asyncio.Event()

            @sio.event
            async def connect():
                self.connected.set()
                self.log.info(f"Socket.IO connected to {self.url}")

            @sio.event
            async def disconnect():
                self.connected.clear()
                disconnected.set()
                self.log.warning("Socket.IO disconnected")

            @sio.on("shot:incoming")
            async def on_shot_incoming(data=None):
                ts = data.get("timestamp") if isinstance(data, dict) else int(time.time())
                self.recv_queue.put({"type": "shot_incoming", "timestamp": ts})

            try:
                await sio.connect(self.url)
                backoff = 1.0
                await asyncio.gather(
                    self._sender(sio, disconnected),
                    sio.wait(),
                    return_exceptions=True,
                )
            except Exception as e:
                if self._stop.is_set():
                    break
                self.log.warning(f"Socket.IO disconnected: {e}. Reconnecting in {backoff:.1f}s")
            finally:
                self.connected.clear()
                with contextlib.suppress(Exception):
                    await sio.disconnect()

            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 10.0)

    async def _sender(self, sio, disconnected: asyncio.Event):
        loop = asyncio.get_event_loop()
        while not self._stop.is_set() and not disconnected.is_set():
            try:
                msg = await loop.run_in_executor(None, self.send_queue.get, True, 0.2)
            except queue.Empty:
                continue
            if msg is None:
                continue
            result = "goal" if msg.get("type") == "goal" else "save"
            try:
                await sio.emit("shot:response", {
                    "timestamp": msg.get("shot_ts") or int(time.time()),
                    "payload": {"result": result},
                })
            except Exception as e:
                self.log.warning(f"Socket.IO emit failed: {e}")


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

@dataclass
class DetectorState:
    armed_until: float = 0.0       # epoch time; if > now, we're in the armed window
    last_goal_emit: float = 0.0
    always_on: bool = False
    pending_shot_ts: Optional[int] = None  # timestamp from the game server's shot:incoming
    # Pending goal confirmation (set when a line crossing is detected; resolved after confirm window)
    pending_goal_msg: Optional[dict] = None
    pending_goal_until: float = 0.0
    pending_goal_crossing_speed: float = 0.0
    pending_goal_ever_in_net: bool = False
    pending_goal_cancelled: bool = False
    debug_crossing_t: float = 0.0  # time of last crossing regardless of armed state (debug only)


def run_detector(cfg: dict, ws: "SIOClient", debug: bool, log: logging.Logger,
                 admin_ws: Optional[WSClient] = None) -> None:
    if cfg["goal_line"] is None or cfg["net_roi"] is None:
        log.error("No calibration found. Run with --calibrate first.")
        sys.exit(3)

    def _send(msg: dict) -> None:
        if msg.get("type") in ("goal", "no_goal"):
            ws.send(msg)
        if admin_ws is not None:
            admin_ws.send(dict(msg))

    line_a = tuple(cfg["goal_line"][0])
    line_b = tuple(cfg["goal_line"][1])
    net_centroid = polygon_centroid(cfg["net_roi"])
    net_roi_np = np.array(cfg["net_roi"], dtype=np.int32)

    cap = open_capture(cfg, log)
    bg = cv2.createBackgroundSubtractorMOG2(
        history=cfg["bg_history"],
        varThreshold=cfg["bg_var_threshold"],
        detectShadows=cfg["detect_shadows"],
    )

    # Two separate kernels:
    #   - open kernel: small, removes single-pixel speckle noise
    #   - close kernel: LARGE, merges nearby fragments of the same moving object
    #     into one blob. This is the key to handling fast-moving objects that
    #     fragment in the foreground mask.
    def _odd(n: int) -> int:
        return n if n % 2 == 1 else n + 1
    open_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (_odd(cfg["morph_open_size"]), _odd(cfg["morph_open_size"])))
    close_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (_odd(cfg["morph_close_size"]), _odd(cfg["morph_close_size"])))

    tracker = SimpleTracker(
        max_dist_px=cfg["tracker_max_dist_px"],
        max_age_s=cfg["tracker_max_age_s"],
    )
    state = DetectorState(always_on=cfg["always_on_default"])

    # Resolve verbose-logging config once. The values are accessed every frame
    # so a local-variable lookup is cheaper than dict lookup.
    verbose_contours = bool(cfg.get("verbose_contour_logging", False))
    verbose_margin = float(cfg.get("verbose_contour_log_margin", 0.5))
    verbose_min_interval = 1.0 / max(0.1, float(cfg.get("verbose_log_throttle_hz", 5)))
    verbose_log_lo = cfg["puck_min_area"] * (1.0 - verbose_margin)
    verbose_log_hi = cfg["puck_max_area"] * (1.0 + verbose_margin)
    last_verbose_log_t = 0.0
    if verbose_contours:
        log.info(f"Verbose contour logging ENABLED: logging accepts and "
                 f"rejects with area in [{verbose_log_lo:.0f}, {verbose_log_hi:.0f}] "
                 f"px (full-res), throttled to {cfg.get('verbose_log_throttle_hz', 5)}/s")

    method = cfg.get("detection_method", "motion").lower()
    if method not in ("motion", "dark", "combined"):
        log.warning(f"Unknown detection_method '{method}', falling back to 'motion'")
        method = "motion"
    use_motion = method in ("motion", "combined")
    use_dark = method in ("dark", "combined")
    dark_threshold = int(cfg.get("dark_threshold", 60))
    log.info(f"Detection method: {method} "
             f"(motion={use_motion}, dark={use_dark}, dark_threshold={dark_threshold})")

    # Mask for ROI - we want to detect activity in the net AND in the approach
    # zone in front of the goal line. The approach zone is critical because the
    # goal-cross logic compares each track's previous position to its current
    # position to detect a crossing - if the puck's first detection is already
    # inside the net (because the approach was outside the mask), there's no
    # "before" position to compare to and the goal is missed.
    #
    # We build the approach zone by reflecting the net polygon across the goal
    # line. This automatically scales to the camera's perspective: a tight
    # close-up view gets a small approach zone, a wider view gets a larger one,
    # and they're always the same size as the net itself.
    h, w = cfg["frame_height"], cfg["frame_width"]
    detection_mask = np.zeros((h, w), dtype=np.uint8)

    def _reflect_point_across_line(p, a, b):
        """Reflect point p across the line through a and b."""
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        denom = dx * dx + dy * dy
        if denom == 0:
            return p
        # t = how far along the line segment the foot of the perpendicular falls
        t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / denom
        # foot = closest point on the line to p
        fx = ax + t * dx
        fy = ay + t * dy
        # reflection = p mirrored across that foot
        return (int(2 * fx - p[0]), int(2 * fy - p[1]))

    # Approach zone: use the manually-drawn polygon from calibration if present,
    # otherwise fall back to reflecting the net polygon across the goal line.
    if cfg.get("approach_roi"):
        approach_poly = np.array(cfg["approach_roi"], dtype=np.int32)
        log.info(f"Detection mask: net polygon ({len(net_roi_np)} verts) + "
                 f"manual approach zone ({len(approach_poly)} verts)")
    else:
        approach_poly = np.array(
            [_reflect_point_across_line(tuple(p), line_a, line_b)
             for p in cfg["net_roi"]],
            dtype=np.int32,
        )
        log.info(f"Detection mask: net polygon ({len(net_roi_np)} verts) + "
                 f"reflected approach zone ({len(approach_poly)} verts) "
                 f"[re-run --calibrate to draw approach zone manually]")

    # Back zone: veto region behind the net. If a puck enters here after crossing
    # the goal line it means it went over rather than into the net.
    back_roi_np: Optional[np.ndarray] = None
    if cfg.get("back_roi"):
        back_roi_np = np.array(cfg["back_roi"], dtype=np.int32)
        log.info(f"Back zone calibrated ({len(back_roi_np)} verts) — over-net veto active")
    else:
        log.info("No back_roi calibrated — over-net veto disabled (re-run --calibrate to add)")

    # Goalie zone: birth exclusion zone. New tracks whose first detection is
    # inside this polygon are suppressed (goalie body movement). Tracks that
    # originated outside and travel through are unaffected.
    goalie_roi_np: Optional[np.ndarray] = None
    if cfg.get("goalie_roi") and cfg.get("goalie_roi_enabled", True):
        goalie_roi_np = np.array(cfg["goalie_roi"], dtype=np.int32)
        log.info(f"Goalie zone calibrated ({len(goalie_roi_np)} verts) — birth suppression active")
    else:
        log.info("Goalie zone birth suppression disabled"
                 + (" (re-run --calibrate to add)" if not cfg.get("goalie_roi") else " (goalie_roi_enabled=false)"))

    # Build the combined mask: net interior + approach zone + back zone (if present).
    # We also include a thin band ON the line itself to ensure no gap if the
    # two polygons don't quite meet (numerical edge case).
    cv2.fillPoly(detection_mask, [net_roi_np], 255)
    cv2.fillPoly(detection_mask, [approach_poly], 255)
    cv2.line(detection_mask, line_a, line_b, 255, thickness=10)
    if back_roi_np is not None:
        cv2.fillPoly(detection_mask, [back_roi_np], 255)

    # Pre-compute the scaled-down detection mask + dimensions so we don't
    # rebuild them every frame.
    scale = float(cfg["process_scale"])
    if scale <= 0 or scale > 1.0:
        log.warning(f"process_scale={scale} out of range; clamping to 1.0")
        scale = 1.0
    proc_w = max(1, int(w * scale))
    proc_h = max(1, int(h * scale))
    if scale < 1.0:
        detection_mask_proc = cv2.resize(detection_mask, (proc_w, proc_h),
                                         interpolation=cv2.INTER_NEAREST)
        log.info(f"CV pipeline runs at {proc_w}x{proc_h} (scale={scale}); "
                 f"display/calibration at {w}x{h}")
    else:
        detection_mask_proc = detection_mask
    inv_scale = 1.0 / scale

    last_fps_t = time.time()
    frames_since = 0
    fps_smoothed = 0.0
    last_heartbeat = 0.0
    tracks = []
    consecutive_fails = 0

    log.info(f"Detector running. Mode: {'always-on' if state.always_on else 'armed-only'}. "
             f"Press 'a' to toggle armed/always-on, 'd' to toggle debug, 'q' to quit.")

    _WIN_MAIN = "goal_detector (q=quit, a=toggle mode, d=hide debug)"
    _WIN_MASK = "foreground mask"
    if debug:
        cv2.namedWindow(_WIN_MAIN, cv2.WINDOW_NORMAL)
        cv2.namedWindow(_WIN_MASK, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(_WIN_MAIN, w, h)
        cv2.resizeWindow(_WIN_MASK, w, h)

    # Per-stage timing accumulators. Reset and reported every second alongside fps.
    t_capture_acc = 0.0
    t_cv_acc = 0.0
    t_display_acc = 0.0
    t_frames = 0

    try:
        while True:
            t_a = time.perf_counter()
            ok, frame = cap.read()
            t_b = time.perf_counter()
            now = time.time()

            if not ok:
                consecutive_fails += 1
                log.warning(f"Camera read failed (attempt {consecutive_fails}); retrying...")
                # Keep sending heartbeats so the browser doesn't show stale
                if now - last_heartbeat >= 1.0:
                    last_heartbeat = now
                    _send({
                        "type": "heartbeat",
                        "fps": 0.0,
                        "mode": "always_on" if state.always_on else "armed",
                        "armed": False,
                        "tracks": 0,
                    })
                if consecutive_fails >= 10:
                    log.warning("10 consecutive failures; reinitializing camera...")
                    try:
                        cap.release()
                    except Exception:
                        pass
                    time.sleep(2.0)
                    try:
                        cap = open_capture(cfg, log)
                        bg = cv2.createBackgroundSubtractorMOG2(
                            history=cfg["bg_history"],
                            varThreshold=cfg["bg_var_threshold"],
                            detectShadows=cfg["detect_shadows"],
                        )
                        tracker = SimpleTracker(
                            max_dist_px=cfg["tracker_max_dist_px"],
                            max_age_s=cfg["tracker_max_age_s"],
                        )
                        tracks = []
                        consecutive_fails = 0
                        log.info("Camera reinitialized successfully")
                    except Exception as e:
                        log.error(f"Camera reinitialization failed: {e}; will retry")
                else:
                    time.sleep(0.05)
                continue

            consecutive_fails = 0

            # ---- Process inbound WS messages (game server + admin panel) ----
            try:
                while True:
                    msg = ws.recv_queue.get_nowait()
                    handle_inbound(msg, state, cfg, log)
            except queue.Empty:
                pass
            if admin_ws is not None:
                try:
                    while True:
                        msg = admin_ws.recv_queue.get_nowait()
                        handle_inbound(msg, state, cfg, log)
                except queue.Empty:
                    pass

            # ---- Detection ----
            # Downscale before CV. All MOG2/threshold/morph/contour ops run at
            # the smaller resolution; results are scaled back up to full-res
            # coordinates so they line up with the calibration data.
            if scale < 1.0:
                proc_frame = cv2.resize(frame, (proc_w, proc_h),
                                        interpolation=cv2.INTER_AREA)
            else:
                proc_frame = frame

            # --- Motion detection (MOG2) ---
            # We run MOG2 even in 'dark' mode because it needs to keep updating
            # its background model so that if you switch modes at runtime the
            # model is fresh. Cost is ~2-3ms; if perf becomes critical this
            # could be skipped when use_motion=False.
            motion_mask = bg.apply(proc_frame)
            if use_motion:
                # Threshold at fg_threshold drops shadow pixels (MOG2 marks
                # them with value 127 when detect_shadows is on).
                _, motion_mask = cv2.threshold(motion_mask, cfg["fg_threshold"],
                                               255, cv2.THRESH_BINARY)

            # --- Dark detection (brightness threshold) ---
            # Anything darker than dark_threshold becomes white in dark_mask.
            # For a black puck on a brighter background, this isolates the puck
            # without being fooled by shadows (which are darker than background
            # but typically not as dark as the puck itself).
            if use_dark:
                gray = cv2.cvtColor(proc_frame, cv2.COLOR_BGR2GRAY)
                _, dark_mask = cv2.threshold(gray, dark_threshold, 255,
                                             cv2.THRESH_BINARY_INV)

            # --- Combine into final foreground mask ---
            if use_motion and use_dark:
                fg = cv2.bitwise_and(motion_mask, dark_mask)
            elif use_dark:
                fg = dark_mask
            else:
                fg = motion_mask

            # Apply ROI mask (net + approach zone)
            fg = cv2.bitwise_and(fg, fg, mask=detection_mask_proc)

            # Order matters: open first (drop speckle), then close with a BIG
            # kernel (merge fragments of the same fast-moving object).
            fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, open_kernel)
            fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, close_kernel)

            contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            detections: list[tuple[float, float]] = []
            # When debug is on, also collect rejected contours so we can
            # visualize them and tune the area thresholds. Each entry:
            #   (x, y, w, h, area, passed) - all in FULL-RES coordinates.
            debug_contours: list[tuple[int, int, int, int, float, bool]] = []

            # Puck area thresholds are specified in FULL-RES pixels (config),
            # but contour areas are in processed-image pixels. Convert the
            # thresholds once instead of converting every contour.
            area_scale = scale * scale  # 0.25 for scale=0.5
            min_area_proc = cfg["puck_min_area"] * area_scale
            max_area_proc = cfg["puck_max_area"] * area_scale

            for c in contours:
                area_proc = cv2.contourArea(c)
                area_full = area_proc / area_scale  # full-res equivalent
                passed = min_area_proc <= area_proc <= max_area_proc

                # Verbose contour logging: emit info about each contour that
                # passed OR was close enough to the threshold to be interesting.
                # Throttled so a noisy frame can't drown the console.
                if verbose_contours and (passed or verbose_log_lo <= area_full <= verbose_log_hi):
                    if now - last_verbose_log_t >= verbose_min_interval:
                        last_verbose_log_t = now
                        x_v, y_v, w_v, h_v = cv2.boundingRect(c)
                        cx_v = int((x_v + w_v / 2) * inv_scale)
                        cy_v = int((y_v + h_v / 2) * inv_scale)
                        if passed:
                            verdict = "ACCEPT"
                            reason = ""
                        elif area_full < cfg["puck_min_area"]:
                            verdict = "REJECT"
                            reason = f" (below min={cfg['puck_min_area']})"
                        else:
                            verdict = "REJECT"
                            reason = f" (above max={cfg['puck_max_area']})"
                        # Aspect ratio is often the giveaway for goalie body parts
                        # vs pucks - log it so you can spot patterns.
                        long_side = max(w_v, h_v)
                        short_side = max(1, min(w_v, h_v))
                        aspect = long_side / short_side
                        log.info(f"contour {verdict} area={area_full:.0f} "
                                 f"pos=({cx_v},{cy_v}) bbox={int(w_v*inv_scale)}x"
                                 f"{int(h_v*inv_scale)} aspect={aspect:.2f}{reason}")

                if debug:
                    x, y, w_box, h_box = cv2.boundingRect(c)
                    # Scale up to full-res coordinates for display
                    x_f = int(x * inv_scale)
                    y_f = int(y * inv_scale)
                    w_f = int(w_box * inv_scale)
                    h_f = int(h_box * inv_scale)
                    debug_contours.append((x_f, y_f, w_f, h_f, area_full, passed))
                if not passed:
                    continue
                M = cv2.moments(c)
                if M["m00"] == 0:
                    continue
                cx = (M["m10"] / M["m00"]) * inv_scale
                cy = (M["m01"] / M["m00"]) * inv_scale
                detections.append((cx, cy))

            t_c = time.perf_counter()

            tracks = tracker.update(detections, now, birth_exclusion_np=goalie_roi_np)

            # ---- Pending goal resolution ----
            # Runs every frame while a crossing is being confirmed.
            if state.pending_goal_msg is not None:
                # Back-zone veto: any track in back_roi means the puck went over
                if not state.pending_goal_cancelled and back_roi_np is not None:
                    for tr in tracks:
                        px, py = tr.position
                        if cv2.pointPolygonTest(back_roi_np, (float(px), float(py)), False) >= 0:
                            state.pending_goal_cancelled = True
                            log.info(f"GOAL_CANCELLED track={tr.id} entered back zone (puck went over)")
                            break

                if not state.pending_goal_cancelled:
                    # Check whether any track is inside the net
                    for tr in tracks:
                        px, py = tr.position
                        if cv2.pointPolygonTest(net_roi_np, (float(px), float(py)), False) >= 0:
                            state.pending_goal_ever_in_net = True
                            # Early confirm: speed dropped to a fraction of crossing speed
                            vx, vy = tr.velocity
                            speed = (vx * vx + vy * vy) ** 0.5
                            threshold = state.pending_goal_crossing_speed * cfg["goal_speed_confirm_fraction"]
                            if speed < threshold:
                                log.info(f"GOAL_CONFIRMED early track={tr.id} "
                                         f"speed={speed:.0f} < {threshold:.0f} px/s (speed drop)")
                                _send(state.pending_goal_msg)
                                state.last_goal_emit = now
                                if not state.always_on:
                                    state.armed_until = 0.0
                                    state.pending_shot_ts = None
                                state.pending_goal_msg = None
                                break

                # Window expiry
                if state.pending_goal_msg is not None and now >= state.pending_goal_until:
                    if not state.pending_goal_cancelled and state.pending_goal_ever_in_net:
                        log.info("GOAL_CONFIRMED (window expired, puck was in net)")
                        _send(state.pending_goal_msg)
                        state.last_goal_emit = now
                        if not state.always_on:
                            state.armed_until = 0.0
                            state.pending_shot_ts = None
                    else:
                        reason = "back_zone_veto" if state.pending_goal_cancelled else "puck_not_in_net"
                        log.info(f"GOAL_CANCELLED (window expired, reason={reason})")
                        # In armed mode emit no_goal since the shot window is also done
                        if not state.always_on and state.pending_shot_ts is not None:
                            _send({"type": "no_goal", "shot_ts": state.pending_shot_ts,
                                   "reason": reason})
                            state.armed_until = 0.0
                            state.pending_shot_ts = None
                    state.pending_goal_msg = None
                    state.pending_goal_until = 0.0
                    state.pending_goal_crossing_speed = 0.0
                    state.pending_goal_ever_in_net = False
                    state.pending_goal_cancelled = False

            # ---- Goal line crossing detection ----
            looking = state.always_on or (now < state.armed_until)
            if looking and state.pending_goal_msg is None:
                for tr in tracks:
                    if tr.hits < cfg["min_track_hits"]:
                        continue
                    if tr.crossed or len(tr.history) < 2:
                        continue
                    _, px, py = tr.history[-2]
                    _, cx, cy = tr.history[-1]
                    if crossed_into_net((px, py), (cx, cy), line_a, line_b, net_centroid):
                        if now - state.last_goal_emit < cfg["goal_cooldown_seconds"]:
                            continue
                        tr.crossed = True
                        vx, vy = tr.velocity
                        speed_px_s = (vx * vx + vy * vy) ** 0.5
                        confirm_s = cfg["goal_confirm_ms"] / 1000.0
                        log.info(f"GOAL_PENDING track={tr.id} speed_px_s={speed_px_s:.0f} "
                                 f"shot_ts={state.pending_shot_ts} confirm_window={cfg['goal_confirm_ms']}ms")
                        state.pending_goal_msg = {
                            "type": "goal",
                            "track_id": tr.id,
                            "speed_px_s": round(speed_px_s, 1),
                            "shot_ts": state.pending_shot_ts,
                            "mode": "always_on" if state.always_on else "armed",
                        }
                        state.pending_goal_until = now + confirm_s
                        state.pending_goal_crossing_speed = speed_px_s
                        state.pending_goal_ever_in_net = False
                        state.pending_goal_cancelled = False
                        break  # one pending goal at a time

            # ---- Net-appearance goal (require_approach_zone = False) ----
            # When approach zone is not required, a confirmed track whose entire
            # history is inside the net ROI counts as a goal. Handles 5-hole where
            # the puck is fully occluded before the line and never seen approaching.
            if looking and not cfg.get("require_approach_zone", True) and state.pending_goal_msg is None:
                for tr in tracks:
                    if tr.hits < cfg["min_track_hits"] or tr.crossed:
                        continue
                    if now - state.last_goal_emit < cfg["goal_cooldown_seconds"]:
                        continue
                    if all(cv2.pointPolygonTest(net_roi_np, (float(h[1]), float(h[2])), False) >= 0
                           for h in tr.history):
                        tr.crossed = True
                        vx, vy = tr.velocity
                        speed_px_s = (vx * vx + vy * vy) ** 0.5
                        log.info(f"GOAL (net appearance) track={tr.id} speed={speed_px_s:.0f}px/s "
                                 f"shot_ts={state.pending_shot_ts}")
                        _send({
                            "type": "goal",
                            "track_id": tr.id,
                            "speed_px_s": round(speed_px_s, 1),
                            "shot_ts": state.pending_shot_ts,
                            "mode": "always_on" if state.always_on else "armed",
                        })
                        state.last_goal_emit = now
                        if not state.always_on:
                            state.armed_until = 0.0
                            state.pending_shot_ts = None
                        break

            # ---- Debug crossing detection (fires regardless of armed state) ----
            if debug:
                for tr in tracks:
                    if tr.hits < cfg["min_track_hits"]:
                        continue
                    if tr.crossed or len(tr.history) < 2:
                        continue
                    _, px, py = tr.history[-2]
                    _, cx, cy = tr.history[-1]
                    if crossed_into_net((px, py), (cx, cy), line_a, line_b, net_centroid):
                        tr.crossed = True
                        state.debug_crossing_t = now
                        break
                if not cfg.get("require_approach_zone", True):
                    for tr in tracks:
                        if tr.hits < cfg["min_track_hits"] or tr.crossed:
                            continue
                        if all(cv2.pointPolygonTest(net_roi_np, (float(h[1]), float(h[2])), False) >= 0
                               for h in tr.history):
                            tr.crossed = True
                            state.debug_crossing_t = now
                            break

            # ---- Armed-window expiration -> emit explicit no_goal ----
            # Skip if a pending goal is still being confirmed — it will emit
            # no_goal on cancellation if needed.
            if (not state.always_on
                    and state.pending_shot_ts is not None
                    and state.pending_goal_msg is None
                    and now >= state.armed_until
                    and state.armed_until != 0.0):
                log.info(f"NO_GOAL shot_ts={state.pending_shot_ts} (armed window expired)")
                _send({
                    "type": "no_goal",
                    "shot_ts": state.pending_shot_ts,
                    "reason": "window_expired",
                })
                state.armed_until = 0.0
                state.pending_shot_ts = None

            # ---- FPS / heartbeat ----
            frames_since += 1
            if now - last_fps_t >= 1.0:
                fps_smoothed = frames_since / (now - last_fps_t)
                # Per-stage averages in ms
                if t_frames > 0:
                    avg_cap = (t_capture_acc / t_frames) * 1000
                    avg_cv = (t_cv_acc / t_frames) * 1000
                    avg_disp = (t_display_acc / t_frames) * 1000
                    log.debug(
                        f"fps={fps_smoothed:.1f}  "
                        f"capture={avg_cap:.1f}ms  "
                        f"cv={avg_cv:.1f}ms  "
                        f"display={avg_disp:.1f}ms  "
                        f"total={avg_cap + avg_cv + avg_disp:.1f}ms"
                    )
                frames_since = 0
                last_fps_t = now
                t_capture_acc = t_cv_acc = t_display_acc = 0.0
                t_frames = 0
            if now - last_heartbeat >= 1.0:
                last_heartbeat = now
                _send({
                    "type": "heartbeat",
                    "fps": round(fps_smoothed, 1),
                    "mode": "always_on" if state.always_on else "armed",
                    "armed": now < state.armed_until,
                    "tracks": len(tracks),
                })

            # ---- Debug overlay ----
            t_d = time.perf_counter()
            if debug:
                draw_debug(frame, fg, tracks, debug_contours, cfg,
                           line_a, line_b, net_roi_np, approach_poly,
                           state, fps_smoothed, now, back_roi_np, goalie_roi_np)

                # Flash a big GOAL banner if we're within the flash window of
                # the last detected goal. Compares to state.last_goal_emit which
                # is set in the goal-detection path above.
                flash_dur = cfg.get("debug_goal_flash_seconds", 0.5)
                if state.last_goal_emit > 0 and (now - state.last_goal_emit) < flash_dur:
                    _draw_goal_flash(frame)
                if state.debug_crossing_t > 0 and (now - state.debug_crossing_t) < flash_dur:
                    _draw_would_score_flash(frame)

                # Scale down the display to reduce imshow cost on the Pi. The
                # actual frame buffer is unchanged - only the displayed copy
                # is resized. Same for the foreground mask window.
                disp_scale = float(cfg.get("debug_display_scale", 1.0))
                if disp_scale < 1.0 and disp_scale > 0:
                    disp_w = int(frame.shape[1] * disp_scale)
                    disp_h = int(frame.shape[0] * disp_scale)
                    frame_disp = cv2.resize(frame, (disp_w, disp_h),
                                            interpolation=cv2.INTER_AREA)
                    fg_disp = cv2.resize(fg, (disp_w, disp_h),
                                         interpolation=cv2.INTER_NEAREST)
                else:
                    frame_disp = frame
                    fg_disp = fg

                cv2.imshow(_WIN_MAIN, frame_disp)
                cv2.imshow(_WIN_MASK, fg_disp)
                k = cv2.waitKey(1) & 0xFF
                if k == ord('q'):
                    break
                if k == ord('a'):
                    state.always_on = not state.always_on
                    log.info(f"Mode toggled to {'always-on' if state.always_on else 'armed-only'}")
                if k == ord('d'):
                    debug = False
                    cv2.destroyAllWindows()
            t_e = time.perf_counter()

            # Accumulate per-stage timings
            t_capture_acc += (t_b - t_a)
            t_cv_acc += (t_c - t_b)
            t_display_acc += (t_e - t_d)
            t_frames += 1

    finally:
        cap.release()
        cv2.destroyAllWindows()


def handle_inbound(msg: dict, state: DetectorState, cfg: dict, log: logging.Logger) -> None:
    t = msg.get("type")
    if t == "shot_incoming":
        state.armed_until = time.time() + cfg["armed_window_seconds"]
        state.pending_shot_ts = msg.get("timestamp") or int(time.time())
        log.info(f"SHOT_INCOMING ts={state.pending_shot_ts} "
                 f"window={cfg['armed_window_seconds']}s")
    elif t == "set_mode":
        mode = msg.get("mode")
        if mode in ("always_on", "armed"):
            state.always_on = (mode == "always_on")
            log.info(f"Mode set to {mode} via remote command")
    elif t == "ping":
        pass  # ignore; heartbeat covers liveness
    else:
        log.debug(f"Unhandled inbound: {msg}")


def _draw_label(img, text: str, org: tuple[int, int], fg_color, scale: float = 0.7,
                thickness: int = 2, pad: int = 4) -> None:
    """Draw text on a solid black rectangle for readability against any background.

    `org` is the desired top-left of the *background box*. The text is drawn
    inside, with `pad` pixels of padding.
    """
    font = cv2.FONT_HERSHEY_SIMPLEX
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    x, y = org
    h, w = img.shape[:2]
    # Clamp so the box stays inside the frame
    x = max(0, min(x, w - tw - 2 * pad))
    y = max(0, min(y, h - th - baseline - 2 * pad))
    cv2.rectangle(img, (x, y), (x + tw + 2 * pad, y + th + baseline + 2 * pad),
                  (0, 0, 0), -1)
    cv2.putText(img, text, (x + pad, y + pad + th), font, scale, fg_color, thickness,
                cv2.LINE_AA)


def _draw_goal_flash(img) -> None:
    """Draw a big GOAL banner in the top-right corner. Used to announce a
    just-detected goal in the debug view. Drawn at full-frame resolution; gets
    scaled down with the rest of the display if debug_display_scale < 1.0.

    Sized so it remains readable even after a 0.5x downscale.
    """
    h, w = img.shape[:2]
    text = "GOAL"
    font = cv2.FONT_HERSHEY_SIMPLEX
    # Scale based on frame width so the banner is proportional regardless
    # of capture resolution.
    scale = w / 480.0  # gives ~2.6 at 1280px wide
    thickness = max(2, int(scale * 2))
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    pad = int(scale * 12)

    # Top-right placement
    x2 = w - pad
    y1 = pad
    x1 = x2 - tw - 2 * pad
    y2 = y1 + th + baseline + 2 * pad

    # Bright green box, black border for contrast on any background
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), -1)
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 0), 3)
    cv2.putText(img, text, (x1 + pad, y1 + pad + th),
                font, scale, (255, 255, 255), thickness, cv2.LINE_AA)


def _draw_would_score_flash(img) -> None:
    """Yellow 'WOULD SCORE' banner shown when a crossing is detected but the
    detector is not currently armed/looking. Sits below the GOAL flash position."""
    w = img.shape[1]
    text = "WOULD SCORE"
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = w / 640.0
    thickness = max(2, int(scale * 2))
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    pad = int(scale * 10)

    x2 = w - pad
    y1 = pad + int(scale * 80)  # below the GOAL flash position
    x1 = x2 - tw - 2 * pad
    y2 = y1 + th + baseline + 2 * pad

    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 220), -1)  # amber/yellow
    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 0), 3)
    cv2.putText(img, text, (x1 + pad, y1 + pad + th),
                font, scale, (0, 0, 0), thickness, cv2.LINE_AA)


def draw_debug(frame, fg, tracks, debug_contours, cfg,
               line_a, line_b, net_roi_np, approach_poly, state, fps, now,
               back_roi_np=None, goalie_roi_np=None):
    # Static overlays:
    #   - approach zone in cyan (watch zone in front of the goal line)
    #   - net ROI in green (where pucks count as IN)
    #   - back zone in orange (over-net veto region)
    #   - goal line in red (the actual line to cross)
    cv2.polylines(frame, [approach_poly], True, (0, 220, 220), 1)
    cv2.polylines(frame, [net_roi_np], True, (0, 255, 0), 1)
    if back_roi_np is not None:
        cv2.polylines(frame, [back_roi_np], True, (0, 128, 255), 1)
    if goalie_roi_np is not None:
        cv2.polylines(frame, [goalie_roi_np], True, (255, 0, 255), 2)
    cv2.line(frame, line_a, line_b, (0, 0, 255), 2)

    # Pending goal indicator
    if state.pending_goal_msg is not None:
        color = (0, 50, 200) if state.pending_goal_cancelled else (0, 200, 255)
        label = "GOAL CANCELLED" if state.pending_goal_cancelled else "CONFIRMING GOAL..."
        _draw_label(frame, label, (10, 80), color, scale=0.7, thickness=2)

    # Color scheme:
    #   passing contours (within puck size range): cyan box, cyan label
    #   failing contours (too small or too large):  red  box, red  label
    PASS_COLOR = (255, 220, 0)   # cyan-ish in BGR
    FAIL_COLOR = (60, 60, 230)   # red in BGR

    pass_count = 0
    fail_count = 0
    for x, y, w_box, h_box, area, passed in debug_contours:
        color = PASS_COLOR if passed else FAIL_COLOR
        cv2.rectangle(frame, (x, y), (x + w_box, y + h_box), color, 2)
        # Label above the box if there's room, otherwise below
        label = f"{int(area)} px"
        label_y = y - 32 if y > 40 else y + h_box + 4
        _draw_label(frame, label, (x, label_y), color, scale=0.75, thickness=2)
        if passed:
            pass_count += 1
        else:
            fail_count += 1

    # Track trails (drawn on top of contour boxes)
    # A track is "confirmed" once it has enough hits to be trusted for goal logic.
    min_hits = cfg.get("min_track_hits", 2)
    for tr in tracks:
        x, y = tr.position
        confirmed = tr.hits >= min_hits
        if tr.crossed:
            color = (0, 165, 255)         # orange = already counted as goal
        elif confirmed:
            color = (0, 255, 0)           # green = real, trusted track
        else:
            color = (200, 200, 200)       # gray = not enough hits yet (probably noise)

        # Trail
        pts = [(int(p[1]), int(p[2])) for p in tr.history]
        for i in range(1, len(pts)):
            cv2.line(frame, pts[i - 1], pts[i], color, 2 if confirmed else 1)
        # Current position dot (bigger for confirmed)
        cv2.circle(frame, (int(x), int(y)), 8 if confirmed else 4, color, -1)
        # Track id + hit count, near the dot
        _draw_label(frame, f"#{tr.id} hits={tr.hits}",
                    (int(x) + 12, int(y) - 28), color, scale=0.55, thickness=1, pad=3)

    # Top-left status block: mode + fps, large and readable
    armed = now < state.armed_until
    mode_txt = "ALWAYS-ON" if state.always_on else ("ARMED" if armed else "IDLE")
    mode_color = (0, 255, 0) if (state.always_on or armed) else (180, 180, 180)
    _draw_label(frame, f"{mode_txt}   {fps:.0f} fps",
                (10, 10), mode_color, scale=1.0, thickness=2, pad=8)

    # Below status: contour pass/fail counts and current thresholds
    threshold_txt = (f"area: pass={pass_count}  fail={fail_count}   "
                     f"min={cfg['puck_min_area']}  max={cfg['puck_max_area']}")
    _draw_label(frame, threshold_txt, (10, 70), (255, 255, 255),
                scale=0.7, thickness=2, pad=6)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="HHOF goalie sim - goal detector")
    ap.add_argument("--calibrate", action="store_true", help="Run interactive calibration")
    ap.add_argument("--debug", action="store_true", help="Show overlay windows")
    ap.add_argument("--source", type=int, default=None, help="Override webcam source index")
    ap.add_argument("--server", type=str, default=None, help="Override game server URL (Socket.IO, e.g. http://host:3000)")
    ap.add_argument("--net-id", type=str, default=None, help="Override net_id")
    ap.add_argument("--log-level", type=str, default=None,
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                    help="Override log level from config")
    args = ap.parse_args()

    cfg = load_config()
    if args.source is not None:
        cfg["source"] = args.source
    if args.server is not None:
        cfg["server_url"] = args.server
    if args.net_id is not None:
        cfg["net_id"] = args.net_id
    if args.log_level is not None:
        cfg["log_level"] = args.log_level

    log = setup_logging(cfg["log_level"])

    if args.calibrate:
        calibrate(cfg, log)
        return

    # Prevent two detector instances from running simultaneously — they would
    # both try to open the GigE camera and one would get SC_ERR_ACCESS_DENIED.
    if sys.platform == "win32":
        try:
            import ctypes
            _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "hhof-goal-detector-mutex")
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                log.error("Another instance of goal_detector.py is already running. Exiting.")
                sys.exit(1)
        except Exception:
            pass  # non-fatal if mutex check fails

    ws = SIOClient(cfg["server_url"], cfg["net_id"], log)
    ws.start()

    admin_ws = None
    admin_url = cfg.get("admin_server_url")
    if admin_url:
        admin_ws = WSClient(admin_url, cfg["net_id"], log)
        admin_ws.start()

    def shutdown(signum, frame):
        log.info("Shutting down...")
        ws.stop()
        if admin_ws is not None:
            admin_ws.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    run_detector(cfg, ws, args.debug, log, admin_ws)
    ws.stop()
    if admin_ws is not None:
        admin_ws.stop()


if __name__ == "__main__":
    main()
