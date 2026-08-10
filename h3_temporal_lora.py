"""Temporal LoRA blending for MiniMax H3 — different weights before/after a
moment IN THE CLIP (not a denoise-step schedule; core's hook keyframes already
do steps).

A LoRA is a weight edit and weights apply to every sequence row equally, so no
single forward pass can be "LoRA'd for the first 2 seconds only". This node
does it the only coherent way: each sampling step runs the denoise prediction
TWICE — once with model A's weight state, once with model B's — and blends the
two predictions along the video latent's time axis with a feathered ramp. Both
predictions act on the same evolving latent, so motion stays continuous across
the boundary; A's styling still echoes forward a little through self-attention
(late frames attend to early ones), which reads as a soft hand-off.

Mechanics: all three models must be patch-clones of ONE loaded checkpoint
(load once, branch LoRA loaders). The sampler loads the base state; at each
step this wrapper snapshots the touched weights lazily, rewrites them to
base+LoRA_A for prediction A and base+LoRA_B for prediction B, then restores
the base state exactly (copy-back, no cumulative drift). Costs: ~2x sampling
time, plus VRAM for one extra copy of every LoRA-touched weight.
"""

import logging

import torch

import comfy.lora
import comfy.nested_tensor
import comfy.patcher_extension
import comfy.utils
from comfy_api.latest import io

FPS = 24.0


def _extra_patches(patcher, base):
    """Patch entries present in `patcher` but not inherited from `base`.

    clone() copies the patch LISTS but keeps the entry tuples, so identity
    membership separates a branch's own LoRA entries from the shared ones.
    """
    out = {}
    for key, plist in patcher.patches.items():
        base_ids = {id(t) for t in base.patches.get(key, [])}
        extra = [t for t in plist if id(t) not in base_ids]
        if extra:
            out[key] = extra
    return out


class _TemporalBlend:
    def __init__(self, module, patches_a, patches_b, boundary, feather, audio_from):
        self.module = module
        self.patches_a = patches_a
        self.patches_b = patches_b
        self.touched = sorted(set(patches_a) | set(patches_b))
        self.boundary = boundary
        self.feather = feather
        self.audio_from = audio_from
        self.snap = None

    # ---- weight juggling ---------------------------------------------------
    def _ensure_snap(self):
        if self.snap is not None:
            return
        if getattr(self.module, "model_lowvram", False):
            raise RuntimeError(
                "H3 Temporal LoRA Blend: the model is partially offloaded (lowvram "
                "weight streaming) — per-step weight rewrites would be overwritten. "
                "This node needs the model fully resident on the sampling device.")
        snap = {}
        missing = []
        for key in self.touched:
            try:
                w = comfy.utils.get_attr(self.module, key)
            except Exception:
                missing.append(key)
                continue
            if not torch.is_tensor(w):
                missing.append(key)
                continue
            snap[key] = w.detach().clone()
        if missing:
            logging.warning("H3TemporalLoraBlend: %d LoRA key(s) not found on the "
                            "model and skipped (e.g. %s).", len(missing), missing[0])
            self.touched = [k for k in self.touched if k in snap]
        self.snap = snap
        mb = sum(t.numel() * t.element_size() for t in snap.values()) / 1e6
        logging.info("H3TemporalLoraBlend: snapshotting %d weights (%.0f MB) for "
                     "per-step swaps; boundary %.2fs, feather %.2fs.",
                     len(snap), mb, self.boundary, self.feather)

    @torch.no_grad()
    def _apply(self, patches):
        for key in self.touched:
            w = comfy.utils.get_attr(self.module, key)
            plist = patches.get(key)
            if plist:
                # copy=True is load-bearing: when the snapshot is already fp32,
                # a bare .to() aliases it and calculate_weight would corrupt
                # the snapshot in place
                new_w = comfy.lora.calculate_weight(
                    plist, self.snap[key].to(torch.float32, copy=True), key)
                w.copy_(new_w.to(w.dtype))
            else:
                w.copy_(self.snap[key])

    @torch.no_grad()
    def _restore(self):
        for key in self.touched:
            comfy.utils.get_attr(self.module, key).copy_(self.snap[key])

    @torch.no_grad()
    def release(self):
        """Drop the weight snapshots when a sampling run ends.

        Without this the snapshot dict (a full device-resident copy of every
        LoRA-touched weight — hundreds of MB for a big LoRA) stayed alive on
        the cached model clone BETWEEN runs. Weights are already back on base
        after every step's finally-restore; the extra restore here is a cheap
        belt-and-braces before letting the copies go. The next run simply
        re-snapshots lazily.
        """
        if self.snap is None:
            return
        try:
            self._restore()
        except Exception:
            logging.exception("H3TemporalLoraBlend: restore-on-release failed; "
                              "dropping snapshots anyway.")
        mb = sum(t.numel() * t.element_size() for t in self.snap.values()) / 1e6
        self.snap = None
        logging.info("H3TemporalLoraBlend: released %.0f MB of weight snapshots.", mb)

    # ---- prediction blend --------------------------------------------------
    def _ramp(self, seconds, device):
        # weight of model A per position: 1 before the boundary, 0 after,
        # linear feather centred on the boundary
        if self.feather <= 1e-6:
            return (seconds < self.boundary).to(torch.float32)
        lo = self.boundary - self.feather / 2.0
        return (1.0 - (seconds - lo) / self.feather).clamp(0.0, 1.0)

    def _blend(self, pa, pb):
        try:
            va, aa = pa.tensors
            vb, ab = pb.tensors
        except AttributeError:
            # not an AV nested latent: blend along dim 2 when it exists
            if pa.ndim >= 5:
                t = pa.shape[2]
                frames = (t - 2) // 5 * 17 + 5 if t > 2 else 5
                sec = torch.linspace(0, frames / FPS, t, device=pa.device)
                w = self._ramp(sec, pa.device).view(1, 1, t, 1, 1).to(pa.dtype)
                return pa * w + pb * (1.0 - w)
            return pa
        t = va.shape[2]
        frames = (t - 2) // 5 * 17 + 5 if t > 2 else 5
        clip_sec = frames / FPS
        sec_v = torch.linspace(0, clip_sec, t, device=va.device)
        wv = self._ramp(sec_v, va.device).view(1, 1, t, 1, 1).to(va.dtype)
        video = va * wv + vb * (1.0 - wv)
        if self.audio_from == "model_a":
            audio = aa
        elif self.audio_from == "model_b":
            audio = ab
        else:
            at = aa.shape[-1]
            sec_a = torch.linspace(0, clip_sec, at, device=aa.device)
            wa = self._ramp(sec_a, aa.device).view(1, 1, 1, at).to(aa.dtype)
            audio = aa * wa + ab * (1.0 - wa)
        return comfy.nested_tensor.NestedTensor((video, audio))

    # ---- the wrapper -------------------------------------------------------
    def __call__(self, apply_model, args):
        self._ensure_snap()
        try:
            self._apply(self.patches_a)
            pred_a = apply_model(args["input"], args["timestep"], **args["c"])
            self._apply(self.patches_b)
            pred_b = apply_model(args["input"], args["timestep"], **args["c"])
            return self._blend(pred_a, pred_b)
        finally:
            # ALWAYS end the step on the exact base state: the output patcher
            # only knows about the base patches, so anything else left behind
            # would poison the shared module for every other user of it
            self._restore()


class H3TemporalLoraBlend(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3TemporalLoraBlend",
            display_name="MiniMax H3 Temporal LoRA Blend",
            category="model/minimax",
            is_experimental=True,
            description=(
                "Different LoRA weights before/after a moment IN THE CLIP. Each "
                "sampling step runs both models' predictions and blends them along "
                "the video timeline with a feathered ramp — one coherent motion "
                "trajectory, LoRA influence changing at your chosen second.\n\n"
                "Wire model_base straight from the checkpoint loader, branch your "
                "LoRA loaders off it for model_a (used BEFORE the boundary) and "
                "model_b (AFTER; leave empty to drop to the clean base). All three "
                "must come from the SAME loaded checkpoint.\n\n"
                "Costs ~2x sampling time and one extra VRAM copy of the LoRA-touched "
                "weights. Needs the model fully resident (no lowvram streaming). Do "
                "NOT put the turbo/distill LoRA on only one side — both sides must "
                "expect the same sigma schedule."),
            inputs=[
                io.Model.Input("model_base",
                    tooltip="The shared checkpoint, WITHOUT the time-windowed LoRAs. Sampler loads this state."),
                io.Model.Input("model_a",
                    tooltip="Used BEFORE boundary_seconds. Usually model_base -> LoRA loader(s)."),
                io.Model.Input("model_b", optional=True,
                    tooltip="Used AFTER boundary_seconds. Empty = the clean base (i.e. the LoRA is dropped at the boundary)."),
                io.Float.Input("boundary_seconds", default=2.0, min=0.0, max=20.0, step=0.1,
                    tooltip="Clip time where influence hands over from model_a to model_b."),
                io.Float.Input("feather_seconds", default=0.5, min=0.0, max=10.0, step=0.1,
                    tooltip="Width of the linear cross-blend centred on the boundary. 0 = hard switch between adjacent latent frames."),
                io.Combo.Input("audio_from", options=["ramp", "model_a", "model_b"], default="ramp",
                    tooltip="H3 generates audio jointly. 'ramp' blends the audio stream on the same time curve; or pin the whole soundtrack's prediction to one side."),
            ],
            outputs=[io.Model.Output(tooltip="Wrapped model — feed this to the sampler.")],
        )

    @classmethod
    def execute(cls, model_base, model_a, boundary_seconds,
                feather_seconds=0.5, audio_from="ramp", model_b=None) -> io.NodeOutput:
        if model_a.model is not model_base.model or \
                (model_b is not None and model_b.model is not model_base.model):
            raise ValueError(
                "MiniMax H3 Temporal LoRA Blend: model_a/model_b must be patch-clones "
                "of model_base (load the checkpoint ONCE and branch the LoRA loaders "
                "from it — separate loader nodes create separate weight copies).")
        patches_a = _extra_patches(model_a, model_base)
        patches_b = _extra_patches(model_b, model_base) if model_b is not None else {}
        if not patches_a and not patches_b:
            logging.warning("H3TemporalLoraBlend: neither side adds any patches over "
                            "the base — passing the base model through unchanged.")
            return io.NodeOutput(model_base)
        out = model_base.clone()
        blend = _TemporalBlend(out.model, patches_a, patches_b,
                               float(boundary_seconds), float(feather_seconds),
                               audio_from)
        out.set_model_unet_function_wrapper(blend)

        # snapshots must not outlive the run: an OUTER_SAMPLE wrapper brackets
        # the whole sampling call, so the finally fires exactly once per run
        # (including on interrupt/error)
        def _release_after_sample(executor, *args, **kwargs):
            try:
                return executor(*args, **kwargs)
            finally:
                blend.release()
        out.add_wrapper_with_key(comfy.patcher_extension.WrappersMP.OUTER_SAMPLE,
                                 "h3_tlora_release", _release_after_sample)
        return io.NodeOutput(out)
