"""Find where a clip stops moving, so a continuation never pins a frozen tail.

H3 clips often stop near the end. With a last frame set the model reaches it
early and then holds it; even without one, a shot tends to settle. Whatever the
cause, the last half-second of a clip is frequently the worst possible material
to continue from: pin a freeze at the head of the next clip and the next clip
opens frozen, which reads as the video stalling. It also poisons the chain --
the frozen frames are what the following link pins in turn.

WHY NOT JUST LOOK FOR LOW MOTION. Because H3 DECELERATES as it converges on its
ending, so a plain motion threshold fires during the slow-down and cuts far too
early, throwing away good footage. The distinction that works is not "is this
frame moving" but "has this frame already arrived at the clip's final state":

  1. Build a stable reference for what the ending looks like -- the pixel-wise
     MEDIAN of the last few frames, so one shimmering or oddly-decoded frame
     cannot define it.
  2. A frame is "landed" when it matches that reference closely, both in mean
     error and in how much of the picture differs at all.
  3. A run is a freeze when nearly all of its frames are landed AND its
     frame-to-frame transitions are near-static -- both gates, so a slow drift
     toward the ending is not mistaken for having arrived.

Tolerances rather than absolutes on both gates: a few outliers are allowed, a
long consecutive streak of them is not, because real H3 tails shimmer slightly.

The approach, the two-gate structure and the reasoning behind them are
Herrgotts-H3-Infinite-Continuation-Suite's (motion_analysis.py); the code here
is written independently and the thresholds are ours, but the insight that a
final-state comparison beats a motion threshold is theirs and it is the whole
trick. See the README credit.

Nothing here changes a render. It only reports where a clip stopped moving.
"""

import logging

import torch
import torch.nn.functional as F

# Analysis is done small and slightly blurred: we are asking about the picture,
# not about single-pixel codec noise.
ANALYSIS_EDGE = 192
REFERENCE_FRAMES = 15      # how many trailing frames define "the final state"
ANALYSIS_WINDOW = 96       # how far back to look for the start of the lock

# "landed": close to the final-state reference
LANDED_MEAN = 0.012        # mean |RGB| difference
LANDED_ACTIVE_PIXEL = 0.025   # a pixel counts as changed above this
LANDED_MAX_AREA = 0.03     # at most 3% of the picture may differ
# "static": no residual frame-to-frame motion
STATIC_MEAN = 0.002
STATIC_ACTIVE_PIXEL = 0.010
STATIC_MAX_AREA = 0.01
# tolerance for shimmer
MIN_LANDED = 0.75          # 75% of the run must be landed
MIN_STATIC = 0.70
MAX_LANDED_STREAK = 3      # consecutive misses allowed
MAX_STATIC_STREAK = 2

MIN_FREEZE = 4             # shorter than this is not a freeze, it is an ending

FRAME_PER_TOKEN = (1, 4, 4, 4, 4)


def snap_to_step(frame):
    """The largest latent-step boundary at or before `frame`.

    A handover has to land on one. Anywhere else leaves a few frames that the
    previous clip shows but the continuation never pinned -- and a car entering
    frame during those is a car that vanishes at the cut. Snapping the
    recommendation down means the trim and the pinned run agree exactly.
    """
    acc, best = 0, 0
    for k in range(4096):
        if acc > int(frame):
            break
        best = acc
        acc += FRAME_PER_TOKEN[k % 5]
    return best


def _prepare(frames):
    """[T,H,W,C] in 0..1 -> small, lightly blurred [T,3,h,w]."""
    if frames.ndim != 4 or frames.shape[-1] < 3:
        raise ValueError("expected [T,H,W,C>=3], got %s" % (tuple(frames.shape),))
    x = frames[..., :3].detach().float().movedim(-1, 1)
    h, w = int(x.shape[-2]), int(x.shape[-1])
    scale = min(1.0, ANALYSIS_EDGE / float(max(h, w)))
    th, tw = max(16, int(round(h * scale))), max(16, int(round(w * scale)))
    if (th, tw) != (h, w):
        x = F.interpolate(x, size=(th, tw), mode="bilinear",
                          align_corners=False, antialias=True)
    # a 3x3 mean kills shimmer without touching real subject motion
    return F.avg_pool2d(x, kernel_size=3, stride=1, padding=1)


def _worst_streak(flags):
    worst = run = 0
    for f in flags:
        run = 0 if f else run + 1
        worst = max(worst, run)
    return worst


def analyse(frames):
    """Where does this clip stop moving?

    `frames` is an IMAGE tensor [T,H,W,C] in 0..1.

    Returns a dict:
        frozen          bool
        freeze_start    first frame of the locked run (-1 when not frozen)
        frozen_frames   how many frames are in the lock
        landed_ratio    diagnostics for the two gates, over the chosen run
        static_ratio
    """
    n = int(frames.shape[0])
    out = {"frozen": False, "freeze_start": -1, "frozen_frames": 0,
           "landed_ratio": 0.0, "static_ratio": 0.0, "frames": n}
    if n < MIN_FREEZE + 2:
        return out

    window = min(n, max(ANALYSIS_WINDOW, REFERENCE_FRAMES + MIN_FREEZE))
    base = n - window
    rgb = _prepare(frames[base:])
    gray = (0.2126 * rgb[:, 0:1] + 0.7152 * rgb[:, 1:2] + 0.0722 * rgb[:, 2:3])

    # the final state, as a median so one bad frame cannot define it
    ref = rgb[-min(REFERENCE_FRAMES, rgb.shape[0]):].median(dim=0, keepdim=True).values

    d = (rgb - ref).abs()
    landed = ((d.mean(dim=(1, 2, 3)) <= LANDED_MEAN)
              & ((d.mean(dim=1) >= LANDED_ACTIVE_PIXEL).float().mean(dim=(1, 2))
                 <= LANDED_MAX_AREA)).tolist()

    td = (gray[1:] - gray[:-1]).abs()
    static = ((td.mean(dim=(1, 2, 3)) <= STATIC_MEAN)
              & ((td[:, 0] >= STATIC_ACTIVE_PIXEL).float().mean(dim=(1, 2))
                 <= STATIC_MAX_AREA)).tolist()

    # longest trailing run satisfying both gates. Walk backwards and keep the
    # EARLIEST start that still passes, so the whole lock is reported, not just
    # its tail.
    best = None
    for start in range(len(landed) - MIN_FREEZE, -1, -1):
        run_landed = landed[start:]
        run_static = static[start:] if start < len(static) else []
        if not run_landed[0]:
            continue          # the run has to begin on a landed frame
        lr = sum(run_landed) / float(len(run_landed))
        sr = (sum(run_static) / float(len(run_static))) if run_static else 1.0
        if (lr >= MIN_LANDED and sr >= MIN_STATIC
                and _worst_streak(run_landed) <= MAX_LANDED_STREAK
                and _worst_streak(run_static) <= MAX_STATIC_STREAK):
            best = (start, lr, sr)
        elif best is not None:
            break             # extending further back stops qualifying
    if best is None:
        return out
    start, lr, sr = best
    out.update(frozen=True, freeze_start=base + start,
               frozen_frames=n - (base + start),
               landed_ratio=float(lr), static_ratio=float(sr))
    return out


def safe_cut(frames, cut, min_keep, safety=3):
    """Pull a handover point back out of a frozen tail.

    `cut` is the frame the continuation would take its history up to (exclusive),
    `min_keep` the fewest frames that must remain before it for the pinned run to
    exist at all. `safety` backs off a little further, because the frames just
    before a lock are already most of the way there.

    Returns (cut, info). The cut is unchanged when there is no freeze, when the
    freeze starts after the cut anyway, or when moving would not leave room.
    """
    info = analyse(frames[:cut] if cut < frames.shape[0] else frames)
    info["applied"] = False
    info["cut_was"] = int(cut)
    if not info["frozen"]:
        return cut, info
    want = info["freeze_start"] - int(safety)
    if want >= cut:
        return cut, info      # the lock is not inside what we were going to use
    if want < min_keep:
        info["refused"] = ("only %d frames before the freeze, need %d"
                           % (max(0, want), min_keep))
        return cut, info
    info["applied"] = True
    info["cut_now"] = int(want)
    return int(want), info


def describe(info):
    if not info.get("frozen"):
        return "no freeze in the tail"
    s = ("locked from frame %d (%d frames, %.0f%% landed, %.0f%% static)"
         % (info["freeze_start"], info["frozen_frames"],
            100 * info["landed_ratio"], 100 * info["static_ratio"]))
    if info.get("applied"):
        s += " -- handover moved back from frame %d to %d" % (info["cut_was"],
                                                              info["cut_now"])
    elif info.get("refused"):
        s += " -- left alone (%s)" % info["refused"]
    else:
        s += " -- after the handover anyway, nothing to do"
    return s


# --- editor endpoint --------------------------------------------------------

def register():
    """Expose the scan to the editor, so a clip joining the reel can have its
    frozen tail trimmed AND be continued from before it -- one decision, one
    number, used by both the export and the next render."""
    try:
        from server import PromptServer
        from aiohttp import web
        # .instance only exists once a server is actually running -- a headless
        # or test import gets the class but not the attribute
        routes = PromptServer.instance.routes
    except Exception:
        return  # headless / test import: no server to attach to

    @routes.post("/h3studio/freeze_scan")
    async def h3studio_freeze_scan(request):
        import asyncio
        try:
            body = await request.json()
            name = str(body.get("name") or "").strip()
            fps = float(body.get("fps") or 24.0)
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        if not name:
            return web.json_response({"error": "no name"}, status=400)

        def work():
            from .h3_studio import load_input_video
            frames, _ = load_input_video(name, "freeze_scan", max_seconds=None)
            info = analyse(frames)
            # snapped so the trim lands where a pinned run can actually end
            cut = snap_to_step(info["freeze_start"]) if info["frozen"] else 0
            info["cut_frame"] = cut
            info["seconds"] = (cut / fps) if cut > 0 else 0.0
            return info

        try:
            info = await asyncio.get_running_loop().run_in_executor(None, work)
        except Exception as exc:
            logging.debug("H3Studio: freeze scan failed", exc_info=True)
            return web.json_response(
                {"error": "%s: %s" % (type(exc).__name__, exc)}, status=200)
        return web.json_response(info)
