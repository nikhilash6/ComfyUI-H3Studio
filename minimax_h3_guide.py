"""MiniMax H3 Image to Video, with per-keyframe strength and middle-frame waypoints.

A duplicate of the core MiniMaxH3ImageToVideo node (comfy_extras/nodes_minimax_h3.py)
with three additions:

- `first_frame_strength` / `last_frame_strength`, which weaken either keyframe so it
  acts as a target the motion drifts toward rather than a frame it must land on
- middle keyframes at arbitrary points in the clip, each with its own strength and
  optional description (see middle_frame_patch.py for how the anchor is placed)

How it works
------------
H3 feeds keyframes to the DiT as condition rows that ride through every sampling
step and are never denoised. The DiT's own strength knob (`visual_cond_noise_aug`,
default 0.999) blends every condition latent toward gaussian noise -- but it is a
single global value, so it cannot weaken one keyframe without equally weakening the
other. This node instead dilutes each keyframe's condition latent directly, before
it is attached to the conditioning, with an independent dial per frame.

The blend is LINEAR (the flow-matching forward process, same form as core's
global aug):

    z' = a*z + (1 - a) * noise,   a = s * VISUAL_COND_TIMESTEP,   for s <= 1

and — via h3_row_aug_patch — each row's modulation timestep is relabeled to `a`,
exactly as core does for its global knob. The model then reads a softened
keyframe as "a reference at noise level a" (in-distribution) rather than "a
clean image full of static" (the old variance-preserving blend without
relabeling, which caused visible distortion at sub-1.0 strengths). When the
label patch cannot install (core drift, turbo LoRA), we fall back to the
linear blend with the global label -- degraded but sane. We could not relabel per keyframe
(both keyframes share the "cond" segment kind), so the model still reads this row
as near-clean. Keeping unit variance means it reads as a properly-exposed noisy
frame instead of a washed-out one, which avoids low-contrast endings.
"""

import logging
import math
import os

import numpy as np
import torch
import torchaudio
from PIL import Image, ImageOps

import comfy.nested_tensor
import folder_paths
import nodes
import node_helpers
from comfy_api.latest import ComfyExtension, io, InputImpl

from . import extra_conds_patch, h3_row_aug_patch, middle_frame_patch, turbo_compat

try:
    from comfy.ldm.minimax.model import VISUAL_COND_TIMESTEP as _VIS_T
except Exception:  # core moved the constant; 0.999 is its long-standing value
    _VIS_T = 0.999
from .load_image_zoom_pan import crop_box

FPS_HINT = 24  # for the "at N.N seconds" prompt labels only

try:
    from comfy_extras.nodes_minimax_h3 import _empty_av_latent, _resize, adapt_canvas
except ImportError:  # core moved or renamed the helpers -- keep working standalone
    import comfy.model_management
    import comfy.nested_tensor
    import comfy.utils

    FPS = 24
    AUDIO_LATENT_FPS = 40

    def _align_frame_count(n):
        while n % 17 != 5:
            n += 1
        return n

    def _video_latent_t(frame_count):
        return 2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2

    def _resize(image, width, height, crop):
        samples = image[..., :3].movedim(-1, 1)
        samples = comfy.utils.common_upscale(samples, width, height, "lanczos", crop)
        return samples.movedim(1, -1)

    def _empty_av_latent(width, height, length, batch_size=1):
        frame_count = _align_frame_count(max(5, length))
        latent_t = _video_latent_t(frame_count)
        audio_t = round((frame_count / FPS) * AUDIO_LATENT_FPS)
        device = comfy.model_management.intermediate_device()
        video = torch.zeros([batch_size, 24, latent_t, height // 16, width // 16], device=device)
        audio = torch.zeros([batch_size, 32, 2, audio_t], device=device)
        return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}, frame_count

    def adapt_canvas(width, height):
        # 768-short-edge canvas with 768*1344 area cap, per-axis round to 32
        ratio = width / height
        if ratio >= 1.0:
            nom_w, nom_h = 768 * ratio, 768.0
        else:
            nom_w, nom_h = 768.0, 768 / ratio
        if nom_w * nom_h > 768 * 1344:
            s = math.sqrt((768 * 1344) / (nom_w * nom_h))
            nom_w, nom_h = nom_w * s, nom_h * s
        return (max(32, round(nom_w / 32) * 32), max(32, round(nom_h / 32) * 32))


# Deterministic, mirroring the core's habit of restarting the same RNG stream for
# every condition latent, so a given strength always yields the same guide.
#
# Unlike core we vary the stream *per role*: core reuses one stream for every
# condition, which is harmless when only one latent is diluted, but if both
# keyframes are weakened they would receive the identical noise field at both ends
# of the clip -- a static texture the model can lock onto. The seeds are pinned to
# the role, not to list position, so a frame's noise never changes based on whether
# the other frame happens to be connected.
LAST_FRAME_NOISE_SEED = 0
FIRST_FRAME_NOISE_SEED = 1
MIDDLE_FRAME_NOISE_SEED_BASE = 100  # middle N gets 100 + N, well clear of the two above

# Used when middle frames are connected but the spec is left empty: waypoints spread
# evenly through the clip at a deliberately loose strength.
AUTO_MIDDLE_STRENGTH = 1.0  # MUST match web/h3_timeline.js

# Reference images default to a full-strength identity lock; the spec overrides.
AUTO_REF_STRENGTH = 1.0
REF_NOISE_SEED_BASE = 200
REF_AUDIO_NOISE_SEED_BASE = 300
REF_VIDEO_NOISE_SEED_BASE = 400
REF_VIDEO_AUDIO_NOISE_SEED_BASE = 500
V2V_NOISE_SEED = 600      # v2v structure scramble (video then audio stream)
REF_VIDEO_MAX_SECONDS = 15  # core's stated trained range for reference videos
CANVAS_MULTIPLE = 32
REF_IMAGE_SHORT_EDGE = 2048

# Motion context: the previous clip's tail pinned at the new clip's head, so
# instantaneous motion (velocity, direction) and the actual audio waveform
# continue through a join instead of being re-decided / imitated. Technique
# absorbed from ComfyUI-H3-Motion-Context (see README credit); Peter
# render-verified their approach before we adopted it natively.
MC_FRAME_PER_TOKEN = (1, 4, 4, 4, 4)  # pixel frames covered by latent step k%5
MC_VIDEO_RUN_GRID = (39, 22, 5, 1)    # run lengths the video VAE distinguishes
MC_AUDIO_END_KEY = "minimax_mc_audio_end_frame"


def mc_step_offsets(latent_t):
    """Pixel-frame index at which each latent step of a video run begins.

    The exclusive cumulative sum of MC_FRAME_PER_TOKEN: [0, 1, 5, 9, 13, 17, 18, ...].
    Each step becomes one keyframe anchored at exactly this offset, so the pinned
    content and its RoPE time coordinate agree.
    """
    out, acc = [], 0
    for k in range(latent_t):
        out.append(acc)
        acc += MC_FRAME_PER_TOKEN[k % 5]
    return out


def mc_pixel_frames(latent_t):
    """Pixel frames covered by latent_t latent steps of a video run."""
    return sum(MC_FRAME_PER_TOKEN[k % 5] for k in range(latent_t))


def parse_ref_spec(spec, count, field="ref_spec"):
    """Parse a strength spec: one 'strength' per reference, or empty for 1.0."""
    lines = [ln.strip() for ln in spec.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if count and not lines:
        return [AUTO_REF_STRENGTH] * count
    if len(lines) != count:
        raise ValueError(
            "MiniMax H3 Guide: %s has %d line(s) but %d reference(s) are "
            "connected. One strength per reference, or leave it empty for %.1f."
            % (field, len(lines), count, AUTO_REF_STRENGTH))
    out = []
    for i, line in enumerate(lines):
        try:
            s = float(line.split(",")[0].strip())
        except ValueError:
            raise ValueError("MiniMax H3 Guide: %s line %d (%r) must be a number."
                             % (field, i + 1, line))
        if not 0.0 <= s <= 2.0:
            raise ValueError("MiniMax H3 Guide: %s strength on line %d must be 0.0-2.0, "
                             "got %s" % (field, i + 1, s))
        out.append(s)
    return out


def encode_ref_image(vae, img, width, height, ref_image_size, megapixels=0.0):
    """Mirror the core reference node's image sizing, then VAE-encode.

    'match' scales the reference (down only, keeping aspect) to the generation's pixel
    area; 'max' uses the reference pipeline's 2048px short edge for best identity
    fidelity. Reference rows ride through every sampling step, so 'max' is costly.
    A megapixels value > 0 overrides both with an explicit area cap (down only) --
    keyframes never need this (they are stretched to the canvas regardless), so it
    applies to references alone.
    """
    h, w = img.shape[1], img.shape[2]
    if megapixels > 0.0:
        scale = min(1.0, math.sqrt((megapixels * 1_000_000.0) / (w * h)))
    elif ref_image_size == "match":
        scale = min(1.0, math.sqrt((width * height) / (w * h)))
    else:
        scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(w, h))
    tw = max(CANVAS_MULTIPLE, round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    th = max(CANVAS_MULTIPLE, round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    resized = _resize(img[:1], tw, th, "disabled")
    return resized, {"kind": "image", "latent_h": th // 16, "latent_w": tw // 16,
                     "latent": vae.encode(resized)}


def file_lines(text):
    return [ln.strip() for ln in (text or "").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def parse_crop_spec(text, count, field="crops"):
    """Crop lines -> [(cx, cy, zoom) | None] * count, padded/truncated to count.

    One line per image in the combined (sockets-then-files) ordering; '-' means no
    crop. Written by the editor's framing tool, hand-editable like the other specs.
    """
    all_lines = file_lines(text)
    if len(all_lines) > count:
        logging.warning("MiniMaxH3Guide: %s has %d framing line(s) for %d item(s) -- extra "
                        "lines ignored; check for a stale framing after removing something.",
                        field, len(all_lines), count)
    out = []
    for i, line in enumerate(all_lines[:count]):
        if line in ("-", "none"):
            out.append(None)
            continue
        p = [x.strip() for x in line.split(",")]
        if len(p) != 3:
            raise ValueError("MiniMax H3 Guide: %s line %d (%r) must be "
                             "'center_x, center_y, zoom' or '-'." % (field, i + 1, line))
        try:
            cx, cy, z = float(p[0]), float(p[1]), float(p[2])
        except ValueError:
            raise ValueError("MiniMax H3 Guide: %s line %d (%r) -- values must be numbers."
                             % (field, i + 1, line))
        out.append((min(max(cx, 0.0), 1.0), min(max(cy, 0.0), 1.0), max(1.0, z)))
    while len(out) < count:
        out.append(None)
    return out


def apply_crop(img, crop, out_w=None, out_h=None):
    """Crop [B,H,W,C] pixels to a window chosen by (cx, cy, zoom).

    With out_w/out_h given, the window is locked to that aspect (keyframes: the
    generation canvas), so the downstream resize is distortion-free by
    construction. Without them the window keeps the source aspect (references:
    pure zoom/pan into the picture). Same crop_box maths as the Zoom & Pan loader.
    """
    if crop is None:
        return img
    h, w = img.shape[1], img.shape[2]
    if out_w is None:
        out_w, out_h = w, h
    (x0, y0, x1, y1), _ = crop_box(w, h, out_w, out_h, crop[2], crop[0], crop[1])
    return img[:, y0:y1, x0:x1, :]


def load_input_audio(name, field):
    """Load an audio file from the input folder as {waveform [1,C,L], sample_rate}.

    Decodes through comfy's own audio loader (PyAV) when available, so wav / mp3 /
    m4a / flac all work — the same formats LoadAudio accepts. The editor's mic
    recorder uploads 16-bit WAV, which needs no special handling anywhere.
    """
    if not folder_paths.exists_annotated_filepath(name):
        raise ValueError("MiniMax H3 Guide: %s names %r, which is not in the input folder."
                         % (field, name))
    path = folder_paths.get_annotated_filepath(name)
    try:
        from comfy_extras.nodes_audio import load as _load_audio
        waveform, sr = _load_audio(path)          # [C, L]
    except ImportError:
        waveform, sr = torchaudio.load(path)
    return {"waveform": waveform.unsqueeze(0), "sample_rate": sr}


def load_input_image(name, field):
    """Load an image from the ComfyUI input folder as [1, H, W, 3] float32.

    The *_file inputs let the node source images itself (the timeline UI's picker
    writes them), so unconnected slots don't need external Load Image nodes. A
    connected socket always wins over its file field.
    """
    if not folder_paths.exists_annotated_filepath(name):
        raise ValueError("MiniMax H3 Guide: %s names %r, which is not in the input folder."
                         % (field, name))
    img = node_helpers.pillow(Image.open, folder_paths.get_annotated_filepath(name))
    img = node_helpers.pillow(ImageOps.exif_transpose, img).convert("RGB")
    return torch.from_numpy(np.array(img).astype(np.float32) / 255.0)[None]


def mask_to_latent_grid(mask, lh, lw):
    """MASK [B,H,W] (1 = keep) -> [1,1,1,lh,lw], area-downsampled to the latent grid.

    'area' rather than a filtered resize: averaging over each latent cell is what a
    coverage fraction means, and it avoids ringing pushing values outside 0..1.
    """
    m = mask if mask.ndim == 3 else mask[None]
    m = m[:1].unsqueeze(1).to(torch.float32)                     # [1,1,H,W]
    m = torch.nn.functional.interpolate(m, size=(lh, lw), mode="area")
    return m.clamp(0.0, 1.0).unsqueeze(2)                        # [1,1,1,lh,lw]


def mask_to_pixel_grid(mask, h, w):
    """MASK [B,H,W] -> [1,h,w,1] for compositing against an image."""
    m = mask if mask.ndim == 3 else mask[None]
    m = m[:1].unsqueeze(1).to(torch.float32)
    m = torch.nn.functional.interpolate(m, size=(h, w), mode="bilinear", align_corners=False)
    return m.clamp(0.0, 1.0).movedim(1, -1)                      # [1,h,w,1]


def weaken_cond_latent_masked(z, strength_map, seed):
    """Spatially-varying version of weaken_cond_latent.

    strength_map broadcasts against z. The single expression covers both regimes the
    scalar function splits on: where s <= 1 it is the variance-preserving blend
    s*z + sqrt(1-s^2)*noise, and where s > 1 the noise term falls to zero, leaving the
    plain amplification s*z. With a uniform map it reproduces the scalar path exactly,
    same RNG stream included.
    """
    z = z.to(torch.float32)
    s = strength_map.to(z.device, torch.float32)
    gen = torch.Generator("cpu").manual_seed(int(seed))
    noise = torch.randn(z.shape, generator=gen, dtype=torch.float32).to(z.device)
    keep = s.clamp(max=1.0)
    return s * z + (1.0 - keep) * noise


def load_input_video(name, field, max_seconds=REF_VIDEO_MAX_SECONDS):
    """Load a video file from the input/output folder -> (frames [T,H,W,C], audio|None).

    Decodes via comfy's VideoFromFile (PyAV: mp4/webm/mov/mkv all work) and
    resamples to H3's 24 fps by frame index. max_seconds caps the result (the
    trained 15 s for reference videos); None keeps the whole file -- the v2v
    path needs that, because its section is cut in SOURCE time and may sit past
    15 s. (Decode is whole-file either way; the cap only trims what we keep.)
    """
    if not folder_paths.exists_annotated_filepath(name):
        raise ValueError("MiniMax H3 Guide: %s names %r, which is not in the input/output "
                         "folder." % (field, name))
    comp = InputImpl.VideoFromFile(folder_paths.get_annotated_filepath(name)).get_components()
    frames = comp.images
    if frames is None or frames.shape[0] == 0:
        raise ValueError("MiniMax H3 Guide: %r has no video frames." % name)
    fps = float(comp.frame_rate) if comp.frame_rate else float(FPS_HINT)
    duration = frames.shape[0] / max(fps, 1e-6)
    n_out = int(duration * FPS_HINT)
    if max_seconds is not None and n_out > max_seconds * FPS_HINT:
        n_out = max_seconds * FPS_HINT
        logging.info("MiniMaxH3Guide: %s %r is %.1fs -- keeping the first %ds "
                     "(trained reference range).", field, name, duration, max_seconds)
    if n_out < 5:
        raise ValueError("MiniMax H3 Guide: %r is under ~0.2s — reference videos need at "
                         "least 5 frames at 24 fps." % name)
    idx = [min(frames.shape[0] - 1, int(round(i * fps / FPS_HINT))) for i in range(n_out)]
    frames = frames[idx]
    audio = None
    if comp.audio is not None and comp.audio.get("waveform") is not None:
        wf = comp.audio["waveform"]
        if wf.ndim == 2:
            wf = wf.unsqueeze(0)
        sr = comp.audio["sample_rate"]
        keep = None if max_seconds is None else max_seconds * sr
        audio = {"waveform": wf[..., :keep] if keep else wf, "sample_rate": sr}
    return frames, audio


def encode_ref_video(vae, audio_vae, frames, soundtrack, frame_count, megapixels=0.0, label=""):
    """Port of the core reference node's video path, plus an optional MP cap.

    megapixels > 0 replaces core's adapt_canvas with an aspect-preserving area cap
    (down only, per-axis rounded to 32 -- never a squish); 0 keeps core's
    768-short-edge canvas rule. Returns (has_soundtrack, tokenizer_item, dit_block).
    """
    vh, vw = frames.shape[1], frames.shape[2]
    if megapixels > 0.0:
        scale = min(1.0, math.sqrt((megapixels * 1_000_000.0) / (vw * vh)))
        cw = max(CANVAS_MULTIPLE, round(vw * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        ch = max(CANVAS_MULTIPLE, round(vh * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    else:
        cw, ch = adapt_canvas(vw, vh)
        if vw * vh < cw * ch:  # never upscale a small source
            cw = max(CANVAS_MULTIPLE, round(vw / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            ch = max(CANVAS_MULTIPLE, round(vh / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    fr = _resize(frames, cw, ch, "disabled")
    if fr.shape[0] > frame_count:
        fr = fr[:frame_count]
    n = fr.shape[0]
    if n < 5:
        raise ValueError("MiniMax H3 Guide: %s needs at least 5 frames "
                         "(~0.2s at 24 fps)" % (label or "a reference video"))
    while n % 17 != 5:
        n -= 1
    fr = fr[:n]
    z = vae.encode(fr)
    audio_latent, ref_audio_t = None, 0
    if soundtrack is not None:
        audio_latent, ref_audio_t = encode_ref_audio(audio_vae, soundtrack)
    # Qwen sees the video at 2 fps with timestamps, exactly as core presents it
    sample_idx = list(range(0, fr.shape[0], FPS_HINT // 2))
    item = {"type": "video", "data": fr[sample_idx],
            "timestamps": [i / 2.0 for i in range(len(sample_idx))]}
    block = {"kind": "video_audio" if ref_audio_t else "video",
             "latent_t": z.shape[2], "latent_h": ch // 16, "latent_w": cw // 16,
             "ref_audio_t": ref_audio_t, "latent": z, "audio_latent": audio_latent}
    return soundtrack is not None, item, block


def encode_ref_audio(audio_vae, audio):
    """Resample to the audio VAE's rate and encode. Mirrors the core reference node."""
    waveform = audio["waveform"]  # [B, C, L]
    sr = audio["sample_rate"]
    vae_sr = getattr(audio_vae, "audio_sample_rate", 32000)
    if sr != vae_sr:
        waveform = torchaudio.functional.resample(waveform, sr, vae_sr)
    z = audio_vae.encode(waveform[:1].movedim(1, -1))  # [1, 32, 2, T]
    return z, z.shape[-1]


def parse_middle_spec(spec, count, frame_count):
    """Parse the middle-frame spec block into one entry per connected middle frame.

    One line per frame, in the order the middle_frame_ slots are filled:

        position, strength[, description]

    `position` is a fraction of the clip when <= 1.0 (0.5 = halfway), or an absolute
    frame number when > 1.0. Anchors are clamped to the interior, since frame 0 and
    the final frame belong to first_frame / last_frame.
    """
    lines = [ln.strip() for ln in spec.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    auto = not lines  # comment-only specs count as auto too (bug-hunt finding)
    if count and not lines:
        # Empty spec: spread the waypoints evenly and keep them loose. Positions are
        # logged rather than left implicit, so it is obvious what was chosen.
        auto = ["%.4f, %s" % ((i + 1) / (count + 1.0), AUTO_MIDDLE_STRENGTH)
                for i in range(count)]
        logging.info("MiniMaxH3Guide: middle_frame_spec empty -- auto-placing %d waypoint(s) "
                     "at %s (strength %s). Fill the spec to override.",
                     count, ", ".join(a.split(",")[0] for a in auto), AUTO_MIDDLE_STRENGTH)
        lines = auto
    if len(lines) != count:
        raise ValueError(
            "MiniMax H3 Guide: middle_frame_spec has %d line(s) but %d middle frame(s) are "
            "connected. They pair up in order, so the counts must match -- or clear the "
            "spec entirely to have the waypoints spaced automatically."
            % (len(lines), count))

    interior_lo, interior_hi = 1, max(1, frame_count - 2)
    out = []
    for i, line in enumerate(lines):
        parts = [p.strip() for p in line.split(",", 2)]
        if len(parts) < 2:
            raise ValueError(
                "MiniMax H3 Guide: middle_frame_spec line %d (%r) needs at least "
                "'position, strength'." % (i + 1, line))
        try:
            pos, strength = float(parts[0]), float(parts[1])
        except ValueError:
            raise ValueError(
                "MiniMax H3 Guide: middle_frame_spec line %d (%r) -- position and strength "
                "must be numbers." % (i + 1, line))
        if not (math.isfinite(pos) and math.isfinite(strength)):
            raise ValueError(
                "MiniMax H3 Guide: middle_frame_spec line %d (%r) -- position and strength "
                "must be finite numbers." % (i + 1, line))
        if not 0.0 <= strength <= 2.0:
            raise ValueError("MiniMax H3 Guide: middle_frame_spec strength on line %d must "
                             "be 0.0-2.0, got %s" % (i + 1, strength))
        # half-UP rounding: banker's collapsed half-frame positions a full frame
        # apart into spurious duplicates (edge-hunt finding). JS mirrors this.
        x = pos * (frame_count - 1) if pos <= 1.0 else pos
        index = int(math.floor(x + 0.5))
        index = min(max(int(index), interior_lo), interior_hi)
        out.append({"index": index, "strength": strength, "slot": i,
                    "text": parts[2].strip() if len(parts) > 2 else ""})

    if len(set(e["index"] for e in out)) != len(out):
        if not auto:
            raise ValueError("MiniMax H3 Guide: two middle frames resolved to the same frame "
                             "index. Spread the positions further apart, or lengthen the clip.")
        # auto-placement must never fail on its own rounding: nudge collisions apart
        used = set()
        for e in out:
            idx = e["index"]
            while idx in used:
                idx += 1
            if idx > interior_hi:
                raise ValueError("MiniMax H3 Guide: clip too short for %d middle frames "
                                 "(only %d interior frame(s)). Lengthen the clip or "
                                 "disconnect some." % (len(out), interior_hi - interior_lo + 1))
            used.add(idx)
            e["index"] = idx
    # line order == slot order; the caller sorts the image/entry pairs by index
    return out


TIMED_TEXT_MODES = ["text only", "rope + text", "rope only"]


def parse_timed_text(spec, frame_count):
    """Parse the timed-text block: one 'position, text' per line, sorted by time."""
    out = []
    for i, raw in enumerate(spec.splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",", 1)
        if len(parts) < 2 or not parts[1].strip():
            raise ValueError("MiniMax H3 Guide: timed_text line %d (%r) needs "
                             "'position, some text'." % (i + 1, line))
        try:
            pos = float(parts[0].strip())
        except ValueError:
            raise ValueError("MiniMax H3 Guide: timed_text line %d (%r) -- position must be "
                             "a number (fraction of the clip, or a frame number above 1.0)."
                             % (i + 1, line))
        if not math.isfinite(pos):
            raise ValueError("MiniMax H3 Guide: timed_text line %d (%r) -- position must be "
                             "a finite number." % (i + 1, line))
        x = pos * (frame_count - 1) if pos <= 1.0 else pos
        index = int(math.floor(x + 0.5))
        out.append({"index": min(max(index, 0), frame_count - 1),
                    "text": parts[1].strip()})
    return sorted(out, key=lambda e: e["index"])


def text_ids_fn(clip):
    """The tokenizer's raw text->ids helper, or None if this isn't a MiniMax H3 clip."""
    tok = getattr(clip, "tokenizer", None)
    fn = getattr(tok, "_text_ids", None)
    return fn if callable(fn) else None


def beat_spans(text_ids, full_prompt, fragments):
    """Token spans of each fragment inside full_prompt, as offsets from the sequence end.

    Measured cumulatively rather than by tokenising fragments alone, so tokens that
    merge across a fragment boundary land on the correct side. End-relative because
    the prompt is always emitted last, after the vision blocks -- so these offsets
    hold whatever the images expand to.
    """
    total = len(text_ids(full_prompt))
    spans = []
    for prefix_before, prefix_after in fragments:
        start = len(text_ids(prefix_before))
        stop = len(text_ids(prefix_after))
        if start >= stop or stop > total:
            return None
        spans.append((total - start, total - stop))
    return spans


def weaken_cond_latent(z, strength, seed=LAST_FRAME_NOISE_SEED):
    """Dilute a condition latent toward noise. 1.0 = untouched, 0.0 = pure noise.

    LINEAR blend s*z + (1-s)*noise — the flow-matching forward process, the same
    form core's global noise-aug uses. (The earlier variance-preserving sphere
    blend put sqrt(1-s^2) of noise power in the row — at 0.65 that is 76% noise
    instead of 35%, which the model happily painted as texture distortion.)

    Above 1.0 the latent is amplified instead (no noise added), overdriving the
    model's pull toward the frame.
    """
    if strength >= 1.0:
        return z * strength if strength > 1.0 else z
    z = z.to(torch.float32)
    gen = torch.Generator("cpu").manual_seed(int(seed))
    noise = torch.randn(z.shape, generator=gen, dtype=torch.float32)
    return strength * z + (1.0 - strength) * noise.to(z.device)


def flow_blend(z, a, seed):
    """a*z + (1-a)*noise: the flow forward at time a, role-pinned seed.

    Used with the per-row label patch: the row's modulation timestep becomes a,
    so the model reads the noise as UNCERTAINTY (in-distribution) instead of
    content. a = strength * VISUAL_COND_TIMESTEP.
    """
    if a >= 1.0:
        return z
    z = z.to(torch.float32)
    gen = torch.Generator("cpu").manual_seed(int(seed))
    noise = torch.randn(z.shape, generator=gen, dtype=torch.float32)
    return a * z + (1.0 - a) * noise.to(z.device)


def row_aug_ready():
    """Per-row labels just need the source patch. Turbo included: its rebuilt
    adaln E-grid interpolates at arbitrary timesteps and turbo_compat's
    correct_unique_t feeds it the per-row values, while segment assignment
    comes from the same patched _forward table."""
    return h3_row_aug_patch.install()


class MiniMaxH3ImageToVideoGuide(io.ComfyNode):
    """t2va and fl2va with an independent strength dial on each keyframe."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="MiniMaxH3ImageToVideoGuide",
            display_name="MiniMax H3 Image to Video (Guide)",
            category="model/conditioning/minimax",
            description="MiniMax H3 first/last frame conditioning with an adjustable strength per keyframe, so either frame can guide the motion instead of pinning it.",
            inputs=[
                io.Clip.Input("clip"),
                io.Vae.Input("vae"),
                io.String.Input("prompt", multiline=True, dynamic_prompts=True),
                io.Int.Input("width", default=1344, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("height", default=768, min=32, max=nodes.MAX_RESOLUTION, step=32),
                io.Int.Input("length", default=124, min=5, max=3600, step=17, tooltip="Frame count at 24 fps, snapped up to the model's 17k+5 grid (124 = ~5s; trained range is ~124-362, longer is untested)"),
                io.Float.Input("first_frame_strength", default=1.0, min=0.0, max=2.0, step=0.05,
                    tooltip="How hard the start frame is enforced. 1.0 matches the stock node (opens exactly on the frame). Lower values dilute the start keyframe toward noise so it reads as a direction rather than a fixed opening -- useful when the first frame is a rough comp or the wrong crop, or to let the model reinterpret the subject. Note the start frame also sets the shot's geometry, so it usually wants a higher value than the end frame. Above 1.0 amplifies the latent and overdrives adherence. No effect unless a first frame is connected or file-picked."),
                io.Float.Input("last_frame_strength", default=1.0, min=0.0, max=2.0, step=0.05,
                    tooltip="How hard the end frame is enforced. 1.0 matches the stock node (lands on the frame). Lower values dilute the end keyframe toward noise so it reads as a direction rather than a destination -- 0.6-0.8 keeps composition and colour while freeing the motion, 0.3-0.5 is a loose hint, 0.0 ignores it. Above 1.0 amplifies the latent and overdrives adherence. No effect unless a last frame is connected or file-picked."),
                io.Image.Input("first_frame", optional=True),
                io.Image.Input("last_frame", optional=True),
                io.String.Input("first_frame_file", default="",
                    tooltip="Load the first frame straight from the input folder (the timeline UI's picker writes this). Ignored when the first_frame socket is connected -- sockets always win, so Zoom & Pan / graph sources keep working."),
                io.String.Input("last_frame_file", default="",
                    tooltip="Load the last frame straight from the input folder. Ignored when the last_frame socket is connected."),
                io.String.Input("middle_frame_files", multiline=True, default="",
                    tooltip="Extra middle frames loaded straight from the input folder, one filename per line (the timeline UI's picker writes this). These come AFTER any connected middle_frame_ sockets in the ordering that middle_frame_spec lines pair with. File-based frames can't take ref masks."),
                io.String.Input("ref_image_files", multiline=True, default="",
                    tooltip="Extra reference images loaded straight from the input folder, one filename per line. These come AFTER any connected ref_image_ sockets in the ordering ref_spec pairs with. File-based references can't take ref masks (masks pair with socket slots)."),
                io.String.Input("ref_video_spec", multiline=True, default="",
                    tooltip="OPTIONAL. One strength per reference video, in sockets-then-files order. Empty = 1.0. Applies to the video latent AND its soundtrack together."),
                io.String.Input("ref_video_files", multiline=True, default="",
                    tooltip="Reference videos loaded straight from the input/output folder, one filename per line (mp4/webm/mov -- the editor's picker writes this, and 'name [output]' chains from a previous render). Resampled to 24 fps, capped at 15s; an embedded soundtrack is used automatically (needs audio_vae)."),
                io.Float.Input("ref_video_megapixels", default=0.0, min=0.0, max=4.0, step=0.05,
                    tooltip="Optional area cap for reference video frames, e.g. 0.4. Aspect-preserving (never a squish), down-only, per-axis rounded to the model's 32px grid. 0 = core's 768-short-edge canvas rule. Video reference rows ride through EVERY sampling step multiplied by their duration, so this is the single biggest speed dial in the pack."),
                io.Autogrow.Input("ref_videos", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref_video", tooltip="Reference video frames at 24 fps (2-15s), e.g. from a video loader. Conditions motion/identity for the whole clip; cite as <Video N> in the prompt."),
                        prefix="ref_video_", min=0, max=3)),
                io.Autogrow.Input("ref_video_audios", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Audio.Input("ref_video_audio", tooltip="Soundtrack of the SAME-NUMBERED ref_video socket (ref_video_audio_0 pairs with ref_video_0)."),
                        prefix="ref_video_audio_", min=0, max=3)),
                io.String.Input("ref_audio_files", multiline=True, default="",
                    tooltip="Reference audio loaded straight from the input folder, one filename per line (wav/mp3/m4a/flac -- the editor's picker and mic recorder write this). These come AFTER any connected ref_audio_ sockets. Needs audio_vae connected, like all reference audio."),
                io.String.Input("first_frame_crop", default="",
                    tooltip="Optional framing for the first frame: 'center_x, center_y, zoom' (set by the editor's framing tool). The crop window is locked to the generation's aspect, so what the model sees is exactly what you framed -- no stretch, no blind centre-crop. Empty or '-' = whole image."),
                io.String.Input("last_frame_crop", default="",
                    tooltip="Optional framing for the last frame, same format as first_frame_crop."),
                io.String.Input("middle_frame_crops", multiline=True, default="",
                    tooltip="Optional framing per middle frame, one line per image in the same sockets-then-files order as middle_frame_spec: 'center_x, center_y, zoom' or '-' for none. Windows are locked to the generation's aspect."),
                io.String.Input("ref_image_crops", multiline=True, default="",
                    tooltip="Optional framing per reference image, one line per image in the same order as ref_spec: 'center_x, center_y, zoom' or '-'. Reference windows keep the SOURCE aspect (pure zoom/pan) -- references aren't resized to the canvas, so there is no aspect to match, just content to choose."),
                io.String.Input("middle_frame_spec", multiline=True, default="",
                    tooltip="OPTIONAL. Leave empty and connected middle frames are spaced evenly through the clip at strength 0.6 (the positions chosen are printed to the log).\n\nTo place them yourself, one line per connected middle frame, paired in slot order:\n\n    position, strength[, description]\n\nposition is a fraction of the clip (0.5 = halfway) or an absolute frame number if above 1.0. strength works exactly like the first/last dials. description is optional -- it is appended to the prompt as a labelled '<Picture N> at N.Ns: ...' note so the text encoder knows what that frame is meant to show.\n\nExample:\n    0.33, 0.7, boat halfway out of frame\n    0.66, 0.6, boat near the horizon\n\nEither fill in a line for every frame or leave it completely empty -- a partial spec is ambiguous and will error. Lines starting with # are ignored. EXPERIMENTAL: H3 is trained on first/last anchors only."),
                io.String.Input("timed_text", multiline=True, default="",
                    tooltip="EXPERIMENTAL. Beats of description pinned to moments in the clip, one per line:\n\n    position, what happens\n\nposition is a fraction of the clip (0.5 = halfway) or an absolute frame number above 1.0. No image needed -- this is text, not a keyframe.\n\nExample:\n    0.2, the boat pulls away from the dock\n    0.8, only wake remains on the water\n\nSee timed_text_mode for how the timing is delivered. Lines starting with # are ignored."),
                io.Combo.Input("timed_text_mode", options=TIMED_TEXT_MODES, default="text only",
                    tooltip="How timed_text beats reach the model.\n\n'text only' (default, safe): appended as plain prose, 'At 2.0 seconds: the boat pulls away.' Pure prompting -- the model has seen <N.N seconds> labels in its reference-video training, so this vocabulary is familiar. No patching.\n\n'rope + text': the same prose, but the beat's tokens are ALSO moved onto the video timeline so RoPE places them at that moment. Degrades gracefully to 'text only' if the positional trick does nothing.\n\n'rope only': positions moved, no time words. The purest test of whether the mechanism works -- and the most likely to simply fail.\n\nThe rope modes are a real distribution violation: text has occupied its own coordinate range in every sample the model ever saw. Try 'text only' first."),
                io.String.Input("ref_spec", multiline=True, default="",
                    tooltip="OPTIONAL. One strength per connected reference image, in slot order. Leave empty for 1.0 (a firm identity lock). Lower values soften the reference into a suggestion -- 0.7 keeps the likeness while letting the model adapt pose and lighting.\n\nExample:\n    1.0\n    0.7\n\nEither one line per image or completely empty."),
                io.Combo.Input("ref_image_size", options=["match", "max"], default="match",
                    tooltip="Reference image sizing. 'match' scales each reference (down only, keeping aspect) to the generation's pixel area. 'max' uses the reference pipeline's 2048px short edge for best identity fidelity. Reference rows ride through EVERY sampling step, so 'max' can be several times slower. Ignored when ref_megapixels is set."),
                io.Float.Input("ref_megapixels", default=0.0, min=0.0, max=8.0, step=0.05,
                    tooltip="Optional area cap for reference images, in megapixels -- e.g. 0.4. Overrides ref_image_size when above 0, scaling each reference down (never up) to at most this many pixels, so no external resize nodes are needed. Keyframe inputs never need capping: they are always stretched to the generation canvas anyway. 0 = off."),
                io.Float.Input("ref_audio_strength", default=1.0, min=0.0, max=2.0, step=0.05,
                    tooltip="Strength for every connected reference audio. 1.0 is a firm lock on the voice/sound character; lower loosens it. Note H3 leaves audio conditioning completely clean by default (its audio_cond_noise_aug is 1.0, i.e. no noise at all), so this dial is the only way to soften an audio reference."),
                io.Vae.Input("audio_vae", optional=True,
                    tooltip="The MiniMax H3 audio VAE. Required only when a reference audio is connected."),
                io.Autogrow.Input("ref_images", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("ref_image", tooltip="Identity/subject reference for the whole clip -- not tied to a moment. Refer to it in the prompt by its <Picture N> number (printed to the log when you run)."),
                        prefix="ref_image_", min=0, max=4)),
                io.Boolean.Input("mask_ref_pixels", default=False,
                    tooltip="What a reference mask applies to.\n\nOFF (default): only the condition latent is masked. The text encoder still sees the whole reference photo, so it can describe the background -- conservative, and the usual choice.\n\nON: the excluded area is also greyed out in the image handed to the text encoder, so it describes only the kept region. More consistent, but the encoder sees a hard-edged cutout, which it may read oddly."),
                # -- NEW WIDGET INPUTS GO HERE, AT THE END, ALWAYS. The frontend
                # serializes widgets_values positionally; inserting a widget
                # mid-list scrambles every older save (bug-hunt wave 3 finding).
                io.String.Input("ref_video_crops", multiline=True, default="",
                    tooltip="Optional framing per reference video, one line per video in the same sockets-then-files order as ref_video_spec: 'center_x, center_y, zoom' or '-'. The window keeps the SOURCE aspect (pure zoom/pan into the footage) and applies before sizing, so the MP cap operates on the cropped region. Set by the editor's framing tool -- essential when continuing a clip of a different aspect."),
                io.String.Input("v2v_video_file", default="",
                    tooltip="Video-to-video source loaded straight from the input/output folder (the editor's v2v bar writes this; 'name [output]' restyles a previous render). When set (or when v2v_images is connected), the node's LATENT output becomes this footage encoded for partial denoise instead of an empty latent -- sample at denoise 0.3-0.7 to restyle while keeping the motion. The clip length follows the footage; the length widget is ignored. EXPERIMENTAL."),
                io.Float.Input("v2v_start_seconds", default=0.0, min=0.0, max=10000.0, step=0.1,
                    tooltip="Restyle only a section of the v2v source: start, in seconds."),
                io.Float.Input("v2v_end_seconds", default=0.0, min=0.0, max=10000.0, step=0.1,
                    tooltip="Section end in seconds; 0 = to the end of the source."),
                io.Float.Input("v2v_denoise", default=0.55, min=0.0, max=1.0, step=0.05,
                    tooltip="The v2v restyle amount, as a VALUE THIS NODE EMITS (third output): wire it to H3 Basic Scheduler's denoise input -> SamplerCustom/sigmas, and the editor's v2v bar controls the whole restyle. Has no effect on this node's own outputs otherwise; with a plain KSampler just set its denoise to match. ~0.3 barely touches the footage, 0.4-0.7 restyles keeping motion, 1.0 ignores it.\n\nWith NO v2v source connected the output is forced to 1.0, since the latent is then an empty one that must denoise in full -- so the same wired graph does normal generation and restyles without rewiring."),
                io.String.Input("v2v_crop", default="",
                    tooltip="Optional framing for the v2v source: 'center_x, center_y, zoom' (the editor's v2v ⛶ writes this). The window is locked to the width x height widgets' aspect and the footage is resized to exactly that canvas -- so WITH a framing, width/height matter again (reframe landscape footage into a vertical clip, etc.). Without one, the canvas follows the footage and width/height are ignored."),
                io.String.Input("motion_context_file", default="",
                    tooltip="Continue WITH MOTION from this video (input/output folder; 'name [output]' chains from a previous render -- the editor's ⏭▶ writes this). Its tail frames are pinned at the head of the new clip ON ITS OWN TIMELINE, so instantaneous motion carries through the join instead of being re-decided from a still, and the tail audio is continued as the same waveform rather than imitated. The pinned frames come back at the start of the render; the editor trims them automatically when the clip joins the reel. When set, first_frame is ignored (the context IS the opening). Technique credit: ComfyUI-H3-Motion-Context."),
                io.Float.Input("motion_context_end_seconds", default=0.0, min=0.0, max=10000.0, step=0.1,
                    tooltip="Continue from this moment of the context video, in seconds (0 = its end). The reel's ⏭▶ passes the card's out-trim, so the join lands exactly at the cut."),
                io.Int.Input("motion_context_frames", default=22, min=1, max=39,
                    tooltip="How many tail frames to pin. The video VAE only distinguishes runs of 5, 22 and 39 -- anything else snaps DOWN to the nearest. 22 (default) is nearly seamless; 5 is just barely fluid but much cheaper; 39 is untested. Every pinned row rides through all sampling steps, so this is also a speed dial."),
                io.Int.Input("motion_context_audio_frames", default=22, min=0, max=240,
                    tooltip="Tail AUDIO to pin, in frames at 24 fps, END-ALIGNED with the pinned video so both finish at the join -- the model continues the actual waveform (same recording) instead of playing a sound-alike. 0 = no audio context. Needs audio_vae when above 0 and the context video has sound. 22 overlays the video window exactly (the tested configuration)."),
                io.Float.Input("v2v_noise", default=0.0, min=0.0, max=1.0, step=0.05,
                    tooltip="Scramble the v2v footage BEFORE sampling: the latent is blended toward noise by this fraction (0 = untouched footage). This is the dial for 'the restyle keeps handing me back the same material'. Raising denoise alone often can't break it: the footage's structure is CORRELATED across frames while the sampler's noise is not, so the model integrates the source back out of the noise. Destroying the structure up front is the one thing it cannot undo. 0.3-0.6 keeps timing and rough motion while freeing content."),
                io.Float.Input("v2v_noise_declare", default=1.0, min=0.0, max=1.0, step=0.05,
                    tooltip="How much of v2v_noise is DECLARED to the sampler through the v2v_denoise output (the sigma dial).\n\n1.0 (default, safe): the emitted denoise is raised so the schedule starts where the latent actually is -- clean output, and the net effect is like a higher denoise except the source structure is genuinely gone rather than merely diluted.\n\nBelow 1.0: the sampler is told the latent is cleaner than it really is, so it commits fewer/gentler steps over already-scrambled content -- the furthest you can get from the source, at the cost of grain if you push it (undeclared noise has to land somewhere).\n\nNeeds the v2v_denoise output wired to H3 Basic Scheduler, and does nothing unless v2v_noise is above 0."),
                io.Autogrow.Input("ref_masks", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Mask.Input("ref_mask", tooltip="Optional mask for the SAME-NUMBERED ref_image (ref_mask_0 masks ref_image_0). White keeps the reference at its full strength, black dilutes it to noise. Use it to take just a face or a subject from a busy photo. Note the masked-out area still costs the same compute -- it loses influence, not sequence rows."),
                        prefix="ref_mask_", min=0, max=4)),
                io.Autogrow.Input("ref_audios", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Audio.Input("ref_audio", tooltip="Standalone reference audio for the whole clip -- a voice, a room tone, a music bed. Needs audio_vae connected. Refer to it in the prompt as <Audio N> (printed to the log when you run)."),
                        prefix="ref_audio_", min=0, max=3)),
                io.Autogrow.Input("middle_frames", optional=True,
                    template=io.Autogrow.TemplatePrefix(
                        input=io.Image.Input("middle_frame", tooltip="A waypoint the clip passes through. Position and strength come from the matching line of middle_frame_spec."),
                        prefix="middle_frame_", min=0, max=4)),
                io.Image.Input("v2v_images", optional=True,
                    tooltip="Video-to-video source frames from the graph (wins over v2v_video_file). The LATENT output becomes this footage encoded for partial denoise."),
                io.Audio.Input("v2v_audio", optional=True,
                    tooltip="Soundtrack for the v2v source (wins over a file's embedded audio). Needs audio_vae."),
            ],
            outputs=[io.Conditioning.Output(display_name="positive"), io.Latent.Output(),
                     io.Float.Output(display_name="v2v_denoise",
                                     tooltip="The restyle amount -- wire to H3 Basic Scheduler's denoise socket so the editor drives it. Forced to 1.0 when no v2v source is set (an empty latent must denoise in full), so one graph handles both generation and restyling.")],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        """Re-execute when a file-sourced image changes on disk.

        Filenames alone are part of the normal input comparison; this adds
        mtime+size so overwriting a file in the input folder invalidates the cache
        (full-content hashing would be slow for several multi-MB stills).
        """
        names = [f.strip() for f in (kwargs.get("first_frame_file", ""),
                                     kwargs.get("last_frame_file", "")) if f and f.strip()]
        for field in ("middle_frame_files", "ref_image_files", "ref_audio_files",
                      "ref_video_files"):
            names += file_lines(kwargs.get(field, ""))
        if (kwargs.get("v2v_video_file") or "").strip():
            names.append(kwargs["v2v_video_file"].strip())
        if (kwargs.get("motion_context_file") or "").strip():
            names.append(kwargs["motion_context_file"].strip())
        parts = []
        for n in names:
            try:
                st = os.stat(folder_paths.get_annotated_filepath(n))
                parts.append("%s:%d:%d" % (n, st.st_mtime_ns, st.st_size))
            except OSError:
                parts.append(n + ":missing")
        return ";".join(parts)

    @staticmethod
    def _append_frame_notes(prompt, spec, has_first):
        """Append '<Picture N> at N.Ns: description' notes for described waypoints.

        Picture numbering must match the tokenizer's, which labels images 1-based in
        the order they are handed over -- so it is the waypoint's position among the
        images, not its spec line, that decides the label.
        """
        if not any(e["text"] for e in spec):
            return prompt
        base = 2 if has_first else 1  # waypoints follow first_frame in the image list
        notes = []
        for offset, entry in enumerate(sorted(spec, key=lambda e: e["index"])):
            if not entry["text"]:
                continue
            notes.append("<Picture %d> at %.1f seconds: %s." % (
                base + offset, entry["index"] / float(FPS_HINT), entry["text"].rstrip(".")))
        joined = " ".join(notes)
        return "%s %s" % (prompt.rstrip(), joined) if prompt.strip() else joined

    @staticmethod
    def _compose_timed_text(prompt, beats, mode):
        """Append beat prose and return (prompt, [(prefix_before, prefix_after), ...]).

        The prefix pairs let beat_spans() measure each beat's token range cumulatively.
        In 'rope only' the time words are dropped, so only the description itself is
        relocated -- the position has to carry the timing on its own.
        """
        spans = []
        out = prompt.rstrip()
        for beat in beats:
            if mode == "rope only":
                fragment = beat["text"].rstrip(".") + "."
            else:
                fragment = "At %.1f seconds: %s." % (
                    beat["index"] / float(FPS_HINT), beat["text"].rstrip("."))
            before = out
            out = ("%s %s" % (out, fragment)).strip() if out else fragment
            spans.append((before, out))
        return out, spans

    @classmethod
    def execute(cls, clip, vae, prompt, width, height, length,
                first_frame_strength=1.0, last_frame_strength=1.0, middle_frame_spec="",
                timed_text="", timed_text_mode="text only",
                ref_spec="", ref_image_size="match", ref_megapixels=0.0,
                ref_audio_strength=1.0, mask_ref_pixels=False,
                first_frame_file="", last_frame_file="", middle_frame_files="",
                ref_image_files="", ref_audio_files="",
                ref_video_spec="", ref_video_files="", ref_video_megapixels=0.0,
                ref_video_crops="",
                v2v_video_file="", v2v_start_seconds=0.0, v2v_end_seconds=0.0,
                v2v_crop="", v2v_denoise=0.55,
                motion_context_file="", motion_context_end_seconds=0.0,
                motion_context_frames=22, motion_context_audio_frames=22,
                v2v_noise=0.0, v2v_noise_declare=1.0,
                first_frame_crop="", last_frame_crop="",
                middle_frame_crops="", ref_image_crops="",
                first_frame=None, last_frame=None, middle_frames=None,
                ref_images=None, ref_masks=None, ref_audios=None,
                ref_videos=None, ref_video_audios=None,
                v2v_images=None, v2v_audio=None,
                audio_vae=None) -> io.NodeOutput:
        # v2v: when source footage is given, the LATENT output becomes that
        # footage encoded for partial denoise, and the clip length follows it.
        # All conditioning (keyframes, refs, beats) still applies on top.
        v2v_frames, v2v_snd = None, None
        if v2v_images is not None:
            v2v_frames, v2v_snd = v2v_images, v2v_audio
        elif v2v_video_file.strip():
            # no 15s cap here: the section is cut in SOURCE time below, so the
            # scrubbed in/out points must be reachable anywhere in the file
            v2v_frames, v2v_snd = load_input_video(v2v_video_file.strip(), "v2v_video_file",
                                                   max_seconds=None)
            if v2v_audio is not None:
                v2v_snd = v2v_audio
        elif v2v_audio is not None:
            raise ValueError("MiniMax H3 Guide: v2v_audio is connected but there is no v2v "
                             "source (connect v2v_images or set v2v_video_file).")
        if v2v_frames is not None:
            total = v2v_frames.shape[0]
            s = max(0, min(total, int(round(v2v_start_seconds * FPS_HINT))))
            e = total if v2v_end_seconds <= 0 else max(0, min(total, int(round(v2v_end_seconds * FPS_HINT))))
            if v2v_end_seconds > 0 and v2v_end_seconds * FPS_HINT > total:
                logging.info("MiniMaxH3Guide: v2v section end %.1fs is past the %.1fs source "
                             "-- clamped to the end.", v2v_end_seconds, total / FPS_HINT)
            if e <= s:
                raise ValueError("MiniMax H3 Guide: empty v2v section -- start %.2fs to end "
                                 "%s of a %.2fs source."
                                 % (v2v_start_seconds,
                                    "the end (0)" if v2v_end_seconds <= 0
                                    else "%.2fs" % v2v_end_seconds,
                                    total / FPS_HINT))
            frames_sel = v2v_frames[s:e]
            snd_sel = v2v_snd
            if snd_sel is not None and snd_sel.get("waveform") is not None:
                sr = snd_sel["sample_rate"]
                a0, a1 = int(round(s / FPS_HINT * sr)), int(round(e / FPS_HINT * sr))
                sl = snd_sel["waveform"][..., a0:a1]
                if sl.shape[-1] < a1 - a0:
                    pad = torch.zeros(*sl.shape[:-1], (a1 - a0) - sl.shape[-1],
                                      dtype=sl.dtype, device=sl.device)
                    sl = torch.cat([sl, pad], dim=-1)
                snd_sel = {"waveform": sl, "sample_rate": sr}
            vc = parse_crop_spec(v2v_crop, 1, "v2v_crop")[0]
            if vc is not None:
                # framed v2v: the window is locked to the WIDGET canvas aspect and
                # resized to exactly that canvas -- width/height mean something
                # again (unframed v2v below lets the footage define the canvas).
                # Snap the widgets to the 32 grid FIRST: build_v2v_latent re-rounds
                # anyway, and resizing to unsnapped dims would mean a second
                # resample plus a canvas that silently differs from the promise.
                cw = max(32, int(round(width / 32)) * 32)
                ch = max(32, int(round(height / 32)) * 32)
                if (cw, ch) != (width, height):
                    logging.info("MiniMaxH3Guide: v2v framing canvas snapped to the 32 grid: "
                                 "%dx%d -> %dx%d.", width, height, cw, ch)
                frames_sel = apply_crop(frames_sel, vc, cw, ch)
                frames_sel = _resize(frames_sel, cw, ch, "disabled")
            from .h3_v2v import build_v2v_latent
            latent, frame_count = build_v2v_latent(vae, audio_vae, frames_sel, snd_sel,
                                                   FPS_HINT, 0.0, "v2v source")
            # the footage defines the canvas: keyframes, crop windows and 'match'
            # ref sizing must all conform to IT, or their spatial rope grids
            # misalign with the video rows they anchor to
            vshape = latent["samples"].tensors[0].shape
            fw, fh = vshape[-1] * 16, vshape[-2] * 16
            if (fw, fh) != (width, height):
                logging.info("MiniMaxH3Guide: v2v canvas follows the footage: %dx%d "
                             "(width/height widgets %dx%d ignored).", fw, fh, width, height)
            width, height = fw, fh
            trained_area = 768 * 1344
            if width * height > trained_area * 1.1:
                logging.warning("MiniMaxH3Guide: v2v canvas %dx%d is %.1fx the trained "
                                "~768x1344 area -- steps will be slow and quality may "
                                "drift. Frame the footage (v2v ⛶) to a smaller canvas, "
                                "or downscale the source first.",
                                width, height, width * height / trained_area)
            logging.info("MiniMaxH3Guide: v2v source active -- clip length follows the "
                         "footage (%d frames, %.1fs); the length widget is ignored. "
                         "Sample at denoise 0.3-0.7 to restyle.",
                         frame_count, frame_count / FPS_HINT)
            # Structure scramble: blend the footage latent toward noise BEFORE
            # the sampler sees it. Raising denoise alone can't reliably break
            # the source, because the footage is correlated along the time axis
            # while the sampler's noise is i.i.d. per frame -- the model
            # integrates the structure back out. Destroying it here is the one
            # thing sampling cannot undo.
            b = max(0.0, min(1.0, float(v2v_noise)))
            if b > 0.0:
                parts = list(latent["samples"].unbind())
                parts = [flow_blend(p, 1.0 - b, V2V_NOISE_SEED + i)
                         for i, p in enumerate(parts)]
                latent = {"samples": comfy.nested_tensor.NestedTensor(tuple(parts))}
                logging.info("MiniMaxH3Guide: v2v latent scrambled %.0f%% toward noise "
                             "(%.0f%% of the footage structure left).", b * 100,
                             (1.0 - b) * 100)
        else:
            latent, frame_count = _empty_av_latent(width, height, length)

        # Autogrow hands back a name->value dict; order by slot number, drop empties.
        # Slots are kept because masks pair with their SAME-NUMBERED image, not with
        # its position among the connected ones -- ref_mask_2 belongs to ref_image_2
        # even when ref_image_0 and ref_image_1 are empty.
        def slot_items(grow):
            return [(int(k.rsplit("_", 1)[-1]), grow[k])
                    for k in sorted((grow or {}).keys(),
                                    key=lambda n: int(n.rsplit("_", 1)[-1]))
                    if grow[k] is not None]

        def in_slot_order(grow):
            return [v for _, v in slot_items(grow)]

        # sockets first (slot order), then file-picked images -- the ordering every
        # spec field pairs with. Sockets win over their file fields for first/last.
        if first_frame is None and first_frame_file.strip():
            first_frame = load_input_image(first_frame_file.strip(), "first_frame_file")
        if last_frame is None and last_frame_file.strip():
            last_frame = load_input_image(last_frame_file.strip(), "last_frame_file")

        # user-chosen framing: keyframe windows are locked to the generation aspect,
        # so the stretch/cover resizes below become distortion-free by construction
        if first_frame is not None:
            first_frame = apply_crop(first_frame,
                                     parse_crop_spec(first_frame_crop, 1, "first_frame_crop")[0],
                                     width, height)
        if last_frame is not None:
            last_frame = apply_crop(last_frame,
                                    parse_crop_spec(last_frame_crop, 1, "last_frame_crop")[0],
                                    width, height)

        mids = in_slot_order(middle_frames)
        mids += [load_input_image(fn, "middle_frame_files") for fn in file_lines(middle_frame_files)]
        mids = [apply_crop(img, c, width, height)
                for img, c in zip(mids, parse_crop_spec(middle_frame_crops, len(mids),
                                                        "middle_frame_crops"))]

        ref_slots = slot_items(ref_images)
        ref_slots += [(None, load_input_image(fn, "ref_image_files"))
                      for fn in file_lines(ref_image_files)]
        refs = [img for _, img in ref_slots]
        ref_mask_by_slot = dict(slot_items(ref_masks))
        stray = sorted(set(ref_mask_by_slot) - {s for s, _ in ref_slots if s is not None})
        if stray:
            raise ValueError(
                "MiniMax H3 Guide: ref_mask_%s has no matching ref_image_%s. Masks pair "
                "with the same-numbered reference image."
                % (stray[0], stray[0]))
        ref_strengths = parse_ref_spec(ref_spec, len(refs))
        spec = parse_middle_spec(middle_frame_spec, len(mids), frame_count)
        if spec and not middle_frame_patch.install():
            raise RuntimeError("MiniMax H3 Guide: middle keyframes need the PackedLayout "
                               "extension, which could not be installed. See the log.")

        # -- motion context: pin the previous clip's tail at this clip's head --
        # One VAE call on a grid-snapped run of tail frames; each latent step of
        # the encoded run becomes a full-strength keyframe at its exact pixel
        # offset (the exclusive cumsum of the (1,4,4,4,4) token pattern), so the
        # motion between the frames lives INSIDE the pinned latents. Context
        # rows are ground truth -- strength 1.0, no noise_aug, untouched by the
        # sub-1.0 label machinery.
        mc_file = (motion_context_file or "").strip()
        mc_keyframes, mc_span, mc_audio_block = [], 0, None
        if mc_file:
            if not middle_frame_patch.install():
                raise RuntimeError(
                    "MiniMax H3 Guide: motion context needs the PackedLayout extension, "
                    "which could not be installed. See the log. Plain ⏭ continuation "
                    "(final frame -> first frame) still works.")
            if v2v_frames is not None:
                logging.warning(
                    "MiniMaxH3Guide: motion context AND a v2v source are both set -- the "
                    "pinned head frames will fight the footage being restyled over the "
                    "same opening. This combination is untested; expect odd joins.")
            mc_frames, mc_snd = load_input_video(mc_file, "motion_context_file",
                                                 max_seconds=None)
            total_mc = mc_frames.shape[0]
            cut = total_mc
            if motion_context_end_seconds > 0:
                cut = max(1, min(total_mc,
                                 int(round(motion_context_end_seconds * FPS_HINT))))
                if cut < total_mc:
                    logging.info("MiniMaxH3Guide: motion context continues from %.2fs "
                                 "(frame %d of %d) of %r.",
                                 motion_context_end_seconds, cut, total_mc, mc_file)
            n = max(1, min(int(motion_context_frames), cut))
            run = next(g for g in MC_VIDEO_RUN_GRID if g <= n)
            if run != n:
                logging.info("MiniMaxH3Guide: motion context %d frames is off the video "
                             "VAE's run grid -- pinning the last %d instead (usable "
                             "runs: 1, 5, 22, 39).", n, run)
            if run >= frame_count:
                raise ValueError(
                    "MiniMax H3 Guide: motion context of %d frames into a %d-frame clip "
                    "-- the pinned run must be a small fraction of the timeline. "
                    "Lengthen the clip or lower motion_context_frames." % (run, frame_count))
            # snap BEFORE slicing: encoding an off-grid count would make the VAE
            # keep the FIRST covered frames of the slice, ending the pinned run
            # early and shifting the join by the difference
            tail = _resize(mc_frames[cut - run:cut], width, height, "disabled")
            enc = vae.encode(tail)
            steps = int(enc.shape[2])
            if mc_pixel_frames(steps) != run:
                raise RuntimeError(
                    "MiniMax H3 Guide: %d context frames encoded to %d latent steps "
                    "covering %d -- the video VAE's downscale grid changed upstream. "
                    "Refusing to render a shifted join." % (run, steps, mc_pixel_frames(steps)))
            offsets = mc_step_offsets(steps)
            mc_keyframes = [{"resolved_frame_index": offsets[k],
                             "latent": enc[:, :, k:k + 1]} for k in range(steps)]
            mc_span = run

            # audio context: the tail waveform ending at the SAME instant as the
            # pinned video, so both finish at the join. It rides the reference
            # machinery for construction; extra_conds_patch relocates its time
            # coordinates onto this clip's own timeline (ending at frame
            # mc_span), which is what turns imitation into continuation.
            a_frames = int(motion_context_audio_frames)
            if a_frames > 0:
                if mc_snd is None or mc_snd.get("waveform") is None:
                    logging.info("MiniMaxH3Guide: motion context source %r has no "
                                 "soundtrack -- audio context skipped.", mc_file)
                elif audio_vae is None:
                    raise ValueError(
                        "MiniMax H3 Guide: motion context audio needs audio_vae -- wire "
                        "the MiniMax H3 audio VAE in, or set motion_context_audio_frames "
                        "to 0 to continue picture only.")
                else:
                    sr = mc_snd["sample_rate"]
                    a_end = int(round(cut / FPS_HINT * sr))
                    want = int(round(a_frames / FPS_HINT * sr))
                    wf = mc_snd["waveform"][..., max(0, a_end - want):a_end]
                    if wf.shape[-1] <= 0:
                        logging.warning("MiniMaxH3Guide: motion context audio window is "
                                        "empty at the cut point -- audio context skipped.")
                    else:
                        if wf.shape[-1] < want:
                            logging.warning(
                                "MiniMaxH3Guide: motion context audio is %.2fs, shorter "
                                "than the %.2fs window -- pinning what there is.",
                                wf.shape[-1] / sr, want / sr)
                        z_a, rt_a = encode_ref_audio(
                            audio_vae, {"waveform": wf, "sample_rate": sr})
                        if rt_a > 0:
                            mc_audio_block = {"kind": "audio", "ref_audio_t": rt_a,
                                              "audio_latent": z_a,
                                              MC_AUDIO_END_KEY: float(mc_span)}

            if first_frame is not None or first_frame_file.strip():
                logging.info("MiniMaxH3Guide: motion context occupies frames 0-%d -- "
                             "first_frame is ignored (the context IS the opening).",
                             mc_span - 1)
                first_frame = None
            bad_wp = [e for e in spec if e["index"] < mc_span]
            if bad_wp:
                raise ValueError(
                    "MiniMax H3 Guide: a middle frame at frame %d sits inside the "
                    "%d-frame motion-context head (frames 0-%d are pinned). Move it "
                    "past %.2fs or shorten the context."
                    % (bad_wp[0]["index"], mc_span, mc_span - 1, mc_span / FPS_HINT))
            logging.info("MiniMaxH3Guide: motion context -- %d frames of %r pinned at "
                         "the head as %d cond block(s) at indices %s, audio %s. The "
                         "first %.2fs of the render repeats the source tail; the editor "
                         "trims it on the reel.",
                         run, mc_file, len(mc_keyframes),
                         "%d..%d" % (offsets[0], offsets[-1]),
                         ("%d latent steps ending at the join" % mc_audio_block["ref_audio_t"])
                         if mc_audio_block else "off",
                         mc_span / FPS_HINT)

        # Ordered along the timeline so <Picture N> labels read in temporal order.
        # Motion-context blocks come first (they occupy the head) and carry their
        # final latents already -- no image, so they never enter the tokenizer's
        # picture list or the strength/encode loop below.
        images = []
        keyframes = list(mc_keyframes)

        def add(img, index, strength, seed):
            images.append(img)
            keyframes.append({"resolved_frame_index": index, "image": img,
                              "strength": strength, "noise_seed": seed})

        if first_frame is not None:
            # geometry anchor: plain stretch to canvas
            add(_resize(first_frame[:1], width, height, "disabled"), 0,
                first_frame_strength, FIRST_FRAME_NOISE_SEED)
        for img, entry in sorted(zip(mids, spec), key=lambda p: p[1]["index"]):
            # waypoints follow the last frame's convention: aspect-preserving cover-crop.
            # Seed is keyed to the spec line, so reordering waypoints in time does not
            # reshuffle which noise field each one gets.
            add(_resize(img[:1], width, height, "center"), entry["index"],
                entry["strength"], MIDDLE_FRAME_NOISE_SEED_BASE + entry["slot"])
        if last_frame is not None:
            # follower: aspect-preserving cover-crop
            add(_resize(last_frame[:1], width, height, "center"), frame_count - 1,
                last_frame_strength, LAST_FRAME_NOISE_SEED)

        # Reference images: not anchored to a moment, they condition the whole clip.
        # They pack after the keyframes, which is the order extra_conds must rebuild
        # cond_video_latents in.
        ref_items, ref_blocks = [], []
        ref_crops = parse_crop_spec(ref_image_crops, len(ref_slots), "ref_image_crops")
        for n, ((slot, img), strength) in enumerate(zip(ref_slots, ref_strengths)):
            # reference framing keeps the source aspect: pure zoom/pan into the image
            img = apply_crop(img, ref_crops[n])
            resized, block = encode_ref_image(vae, img, width, height, ref_image_size,
                                              ref_megapixels)
            mask = ref_mask_by_slot.get(slot)
            if mask is not None and ref_crops[n] is not None:
                # the mask must follow the same window, or it lands on the wrong
                # pixels of the cropped content (bug-hunt finding)
                m3 = mask if mask.ndim == 3 else mask[None]
                mask = apply_crop(m3[:1].unsqueeze(-1), ref_crops[n]).squeeze(-1)
            seed = REF_NOISE_SEED_BASE + n
            if mask is None:
                if strength < 1.0 and row_aug_ready():
                    block["noise_aug"] = strength * _VIS_T
                    block["latent"] = flow_blend(block["latent"], block["noise_aug"], seed)
                else:
                    block["latent"] = weaken_cond_latent(block["latent"], strength, seed)
            else:
                if float(mask.max()) <= 0.0:
                    logging.warning(
                        "MiniMaxH3Guide: ref_mask_%d is entirely black, so reference "
                        "image %d is fully replaced by noise. If you wired a Load Image "
                        "MASK output from a file with no alpha, that is the cause.",
                        slot, n + 1)
                # scalar strength scales the whole map, so mask and strength compose
                smap = mask_to_latent_grid(mask, block["latent_h"], block["latent_w"]) * strength
                if strength < 1.0 and row_aug_ready():
                    block["noise_aug"] = strength * _VIS_T   # label only; map does the blend
                block["latent"] = weaken_cond_latent_masked(block["latent"], smap, seed)
                if mask_ref_pixels:
                    m = mask_to_pixel_grid(mask, resized.shape[1], resized.shape[2])
                    resized = resized * m + 0.5 * (1.0 - m)   # excluded area -> mid grey
            ref_items.append({"type": "image", "data": resized})
            ref_blocks.append(block)
        if ref_blocks:
            logging.info("MiniMaxH3Guide: %d reference image(s) presented as %s -- use those "
                         "numbers in the prompt.", len(ref_blocks),
                         ", ".join("<Picture %d>" % (len(images) + i + 1)
                                   for i in range(len(ref_blocks))))

        # Reference videos, after the images and before standalone audio -- core's
        # presentation order. Each soundtrack's <Audio j> label is emitted right
        # before its <Video k>, so soundtracks take audio numbers ahead of any
        # standalone audio below.
        video_sources = []
        rv_audios = ref_video_audios or {}
        vid_slots = slot_items(ref_videos)
        stray_va = sorted(set(int(k.rsplit("_", 1)[-1]) for k, v in rv_audios.items()
                              if v is not None)
                          - {s for s, _ in vid_slots})
        if stray_va:
            raise ValueError(
                "MiniMax H3 Guide: ref_video_audio_%d has no same-numbered ref_video_%d "
                "connected -- soundtracks pair by socket number (file-picked videos use "
                "their own embedded audio)." % (stray_va[0], stray_va[0]))
        for slot, framesv in vid_slots:
            video_sources.append((framesv, rv_audios.get("ref_video_audio_%d" % slot)))
        for fn in file_lines(ref_video_files):
            video_sources.append(load_input_video(fn, "ref_video_files"))
        if len(video_sources) > 3:
            raise ValueError("MiniMax H3 Guide: %d reference videos supplied -- H3 supports "
                             "at most 3 (sockets + ref_video_files combined)."
                             % len(video_sources))
        video_strengths = parse_ref_spec(ref_video_spec, len(video_sources), "ref_video_spec")
        video_crops = parse_crop_spec(ref_video_crops, len(video_sources), "ref_video_crops")
        if any(snd is not None for _, snd in video_sources) and audio_vae is None:
            raise ValueError("MiniMax H3 Guide: a reference video has a soundtrack but "
                             "audio_vae is empty. Wire the MiniMax H3 audio VAE in, or use "
                             "a video without audio.")
        n_soundtracks = 0
        for n, ((framesv, snd), strength) in enumerate(zip(video_sources, video_strengths)):
            # user framing first (source-aspect zoom/pan), so the canvas rule and
            # MP cap size the REGION the user chose, not the raw footage
            framesv = apply_crop(framesv, video_crops[n])
            has_snd, vitem, block = encode_ref_video(vae, audio_vae, framesv, snd,
                                                     frame_count, ref_video_megapixels,
                                                     "reference video %d" % (n + 1))
            if strength < 1.0 and row_aug_ready():
                block["noise_aug"] = strength * _VIS_T
                block["latent"] = flow_blend(block["latent"], block["noise_aug"],
                                             REF_VIDEO_NOISE_SEED_BASE + n)
            else:
                block["latent"] = weaken_cond_latent(block["latent"], strength,
                                                     REF_VIDEO_NOISE_SEED_BASE + n)
            if block["audio_latent"] is not None:
                block["audio_latent"] = weaken_cond_latent(
                    block["audio_latent"], strength, REF_VIDEO_AUDIO_NOISE_SEED_BASE + n)
            if has_snd:
                ref_items.append({"type": "audio"})
                n_soundtracks += 1
            ref_items.append(vitem)
            ref_blocks.append(block)
        if video_sources:
            logging.info("MiniMaxH3Guide: %d reference video(s) presented as %s%s.",
                         len(video_sources),
                         ", ".join("<Video %d>" % (i + 1) for i in range(len(video_sources))),
                         (" with soundtrack(s) %s" % ", ".join(
                             "<Audio %d>" % (i + 1) for i in range(n_soundtracks)))
                         if n_soundtracks else "")

        # Standalone reference audio, after the videos -- the order core's reference
        # node uses. Audio never enters Qwen; it contributes a "<Audio N>: " label to
        # the presentation and its own rows to the DiT.
        ref_audio_list = in_slot_order(ref_audios)
        ref_audio_list += [load_input_audio(fn, "ref_audio_files")
                           for fn in file_lines(ref_audio_files)]
        if len(ref_audio_list) > 3:
            raise ValueError("MiniMax H3 Guide: %d standalone reference audios supplied -- "
                             "H3 supports at most 3 (sockets + ref_audio_files combined)."
                             % len(ref_audio_list))
        if ref_audio_list and audio_vae is None:
            raise ValueError(
                "MiniMax H3 Guide: %d reference audio input(s) connected but audio_vae is "
                "empty. Wire the MiniMax H3 audio VAE into audio_vae."
                % len(ref_audio_list))
        for n, audio in enumerate(ref_audio_list):
            z, ref_audio_t = encode_ref_audio(audio_vae, audio)
            if ref_audio_t <= 0:
                raise ValueError("MiniMax H3 Guide: reference audio %d encoded to zero "
                                 "length. Check the clip is not empty." % (n + 1))
            z = weaken_cond_latent(z, ref_audio_strength, REF_AUDIO_NOISE_SEED_BASE + n)
            ref_items.append({"type": "audio"})
            ref_blocks.append({"kind": "audio", "ref_audio_t": ref_audio_t,
                               "audio_latent": z})
        if mc_audio_block is not None:
            # Always the LAST ref block, and deliberately item-less: the tokenizer
            # never numbers it, so <Audio N> labels stay stable whether or not a
            # context is set. Its rows reach the DiT; extra_conds_patch moves
            # their time coordinates onto the clip's own timeline. It is also
            # excluded from ref_audio_strength -- context is ground truth.
            ref_blocks.append(mc_audio_block)
            if not extra_conds_patch.install():
                raise RuntimeError(
                    "MiniMax H3 Guide: motion-context audio needs the extra_conds "
                    "patch, which could not be installed. See the log; set "
                    "motion_context_audio_frames to 0 to continue picture only.")
        if ref_audio_list or n_soundtracks or mc_audio_block is not None:
            # Retry in case the turbo pack loaded after this one. Its adaln row table
            # ignores audio conditioning and dies with a shape mismatch otherwise.
            if not turbo_compat.install() and turbo_compat.turbo_present():
                logging.warning(
                    "MiniMaxH3Guide: ComfyUI-MiniMax-H3-Turbo is loaded but could not be "
                    "patched for audio conditioning. If sampling fails with 'size of "
                    "tensor a (N) must match tensor b (N-1)', that is why -- remove the "
                    "reference audio or the turbo LoRA.")
        if ref_audio_list:
            # soundtracks were labelled first, so standalone numbering starts after them
            logging.info("MiniMaxH3Guide: %d reference audio presented as %s (strength %.2f).",
                         len(ref_audio_list),
                         ", ".join("<Audio %d>" % (n_soundtracks + i + 1)
                                   for i in range(len(ref_audio_list))),
                         ref_audio_strength)

        # Middle-frame descriptions are appended as labelled notes. The tokenizer
        # already emits "<Picture N>: " before each image and Qwen3-VL binds those
        # labels, so referring back to them is enough to tie text to a frame -- the
        # keyframe presentation has no slot for text interleaved between images.
        prompt = cls._append_frame_notes(prompt, spec, first_frame is not None)

        # Timed beats go last so their token spans sit at the very end of the
        # sequence, where end-relative offsets are stable.
        beats = parse_timed_text(timed_text, frame_count)
        text_beats = None
        if beats:
            prompt, fragments = cls._compose_timed_text(prompt, beats, timed_text_mode)
            if timed_text_mode != "text only":
                text_ids = text_ids_fn(clip)
                if text_ids is None:
                    raise ValueError(
                        "MiniMax H3 Guide: timed_text_mode %r needs the MiniMax H3 text "
                        "encoder (its tokenizer exposes _text_ids). Use 'text only' with "
                        "this clip." % timed_text_mode)
                spans = beat_spans(text_ids, prompt, fragments)
                if spans is None:
                    raise ValueError("MiniMax H3 Guide: could not locate the timed_text "
                                     "beats in the tokenised prompt. Use 'text only'.")
                if not extra_conds_patch.install():
                    raise RuntimeError("MiniMax H3 Guide: timed-text anchoring could not be "
                                       "installed. See the log; 'text only' still works.")
                text_beats = [{"start_from_end": s, "stop_from_end": e,
                               "frame_index": b["index"]}
                              for (s, e), b in zip(spans, beats)]

        # The text encoder still sees the full-strength images: weakening the pixel
        # side too would blunt the prompt's own read of the scene, and Qwen's
        # description of a keyframe is a much softer constraint than the condition
        # latent. Only the latents are diluted.
        # The tokenizer's two presentation paths are mutually exclusive -- pass
        # minimax_ref_items and the images= list is ignored outright. Its image branch
        # emits exactly the same '<Picture N>: ' + vision block that images= does, so
        # when references are in play everything goes through ref_items instead:
        # keyframes first (preserving their numbering), then the references. With no
        # references we stay on the stock path, byte-for-byte.
        if ref_items:
            tokens = clip.tokenize(prompt, minimax_ref_items=(
                [{"type": "image", "data": img} for img in images] + ref_items))
        else:
            tokens = clip.tokenize(prompt, images=images)
        cond = clip.encode_from_tokens_scheduled(tokens)
        # the FINAL prompt (frame notes + beats appended) rides along so
        # downstream nodes can locate token spans (H3RegionalPrompt); core
        # ignores unknown cond keys
        cond = node_helpers.conditioning_set_values(cond, {"minimax_prompt_text": prompt})
        if ref_blocks:
            cond = node_helpers.conditioning_set_values(cond, {"minimax_refs": ref_blocks})
            if keyframes and not extra_conds_patch.install():
                raise RuntimeError(
                    "MiniMax H3 Guide: combining reference images with keyframes needs the "
                    "extra_conds merge patch, which could not be installed. See the log. "
                    "Use references OR keyframes alone until resolved.")
        if text_beats:
            cond = node_helpers.conditioning_set_values(cond,
                                                        {"minimax_text_beats": text_beats})

        if keyframes:
            for kf in keyframes:
                if "image" not in kf:
                    continue  # motion-context block: latent already final
                z = vae.encode(kf.pop("image"))
                s = kf.pop("strength")
                seed = kf.pop("noise_seed")
                if s < 1.0 and row_aug_ready():
                    kf["noise_aug"] = s * _VIS_T
                    kf["latent"] = flow_blend(z, kf["noise_aug"], seed)
                else:
                    kf["latent"] = weaken_cond_latent(z, s, seed)
            cond = node_helpers.conditioning_set_values(cond, {
                "minimax_keyframes": keyframes,
                "minimax_frame_count": frame_count,
            })
        # The sigma dial: injected noise raises where the latent ACTUALLY sits on
        # the flow path. Composing "footage noised to b" with the sampler's own
        # start d puts the signal at (1-d)(1-b), i.e. an effective time of
        # 1-(1-d)(1-b). Declaring all of it (1.0) hands the scheduler an honest
        # start -> clean; declaring less makes the model commit fewer steps over
        # already-scrambled content -> the furthest from the source, grain the
        # price. Only meaningful when a v2v source was actually noised.
        # No v2v source = the latent output is an EMPTY latent, which must be
        # denoised in full. Emitting the widget's restyle value there would make
        # a plain t2v/i2v render stop short of finishing (the wired scheduler
        # would start mid-schedule over pure noise).
        out_denoise = float(v2v_denoise)
        if v2v_frames is None:
            if abs(out_denoise - 1.0) > 1e-6:
                logging.info("MiniMaxH3Guide: no v2v source -- emitting denoise 1.0 "
                             "(the v2v_denoise widget's %.2f applies only to a "
                             "restyle).", out_denoise)
            out_denoise = 1.0
        elif float(v2v_noise) > 0.0:
            b = max(0.0, min(1.0, float(v2v_noise)))
            eff = 1.0 - (1.0 - out_denoise) * (1.0 - b)
            declare = max(0.0, min(1.0, float(v2v_noise_declare)))
            out_denoise = min(1.0, out_denoise + declare * (eff - out_denoise))
            logging.info("MiniMaxH3Guide: v2v_denoise %.2f + noise %.2f declared "
                         "%.0f%% -> emitting %.3f (the latent truly sits at %.3f).",
                         float(v2v_denoise), b, declare * 100, out_denoise, eff)
        return io.NodeOutput(cond, latent, out_denoise)


class MiniMaxH3GuideExtension(ComfyExtension):
    async def get_node_list(self):
        from .load_image_zoom_pan import LoadImageZoomPan
        from .h3_v2v import H3VideoToLatent, H3FrameRange, H3Splice
        from .h3_temporal_lora import H3TemporalLoraBlend
        from .h3_v2v import H3BasicScheduler
        from .h3_diff_v2v import H3SoftDenoiseZone
        from .h3_regional import H3RegionalPrompt
        return [MiniMaxH3ImageToVideoGuide, LoadImageZoomPan,
                H3VideoToLatent, H3FrameRange, H3Splice, H3TemporalLoraBlend,
                H3BasicScheduler, H3SoftDenoiseZone, H3RegionalPrompt]


async def comfy_entrypoint() -> MiniMaxH3GuideExtension:
    return MiniMaxH3GuideExtension()
