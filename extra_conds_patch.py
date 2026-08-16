"""Single wrapper around MiniMaxH3.extra_conds.

Everything here needs the same hook, so it is one wrapper rather than a stack
(each would capture the other as "the original" and refuse to install).

Several of these fixes are for the OLDER H3 layout only -- ComfyUI's e01fb4c
implemented them itself. Each says which, and asks h3_core rather than sniffing
around in core. On a current build this wrapper does timed text and motion
timing and nothing else.

1. REF MERGE (older layout only). `extra_conds` built the DiT's flat list of
   never-denoised condition latents and assigned it twice:

       payload["cond_video_latents"] = [kf["latent"] for kf in keyframes]   # then...
       payload["cond_video_latents"] = [r["latent"] for r in refs ...]      # ...clobbered

   With both present the packed layout reserves rows for keyframes AND refs while
   the model supplies only the refs, so filling the non-denoised slots raises a
   shape mismatch -- keyframes and reference images could not be combined at all.
   The layout itself packs both correctly, so the fix is to concatenate in the
   order it allocates: keyframes, then refs. Current core concatenates.

2. KEYFRAME ANCHORS (older layout only, or whenever motion timing is on).
   Anchors used to be measured from text_len, which is only the target video's
   origin when there are no references. Current core measures from the post-refs
   cursor. We still re-anchor when the clock has been rescaled, because then the
   frame a keyframe is pinning has moved.

3. PER-ROW STRENGTH LABELS. The list of noise-aug labels handed to
   h3_row_aug_patch, one per condition BLOCK in layout order. Built here because
   this is the only place that sees the final keyframe and reference lists
   together.

4. TIMED TEXT. Relocates spans of prompt text onto the video timeline. See the
   notes on apply_text_beats below.

5. TIME WARP. Rescales the target video's RoPE clock to dial motion up or down.
   See h3_time_warp. Everything anchored to a frame index lives on that same
   axis, so the warp table is threaded through the relocations below as well --
   otherwise a keyframe pinned at frame k stays at the old coordinate while the
   frame it was pinning moves out from under it.

6. TIMELINE AUDIO (older layout only). Motion-context audio rides the reference
   machinery and has its coordinates moved onto the target timeline. Current core
   lets a keyframe carry audio directly, which does the same job with no patch,
   so h3_studio builds it that way and this never runs. See relocate_context_audio.
"""

import logging

import torch

import comfy.ldm.minimax.model as _mmm
import comfy.model_base as _mb

from . import h3_core, h3_time_warp

_orig_extra_conds = _mb.MiniMaxH3.extra_conds

# A keyframe carrying only audio, whose window must END at a given pixel frame
# and reach BACKWARDS from it (the motion-context join). The anchor is already
# correct as built; this is only consulted when a time warp moves that frame.
AUDIO_END_KEY = "h3_audio_end_frame"


def _wf(warp, frame):
    """A frame index in warped time (identity when no warp is in play)."""
    return h3_time_warp.warp_at(warp, frame) if warp else float(frame)


# --- 1. reference / keyframe coexistence -----------------------------------

def merge_cond_latents(payload):
    """Rebuild cond_video_latents as keyframes-then-refs, matching layout row order.

    Returns True if a merge was needed. Only fires on the older layout and only
    when both are present; with one or the other, core's own assignment is
    already correct and is left untouched.
    """
    if h3_core.merges_cond_latents():
        return False
    keyframes = payload.get("keyframes")
    refs = payload.get("refs")
    if not keyframes or not refs:
        return False
    # `if kf.get("latent") is not None` matches how the layout allocates rows:
    # a keyframe with no picture contributes no visual condition block.
    payload["cond_video_latents"] = (
        [kf["latent"] for kf in keyframes if kf.get("latent") is not None] +
        [r["latent"] for r in refs if "latent" in r]
    )
    return True


# --- 2. keyframe anchoring --------------------------------------------------

def _video_time_origin(layout):
    """The t coordinate where the TARGET video grid actually starts."""
    for a, b, kind in layout.segments:
        if kind == "video":
            return float(layout.position_ids[a, 0])
    return None


def _keyframe_segments(layout, keyframes):
    """Pair each keyframe with the segments the layout built for it.

    PackedLayout walks the keyframe list in order, emitting a 'cond' segment for
    a keyframe with a picture and a 'cond_audio' segment for one with audio (both,
    in that order, for a keyframe with both). Reproducing that walk pairs them
    exactly, including audio-only keyframes -- which a simple count of 'cond'
    segments gets wrong, silently skipping every anchor.

    Returns [(keyframe, cond_span_or_None, cond_audio_span_or_None)], or None if
    the layout does not match the keyframe list (core drift: leave it alone).
    """
    conds = [(a, b) for a, b, kind in layout.segments if kind == "cond"]
    cond_audios = [(a, b) for a, b, kind in layout.segments if kind == "cond_audio"]
    want_v = sum(1 for kf in keyframes if kf.get("latent") is not None)
    want_a = sum(1 for kf in keyframes if kf.get("audio_latent") is not None)
    if len(conds) != want_v:
        return None
    if cond_audios and len(cond_audios) != want_a:
        return None
    out, vi, ai = [], 0, 0
    for kf in keyframes:
        v = a = None
        if kf.get("latent") is not None:
            v = conds[vi]
            vi += 1
        if kf.get("audio_latent") is not None and ai < len(cond_audios):
            a = cond_audios[ai]
            ai += 1
        out.append((kf, v, a))
    return out


def re_anchor_keyframes(layout, keyframes, warp=None):
    """Re-write keyframe time anchors relative to the true video origin.

    With no refs and no warp this reproduces core's own values exactly, so it is
    safe to run whenever keyframes exist. Returns the number of segments moved.
    """
    origin = _video_time_origin(layout)
    if origin is None:
        return 0
    pairs = _keyframe_segments(layout, keyframes)
    if pairs is None:
        return 0  # layout shape drifted; leave core's values alone
    moved = 0
    for kf, vspan, aspan in pairs:
        cond_t = origin + _mmm.FRAME_RESCALE * _wf(warp, kf["resolved_frame_index"])
        if vspan is not None:
            moved += _translate(layout, vspan, cond_t)
        if aspan is not None:
            end = kf.get(AUDIO_END_KEY)
            if end is None:
                start = cond_t
            else:
                # a window pinned by its END: the join is a FRAME, so it follows
                # the warp, but the window's WIDTH is a count of audio steps and
                # does not (the waveform itself is not being rescaled)
                rt = (aspan[1] - aspan[0]) // 2
                start = origin + _mmm.FRAME_RESCALE * _wf(warp, end) - float(rt)
            moved += _translate(layout, aspan, start)
    return moved


def _translate(layout, span, new_start):
    """Move a segment so its first row sits at new_start, keeping its shape.

    Assignment would do for a single-frame keyframe -- all its rows share one t.
    A keyframe carrying a CLIP does not: core spreads its latent steps along the
    time axis, and flattening them to one coordinate would tell the model the
    whole run happens in an instant. Translating preserves whatever internal
    structure core built, for pictures and audio windows alike.
    """
    a, b = span
    t = layout.position_ids[a:b, 0]
    t += new_start - float(t[0])
    return 1


# --- 3. per-row strength labels ---------------------------------------------

def _noise_aug_lists(payload):
    """Per-block noise-aug labels in layout order, or (None, None).

    Visual blocks are keyframes-then-refs; audio blocks are keyframe audio then
    reference audio. Both must line up with core's cond_*_latents lists, so the
    same 'has one' filters are used here.
    """
    kfs = payload.get("keyframes") or []
    refs = payload.get("refs") or []
    vis_kf = [k for k in kfs if k.get("latent") is not None]
    vis_ref = [r for r in refs if "latent" in r]
    aud_kf = [k for k in kfs if k.get("audio_latent") is not None]
    aud_ref = [r for r in refs if r.get("audio_latent") is not None]

    vid = None
    if any("noise_aug" in e for e in vis_kf) or any("noise_aug" in e for e in vis_ref):
        v = _mmm.VISUAL_COND_TIMESTEP
        vid = ([float(e.get("noise_aug", v)) for e in vis_kf]
               + [float(e.get("noise_aug", v)) for e in vis_ref])

    aud = None
    if any("audio_noise_aug" in e for e in aud_kf) or \
            any("audio_noise_aug" in e for e in aud_ref):
        a = _mmm.AUDIO_COND_TIMESTEP
        aud = ([float(e.get("audio_noise_aug", a)) for e in aud_kf]
               + [float(e.get("audio_noise_aug", a)) for e in aud_ref])
    return vid, aud


# --- 6. motion-context audio on the older layout ----------------------------

MC_AUDIO_END_KEY = "minimax_mc_audio_end_frame"


def relocate_context_audio(layout, refs, warp=None):
    """Translate a marked audio reference onto the target timeline, so its window
    ENDS where the pinned video ends (the join).

    OLDER LAYOUT ONLY. Current core lets a keyframe carry audio anchored at its
    own coordinate, so h3_studio builds the pinned sound that way instead and
    nothing marks a reference any more.

    Refs and keyframes carry identical row machinery; what makes the model read
    a ref as "a separate clip to imitate" rather than "this clip, continued" is
    purely that its coordinates sit in a span before the target. Moving them
    turns a sound-alike into the same waveform, continued (measured join
    correlation 0.45 -> 0.95+ in ComfyUI-H3-Motion-Context's seam probe, whose
    finding this is).

    We select the marked ref's own ref_audio SEGMENT (matched by audio-ref
    ordinal among the refs list, which is the order segments are laid out in),
    so it coexists with any other audio/video refs. Translation, not per-row
    assignment: += shift preserves whatever intra-block structure core built.

    Guards log-and-skip: a failed relocation degrades to stock ref placement
    (imitation), never a broken render. Returns rows moved.
    """
    marked = [i for i, r in enumerate(refs) if r.get(MC_AUDIO_END_KEY) is not None]
    if not marked:
        return 0
    if len(marked) > 1:
        logging.warning("H3Studio: %d motion-context audio refs marked; expected "
                        "one. Leaving all in stock ref placement.", len(marked))
        return 0
    mi = marked[0]
    blk = refs[mi]
    rt = int(blk.get("ref_audio_t", 0))
    if blk.get("kind") != "audio" or rt <= 0:
        logging.warning("H3Studio: motion-context audio marker on a %r ref with "
                        "%d steps; skipping relocation.", blk.get("kind"), rt)
        return 0
    origin = _video_time_origin(layout)
    if origin is None:
        logging.warning("H3Studio: no video segment in the layout; motion-context "
                        "audio left in ref placement.")
        return 0
    # each audio-bearing ref emits exactly one ref_audio segment, in refs order
    ordinal = sum(1 for r in refs[:mi] if int(r.get("ref_audio_t", 0) or 0) > 0
                  and r.get("kind") in ("audio", "video", "video_audio"))
    audio_segs = [(a, b) for a, b, kind in layout.segments if kind == "ref_audio"]
    if ordinal >= len(audio_segs):
        logging.warning("H3Studio: motion-context audio segment not found "
                        "(ordinal %d of %d ref_audio segments); left in ref placement.",
                        ordinal, len(audio_segs))
        return 0
    a, b = audio_segs[ordinal]
    # the join is a FRAME, so it follows the warp -- the audio window has to end
    # where the pinned video now ends, not where it used to
    end_frame = _wf(warp, float(blk[MC_AUDIO_END_KEY]))
    t0 = float(layout.position_ids[a:b, 0].min())
    shift = (origin + _mmm.FRAME_RESCALE * end_frame - rt) - t0
    layout.position_ids[a:b, 0] += shift
    return b - a


# --- 4. timed text ---------------------------------------------------------

def apply_text_beats(layout, beats, warp=None):
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
            logging.warning("H3Studio: timed-text span [%d,%d) outside the %d-token "
                            "text run; skipping.", start, stop, text_len)
            continue
        base = origin + _mmm.FRAME_RESCALE * _wf(warp, beat["frame_index"])
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

    vid_augs, aud_augs = _noise_aug_lists(payload)
    if vid_augs is not None:
        payload["cond_video_noise_augs"] = vid_augs
    if aud_augs is not None:
        payload["cond_audio_noise_augs"] = aud_augs

    layout0 = payload.get("layout")
    keyframes = payload.get("keyframes")
    # the clock is rescaled first: everything below anchors to frame indices on it
    warp = kwargs.get("minimax_time_warp")
    if warp and layout0 is None:
        logging.warning("H3Studio: no prebuilt layout, motion timing left at stock "
                        "for this run.")
        warp = None
    elif warp:
        h3_time_warp.apply_video_warp(layout0, warp)
    # Core measures anchors from the post-refs cursor by itself, so re-anchoring
    # is only work when the clock moved under them (or on the older layout).
    if layout0 is not None and keyframes and (warp or not h3_core.anchors_after_refs()):
        re_anchor_keyframes(layout0, keyframes, warp)
    all_refs = payload.get("refs") or []
    if layout0 is not None and all_refs:
        relocate_context_audio(layout0, all_refs, warp)

    beats = kwargs.get("minimax_text_beats")
    if beats:
        layout = payload.get("layout")
        if layout is None:
            # the DiT rebuilds a layout when extra_conds could not prebuild one, and
            # that path never sees the beats -- say so rather than silently no-op
            logging.warning("H3Studio: no prebuilt layout, timed-text anchoring "
                            "skipped for this run.")
        else:
            apply_text_beats(layout, beats, warp)
    return out


_patched_extra_conds._guide_extra_conds = True


def install():
    """Idempotently wrap MiniMaxH3.extra_conds. Called only when a feature needs it."""
    current = _mb.MiniMaxH3.extra_conds
    if getattr(current, "_guide_extra_conds", False):
        return True
    if current is not _orig_extra_conds:
        logging.warning("H3Studio: MiniMaxH3.extra_conds was already replaced by "
                        "another extension; reference+keyframe merging, per-row "
                        "strengths, timed text and motion timing are disabled.")
        return False
    _mb.MiniMaxH3.extra_conds = _patched_extra_conds
    h3_core.announce()
    logging.info("H3Studio: extra_conds wrapped.")
    return True
