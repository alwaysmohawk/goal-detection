# HHOF Goal Detector — Tuning Guide

A practical, symptom-driven guide to dialing in the detector on-site. Written to
be used live, with the `--debug` windows and the admin panel open side by side.

> **Golden rule for this venue:** a false negative (missed goal) is always better
> than a false positive (crediting a save as a goal). When in doubt, tune toward
> *stricter*. A frustrated player who saved a shot and got scored on is the worst
> outcome.

---

## 0. Before you touch anything — set up your feedback loop

You cannot tune what you cannot see. Get these three things in front of you:

1. **Debug windows.** Run the detector with `--debug`. You get two windows:
   - **Main** — the camera frame with ROI polygons, tracks, and goal/WOULD-SCORE flashes.
   - **Mask** — the *foreground mask* (white = what the system thinks is a puck-candidate,
     black = ignored). **This is the window you live in while tuning detection.**
     It shows the mask *after* morphology and the ROI cut — i.e. exactly what becomes contours.
2. **Verbose contour logging.** In the admin panel set `verbose_contour_logging: true`.
   The terminal now prints `contour ACCEPT area=…` / `contour REJECT … (below min=…)`
   for every blob near your area thresholds. This tells you *why* a puck did or didn't count.
   - If it scrolls too fast, lower `verbose_log_throttle_hz` to 1–2.
   - To see *every* contour regardless of size, set `verbose_contour_log_margin` to 10.
3. **The admin panel.** Most knobs are **LIVE** — change + Save applies on the next frame,
   no restart. A handful need a restart (marked **[restart]** below). Know which is which
   so you don't waste time waiting.

**Mental model — the pipeline, in order.** Each stage can only work with what the
previous stage handed it. Tune top-down: there is no point tuning morphology if the
puck never makes it into the mask, and no point tuning the tracker if the blob is the wrong size.

```
Light (exposure)
  → MOG2 motion + dark threshold        → raw mask
  → ROI cut (net + approach + back)      → masked
  → morph OPEN (denoise) then CLOSE (merge) → cleaned mask   ← the "Mask" debug window
  → contour area filter (min/max)        → detections
  → tracker (dist / age / hits)          → confirmed tracks
  → goal logic (crossing / confirm / veto) → GOAL
```

---

## 1. Light — get a usable image first  *(everything downstream depends on this)*

A dark venue starves every later stage. Fix this before anything else.

**`lucid_exposure_us`** *(µs of exposure; **[restart]** — applied at camera open)*
- This is the single biggest lever for a dark room. More µs = brighter image.
- **Hard ceiling: 1 / target_fps.** At `target_fps: 200` your budget is **5000 µs** (1/200 s).
  Go above that and the camera silently drops frames to make room.
- **Turn it up** toward 5000 until the image is bright enough that the puck stands out
  from the surface in the **Main** window. If you hit 5000 and it's still too dark, you've
  run out of room at 200 fps — see the trade below.
- **You've gone too far when** the image washes out, motion-blur smears the puck into a
  long streak (it'll trip `puck_max_area`), or fps starts dropping in the heartbeat.

**The fps ↔ light trade.** If 5000 µs isn't enough:
- Drop `target_fps` to 120 **[restart]**, which raises the exposure ceiling to ~8000 µs.
- 120 fps still catches an 80 mph foam puck fine; the extra light is usually worth it in a dark room.
- Confirm the result in the startup log: it prints `ExposureTime` and `AcquisitionFrameRate`
  with the values the camera actually accepted.

> `auto_exposure: true` is a valid fallback for a dark room — the Lucid path also sets
> `ExposureAutoLimitAuto: Continuous`, which caps auto-exposure so it can't silently tank
> your fps. Use it if you'd rather not hand-pick a number. **[restart]**

---

## 2. Get the puck into the foreground mask  *(work in the Mask window)*

Goal of this stage: when a puck flies through, you see **one clean white blob** travelling
across the Mask window, with **no scattered white noise** in the net/approach area when nothing's moving.

### 2a. Detection method — pick the right detector for your lighting  *(**[restart]**)*

`detection_method`:
- **`motion`** — MOG2 background subtraction only. Best general default.
- **`dark`** — brightness only; anything darker than `dark_threshold` is a candidate.
  Good for a dark puck on a *bright, evenly lit* surface. Bad in a dark venue (everything is "dark").
- **`combined`** — pixel must be **moving AND dark**. Great when shadows are your enemy on a
  bright floor. **In a dark venue this can backfire:** if the floor is nearly as dark as the puck,
  the "dark" half adds nothing and the AND just throws away marginal motion pixels.
  **If you're struggling to see pucks in a dark room, switch to `motion`** and lean on MOG2.

### 2b. MOG2 sensitivity — the main "make pucks appear" knob

**`bg_var_threshold`** *(LIVE)* — how different a pixel must be from the learned background
to count as motion. **Lower = more sensitive = more white in the mask.**
- **Turn it DOWN** (e.g. from 100 → 30 → 20 → 16) until the puck shows up as a solid blob.
- **You've gone too far when** the mask fills with twinkling speckle / the net area lights up
  with noise when nothing is moving. Back off until the background is black again at rest.
- Sweet spot is usually **16–30**. Your current 100 is very high — likely a big part of why
  pucks are faint.

**`fg_threshold`** *(LIVE)* — cut applied to MOG2's output. MOG2 marks hard motion as 255 and
*shadows* as 127. A threshold above 127 deletes shadow pixels.
- **The trap in a dark venue:** MOG2 often misclassifies real puck pixels as "shadow" (127).
  With `fg_threshold: 200` those puck pixels get deleted and your blob comes out hollow or partial.
- **Turn it DOWN** toward 64 to let those marginal pixels through and fill the puck in.
- **You've gone too far when** actual shadows start appearing as blobs and tracking on them.

**`detect_shadows`** *(LIVE)* — toggles MOG2's shadow classification.
- If pucks look like **hollow rings or partial outlines** rather than filled circles, set this
  **`false`**. Everything then becomes a clean 0 or 255, no 127 middle-ground to lose the puck in.
  Quickest single fix for "misshapen / smaller than expected" blobs in a dark room.

**`bg_history`** *(LIVE)* — how many frames MOG2 averages into its background model.
- Mostly leave at 300. Lower it (e.g. 100) if the background changes and you want faster
  adaptation; raise it if a near-stationary background is slowly being "learned" as foreground.

### 2c. Dark threshold *(only matters for `dark` / `combined`)*  *(LIVE)*

**`dark_threshold`** — pixels darker than this value are "dark." Only used by `dark`/`combined`.
- Black puck on a bright floor: keep **low** (40–60) so only the puck qualifies, not shadows.
- If a dark puck isn't registering in `combined`, raise it (80–120) so the puck's pixels pass —
  but watch that shadows don't start passing too.

> **If after all of 2 you still can't see pucks:** the puck may be travelling **outside the ROI**.
> Check the Main window — the green approach/net polygons must cover where pucks actually fly.
> Anything outside is masked to black *before* the Mask window. Re-run `--calibrate` if the
> polygons don't match reality.

---

## 3. Make the blob solid and the right shape  *(morphology — still in the Mask window)*

Morphology runs **after** detection. It cannot invent pixels — if the puck is only half-detected,
fix Stage 2 first. What it *can* do is merge fragments and round off edges.

Order is fixed: **OPEN first (erode→dilate, kills speckle), then CLOSE (dilate→erode, merges & fills).**

**`morph_open_size`** *(LIVE; odd numbers)* — noise eraser.
- **Turn it UP** (3 → 5 → 7) if single-pixel speckle is creating junk detections.
- **You've gone too far when** small or slow pucks start vanishing — open *erodes first*, so a
  big open kernel eats the puck. Keep this **small (3–5)**. When in doubt, denoise with
  `bg_var_threshold` instead and leave open low.

**`morph_close_size`** *(LIVE; odd numbers)* — fragment merger / hole filler.
- A fast puck arrives as several disconnected motion fragments; close stitches them into one
  blob big enough to pass the area filter. It also rounds out and fills the shape.
- **Turn it UP** (13 → 18 → 23 → 25) if the puck shows as 2–3 separate small pieces, or looks
  jagged/hollow, and you want one solid round blob.
- **You've gone too far when** the puck blob merges with nearby noise or with the goalie's
  body/stick into one giant contour — that giant blob trips `puck_max_area` and/or causes
  false tracks. Dial back until puck and body stay separate.

> **Yes — changing these *does* change the Mask window.** It shows the post-morphology mask.
> If turning close *up* doesn't visibly enlarge the blob, the pixels aren't there to merge →
> go back to Stage 2 (exposure / `bg_var_threshold` / `fg_threshold` / `detect_shadows`).
>
> Note kernels are sized in **processed pixels**. With `process_scale: 1` that's 1:1 with the
> image. If you ever drop `process_scale` to 0.5 **[restart]**, halve these numbers to keep the
> same physical effect.

---

## 4. Area filter — accept puck-sized blobs, reject the rest  *(LIVE)*

Now the blob exists and is clean. The area gate decides if it's puck-sized. Use **verbose
contour logging** here — it prints the measured area of each candidate so you tune to real numbers.

**`puck_min_area`** / **`puck_max_area`** *(px², full-resolution equivalent)*
- Watch the log while shooting pucks. You want `ACCEPT area=NNN` on real pucks.
- Seeing `REJECT … (below min=80)` on real pucks → **lower `puck_min_area`** (or, better, go back
  and make the blob bigger via close/exposure). Lowering min too far lets noise in.
- Seeing `REJECT … (above max=4000)` on real pucks → motion blur is smearing the puck into a
  streak. **Raise `puck_max_area`**, or shorten exposure slightly to reduce the smear.
- **You've gone too far on min (too low)** when speckle and small body parts start getting ACCEPTed.
- **You've gone too far on max (too high)** when the goalie's stick/arm/torso gets ACCEPTed as a puck.
- The log also prints aspect ratio — a long thin blob is usually a body part or blur streak, not a puck.

---

## 5. Tracking — turn detections into a followed object  *(LIVE)*

A blob has to be seen across frames and matched into a track before it can score.

**`min_track_hits`** — detections required before a track is "real" and eligible to score.
- **Lower to 1** if very fast pucks are detected for only a frame or two and never "confirm"
  (you see the blob fly through but no goal). More sensitive, but a single noise blob can now score.
- **Raise to 3** if random noise blobs are creating phantom tracks. Stricter, but risks missing
  the fastest shots. **2 is the balanced default.**

**`tracker_max_dist_px`** — max pixels a track may jump between frames and still be matched.
- **Raise** (250 → 300+) if a single fast puck shows as several *separate* short tracks that
  never connect (the puck out-ran the match radius), or if tracks keep breaking across a brief occlusion.
- **You've gone too far when** unrelated blobs get linked into one nonsensical track that
  teleports around the frame.

**`tracker_max_age_s`** — how long a track survives with no new detection before it's dropped.
- **Raise** (0.4 → 0.6) to bridge longer occlusions (e.g. puck briefly hidden by the goalie)
  so the track survives until the puck reappears.
- **You've gone too far when** stale tracks linger and re-link to later, unrelated motion.

---

## 6. Goal logic — decide if a tracked puck is a goal  *(all LIVE)*

This is where the false-positive vs false-negative trade is enforced. Tune conservatively.

**Mode (always-on vs armed).** The **Mode** button / `always_on_default`:
- **Always-on** scores any valid crossing, no `shot:incoming` needed. Good for free testing.
- **Armed** only scores within `armed_window_seconds` after the game server sends a shot.
- *If you see WOULD SCORE flashes but no GOAL:* you're in **armed** mode and not receiving shots.
  Switch to always-on (or send a shot). WOULD SCORE = "this crossing would have scored if armed."

**`goal_confirm_ms`** — after a line crossing, how long to wait and confirm the puck actually
stayed in the net (vs. flying over and out).
- **Raise** (200 → 300) if pucks that go *over* the net are being scored as goals — gives the
  back-zone veto more time to catch them. Costs a touch of latency on real goals.
- **Lower** (toward 0) only if you want near-instant scoring and trust your crossing detection;
  raises false-positive risk on over-the-net shots. Given the venue rule, prefer erring higher.

**`goal_speed_confirm_fraction`** — early-confirm trigger: if the puck slows to this fraction of
its crossing speed within the window, it's assumed to have hit the back of the net → score early.
- 0.25 (25%) is a good default. Lower it (0.15) to require a more definite stop before early-confirming.

**`goal_cooldown_seconds`** — minimum gap between goal events. Prevents one shot double-scoring.
Leave ~0.5 unless legitimate rapid-fire shots are being swallowed.

**`require_approach_zone`** — must the puck be seen in the front-of-net zone before scoring?
- **`true` (default, recommended for this venue):** a puck must travel through the approach zone.
  Rejects "appeared from nowhere inside the net," which includes a goalie's hand/foot/butt entering
  the net. **Safe against false positives.**
- **`false`:** also scores when a puck appears *only* inside the net (catches fully-occluded 5-hole
  goals). **Use only for deliberate 5-hole testing** — it *will* also fire on body parts entering
  the net, which violates the venue's "never steal a save" rule. Watch for `GOAL (net appearance)`
  in the log to see exactly what it's catching, and turn it back off for normal play.

---

## Symptom → first knob to reach for

| What you see | Most likely cause | First move |
|---|---|---|
| Image too dark, puck barely visible | Exposure starved | `lucid_exposure_us` ↑ toward 5000 (or fps ↓ to 120) **[restart]** |
| No blobs at all in Mask window | MOG2 too strict / wrong method | `bg_var_threshold` ↓ to ~20; if on `combined`, switch to `motion` |
| Puck blobs hollow / ring-shaped / partial | Shadow pixels (127) being deleted | `detect_shadows: false`, or `fg_threshold` ↓ to 64 |
| Puck blob smaller/misshapen than expected | Missing pixels (upstream), not morph | Fix exposure / `bg_var_threshold` first, *then* `morph_close_size` ↑ |
| Puck shows as 2–3 separate pieces | Fragments not merging | `morph_close_size` ↑ (18 → 23) |
| Mask full of speckle / phantom detections | MOG2 too sensitive | `bg_var_threshold` ↑; small `morph_open_size` ↑ (3 → 5) |
| Real puck logs `REJECT (below min)` | Blob too small for gate | Make blob bigger (close/exposure), or `puck_min_area` ↓ |
| Real puck logs `REJECT (above max)` | Motion-blur streak | `puck_max_area` ↑, or exposure ↓ slightly **[restart]** |
| Goalie stick/arm scored as puck | Area max too loose / close too big | `puck_max_area` ↓; `morph_close_size` ↓ |
| Fast puck seen but never scores | Too few frames to confirm | `min_track_hits` → 1; `tracker_max_dist_px` ↑ |
| One puck becomes several short tracks | Match radius too small | `tracker_max_dist_px` ↑ (250 → 300) |
| Track breaks during occlusion | Track ages out too fast | `tracker_max_age_s` ↑ (0.4 → 0.6) |
| Over-the-net shots scored as goals | Confirm window too short | `goal_confirm_ms` ↑ (200 → 300); verify back zone calibrated |
| WOULD SCORE flashes but no GOAL | In armed mode, no shots arriving | Switch to always-on (Mode button) |
| 5-hole goals missed | Puck fully occluded before line | `require_approach_zone: false` **(testing only — re-enable after)** |
| Goalie reaching in scores a goal | `require_approach_zone` is false | Set it back to `true` |

---

## Recommended order of operations on a fresh site

1. **Light:** get a clean, bright-enough image (`lucid_exposure_us`, maybe `target_fps`). Confirm in Main window + startup log.
2. **Calibrate:** run `--calibrate`, place all 5 regions accurately. Verify polygons in Main window.
3. **Detection:** with `--debug` + verbose logging, get one clean blob per puck in the Mask window
   (`detection_method`, `bg_var_threshold`, `fg_threshold`, `detect_shadows`).
4. **Shape:** solidify/round the blob (`morph_close_size`, `morph_open_size`).
5. **Area:** confirm `ACCEPT` on real pucks, `REJECT` on noise/body (`puck_min_area`/`puck_max_area`).
6. **Track:** make sure pucks form one followed track end-to-end (`min_track_hits`, `tracker_max_dist_px`, `tracker_max_age_s`).
7. **Goal logic:** confirm clean GOAL on real goals and **nothing** on saves. Tune conservatively
   (`goal_confirm_ms`, `require_approach_zone`). Re-test saves specifically — the venue rule is
   "never steal a save."
8. **Quiet the logs:** set `log_level: INFO` (or `WARNING`), turn off `verbose_contour_logging`.

---

## Which changes need a restart?

**Live (apply on Save, next frame):** `bg_history`, `bg_var_threshold`, `detect_shadows`,
`fg_threshold`, `dark_threshold`, `puck_min_area`, `puck_max_area`, `morph_close_size`,
`morph_open_size`, all Tracking, all Goal Logic, all Diagnostics, `log_level`.

**Restart required:** `detection_method`, `process_scale`, `target_fps`, `lucid_exposure_us`,
`auto_exposure`, `source`, `frame_width`/`frame_height`, and anything in the Camera section
(these are applied once when the camera/CV pipeline is opened).
