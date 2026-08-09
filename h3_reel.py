"""Reel export: concatenate a chain of clips into one video, server-side.

POST /h3guide/reel_export   {"names": ["a.mp4 [output]", ...], "fps": 24}
  -> {"name": "h3reel/reel-<stamp>.mp4 [output]"}

Decode via the pack's load_input_video (PyAV underneath — the same machinery
core's video nodes use, no external ffmpeg binary), conform every clip to the
first clip's canvas (aspect-preserving cover), stitch audio sample-accurately
with 15 ms de-click ramps at every join, and encode mp4/h264 through core's
VideoFromComponents. Registered at import when a PromptServer exists.
"""

import logging
import os
import time
from fractions import Fraction

import torch

MAX_TOTAL_FRAMES = 4320   # ~3 minutes at 24fps — keeps the concat in RAM sane
SUBDIR = "h3reel"


def _fit_audio(audio, n_frames, fps, sr, channels):
    """One clip's soundtrack -> exactly n_frames/fps seconds at sr/channels."""
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
            # linear fallback — good enough for matching clip rates
            want_len = int(round(wf.shape[-1] * sr / src_sr))
            wf = torch.nn.functional.interpolate(wf, size=want_len, mode="linear",
                                                 align_corners=False)
    c = wf.shape[1]
    if c < channels:
        wf = wf.repeat(1, channels // max(c, 1), 1)[:, :channels]
    elif c > channels:
        wf = wf[:, :channels]
    if wf.shape[-1] >= want:
        wf = wf[..., :want]
    else:
        pad = torch.zeros(wf.shape[0], wf.shape[1], want - wf.shape[-1])
        wf = torch.cat([wf, pad], dim=-1)
    # 15ms de-click ramps at both ends of every clip
    ramp_n = min(int(sr * 0.015), max(1, want // 4))
    if ramp_n > 1:
        ramp = torch.linspace(0.0, 1.0, ramp_n)
        wf[..., :ramp_n] *= ramp
        wf[..., -ramp_n:] *= ramp.flip(0)
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
        from .minimax_h3_guide import _resize, load_input_video
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        names = body.get("names") or []
        fps = float(body.get("fps") or 24.0)
        if not isinstance(names, list) or not (1 <= len(names) <= 64):
            return web.json_response({"error": "names must be a list of 1-64 clips"},
                                     status=400)
        frames_all, audio_all = [], []
        base_w = base_h = None
        sr, channels = 44100, 2
        total = 0
        try:
            for i, name in enumerate(names):
                frames, audio = load_input_video(str(name), "reel clip %d" % (i + 1),
                                                 max_seconds=None)
                total += frames.shape[0]
                if total > MAX_TOTAL_FRAMES:
                    return web.json_response(
                        {"error": "reel exceeds ~%d s — export in parts"
                         % int(MAX_TOTAL_FRAMES / fps)}, status=413)
                if base_w is None:
                    base_h, base_w = frames.shape[1], frames.shape[2]
                    if audio is not None and audio.get("waveform") is not None:
                        sr = int(audio.get("sample_rate", sr))
                        channels = max(1, audio["waveform"].shape[-2]
                                       if audio["waveform"].ndim >= 2 else 1)
                elif (frames.shape[1], frames.shape[2]) != (base_h, base_w):
                    # conform to the first clip's canvas, aspect-preserving cover
                    frames = _resize(frames, base_w, base_h, "center")
                frames_all.append(frames)
                audio_all.append(_fit_audio(audio, frames.shape[0], fps, sr, channels))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            logging.exception("MiniMaxH3Guide: reel export failed")
            return web.json_response({"error": "%s: %s" % (type(exc).__name__, exc)},
                                     status=500)
        images = torch.cat(frames_all, dim=0)
        waveform = torch.cat(audio_all, dim=-1).clamp(-1.0, 1.0)
        out_dir = os.path.join(folder_paths.get_output_directory(), SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        fname = "reel-%s.mp4" % time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(out_dir, fname)
        try:
            video = InputImpl.VideoFromComponents(Types.VideoComponents(
                images=images, audio={"waveform": waveform, "sample_rate": sr},
                frame_rate=Fraction(int(round(fps)))))
            video.save_to(path, format=Types.VideoContainer.MP4,
                          codec=Types.VideoCodec.H264)
        except Exception as exc:
            logging.exception("MiniMaxH3Guide: reel encode failed")
            return web.json_response({"error": "encode failed: %s" % exc}, status=500)
        logging.info("MiniMaxH3Guide: reel exported — %d clip(s), %d frames -> %s",
                     len(names), images.shape[0], path)
        return web.json_response({"name": SUBDIR + "/" + fname + " [output]",
                                  "frames": int(images.shape[0])})


register()
