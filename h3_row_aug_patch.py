"""Per-row noise-aug labels for MiniMax H3 condition rows.

Why: sub-1.0 keyframe/reference strengths used to dilute the condition latent
while its row kept the near-clean timestep label (0.999). The model, told
"this reference is clean," read the injected noise as CONTENT — the distortion
seen at strengths like 0.65. Core's own global knob avoids this by relabeling
the row's timestep to match its blend level; this patch makes that per-row.

Contract with the guide node:
  payload["cond_video_noise_augs"] = [a_0, a_1, ...]   # one per visual cond
                                                       # row, keyframes-then-
                                                       # refs, values in (0,1]
  * when the list is PRESENT, condition latents arrive FINAL (the node already
    blended them as a*z + (1-a)*noise, flow-forward form, role-pinned seeds)
    and each row's modulation timestep becomes max(t_video, a_i)
  * when ABSENT, behavior is bit-identical to stock core

Both patches are anchored source edits of the LIVE core code (inspect the
source, verify the expected block exists verbatim, substitute, recompile in
the module's namespace). Core drift makes install() fail loudly and callers
fall back to node-side blending with the global label — degraded but sane.
"""

import inspect
import logging
import textwrap

import torch

import comfy.ldm.minimax.model as _mmm

_installed = None

# the exact seg_t block in MiniMaxH3Model._forward (after textwrap.dedent of
# the method source, so the base indent is 4)
_FWD_OLD_1 = """\
    vis_aug = float(payload.get("visual_cond_noise_aug", VISUAL_COND_TIMESTEP))
    aud_aug = float(payload.get("audio_cond_noise_aug", AUDIO_COND_TIMESTEP))
    has_vis_cond = any(k in ("cond", "ref_img") for _, _, k in layout.segments)
    has_aud_cond = any(k == "ref_audio" for _, _, k in layout.segments)
    seg_t = {"text": t_v, "video": t_v, "audio": t_a,
             "cond": max(t_v, vis_aug), "ref_img": max(t_v, vis_aug),
             "ref_audio": max(t_a, aud_aug)}
    unique_t = sorted({t_v, t_a} | ({seg_t["cond"]} if has_vis_cond else set())
                      | ({seg_t["ref_audio"]} if has_aud_cond else set()))
    t_row = {t: i for i, t in enumerate(unique_t)}
"""

_FWD_NEW_1 = """\
    vis_aug = float(payload.get("visual_cond_noise_aug", VISUAL_COND_TIMESTEP))
    aud_aug = float(payload.get("audio_cond_noise_aug", AUDIO_COND_TIMESTEP))
    _row_augs = payload.get("cond_video_noise_augs") or None
    seg_t = {"text": t_v, "video": t_v, "audio": t_a,
             "cond": max(t_v, vis_aug), "ref_img": max(t_v, vis_aug),
             "ref_audio": max(t_a, aud_aug)}
    _seg_t_list = []
    _vc_i = 0
    for _sa, _sb, _sk in layout.segments:
        if _sk in ("cond", "ref_img"):
            if _row_augs is not None and _vc_i < len(_row_augs):
                _seg_t_list.append(max(t_v, float(_row_augs[_vc_i])))
            else:
                _seg_t_list.append(seg_t[_sk])
            _vc_i += 1
        else:
            _seg_t_list.append(seg_t[_sk])
    unique_t = sorted({t_v, t_a} | set(_seg_t_list))
    t_row = {t: i for i, t in enumerate(unique_t)}
"""

_FWD_OLD_2 = """\
    mod_segments = []
    for a, b, kind in layout.segments:
        row_base = t_row[seg_t[kind]] * 3
"""

_FWD_NEW_2 = """\
    mod_segments = []
    for _si, (a, b, kind) in enumerate(layout.segments):
        row_base = t_row[_seg_t_list[_si]] * 3
"""

# original _cond_video_rows, verified verbatim before replacement
_CVR_EXPECTED = '''def _cond_video_rows(self, payload, device):
    """Concatenated visual condition rows (normalized latents -> patchified), with condition noise augmentation."""
    rows = []
    aug = payload.get("visual_cond_noise_aug", VISUAL_COND_TIMESTEP)
    seed = int(payload.get("seed", 0))
    # every condition intentionally restarts the same RNG stream
    for z in payload.get("cond_video_latents", []):
        r = patchify_video(z.to(torch.float32), self.patch_size)
        if aug < 1.0:
            gen = torch.Generator("cpu").manual_seed(seed)
            noise = torch.randn(r.shape, generator=gen, dtype=torch.float32)
            r = aug * r + (1.0 - aug) * noise.to(r.device)
        rows.append(r.to(device))
    return torch.cat(rows, dim=0) if rows else None
'''


def _cond_video_rows_rowaug(self, payload, device):
    """Row-aug aware replacement: with cond_video_noise_augs present the rows
    arrive final from the guide node (pre-blended, labels via the list); the
    stock path is reproduced exactly otherwise."""
    rows = []
    aug = payload.get("visual_cond_noise_aug", _mmm.VISUAL_COND_TIMESTEP)
    row_augs = payload.get("cond_video_noise_augs") or None
    seed = int(payload.get("seed", 0))
    for z in payload.get("cond_video_latents", []):
        r = _mmm.patchify_video(z.to(torch.float32), self.patch_size)
        if row_augs is None and aug < 1.0:
            gen = torch.Generator("cpu").manual_seed(seed)
            noise = torch.randn(r.shape, generator=gen, dtype=torch.float32)
            r = aug * r + (1.0 - aug) * noise.to(r.device)
        rows.append(r.to(device))
    return torch.cat(rows, dim=0) if rows else None


def install():
    """Idempotent. True when per-row labels are active; False -> callers must
    fall back to node-side blending (labels stay global)."""
    global _installed
    if _installed is not None:
        return _installed
    _installed = _install()
    return _installed


def _install():
    model_cls = getattr(_mmm, "MiniMaxH3Model", None)
    if model_cls is None:
        logging.warning("H3Studio: MiniMaxH3Model not found; per-row "
                        "strength labels unavailable.")
        return False
    if getattr(model_cls._forward, "_guide_row_aug", False):
        return True

    # 1. _cond_video_rows: full replacement, original verified verbatim
    try:
        cvr_src = textwrap.dedent(inspect.getsource(model_cls._cond_video_rows))
    except (OSError, TypeError) as exc:
        logging.warning("H3Studio: cannot read core source (%s); per-row "
                        "strength labels unavailable.", exc)
        return False
    if cvr_src != _CVR_EXPECTED:
        logging.warning("H3Studio: core's _cond_video_rows changed upstream; "
                        "per-row strength labels unavailable (using blended fallback).")
        return False

    # 2. _forward: two anchored substitutions in the live source
    try:
        fwd_src = textwrap.dedent(inspect.getsource(model_cls._forward))
    except (OSError, TypeError) as exc:
        logging.warning("H3Studio: cannot read _forward source (%s).", exc)
        return False
    if fwd_src.count(_FWD_OLD_1) != 1 or fwd_src.count(_FWD_OLD_2) != 1:
        logging.warning("H3Studio: core's _forward timestep table changed "
                        "upstream; per-row strength labels unavailable "
                        "(using blended fallback).")
        return False
    new_src = fwd_src.replace(_FWD_OLD_1, _FWD_NEW_1, 1).replace(_FWD_OLD_2, _FWD_NEW_2, 1)
    ns = {}
    try:
        exec(compile(new_src, "<h3studio _forward row-aug>", "exec"), _mmm.__dict__, ns)
    except Exception as exc:
        logging.warning("H3Studio: patched _forward failed to compile (%s: %s).",
                        type(exc).__name__, exc)
        return False
    new_forward = ns["_forward"]
    new_forward._guide_row_aug = True
    _cond_video_rows_rowaug._guide_row_aug = True

    model_cls._forward = new_forward
    model_cls._cond_video_rows = _cond_video_rows_rowaug
    logging.info("H3Studio: per-row condition noise-aug labels installed.")
    return True
