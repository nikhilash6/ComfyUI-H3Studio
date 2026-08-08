"""Single wrapper around MiniMaxH3.extra_conds, covering two separate fixes.

Both need the same hook, so they share one wrapper rather than stacking two (which
would each capture the other as "the original" and refuse to install).

1. REF MERGE. `extra_conds` builds the DiT's flat list of never-denoised condition
   latents, but assigns it twice:

       payload["cond_video_latents"] = [kf["latent"] for kf in keyframes]   # then...
       payload["cond_video_latents"] = [r["latent"] for r in refs ...]      # ...clobbered

   With both present the packed layout reserves rows for keyframes AND refs while the
   model supplies only the refs, so filling the non-denoised slots raises a shape
   mismatch. Keyframes and reference images therefore cannot be combined in stock
   ComfyUI at all. The layout itself is fine -- it packs both correctly -- so the fix
   is simply to concatenate in the order the layout allocates: keyframes, then refs.

2. TIMED TEXT. Relocates spans of prompt text onto the video timeline. See the
   module docstring notes on apply_text_beats below.
"""

import logging

import torch

import comfy.ldm.minimax.model as _mmm
import comfy.model_base as _mb

_orig_extra_conds = _mb.MiniMaxH3.extra_conds


# --- 1. reference / keyframe coexistence -----------------------------------

def merge_cond_latents(payload):
    """Rebuild cond_video_latents as keyframes-then-refs, matching layout row order.

    Returns True if a merge was needed. Only fires when both are present; with one or
    the other, core's own assignment is already correct and is left untouched.
    """
    keyframes = payload.get("keyframes")
    refs = payload.get("refs")
    if not keyframes or not refs:
        return False
    payload["cond_video_latents"] = (
        [kf["latent"] for kf in keyframes] +
        [r["latent"] for r in refs if "latent" in r]
    )
    return True


# --- 1b. keyframe re-anchoring when refs are present ------------------------

def _video_time_origin(layout):
    """The t coordinate where the TARGET video grid actually starts.

    Core anchors keyframe cond rows at text_len + FRAME_RESCALE*index, which is
    only the video origin when there are no refs -- with refs, the target grid
    starts at the post-refs cursor (each image ref advances it 1.0, audio by its
    latent length, video by max(audio, video spans)). Core never combines
    keyframes with refs, so its formula never had to care; our merge makes the
    combination reachable, so the anchors must follow the real origin or first/
    last pinning silently degrades into floating references (bug-hunt finding).
    """
    for a, b, kind in layout.segments:
        if kind == "video":
            return float(layout.position_ids[a, 0])
    return None


def re_anchor_keyframes(layout, keyframes):
    """Re-write cond-segment time anchors relative to the true video origin.

    With no refs the origin equals text_len and this reproduces core's own
    values exactly, so it is safe to run whenever keyframes exist. Returns the
    number of segments moved.
    """
    origin = _video_time_origin(layout)
    if origin is None:
        return 0
    conds = [(a, b) for a, b, kind in layout.segments if kind == "cond"]
    if len(conds) != len(keyframes):
        return 0  # layout shape drifted; leave core's values alone
    for (a, b), kf in zip(conds, keyframes):
        layout.position_ids[a:b, 0] = origin + _mmm.FRAME_RESCALE * float(kf["resolved_frame_index"])
    return len(conds)


# --- 2. timed text ---------------------------------------------------------

def apply_text_beats(layout, beats):
    """Relocate recorded text spans onto the video timeline. Returns spans moved.

    Text normally sits at t = 0 .. text_len-1 and the video timeline starts at
    t = text_len -- the same axis, laid end to end, which is why a keyframe anchor is
    `text_len + FRAME_RESCALE * frame_index`. So a span of text can be given a
    video-time coordinate: RoPE then places those tokens AT that moment rather than
    the words merely describing it.

    The span keeps unit spacing (base+0, base+1, ...) instead of collapsing to a
    single t like a keyframe's condition rows. Those rows can share one t because they
    vary across h/w; text rows all sit at h = w = 0, so an identical t would make
    every token in the beat indistinguishable to RoPE and destroy reading order.
    """
    span = next(((a, b) for a, b, kind in layout.segments if kind == "text"), None)
    if span is None:
        return 0
    text_start, text_stop = span
    text_len = text_stop - text_start
    # anchor beats to the TARGET video grid's real origin -- with refs present it
    # sits after the ref spans, not at text_len (bug-hunt finding)
    origin = _video_time_origin(layout)
    if origin is None:
        origin = float(text_len)

    moved = 0
    for beat in beats:
        start = text_len - int(beat["start_from_end"])
        stop = text_len - int(beat["stop_from_end"])
        if not (0 <= start < stop <= text_len):
            logging.warning("MiniMaxH3Guide: timed-text span [%d,%d) outside the %d-token "
                            "text run; skipping.", start, stop, text_len)
            continue
        base = origin + _mmm.FRAME_RESCALE * float(beat["frame_index"])
        n = stop - start
        layout.position_ids[text_start + start:text_start + stop, 0] = (
            base + torch.arange(n, dtype=torch.float64))
        moved += 1
    return moved


# --- the wrapper -----------------------------------------------------------

def _patched_extra_conds(self, **kwargs):
    out = _orig_extra_conds(self, **kwargs)
    holder = out.get("minimax_payload")
    payload = getattr(holder, "cond", None)
    if not isinstance(payload, dict):
        return out

    merge_cond_latents(payload)

    # per-row noise-aug labels: rows are keyframes-then-refs, matching the
    # cond_video_latents order (merged above, or core's own single-type
    # assignment). Entries without a label default to the global value.
    _kfs = payload.get("keyframes") or []
    _refs = [r for r in (payload.get("refs") or []) if "latent" in r]
    if any("noise_aug" in e for e in _kfs) or any("noise_aug" in e for e in _refs):
        _v = _mmm.VISUAL_COND_TIMESTEP
        payload["cond_video_noise_augs"] = (
            [float(k.get("noise_aug", _v)) for k in _kfs]
            + [float(r.get("noise_aug", _v)) for r in _refs])

    layout0 = payload.get("layout")
    keyframes = payload.get("keyframes")
    if layout0 is not None and keyframes:
        re_anchor_keyframes(layout0, keyframes)

    beats = kwargs.get("minimax_text_beats")
    if beats:
        layout = payload.get("layout")
        if layout is None:
            # the DiT rebuilds a layout when extra_conds could not prebuild one, and
            # that path never sees the beats -- say so rather than silently no-op
            logging.warning("MiniMaxH3Guide: no prebuilt layout, timed-text anchoring "
                            "skipped for this run.")
        else:
            apply_text_beats(layout, beats)
    return out


_patched_extra_conds._guide_extra_conds = True


def install():
    """Idempotently wrap MiniMaxH3.extra_conds. Called only when a feature needs it."""
    current = _mb.MiniMaxH3.extra_conds
    if getattr(current, "_guide_extra_conds", False):
        return True
    if current is not _orig_extra_conds:
        logging.warning("MiniMaxH3Guide: MiniMaxH3.extra_conds was already replaced by "
                        "another extension; reference+keyframe merging and timed-text "
                        "anchoring are disabled.")
        return False
    _mb.MiniMaxH3.extra_conds = _patched_extra_conds
    logging.info("MiniMaxH3Guide: extra_conds wrapped (ref/keyframe merge + timed text).")
    return True
