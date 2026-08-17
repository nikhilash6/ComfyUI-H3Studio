"""Reel export: stitch a chain of clips into one video, server-side.

POST /h3studio/reel_export
  {"clips": [{"name": "a.mp4 [output]", "in": 0.0, "out": 0.0, "xfade": 0.5},
             ...],
   "fade_in": 0.5, "fade_out": 1.0, "fps": 24}
  -> {"name": "h3reel/reel-<stamp>.mp4 [output]", "frames": N}

Non-destructive edits, applied here only: per-clip in/out trims (seconds,
out<=0 = clip end), a crossfade from each clip INTO the next (video linear
blend, audio equal-power), and whole-reel fade in/out (to black + silence).
Hard joints (xfade 0) get 15 ms audio de-click ramps. Decoding and encoding
go through PyAV (core's own video machinery — no external ffmpeg).

Legacy {"names": [...]} bodies still work. Registered at import when a
PromptServer exists.
"""

import asyncio
import logging
import math
import os
import time
from fractions import Fraction

import torch

MAX_TOTAL_FRAMES = 4320   # ~3 minutes at 24fps — keeps the concat in RAM sane
SUBDIR = "h3reel"

# Join flicker fix: available, but OPT-IN per clip. The zigzag it corrects is
# real (+8%/-10% at the first generated frames after a pinned context block),
# but a correction that alters picture shouldn't run on every export by
# default — tick "✨ luma-match join" in a clip's ✂ popup to use it. This
# constant is the master off-switch if it ever needs killing outright.
LUMA_FIX_ENABLED = True


def _conform(audio, sr, channels):
    """Any decoded audio -> [1, channels, L] float32 at the target rate."""
    if audio is None or audio.get("waveform") is None or audio["waveform"].shape[-1] == 0:
        return torch.zeros(1, channels, 0)
    wf = audio["waveform"]
    if wf.ndim == 2:
        wf = wf.unsqueeze(0)
    wf = wf.to(torch.float32)
    src_sr = int(audio.get("sample_rate", sr))
    if src_sr != sr:
        try:
            import torchaudio
            wf = torchaudio.functional.resample(wf, src_sr, sr)
        except Exception:
            want_len = int(round(wf.shape[-1] * sr / src_sr))
            wf = torch.nn.functional.interpolate(wf, size=want_len, mode="linear",
                                                 align_corners=False)
    c = wf.shape[1]
    if c < channels:
        wf = wf.repeat(1, math.ceil(channels / max(c, 1)), 1)[:, :channels]
    elif c > channels:
        wf = wf[:, :channels]
    return wf[:1]


def _clip_audio(audio, start_frames, n_frames, fps, sr, channels):
    """One clip's soundtrack -> exactly n_frames/fps seconds starting at
    start_frames/fps, resampled/channel-matched, padded with silence."""
    want = int(round(n_frames / fps * sr))
    wf = _conform(audio, sr, channels)
    if wf.shape[-1] == 0:
        return torch.zeros(1, channels, want)
    s0 = int(round(start_frames / fps * sr))
    wf = wf[..., s0:s0 + want]
    if wf.shape[-1] < want:
        pad = torch.zeros(wf.shape[0], wf.shape[1], want - wf.shape[-1])
        wf = torch.cat([wf, pad], dim=-1)
    return wf


# The window both corrections read as "what this clip actually looks like":
# past the unstable head, but still ADJACENT TO THE JOIN.
#
# It used to be frames 12-36, which breaks on any clip whose exposure moves
# during it. Measured on a real chain: the clip's own level fell 0.516 -> 0.310
# from end to end (the shot going into shade -- content, not drift), so its
# middle read 12.6% darker than the previous clip's close and the whole clip was
# brightened to compensate. The opening had matched perfectly beforehand, so the
# correction created the very step it exists to remove, and the decay of the
# second correction on top became a visible brighten ramp -- reported as "a
# little flash of the sky".
#
# Continuity is a property of the frames either side of the cut. Measure there.
_BODY = (2, 10)


_PREV = 8        # frames of the outgoing clip used to read where it was heading


def _trend(f, a0, a1):
    """Least-squares line through the mean level of frames [a0, a1).

    A single average is not enough. These corrections compared against a flat
    mean, which is right only while the exposure is steady; on a shot that is
    genuinely changing -- measured on a chain falling ~3% per frame as it went
    into shade -- a flat target pulls the opening frames off the trend and turns
    a smooth decline into a dip-then-rise. That wobble is what reads as a flash,
    and it was the correction making it, not the render.

    Returns (slope, intercept) in frame units, or None when there is too little
    to fit.
    """
    a0 = max(0, min(int(a0), f.shape[0] - 1))
    a1 = min(int(a1), f.shape[0])
    n = a1 - a0
    if n < 2:
        return None
    ys = [float(f[k].mean()) for k in range(a0, a1)]
    if min(ys) <= 1e-4:
        return None
    xs = list(range(a0, a1))
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 1e-9:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return slope, my - slope * mx


def _predict(tr, k):
    return None if tr is None else tr[0] * k + tr[1]


# Regional join correction.
#
# A whole-frame average hides the artifact that actually gets reported. Measured
# on a chain whose join looked clean by every global measure:
#
#            f72      f73      step
#     sky    0.5936   0.6057   +2.0%     <- "a flash of the sky"
#     rest   0.3618   0.3597   -0.6%
#     mean   0.4198   0.4212   +0.3%     <- all a global gain can see
#
# The sky steps up while everything else eases down, so the mean barely moves.
# No single gain can fix that: correcting the sky by darkening the frame would
# drag the road with it. So the correction is a low-resolution GAIN MAP, fitted
# per region from the trend either side of the cut and interpolated smoothly
# back to full resolution.
#
# It decays over about a second. A persistent regional gain would fight the shot
# as content moves through the regions; a decaying one removes the step at the
# cut and then lets the clip be itself.
_GRID = (3, 4)          # rows, cols -- coarse on purpose, few frames to fit from
_REGION_FRAMES = 24     # how long the regional correction takes to release


def _tile_means(f, rows, cols):
    """[T,H,W,C] -> [T, rows, cols] mean level per region."""
    x = f.mean(dim=-1).unsqueeze(1)                     # [T,1,H,W]
    return torch.nn.functional.adaptive_avg_pool2d(x, (rows, cols)).squeeze(1)


def _tile_trend(f, a0, a1, rows, cols):
    """Least-squares line per region over frames [a0,a1). -> (slope, intercept)."""
    a0 = max(0, min(int(a0), f.shape[0] - 1))
    a1 = min(int(a1), f.shape[0])
    if a1 - a0 < 2:
        return None
    ys = _tile_means(f[a0:a1], rows, cols)              # [n, rows, cols]
    n = ys.shape[0]
    xs = torch.arange(a0, a1, dtype=torch.float32).view(n, 1, 1)
    mx, my = xs.mean(), ys.mean(dim=0, keepdim=True)
    den = ((xs - mx) ** 2).sum()
    if float(den) <= 1e-9:
        return None
    slope = (((xs - mx) * (ys - my)).sum(dim=0) / den)
    return slope, my.squeeze(0) - slope * float(mx)


def _region_match(prev_f, next_f, frames=_REGION_FRAMES, lo=0.85, hi=1.18):
    """Correct the incoming clip region by region, decaying to nothing.

    Fixes a join whose discontinuity is local -- a sky that steps while the road
    does not -- which a frame-mean correction cannot even detect. Export-time
    only; the render file is untouched.
    """
    rows, cols = _GRID
    if next_f.shape[0] < 3 or prev_f.shape[0] < 3:
        return next_f
    pt = _tile_trend(prev_f, prev_f.shape[0] - _PREV, prev_f.shape[0], rows, cols)
    nt = _tile_trend(next_f, *_BODY, rows=rows, cols=cols)
    if pt is None or nt is None:
        return next_f
    want = pt[0] * float(prev_f.shape[0]) + pt[1]       # prev, extrapolated to the cut
    have = nt[1]                                        # next, at its own frame 0
    ok = (want > 1e-3) & (have > 1e-3)
    if not bool(ok.any()):
        return next_f
    gain = torch.where(ok, (want / have.clamp(min=1e-3)).clamp(lo, hi),
                       torch.ones_like(have))
    if float((gain - 1.0).abs().max()) < 5e-3:
        return next_f                                    # nothing worth doing
    gmap = torch.nn.functional.interpolate(
        gain.view(1, 1, rows, cols),
        size=(next_f.shape[1], next_f.shape[2]),
        mode="bilinear", align_corners=False).view(1, next_f.shape[1],
                                                   next_f.shape[2], 1)
    n = min(frames, next_f.shape[0])
    for k in range(n):
        w = 1.0 - k / float(frames)
        next_f[k] = (next_f[k] * (1.0 + (gmap[0] - 1.0) * w)).clamp(0.0, 1.0)
    logging.info("H3Studio: join region-matched over %d frame(s), gain %.3f..%.3f "
                 "across %dx%d regions", n, float(gain.min()), float(gain.max()),
                 rows, cols)
    return next_f


def _level_match(prev_f, next_f, lo=0.80, hi=1.25):
    """Whole-clip exposure anchor: ONE clamped constant gain putting this
    clip's level just after the join on its predecessor's closing level.

    Measured across Peter's chained renders, every continuation came out
    darker than its source -- typically 1-3% per link, worst 8% -- which
    compounds invisibly down a chain because each join is smooth. Anchoring
    each clip to its ALREADY-CORRECTED predecessor stops the accumulation by
    construction. Renders continue from the RAW previous file, so the gain has
    to undo cumulative drift; hence the generous clamp, safe here because
    continuations are same-scene by construction. Beyond it the correction
    saturates rather than overcooking, and a chain restart at a natural
    transition remains the honest fix for very long sequences.

    Opt-in per clip (the trim popup's brightness checkbox), export-only.
    """
    # Both sides extrapolated TO THE CUT, so a shot that is already changing
    # carries its change across the join rather than being flattened onto the
    # previous clip's closing average -- which on a fast-darkening shot turned a
    # -5.8% join step into +10.3% by insisting the new clip start where the old
    # one was, not where it was heading.
    pt = _trend(prev_f, prev_f.shape[0] - _PREV, prev_f.shape[0])
    nt = _trend(next_f, *_BODY)
    anchor = _predict(pt, prev_f.shape[0])       # where the outgoing clip was going
    settled = _predict(nt, 0)                    # where the incoming clip comes in
    if anchor is None or settled is None or anchor <= 1e-4 or settled <= 1e-4:
        return next_f
    g = max(lo, min(hi, anchor / settled))
    if abs(g - 1.0) > 1e-3:
        next_f = (next_f * g).clamp_(0.0, 1.0)
        logging.info("H3Studio: clip level-matched — gain %.3f "
                     "(settled %.1f -> anchor %.1f, /255)",
                     g, settled * 255.0, anchor * 255.0)
    return next_f


def _luma_match(next_f, frames=6, lo=0.82, hi=1.22):
    """Flatten a continued clip's opening onto ITS OWN settled level.

    The first delivered frame after a pinned context block carries the CONTEXT's
    exposure rather than the one the model settles on -- the first free latent
    step is decoded using the pinned step as its temporal context. Measured on a
    real chain: last pinned 0.3105, first delivered 0.3067, next frame 0.2847.
    One frame out of step with the rest of its own clip, which on a hard cut
    reads as a flash.

    THE TARGET IS THIS CLIP'S BODY, not the previous clip's tail. That split
    matters: _level_match already puts this clip's body on the previous clip's
    closing level, so aiming here at the previous clip too corrects the same
    error twice, and the second correction's decay becomes a visible brighten
    ramp over the following frames -- reported as "a little flash of the sky",
    measured as +11% over four frames after the join. Two orthogonal jobs:

        _level_match   this clip's BODY  -> the previous clip   (chain drift)
        _luma_match    this clip's HEAD  -> this clip's body    (the flash)

    A frame already sitting on the settled level gets a gain of 1.0 whatever the
    window, so the window only bounds how far in we are willing to reach; it
    cannot drag correct frames the way a fixed decay toward an external anchor
    does.

    Export-time only; the render file is untouched.
    """
    n = next_f.shape[0]
    tr = _trend(next_f, *_BODY)
    if tr is None:
        return next_f
    gains = []
    for k in range(min(frames, n)):
        mk = float(next_f[k].mean())
        want = _predict(tr, k)          # where this frame should sit on the trend
        if mk <= 1e-4 or want is None or want <= 1e-4:
            continue
        g = max(lo, min(hi, want / mk))
        w = 1.0 - k / float(frames)
        gk = 1.0 + (g - 1.0) * w
        if abs(gk - 1.0) < 1e-3:
            continue
        next_f[k] = (next_f[k] * gk).clamp(0.0, 1.0)
        gains.append(gk)
    if gains:
        logging.info("H3Studio: reel join luma-matched over %d frame(s), "
                     "gain %.3f..%.3f (onto this clip's own settled level)",
                     len(gains), min(gains), max(gains))
    return next_f


def _declick(wf, sr, head=False, tail=False):
    n = min(int(sr * 0.015), max(1, wf.shape[-1] // 4))
    if n > 1:
        ramp = torch.linspace(0.0, 1.0, n)
        if head:
            wf[..., :n] *= ramp
        if tail:
            wf[..., -n:] *= ramp.flip(0)
    return wf


def register():
    try:
        from aiohttp import web
        import folder_paths
        from comfy_api.latest import InputImpl, Types
        from server import PromptServer
    except Exception:
        return  # headless import: nothing to attach to
    try:
        routes = PromptServer.instance.routes
    except Exception:
        return

    @routes.post("/h3studio/reel_export")
    async def h3studio_reel_export(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        clips = body.get("clips")
        if clips is None:   # legacy shape
            clips = [{"name": n} for n in (body.get("names") or [])]
        fps = float(body.get("fps") or 24.0)
        fade_in = max(0.0, float(body.get("fade_in") or 0.0))
        fade_out = max(0.0, float(body.get("fade_out") or 0.0))
        music = body.get("music") if isinstance(body.get("music"), dict) else None
        sfx = body.get("sfx") if isinstance(body.get("sfx"), list) else None
        if not isinstance(clips, list) or not (1 <= len(clips) <= 64):
            return web.json_response({"error": "clips must be a list of 1-64 entries"},
                                     status=400)
        # the decode/assemble/encode is seconds of CPU — off the event loop, or
        # every other request (previews, even queue POSTs) stalls behind it
        try:
            payload, status = await asyncio.to_thread(_do_export, clips, fps,
                                                      fade_in, fade_out, music, sfx)
        except Exception as exc:
            logging.exception("H3Studio: reel export failed")
            return web.json_response({"error": "%s: %s" % (type(exc).__name__, exc)},
                                     status=500)
        return web.json_response(payload, status=status)

    def _do_export(clips, fps, fade_in, fade_out, music=None, sfx=None):
        from .h3_studio import _resize, load_input_video, load_input_audio

        pieces = []          # [{frames, audio, xfade_next_frames}]
        base_w = base_h = None
        sr, channels = 44100, 2
        total = 0
        try:
            for i, c in enumerate(clips):
                name = str(c.get("name", ""))
                frames, audio = load_input_video(name, "reel clip %d" % (i + 1),
                                                 max_seconds=None)
                n = frames.shape[0]
                t_in = max(0.0, float(c.get("in") or 0.0))
                t_out = float(c.get("out") or 0.0)
                i0 = min(n - 1, int(round(t_in * fps)))
                i1 = n if t_out <= 0 else max(i0 + 1, min(n, int(round(t_out * fps))))
                if i1 - i0 < 1:
                    return {"error": "clip %d trims to nothing" % (i + 1)}, 400
                total += i1 - i0
                if total > MAX_TOTAL_FRAMES:
                    return {"error": "reel exceeds ~%d s — export in parts"
                            % int(MAX_TOTAL_FRAMES / fps)}, 413
                fsel = frames[i0:i1]
                if base_w is None:
                    base_h, base_w = fsel.shape[1], fsel.shape[2]
                    if audio is not None and audio.get("waveform") is not None:
                        sr = int(audio.get("sample_rate", sr))
                        channels = max(1, audio["waveform"].shape[-2]
                                       if audio["waveform"].ndim >= 2 else 1)
                elif (fsel.shape[1], fsel.shape[2]) != (base_h, base_w):
                    fsel = _resize(fsel, base_w, base_h, "center")
                # per-clip level: 0 mutes the model's own soundtrack, which is
                # what you want when the music bed and fx carry the piece
                vol = c.get("vol")
                vol = 1.0 if vol is None else max(0.0, min(2.0, float(vol)))
                clip_a = _clip_audio(audio, i0, i1 - i0, fps, sr, channels)
                if vol != 1.0:
                    clip_a = clip_a * vol
                pieces.append({
                    "frames": fsel,
                    "audio": clip_a,
                    "xfade": max(0.0, float(c.get("xfade") or 0.0)),
                    # motion-continuation clips get their join brightness matched
                    # (disabled globally by LUMA_FIX_ENABLED)
                    "lumafix": bool(c.get("mc")) and LUMA_FIX_ENABLED,
                })
        except ValueError as exc:
            return {"error": str(exc)}, 400

        # assemble with crossfades (each piece's xfade bleeds INTO the next)
        out_f, out_a = None, None
        for i, p in enumerate(pieces):
            f, a = p["frames"], p["audio"]
            if out_f is None:
                out_f, out_a = f, a
                continue
            prev_x = pieces[i - 1]["xfade"]
            xn = int(round(prev_x * fps))
            xn = min(xn, out_f.shape[0] - 1, f.shape[0] - 1)
            if p.get("lumafix"):
                # whole-clip exposure anchor first (both joint types), then the
                # zigzag bridge below fine-corrects the head on hard cuts
                f = _level_match(out_f, f)
            if xn > 0:
                tv = torch.linspace(0.0, 1.0, xn).view(-1, 1, 1, 1)
                blend = out_f[-xn:] * (1.0 - tv) + f[:xn] * tv
                out_f = torch.cat([out_f[:-xn], blend, f[xn:]], dim=0)
                xs = min(int(round(xn / fps * sr)), out_a.shape[-1] - 1, a.shape[-1] - 1)
                ta = torch.linspace(0.0, 1.0, xs)
                # equal-power keeps perceived loudness level through the blend
                a_bl = (out_a[..., -xs:] * torch.cos(ta * math.pi / 2)
                        + a[..., :xs] * torch.sin(ta * math.pi / 2))
                out_a = torch.cat([out_a[..., :-xs], a_bl, a[..., xs:]], dim=-1)
            else:
                if p.get("lumafix"):
                    # frame-mean first (the whole-clip step), then the regional
                    # residue a mean cannot see, then the head transient
                    f = _region_match(out_f, f)
                    f = _luma_match(f)
                _declick(out_a, sr, tail=True)
                _declick(a, sr, head=True)
                out_f = torch.cat([out_f, f], dim=0)
                out_a = torch.cat([out_a, a], dim=-1)

        # music bed (the editor's ♪ guide track) mixed UNDER the clip audio at
        # the chosen level — before the fades, so it fades with everything else
        if music and music.get("name") and float(music.get("level") or 0) > 0:
            try:
                bed = load_input_audio(str(music["name"]), "reel music")
                lvl = max(0.0, min(1.5, float(music.get("level") or 0)))
                start = max(0.0, float(music.get("from") or 0.0))
                bed_wf = _clip_audio(bed, int(round(start * fps)), out_f.shape[0],
                                     fps, sr, channels)
                # bed-only fades: the reel usually sits mid-song, so the music
                # needs its own ease in/out independent of the whole-reel fades
                m_fi = max(0.0, min(15.0, float(music.get("fade_in") or 0.0)))
                m_fo = max(0.0, min(15.0, float(music.get("fade_out") or 0.0)))
                if m_fi > 0:
                    s = min(int(round(m_fi * sr)), bed_wf.shape[-1])
                    if s > 1:
                        bed_wf[..., :s] *= torch.linspace(0.0, 1.0, s)
                if m_fo > 0:
                    s = min(int(round(m_fo * sr)), bed_wf.shape[-1])
                    if s > 1:
                        bed_wf[..., -s:] *= torch.linspace(1.0, 0.0, s)
                out_a = out_a + bed_wf * lvl
                logging.info("H3Studio: reel music bed mixed — %r at %.0f%% "
                             "from %.1fs (music fades %.1fs/%.1fs)",
                             music["name"], lvl * 100, start, m_fi, m_fo)
            except Exception:
                logging.exception("H3Studio: music bed failed — exporting without it")

        # audio-lane samples: each placed at its reel-time `at`, its own in→out
        # slice of the source file, own level and own head/tail fades, overlaid
        # additively (four UI lanes, but mixing is mixing — the ♪ soundtrack
        # lane arrives here as one entry per clip)
        _SFX_MAX = 96
        if len(sfx or []) > _SFX_MAX:
            logging.warning("H3Studio: %d audio samples sent, mixing the "
                            "first %d — the rest are NOT in this export",
                            len(sfx), _SFX_MAX)
        for k, s in enumerate((sfx or [])[:_SFX_MAX]):
            try:
                sname = str(s.get("name") or "")
                lvl = max(0.0, min(1.5, float(s.get("level")
                                              if s.get("level") is not None else 1.0)))
                if not sname or lvl <= 0:
                    continue
                at = max(0.0, float(s.get("at") or 0.0))
                a0 = int(round(at * sr))
                if a0 >= out_a.shape[-1]:
                    logging.info("H3Studio: fx %r at %.1fs is past the reel end "
                                 "— skipped", sname, at)
                    continue
                wf = _conform(load_input_audio(sname, "fx sample %d" % (k + 1)),
                              sr, channels)
                in_p = max(0.0, float(s.get("in") or 0.0))
                out_p = float(s.get("out") or 0.0)
                i0 = int(round(in_p * sr))
                i1 = wf.shape[-1] if out_p <= 0 else max(i0 + 1,
                                                         int(round(out_p * sr)))
                wf = wf[..., i0:i1]
                room = out_a.shape[-1] - a0
                wf = wf[..., :room]
                if wf.shape[-1] < 2:
                    continue
                s_fi = max(0.0, min(15.0, float(s.get("fade_in") or 0.0)))
                s_fo = max(0.0, min(15.0, float(s.get("fade_out") or 0.0)))
                if s_fi > 0:
                    n2 = min(int(round(s_fi * sr)), wf.shape[-1])
                    if n2 > 1:
                        wf[..., :n2] *= torch.linspace(0.0, 1.0, n2)
                if s_fo > 0:
                    n2 = min(int(round(s_fo * sr)), wf.shape[-1])
                    if n2 > 1:
                        wf[..., -n2:] *= torch.linspace(1.0, 0.0, n2)
                out_a[..., a0:a0 + wf.shape[-1]] += wf * lvl
                logging.info("H3Studio: fx %r mixed at %.1fs (%.1fs, %.0f%%)",
                             sname, at, wf.shape[-1] / sr, lvl * 100)
            except Exception:
                logging.exception("H3Studio: fx sample %d failed — exporting "
                                  "without it", k + 1)

        # whole-reel fades: video to black, audio to silence
        if fade_in > 0:
            fn = min(int(round(fade_in * fps)), out_f.shape[0])
            if fn > 1:
                r = torch.linspace(0.0, 1.0, fn).view(-1, 1, 1, 1)
                out_f[:fn] = out_f[:fn] * r
                s = min(int(round(fade_in * sr)), out_a.shape[-1])
                out_a[..., :s] *= torch.linspace(0.0, 1.0, s)
        if fade_out > 0:
            fn = min(int(round(fade_out * fps)), out_f.shape[0])
            if fn > 1:
                r = torch.linspace(1.0, 0.0, fn).view(-1, 1, 1, 1)
                out_f[-fn:] = out_f[-fn:] * r
                s = min(int(round(fade_out * sr)), out_a.shape[-1])
                out_a[..., -s:] *= torch.linspace(1.0, 0.0, s)

        out_a = out_a.clamp(-1.0, 1.0)
        out_dir = os.path.join(folder_paths.get_output_directory(), SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        fname = "reel-%s.mp4" % time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(out_dir, fname)
        try:
            video = InputImpl.VideoFromComponents(Types.VideoComponents(
                images=out_f, audio={"waveform": out_a, "sample_rate": sr},
                frame_rate=Fraction(int(round(fps)))))
            video.save_to(path, format=Types.VideoContainer.MP4,
                          codec=Types.VideoCodec.H264)
        except Exception as exc:
            logging.exception("H3Studio: reel encode failed")
            return {"error": "encode failed: %s" % exc}, 500
        logging.info("H3Studio: reel exported — %d clip(s), %d frames -> %s",
                     len(clips), out_f.shape[0], path)
        return {"name": SUBDIR + "/" + fname + " [output]",
                "frames": int(out_f.shape[0])}, 200


register()
