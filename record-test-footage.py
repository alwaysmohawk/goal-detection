"""
HHOF Goalie Sim - test footage recorder
========================================
Records camera footage to disk for offline analysis with the detector. Shares
config and capture setup with goal_detector.py so the footage matches exactly
what the detector sees - same backend, same resolution, same exposure, same
FOURCC.

Usage:
    python record_test_footage.py                              # 5 min, defaults from config.json
    python record_test_footage.py --duration 60                # 1 minute
    python record_test_footage.py --out angle_top.mp4
    python record_test_footage.py --source 1                   # override camera index
    python record_test_footage.py --no-display                 # headless / max fps

Exposure testing:
    To test the short-exposure hypothesis without editing config.json, set
    auto_exposure=false and manual_exposure=-10 in config and re-run. Or pass
    --exposure to override:
        python record_test_footage.py --exposure -10
        python record_test_footage.py --exposure auto

Notes:
    - Output is MJPG-in-MP4 (falls back to AVI if container is rejected).
      At 120fps 720p that's ~80-200 MB/minute.
    - Per-second log shows live fps, per-stage ms breakdown, and file size.
    - Press 'q' in the preview window to stop early.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

# Reuse the detector's config + capture so the footage matches reality.
# Any config change (exposure, backend, resolution) automatically applies
# to recordings too - one source of truth.
from goal_detector import load_config, setup_logging, open_capture


def main():
    ap = argparse.ArgumentParser(description="Record camera footage for detector replay")
    ap.add_argument("--duration", type=int, default=300,
                    help="Duration in seconds (default 300 = 5 min)")
    ap.add_argument("--out", type=str, default=None,
                    help="Output file. Default: footage_YYYYMMDD_HHMMSS.mp4")
    ap.add_argument("--source", type=int, default=None,
                    help="Override config 'source' (camera index)")
    ap.add_argument("--exposure", type=str, default=None,
                    help="Override exposure: 'auto' or a log2-seconds value like '-10'. "
                         "Without this, uses config.json's auto_exposure/manual_exposure.")
    ap.add_argument("--no-display", action="store_true",
                    help="Don't show preview window (lower CPU, headless-safe)")
    args = ap.parse_args()

    # Load the same config the detector uses
    cfg = load_config()
    log = setup_logging(cfg["log_level"])

    # Apply CLI overrides into the config dict before open_capture() sees it
    if args.source is not None:
        cfg["source"] = args.source
    if args.exposure is not None:
        if args.exposure.lower() == "auto":
            cfg["auto_exposure"] = True
        else:
            try:
                cfg["auto_exposure"] = False
                cfg["manual_exposure"] = int(args.exposure)
            except ValueError:
                log.error(f"--exposure must be 'auto' or an integer, got {args.exposure!r}")
                sys.exit(2)

    log.info(f"Recording config: source={cfg['source']} "
             f"{cfg['frame_width']}x{cfg['frame_height']} @ {cfg['target_fps']}fps "
             f"auto_exposure={cfg['auto_exposure']} "
             f"manual_exposure={cfg.get('manual_exposure') if not cfg['auto_exposure'] else 'n/a'}")

    cap = open_capture(cfg, log)

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    target_fps = cfg["target_fps"]

    # Output path
    out_path = args.out or f"footage_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    out_path = Path(out_path).resolve()

    # MJPG in MP4 container - encoder is nearly passthrough since the camera
    # delivers MJPG already. Falls back to AVI if the OpenCV build rejects
    # MJPG-in-MP4 (varies by FFmpeg build).
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(out_path), fourcc, target_fps, (width, height))
    if not writer.isOpened():
        fallback = out_path.with_suffix(".avi")
        log.warning(f"MP4 writer rejected, falling back to {fallback}")
        writer = cv2.VideoWriter(str(fallback), fourcc, target_fps, (width, height))
        if not writer.isOpened():
            log.error("Could not open video writer in either container")
            cap.release()
            sys.exit(3)
        out_path = fallback

    log.info(f"writing to {out_path}")
    log.info(f"duration {args.duration}s ({args.duration / 60:.1f} min)")
    log.info("press 'q' to stop early" if not args.no_display else "headless; ctrl-c to stop")

    start = time.time()
    last_log = start
    frames = 0
    frames_since_log = 0
    dropped = 0

    # Per-stage timing - same pattern as goal_detector.py so we can compare
    # whether disk write or display is eating frame budget.
    t_capture_acc = 0.0
    t_write_acc = 0.0
    t_display_acc = 0.0
    t_frames = 0

    try:
        while True:
            now = time.time()
            elapsed = now - start
            if elapsed >= args.duration:
                break

            t_a = time.perf_counter()
            ok, frame = cap.read()
            t_b = time.perf_counter()
            if not ok:
                dropped += 1
                if dropped % 30 == 0:
                    log.warning(f"{dropped} failed reads")
                continue

            writer.write(frame)
            t_c = time.perf_counter()

            frames += 1
            frames_since_log += 1

            if not args.no_display:
                preview = cv2.resize(frame, (width // 2, height // 2),
                                     interpolation=cv2.INTER_AREA)
                remaining = args.duration - elapsed
                label = f"{int(elapsed):3d}/{args.duration}s  frames={frames}  remaining={int(remaining)}s"
                cv2.putText(preview, label, (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.imshow("recording (q to stop)", preview)
                if (cv2.waitKey(1) & 0xFF) == ord('q'):
                    log.info("stopped by user")
                    break
            t_d = time.perf_counter()

            t_capture_acc += (t_b - t_a)
            t_write_acc += (t_c - t_b)
            t_display_acc += (t_d - t_c)
            t_frames += 1

            if now - last_log >= 1.0:
                inst_fps = frames_since_log / (now - last_log)
                avg_cap = (t_capture_acc / t_frames) * 1000
                avg_wr = (t_write_acc / t_frames) * 1000
                avg_dp = (t_display_acc / t_frames) * 1000
                size_mb = out_path.stat().st_size / 1e6
                log.info(f"t={elapsed:5.1f}s  fps={inst_fps:5.1f}  "
                         f"capture={avg_cap:.1f}ms  write={avg_wr:.1f}ms  "
                         f"display={avg_dp:.1f}ms  total={avg_cap + avg_wr + avg_dp:.1f}ms  "
                         f"frames={frames}  dropped={dropped}  size={size_mb:.0f}MB")
                last_log = now
                frames_since_log = 0
                t_capture_acc = t_write_acc = t_display_acc = 0.0
                t_frames = 0

    except KeyboardInterrupt:
        log.info("stopped by ctrl-c")

    finally:
        cap.release()
        writer.release()
        if not args.no_display:
            cv2.destroyAllWindows()

        elapsed = time.time() - start
        avg_fps = frames / elapsed if elapsed > 0 else 0
        size_mb = out_path.stat().st_size / 1e6 if out_path.exists() else 0
        log.info(f"done: {frames} frames in {elapsed:.1f}s = {avg_fps:.1f}fps avg")
        log.info(f"      {size_mb:.0f}MB at {out_path}")
        if avg_fps < target_fps * 0.9:
            log.warning(f"avg fps is {avg_fps / target_fps * 100:.0f}% of target {target_fps}fps. "
                        f"Try --no-display, check disk speed, or reduce exposure.")


if __name__ == "__main__":
    main()