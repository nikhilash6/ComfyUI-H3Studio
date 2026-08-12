"""Compatibility shim for ComfyUI-MiniMax-H3-Turbo (audio conditioning).

The turbo LoRA re-injects its adaln update at run time, which means it has to
rebuild the DiT's per-timestep row table itself. Its copy of that logic omits audio
conditioning entirely:

    s = {t_v, t_a}
    if has_vis_cond:
        s.add(max(t_v, 0.999))
    return sorted(s)                      # no audio branch

Core (comfy/ldm/minimax/model.py) also adds `max(t_a, AUDIO_COND_TIMESTEP)` whenever
a ref_audio segment exists. So with any audio reference the two disagree on the row
count and the adaln injection dies with a shape mismatch:

    RuntimeError: The size of tensor a (3) must match the size of tensor b (2)
                  at non-singleton dimension 0

4 vs 3 through most of the schedule, 3 vs 2 at the extremes where max(t_v, 0.999)
collapses onto t_v. This is not specific to this node pack -- core's own
MiniMax H3 Reference to Video hits it the same way.

Two further errors are corrected while we are here:

- has_vis_cond was `bool(keyframes or refs)`, so an audio-ONLY reference counted as a
  visual condition. That does not crash (the counts coincide) but silently feeds the
  wrong adaln rows.
- The sigma shifts were read only from the model, ignoring the transformer_options
  that MiniMaxH3SigmaShift sets -- so a custom shift produced wrong t_a.

We replace `_inject_adaln_egrid` rather than `_unique_t`, because the buggy call site
is inside a closure that does not pass the payload down. The replacement is installed
at import (before any workflow runs, so the closure built at LoRA-apply time is ours)
and is skipped if the upstream signature changes, on the assumption that means it has
been fixed.
"""

import inspect
import logging
import sys
import types

import comfy.ldm.minimax.model as _mmm
import comfy.patcher_extension

_TURBO_FUNCS = ("_inject_adaln_egrid", "_unique_t", "_interp_egrid", "_make_adaln_forward")


def _find_turbo():
    """Locate the loaded turbo module by shape, not by name.

    The members must be real functions defined in that same module. A plain hasattr
    sweep is not enough: torch.ops and torch.classes are ModuleType subclasses whose
    __getattr__ answers ANY name with a namespace object, so they match every
    attribute test and -- since torch loads long before custom nodes -- would be found
    first and patched instead of the turbo pack, silently doing nothing.

    We read __dict__ rather than using getattr for the same reason: those namespaces
    CACHE whatever name is probed, so a getattr sweep would leave junk entries behind
    in torch's op registry just by looking.
    """
    for mod in list(sys.modules.values()):
        if not isinstance(mod, types.ModuleType):
            continue
        try:
            ns = vars(mod)
        except TypeError:
            continue
        members = [ns.get(name) for name in _TURBO_FUNCS]
        if not all(inspect.isfunction(m) for m in members):
            continue
        # _inject_adaln_egrid may already be our replacement (different __module__),
        # so only the three we never touch have to share an origin
        origins = {m.__module__ for m, name in zip(members, _TURBO_FUNCS)
                   if name != "_inject_adaln_egrid"}
        if len(origins) != 1:
            continue  # a coincidental mix from different modules
        return mod
    return None


def correct_unique_t(timestep, shift_v, shift_a, payload, transformer_options):
    """Reproduce core's unique-timestep set exactly, audio included."""
    to = transformer_options or {}
    shift_v = float(to.get("minimax_h3_sigma_shift_video", shift_v))
    shift_a = float(to.get("minimax_h3_sigma_shift_audio", shift_a))

    sigma_v = (timestep.flatten()[0] / 1000.0).float().clamp(min=1e-6)
    t_v = float(1.0 - sigma_v)
    t_a = float(1.0 - _mmm.time_shift_sigma(sigma_v, shift_v, shift_a))

    layout = payload.get("layout")
    if layout is not None:
        # exactly how core decides, straight off the packed layout
        has_vis = any(k in ("cond", "ref_img") for _, _, k in layout.segments)
        has_aud = any(k == "ref_audio" for _, _, k in layout.segments)
    else:
        refs = payload.get("refs") or []
        has_vis = bool(payload.get("keyframes")) or any(
            r.get("kind") in ("image", "video", "video_audio") for r in refs)
        has_aud = any((r.get("ref_audio_t") or 0) > 0 for r in refs)

    # core reads the aug overrides from the payload (model.py: visual/audio_cond_
    # noise_aug), so the row VALUES must too, not just the counts (bug-hunt finding)
    vis = float(payload.get("visual_cond_noise_aug", _mmm.VISUAL_COND_TIMESTEP))
    aud = float(payload.get("audio_cond_noise_aug", _mmm.AUDIO_COND_TIMESTEP))
    augs = payload.get("cond_video_noise_augs") or None
    s = {t_v, t_a}
    if layout is not None:
        # Mirror h3_row_aug_patch's _seg_t_list EXACTLY: each visual cond segment
        # contributes its own row label, falling back to max(t_v, vis) only when
        # no label covers it. Adding max(t_v, vis) unconditionally (as this did)
        # invents a timestep core never uses whenever EVERY cond row is softened
        # -- e.g. motion_context_strength below 1.0 with no other keyframes --
        # and turbo then builds an adaln table one row taller than the model's
        # t_emb: "size of tensor a (3) must match tensor b (4)".
        vc = 0
        for _a, _b, kind in layout.segments:
            if kind in ("cond", "ref_img"):
                if augs is not None and vc < len(augs):
                    s.add(max(t_v, float(augs[vc])))
                else:
                    s.add(max(t_v, vis))
                vc += 1
            elif kind == "ref_audio":
                s.add(max(t_a, aud))
    else:
        # no prebuilt layout: the DiT rebuilds one, and the patched _forward
        # falls back to the same coarse form
        if has_vis:
            s.add(max(t_v, vis))
            for a in (augs or ()):
                s.add(max(t_v, float(a)))
        if has_aud:
            s.add(max(t_a, aud))
    return sorted(s)


def _make_patched_inject(turbo):
    def _inject_adaln_egrid(new_model, dm, lora, adaln, strength):
        E = turbo._egrid()
        shared = {"silu_temb": None}
        shift_v = float(getattr(dm, "sigma_shift_video", turbo.SHIFT_V))
        shift_a = float(getattr(dm, "sigma_shift_audio", turbo.SHIFT_A))

        def wrap(executor, *args, **kwargs):
            ts = args[1] if len(args) > 1 else kwargs.get("timestep")
            ctx = args[2] if len(args) > 2 else kwargs.get("context")
            to = args[3] if len(args) > 3 else kwargs.get("transformer_options")
            payload = kwargs.get("minimax_payload") or {}
            us = correct_unique_t(ts, shift_v, shift_a, payload, to)
            shared["silu_temb"] = turbo._interp_egrid(us, E, ctx.device, ctx.dtype)
            return executor(*args, **kwargs)

        new_model.add_wrapper_with_key(
            comfy.patcher_extension.WrappersMP.DIFFUSION_MODEL, "h3turbo", wrap)
        for name in adaln:
            a = lora[name + ".lora_A.weight"]
            b = lora[name + ".lora_B.weight"] * strength
            key = "diffusion_model." + name.rsplit(".linear", 1)[0]
            new_model.add_object_patch(
                key + ".forward",
                turbo._make_adaln_forward(new_model.get_model_object(key), a, b, shared))

    _inject_adaln_egrid._guide_compat = True
    return _inject_adaln_egrid


_PATCHED = None


def install(at_import=False):
    """Patch the turbo pack if it is present and still has the unfixed signature.

    Safe to call repeatedly: the node retries at execute time in case the turbo pack
    happened to load after this one.
    """
    global _PATCHED
    # no cached early-return: after a custom-node re-import the turbo module can be
    # a FRESH unpatched object while the cache says patched (edge-hunt finding) --
    # the sys.modules scan is cheap, so verify the live module every call
    turbo = _find_turbo()
    if turbo is None:
        return False  # turbo pack not installed; nothing to do
    if getattr(turbo._inject_adaln_egrid, "_guide_compat", False):
        _PATCHED = turbo
        return True
    try:
        argcount = turbo._unique_t.__code__.co_argcount
    except AttributeError:
        return False
    if argcount != 4:
        logging.info("MiniMaxH3Guide: ComfyUI-MiniMax-H3-Turbo's _unique_t signature has "
                     "changed (%d args); assuming the audio fix landed upstream and "
                     "leaving it alone.", argcount)
        return False
    turbo._inject_adaln_egrid = _make_patched_inject(turbo)
    _PATCHED = turbo
    logging.info("MiniMaxH3Guide: patched ComfyUI-MiniMax-H3-Turbo so audio conditioning "
                 "works with the turbo LoRA (its adaln row table ignored ref_audio).")
    if not at_import:
        # a LoRA node that already ran holds a closure built from the UNPATCHED
        # function; the patch only helps once that node re-executes (bug-hunt finding)
        logging.warning("MiniMaxH3Guide: turbo pack was patched late. If a MiniMax H3 "
                        "Turbo LoRA was already applied this session, nudge that node "
                        "(change any widget) so it re-executes before sampling with audio.")
    return True


def turbo_present():
    """True if the turbo pack is loaded, patched or not."""
    return _PATCHED is not None or _find_turbo() is not None
