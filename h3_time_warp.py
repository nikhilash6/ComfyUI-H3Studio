"""Time dilation for MiniMax H3 -- a direct dial on how much motion happens.

Every other motion control is a hint: show the model a frame, describe the action,
weaken a pin. This one changes the quantity motion is measured against.

The packed layout gives the target video rows a RoPE time coordinate

    t_k = origin + FRAME_RESCALE * F_k          F_k = the pixel frame token k starts at

built from a fixed cadence of spans (`_video_t_grid`, model.py). That spacing IS the
model's statement of how much time separates two latent tokens. Widen the gaps and it
is being told more time passes between them, so more must change: faster motion.
Narrow them and less should: slower. Nothing about the content is touched -- only the
clock the content is read against.

We express that as a speed curve v(f) over pixel frames and integrate it:

    W(f) = the integral of v from 0 to f        t = origin + FRAME_RESCALE * W(f)

v == 1 everywhere reproduces the stock grid exactly, so the identity case is not
merely close to untouched, it IS untouched (parse_motion_curve returns None and
nothing is attached to the conditioning). v is clamped strictly positive, so W is
strictly increasing and the frames can never reorder.

W is absolute, not renormalised: at v = 1.5 the clip's whole timeline is half again
as long, which is the point -- the same tokens now have to cover more happening.
A curve therefore does two things at once, speeding a section up AND pushing what
follows later; that is the honest reading of a time axis, not a bug.

Every other coordinate on that axis has to travel with it. A keyframe pinned at
frame k, a timed-text beat, the motion-context join -- all of them are `origin +
FRAME_RESCALE * index` in the same space, so they map through W too, or they slide
off the moment they were placed at. extra_conds_patch threads the table through to
each of them.

CAVEAT, deliberately accepted: the audio rows advance 1.0 per audio latent frame on
this same axis and are NOT warped, so video and sound drift apart as |v-1| grows.
And the model only ever saw the stock cadence -- this is out of distribution, gently
at 0.9-1.2 and increasingly so past that.
"""

import logging
import math

import torch

import comfy.ldm.minimax.model as _mmm

# strictly positive so W stays monotonic; the ceiling is well past anything useful
MIN_SPEED = 0.05
MAX_SPEED = 4.0


def parse_motion_curve(spec, frame_count, scale=1.0):
    """Build the cumulative warp table, one entry per pixel frame.

    `scale` is a flat multiplier on the whole clip; `spec` is optional per-moment
    control, one `position, speed` per line, linearly interpolated between the
    points given and held flat outside them. The two compose (the curve is scaled).

    Returns None when the result is the identity, so callers can skip the whole
    mechanism and leave the layout byte-for-byte stock.
    """
    if frame_count < 2:
        return None

    points = []
    for i, raw in enumerate(spec.splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) != 2:
            raise ValueError("H3 Studio: motion_curve line %d (%r) needs "
                             "'position, speed'." % (i + 1, raw.strip()))
        try:
            pos, speed = float(parts[0]), float(parts[1])
        except ValueError:
            raise ValueError("H3 Studio: motion_curve line %d (%r) -- position and "
                             "speed must be numbers." % (i + 1, raw.strip()))
        if not (math.isfinite(pos) and math.isfinite(speed)):
            raise ValueError("H3 Studio: motion_curve line %d (%r) -- position and "
                             "speed must be finite numbers." % (i + 1, raw.strip()))
        if speed <= 0.0:
            raise ValueError("H3 Studio: motion_curve line %d speed must be above 0 "
                             "(time cannot stop or run backwards), got %s."
                             % (i + 1, speed))
        # same position convention as middle_frame_spec / timed_text, half-UP rounding
        x = pos * (frame_count - 1) if pos <= 1.0 else pos
        index = min(max(int(math.floor(x + 0.5)), 0), frame_count - 1)
        points.append((index, speed))

    points.sort(key=lambda p: p[0])
    if len(set(p[0] for p in points)) != len(points):
        raise ValueError("H3 Studio: two motion_curve lines resolved to the same "
                         "frame. Spread the positions further apart, or lengthen "
                         "the clip.")

    scale = float(scale)
    if not math.isfinite(scale) or scale <= 0.0:
        raise ValueError("H3 Studio: motion_scale must be above 0, got %s." % scale)
    if not points and abs(scale - 1.0) < 1e-9:
        return None  # nothing to do -- leave the stock clock alone

    speeds = _speed_per_frame(points, frame_count, scale)

    lo = min(speeds)
    hi = max(speeds)
    if lo < MIN_SPEED or hi > MAX_SPEED:
        logging.warning("H3Studio: motion speed clamped to %.2f-%.2f (curve asked "
                        "for %.3f-%.3f).", MIN_SPEED, MAX_SPEED, lo, hi)
        speeds = [min(max(s, MIN_SPEED), MAX_SPEED) for s in speeds]

    if all(abs(s - 1.0) < 1e-9 for s in speeds):
        return None

    # trapezoid integral: W[0] = 0, W[f] = W[f-1] + mean(v[f-1], v[f])
    warp = [0.0]
    for f in range(1, frame_count):
        warp.append(warp[-1] + 0.5 * (speeds[f - 1] + speeds[f]))
    return warp


def _speed_per_frame(points, frame_count, scale):
    """v(f) for every pixel frame: piecewise linear through the points, flat outside."""
    if not points:
        return [scale] * frame_count
    if len(points) == 1:
        return [points[0][1] * scale] * frame_count

    out = []
    seg = 0
    for f in range(frame_count):
        while seg < len(points) - 2 and f > points[seg + 1][0]:
            seg += 1
        (f0, v0), (f1, v1) = points[seg], points[seg + 1]
        if f <= points[0][0]:
            v = points[0][1]
        elif f >= points[-1][0]:
            v = points[-1][1]
        elif f1 == f0:
            v = v1
        else:
            v = v0 + (v1 - v0) * (f - f0) / float(f1 - f0)
        out.append(v * scale)
    return out


def warp_at(warp, frame):
    """Look a (possibly fractional) frame index up in the table, linearly."""
    if not warp:
        return float(frame)
    n = len(warp)
    f = min(max(float(frame), 0.0), n - 1.0)
    lo = int(math.floor(f))
    hi = min(lo + 1, n - 1)
    return warp[lo] + (warp[hi] - warp[lo]) * (f - lo)


def apply_video_warp(layout, warp):
    """Rewrite the target video rows' clock through the warp. Returns rows moved.

    The frame index of each row is recovered from the layout's own coordinates
    rather than recomputed from the cadence constants -- if core ever changes
    FRAME_PER_TOKEN, this follows it instead of silently disagreeing with it.
    """
    if not warp:
        return 0
    if getattr(layout, "_h3_time_warped", False):
        return 0  # a layout is built per sampling run; never compound two warps
    seg = next(((a, b) for a, b, kind in layout.segments if kind == "video"), None)
    if seg is None:
        logging.warning("H3Studio: no video segment in the layout; motion timing "
                        "left at stock.")
        return 0
    a, b = seg
    t = layout.position_ids[a:b, 0]
    origin = float(t[0])
    frames = (t - origin) / _mmm.FRAME_RESCALE

    table = torch.tensor(warp, dtype=torch.float64)
    f = frames.clamp(0.0, float(len(warp) - 1))
    lo = f.floor().to(torch.long)
    hi = (lo + 1).clamp(max=len(warp) - 1)
    frac = f - lo.to(torch.float64)
    layout.position_ids[a:b, 0] = origin + _mmm.FRAME_RESCALE * (
        table[lo] + (table[hi] - table[lo]) * frac)
    layout._h3_time_warped = True
    return b - a


def describe(warp, frame_count, fps):
    """One-line log summary: what the clip's duration became, and where it varies."""
    if not warp:
        return "off"
    span = warp[-1]
    stock = float(len(warp) - 1)
    steps = [warp[i + 1] - warp[i] for i in range(len(warp) - 1)]
    return ("clip time x%.2f (%.2fs of motion in %.2fs of frames), local speed %.2f-%.2f"
            % (span / stock, span / fps, stock / fps, min(steps), max(steps)))
