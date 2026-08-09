"""Reel export: stitch a chain of clips into one video, server-side.

POST /h3guide/reel_export
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


def _clip_audio(audio, start_frames, n_frames, fps, sr, channels):
    """One clip's soundtrack -> exactly n_frames/fps seconds starting at
    start_frames/fps, resampled/channel-matched, padded with silence."""
    want = int(round(n_frames / fps * sr))
    if audio is None or audio.get("waveform") is None or audio["waveform"].shape[-1] == 0:
        return torch.zeros(1, channels, want)
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
    s0 = int(round(start_frames / fps * sr))
    wf = wf[..., s0:s0 + want]
    if wf.shape[-1] < want:
        pad = torch.zeros(wf.shape[0], wf.shape[1], want - wf.shape[-1])
        wf = torch.cat([wf, pad], dim=-1)
    return wf


def _luma_match(prev_f, next_f, frames=12, lo=0.82, hi=1.22):
    """Flatten the brightness zigzag at a motion-continuation join.

    The model's first generated frames after a pinned context block run ~8%
    bright for a frame, then ~10% dark, before converging (measured) -- a
    visible flash on a hard cut. Gain-correct next_f's first `frames` frames
    toward the previous clip's closing level, the correction decaying linearly
    to zero so a legitimate lighting change survives. Export-time only; the
    render file is untouched.
    """
    anchor = float(prev_f[-4:].mean())
    n = min(frames, next_f.shape[0])
    gains = []
    for k in range(n):
        mk = float(next_f[k].mean())
        if mk <= 1e-4 or anchor <= 1e-4:
            continue
        g = max(lo, min(hi, anchor / mk))
        w = 1.0 - k / float(frames)
        gk = 1.0 + (g - 1.0) * w
        next_f[k] = (next_f[k] * gk).clamp(0.0, 1.0)
        gains.append(gk)
    if gains:
        logging.info("MiniMaxH3Guide: reel join luma-matched over %d frame(s), "
                     "gain %.3f..%.3f", len(gains), min(gains), max(gains))
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

    @routes.post("/h3guide/reel_export")
    async def h3guide_reel_export(request):
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
        if not isinstance(clips, list) or not (1 <= len(clips) <= 64):
            return web.json_response({"error": "clips must be a list of 1-64 entries"},
                                     status=400)
        # the decode/assemble/encode is seconds of CPU — off the event loop, or
        # every other request (previews, even queue POSTs) stalls behind it
        try:
            payload, status = await asyncio.to_thread(_do_export, clips, fps,
                                                      fade_in, fade_out)
        except Exception as exc:
            logging.exception("MiniMaxH3Guide: reel export failed")
            return web.json_response({"error": "%s: %s" % (type(exc).__name__, exc)},
                                     status=500)
        return web.json_response(payload, status=status)

    def _do_export(clips, fps, fade_in, fade_out):
        from .minimax_h3_guide import _resize, load_input_video

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
                pieces.append({
                    "frames": fsel,
                    "audio": _clip_audio(audio, i0, i1 - i0, fps, sr, channels),
                    "xfade": max(0.0, float(c.get("xfade") or 0.0)),
                    # motion-continuation clips get their join brightness matched
                    "lumafix": bool(c.get("mc")),
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
                    f = _luma_match(out_f, f)
                _declick(out_a, sr, tail=True)
                _declick(a, sr, head=True)
                out_f = torch.cat([out_f, f], dim=0)
                out_a = torch.cat([out_a, a], dim=-1)

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
            logging.exception("MiniMaxH3Guide: reel encode failed")
            return {"error": "encode failed: %s" % exc}, 500
        logging.info("MiniMaxH3Guide: reel exported — %d clip(s), %d frames -> %s",
                     len(clips), out_f.shape[0], path)
        return {"name": SUBDIR + "/" + fname + " [output]",
                "frames": int(out_f.shape[0])}, 200


register()
