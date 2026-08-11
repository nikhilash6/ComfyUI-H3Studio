"""Remember the last sampled H3 latent, so a continuation can skip the VAE.

Chaining clips normally costs a full round trip per link: the previous clip's
latent was decoded to pixels, encoded to a video file, decoded again, then
re-encoded by the VAE to become conditioning. Every one of those steps is
lossy, and the losses compound down a chain.

The previous clip's latent is the model's own output -- if we still have it,
the pinned context can be sliced straight out of it and every one of those
steps disappears. The obstacle is that a graph cannot wire a sampler's output
back into its own run (that is a cycle), which is why the pack this technique
came from ships explicit save/load-latent nodes and a clip-index to manage by
hand. We do not need any of that: the latent only has to survive from one run
to the next INSIDE the same ComfyUI process, so a small session cache does it,
and the Guide node picks it up by itself.

Mechanism: CFGGuider.sample returns the finished latent (nested video/audio
for H3), so it is wrapped once at import. Capture is idle until a Guide node
arms it -- i.e. until motion context is actually in use -- and only ever holds
ONE entry, on the CPU. Anything that is not an H3 AV latent is ignored.

Nothing here changes sampling: the wrapper returns the original object
untouched and stores a detached CPU copy.

H3's latent format is scale 1.0 with no shift, so the sampler's output space
and the VAE's encode space are the same numbers -- a sliced latent is directly
comparable to one from vae.encode(). (Guarded at install: a future scale
change disables the cache rather than feeding the model mis-scaled rows.)
"""

import logging
import time

import torch

_orig_sample = None
_installed = False
_armed = False
_last = None            # {"video", "audio", "t", "frames"}

MAX_AGE_S = 3600.0      # a stale capture is worse than a re-encode

# Only the TAIL is worth keeping: the largest pinnable run is 39 frames = 12
# latent steps, so 16 steps covers it with slack for a cut slightly inside the
# clip. A cut further back than that falls back to re-encoding (logged) rather
# than holding a whole clip in memory for a case that rarely happens. At a
# 1792x992 canvas a step is ~0.65 MB, so this is ~11 MB instead of ~31 MB for
# a 6 s clip -- and flat regardless of clip length.
KEEP_VIDEO_STEPS = 16
# audio steps are tiny (64 floats each); 640 covers a 16 s context window
KEEP_AUDIO_STEPS = 640


def arm():
    """Start capturing. Called by the Guide node when motion context is set."""
    global _armed
    if not _armed:
        _armed = True
        logging.info("MiniMaxH3Guide: latent cache armed — the next render's "
                     "latent will be kept for the following continuation.")


def get():
    """The last captured latent, or None if there isn't a usable one."""
    if _last is None:
        return None
    if time.time() - _last["t"] > MAX_AGE_S:
        return None
    return _last


def _frames_for(latent_t):
    from .minimax_h3_guide import mc_pixel_frames
    return mc_pixel_frames(int(latent_t))


def install():
    """Idempotently wrap CFGGuider.sample. Safe to call repeatedly."""
    global _orig_sample, _installed
    if _installed:
        return True
    try:
        import comfy.samplers
        import comfy.latent_formats
    except Exception:
        return False
    # the cache is only sound while VAE space == sampler space for H3
    try:
        fmt = comfy.latent_formats.MiniMaxH3AV()
        if float(getattr(fmt, "scale_factor", 1.0)) != 1.0 or \
                getattr(fmt, "latent_rgb_factors", None) is None:
            pass  # rgb factors are cosmetic; only the scale matters
        if float(getattr(fmt, "scale_factor", 1.0)) != 1.0:
            logging.warning("MiniMaxH3Guide: H3 latent scale is no longer 1.0; "
                            "the latent cache is disabled (a sliced latent would "
                            "be mis-scaled).")
            return False
    except Exception:
        pass   # format moved; the shape checks below still gate everything

    cur = comfy.samplers.CFGGuider.sample
    if getattr(cur, "_h3_latent_cache", False):
        _installed = True
        return True
    _orig_sample = cur

    def wrapped(self, *args, **kwargs):
        out = _orig_sample(self, *args, **kwargs)
        if _armed:
            try:
                _capture(out)
            except Exception:
                logging.debug("MiniMaxH3Guide: latent capture skipped",
                              exc_info=True)
        return out

    wrapped._h3_latent_cache = True
    comfy.samplers.CFGGuider.sample = wrapped
    _installed = True
    logging.info("MiniMaxH3Guide: latent cache installed (idle until a "
                 "motion context arms it).")
    return True


def _capture(out):
    """Stash a CPU copy when the result is an H3 AV latent; ignore everything
    else (other models, video-only latents, odd shapes)."""
    global _last
    if not hasattr(out, "unbind"):
        return
    parts = list(out.unbind())
    if len(parts) < 2:
        return
    video, audio = parts[0], parts[1]
    if video.ndim == 4:
        video = video.unsqueeze(0)
    if audio.ndim == 3:
        audio = audio.unsqueeze(0)
    if video.ndim != 5 or audio.ndim != 4 or int(video.shape[1]) != 24:
        return
    frames = _frames_for(video.shape[2])
    t_total = int(video.shape[2])
    a_total = int(audio.shape[-1])
    kept = min(KEEP_VIDEO_STEPS, t_total)
    a_kept = min(KEEP_AUDIO_STEPS, a_total)
    _last = {
        # tail only — see KEEP_VIDEO_STEPS
        "video": video[:, :, t_total - kept:].detach().to("cpu", torch.float32, copy=True),
        "audio": audio[..., a_total - a_kept:].detach().to("cpu", torch.float32, copy=True),
        "t": time.time(),
        "frames": frames,          # pixel frames in the WHOLE clip
        "t_total": t_total,        # latent steps in the whole clip
        "kept": kept,              # how many of those we still have (the tail)
        "a_total": a_total,
        "a_kept": a_kept,
    }
    logging.info("MiniMaxH3Guide: cached this render's tail latent — last %d of "
                 "%d steps (%d-frame clip, %dx%d), %.1f MB. The next "
                 "continuation can skip the VAE.",
                 kept, t_total, frames, int(video.shape[-1]) * 16,
                 int(video.shape[-2]) * 16,
                 (_last["video"].numel() * 4 + _last["audio"].numel() * 4) / 1e6)


def matches(name, want_w, want_h):
    """Is the cached latent the render that produced `name`?

    Checked without decoding the file: container geometry and frame count from
    the header, plus an mtime window (the file is written moments after the
    sampler returns). Returns (entry, reason) — entry is None when it isn't a
    match, and reason explains which way the fallback went.
    """
    entry = get()
    if entry is None:
        return None, "nothing cached yet this session"
    try:
        import os
        import av
        import folder_paths
        path = folder_paths.get_annotated_filepath(name)
        st = os.stat(path)
        with av.open(path) as c:
            s = c.streams.video[0]
            vw, vh = int(s.codec_context.width), int(s.codec_context.height)
            vframes = int(s.frames or 0)
    except Exception as exc:
        return None, "could not read %r (%s)" % (name, exc)
    lw, lh = int(entry["video"].shape[-1]) * 16, int(entry["video"].shape[-2]) * 16
    if (lw, lh) != (vw, vh):
        return None, ("the cached latent is %dx%d but %s is %dx%d"
                      % (lw, lh, name, vw, vh))
    if want_w and want_h and (lw, lh) != (int(want_w), int(want_h)):
        return None, ("the cached latent is %dx%d but this render's canvas is "
                      "%dx%d" % (lw, lh, want_w, want_h))
    if vframes and abs(vframes - entry["frames"]) > 2:
        return None, ("the cached latent covers %d frames, %s has %d"
                      % (entry["frames"], name, vframes))
    dt = st.st_mtime - entry["t"]
    if not (-5.0 <= dt <= 900.0):
        return None, ("%s was written %.0fs from the cached render — not the "
                      "same run" % (name, dt))
    return entry, "cached latent matches"
