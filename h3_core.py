"""Which MiniMax H3 does this ComfyUI have?

ComfyUI rewrote H3's packed layout once, in e01fb4c (MiniMaxH3AddGuide, #15439).
That single commit did four things at once:

  1. generalised the keyframe anchor to any frame index (dropping the guard, and
     with it the `frame_count` argument the guard needed)
  2. let a keyframe carry `audio_latent`, emitted as its own `cond_audio` segment
     anchored at the keyframe's coordinate
  3. made `extra_conds` CONCATENATE keyframe and reference condition latents
     instead of letting the second assignment clobber the first
  4. anchored keyframes to the post-references cursor rather than to text_len

Because they landed together, one probe answers all four. `frame_count` in the
constructor signature is that probe: present means the old layout, absent means
the new one. A version number would not do -- the change sat on master untagged
for a while, so builds reporting the same version differ.

Everything in this pack that has to behave differently on the two shapes asks
here rather than sniffing core itself, so there is exactly one place to update
when core moves again.

Nothing here patches anything. It only looks.
"""

import inspect
import logging

import comfy.ldm.minimax.model as _mmm

# The class object as it was at import, before any of our patches replace the
# module attribute -- so the probe reads CORE's signature, not ours.
CORE_PACKED_LAYOUT = _mmm.PackedLayout

_TAKES_FRAME_COUNT = "frame_count" in inspect.signature(
    CORE_PACKED_LAYOUT.__init__).parameters

_NEW_LAYOUT = not _TAKES_FRAME_COUNT


def takes_frame_count():
    """True when core's PackedLayout still accepts (and needs) `frame_count`."""
    return _TAKES_FRAME_COUNT


def general_anchors():
    """Core anchors a keyframe at any frame index by itself."""
    return _NEW_LAYOUT


def keyframe_audio():
    """A keyframe may carry `audio_latent`, giving it a cond_audio segment.

    On the old layout the constructor reads only `kf["latent"]`, so audio on a
    keyframe is silently dropped -- callers must use the reference path instead.
    """
    return _NEW_LAYOUT


def merges_cond_latents():
    """Core's extra_conds concatenates keyframe and reference cond latents.

    On the old layout the two assignments clobber each other, which is why
    keyframes and references cannot be combined without our merge.
    """
    return _NEW_LAYOUT


def anchors_after_refs():
    """Core measures keyframe anchors from the post-references cursor."""
    return _NEW_LAYOUT


_announced = False


def announce():
    """Log which layout we are on, once per session."""
    global _announced
    if _announced:
        return
    _announced = True
    logging.info("H3Studio: ComfyUI's H3 layout is the %s one (%s). %s",
                 "current" if _NEW_LAYOUT else "older",
                 "no frame_count" if _NEW_LAYOUT else "frame_count present",
                 "Core handles interior anchors, keyframe audio and cond merging "
                 "itself." if _NEW_LAYOUT else
                 "H3 Studio supplies interior anchors, keyframe/reference "
                 "coexistence and timeline audio.")
