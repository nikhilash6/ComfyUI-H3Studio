"""Lets MiniMax H3 accept keyframes at arbitrary frame indices, not just first/last.

ComfyUI implemented the general case itself in e01fb4c (MiniMaxH3AddGuide, #15439):
`cond_t = cursor + FRAME_RESCALE * resolved_frame_index` for any index, with no guard.
The same commit dropped the `frame_count` argument the old guard needed, so its
presence in core's signature is exactly the test for "core still needs help". On a
current build install() confirms core's own support and leaves PackedLayout alone.

Everything below describes the older builds we still patch.

Core's PackedLayout hard-codes two anchors and raises for anything else:

    if pixel_index == 0:                       cond_t = text_len
    elif pixel_index == frame_count - 1:       cond_t = text_len + sum(spans) - FRAME_RESCALE
    else:                                      raise ValueError(...)

Both branches are the same formula written two ways:

    cond_t = text_len + FRAME_RESCALE * pixel_index

because sum(_video_t_spans(latent_t)) == FRAME_RESCALE * frame_count for every valid
length -- the (1,4,4,4,4) token pattern covers 17 pixel frames per 5 latent tokens,
which is exactly the 17k+5 grid the model snaps to. So core's `sum(spans) -
FRAME_RESCALE` is just `FRAME_RESCALE * (frame_count - 1)`. A middle keyframe is a
perfectly well-defined position; core simply never implemented the general case.

We do NOT reimplement the layout. cond_t is written to exactly one place --
`position_ids[:, 0]` for that keyframe's rows -- and nothing else in the constructor
depends on the frame index. So we hand core a sanitised keyframe list it will accept,
let it build the entire layout, then rewrite that one column. Core stays the single
source of truth for the other ~100 lines of packing logic.

Stock first/last workflows never enter the patched path at all.
"""

import logging

import comfy.ldm.minimax.model as _mmm

from . import h3_core

_CorePackedLayout = h3_core.CORE_PACKED_LAYOUT

# Core dropped `frame_count` in the very commit that generalised the anchor formula,
# so the two always move together: signature has it -> guard is still there.
_CORE_TAKES_FRAME_COUNT = h3_core.takes_frame_count()

_announced = False


def core_supports_general_anchors():
    """True when core's own PackedLayout anchors a keyframe at any frame index."""
    return h3_core.general_anchors()


def general_cond_t(text_len, pixel_index):
    """RoPE time coordinate for a condition row anchored at a pixel frame index."""
    return float(text_len) + _mmm.FRAME_RESCALE * float(pixel_index)


class GuidePackedLayout(_CorePackedLayout):
    """PackedLayout that accepts keyframes anchored at any frame index."""

    _guide_patched = True

    def __init__(self, text_len, latent_t, latent_h, latent_w, audio_t,
                 keyframes=None, refs=None, frame_count=None):
        # only builds that still guard the anchor accept frame_count at all
        extra = {"frame_count": frame_count} if _CORE_TAKES_FRAME_COUNT else {}
        anchors = [kf["resolved_frame_index"] for kf in (keyframes or [])]
        native = {0} | ({frame_count - 1} if frame_count is not None else set())
        if not anchors or all(a in native for a in anchors):
            # nothing core can't already do -- defer entirely, byte-for-byte
            super().__init__(text_len, latent_t, latent_h, latent_w, audio_t,
                             keyframes=keyframes, refs=refs, **extra)
            return

        # Build with every anchor moved to index 0 so core's guard passes, then
        # restore the true positions. Only position_ids[:, 0] carries the anchor.
        safe = [dict(kf, resolved_frame_index=0) for kf in keyframes]
        super().__init__(text_len, latent_t, latent_h, latent_w, audio_t,
                         keyframes=safe, refs=refs, **extra)

        cond_spans = [(a, b) for a, b, kind in self.segments if kind == "cond"]
        if len(cond_spans) != len(keyframes):
            raise RuntimeError(
                "H3Studio: expected %d 'cond' segments in the packed layout but "
                "found %d -- ComfyUI's MiniMax H3 layout has changed and the middle-frame "
                "patch is no longer safe. Use first/last keyframes only until this pack "
                "is updated." % (len(keyframes), len(cond_spans)))

        for (start, stop), kf in zip(cond_spans, keyframes):
            self.position_ids[start:stop, 0] = general_cond_t(text_len, kf["resolved_frame_index"])


def install():
    """Idempotently swap core's PackedLayout for the general one.

    Both construction sites resolve the class through the module attribute at call
    time (`comfy.ldm.minimax.model.PackedLayout` in model_base.extra_conds, and the
    module global in the DiT's _forward), so rebinding here catches both.

    On a build where core already anchors at any index this is a no-op that still
    reports success -- callers ask "are middle keyframes available?", and they are.
    """
    global _announced
    if core_supports_general_anchors():
        if not _announced:
            logging.info("H3Studio: ComfyUI anchors keyframes at any frame index "
                         "natively; middle-frame patch not needed.")
            _announced = True
        return True
    if getattr(_mmm.PackedLayout, "_guide_patched", False):
        return True
    if _mmm.PackedLayout is not _CorePackedLayout:
        logging.warning("H3Studio: another extension already replaced MiniMax H3's "
                        "PackedLayout; skipping the middle-frame patch. Middle keyframes "
                        "will not work.")
        return False
    _mmm.PackedLayout = GuidePackedLayout
    logging.info("H3Studio: middle-keyframe support enabled (PackedLayout extended).")
    return True


def self_check():
    """Confirm the generalisation reproduces core's own two anchors exactly.

    Cheap insurance against a future core change to FRAME_PER_TOKEN or the frame
    grid silently desynchronising our formula from core's.
    """
    from comfy_extras.nodes_minimax_h3 import align_frame_count, video_latent_t
    for length in (5, 124, 362):
        fc = align_frame_count(length)
        lt = video_latent_t(fc)
        total = sum(_mmm._video_t_spans(lt))
        if abs((total - _mmm.FRAME_RESCALE) - _mmm.FRAME_RESCALE * (fc - 1)) > 1e-6:
            return False
    return True
