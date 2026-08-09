"""Regional prompting for H3 — tell the model WHAT belongs in a masked region.

The Soft Denoise Zone controls where change is ALLOWED; this node supplies the
missing half: what goes there. A chosen fragment of the prompt ("a woman in a
red coat") is located in the tokenised text, and the DiT's attention is biased
so the masked region's video rows attend MORE to those tokens (+strength on
the attention logits) while the rest of the frame attends LESS to them
(-strength * containment) — the description is pulled into the zone and kept
out of everywhere else. Works on v2v (pair it with the zone node, same mask)
and on plain generation (place a subject spatially with no footage at all).

Why this is possible on H3: the model runs ONE global self-attention over a
single packed sequence — text tokens, condition rows, audio and video rows all
live side by side — so "these pixels should listen to those words" is just an
additive bias on specific (row, column) pairs of the attention matrix. And it
needs NO core edits: comfy's optimized_attention already accepts a mask and
already exposes an override hook via transformer_options; the packed layout
needed to find the rows rides in the payload every forward.

Mechanics:
- the node reads the FINAL prompt text off the conditioning (the Guide node
  attaches it as minimax_prompt_text) and measures the fragment's token span
  with the same cumulative end-relative machinery timed-text uses;
- a wrapper around diffusion_model.forward (ModelPatcher object patch) sees
  the payload's PackedLayout each call, builds the S x S bias once per layout
  (cached on the payload), and hands it down via an
  optimized_attention_override in a copied transformer_options;
- the uncond pass has a different text length than the positive prompt, so it
  is detected and left untouched; the token-refiner runs outside the wrapper
  and the override additionally gates on sequence length.

Cost: the bias is a dense S x S bf16 tensor (~200 MB at S~10k rows) built
once per run, and an attn_mask usually moves sdpa off the flash path — expect
a moderate speed hit while the node is armed.

EXPERIMENTAL: attention bias is a dial this model never trained with. Expect
a usable range (roughly 1.0-2.5) and a too-high range where composition
degrades. One regional node per graph for now (a second one would replace the
first's bias, not merge with it).
"""

import logging

import torch

from comfy_api.latest import io

from .h3_diff_v2v import prepare_zone_mask
from .minimax_h3_guide import beat_spans, text_ids_fn


class H3RegionalPrompt(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3RegionalPrompt",
            display_name="H3 Regional Prompt (mask)",
            category="model/conditioning/minimax",
            description="Bias H3's attention so a fragment of the prompt describes the MASKED region specifically: the zone's pixels listen harder to those words, the rest of the frame stops listening to them. Pairs with the Soft Denoise Zone (same mask) for region swaps; also places subjects spatially in plain generation. EXPERIMENTAL — first regional prompting on H3.",
            inputs=[
                io.Model.Input("model"),
                io.Clip.Input("clip",
                    tooltip="The H3 text encoder — used to measure the fragment's token span."),
                io.Conditioning.Input("conditioning",
                    tooltip="The Guide node's positive conditioning (read-only — it carries the final prompt text). The SAME conditioning still goes to the sampler as usual."),
                io.String.Input("region_text", multiline=True, default="",
                    tooltip="The part of the prompt that describes what belongs in the masked region. Must appear VERBATIM in the prompt (copy-paste it). Example: prompt '...a woman in a red coat walks through the market...' -> region_text 'a woman in a red coat'."),
                io.Float.Input("strength", default=1.5, min=0.0, max=5.0, step=0.1,
                    tooltip="Additive attention-logit bias pulling the region's pixels toward the fragment's tokens. ~1.0-2.5 is the expected usable range; too high degrades composition. 0 disarms the node."),
                io.Float.Input("containment", default=0.5, min=0.0, max=1.0, step=0.05,
                    tooltip="Fraction of strength applied as a NEGATIVE bias from pixels OUTSIDE the mask to the fragment's tokens — keeps the description from leaking into the rest of the frame. 0 = attract only."),
                io.Float.Input("mask_feather", default=0.1, min=0.0, max=0.5, step=0.01,
                    tooltip="Softens the mask outward (fraction of the short side), interior kept at full strength — same dial as the Soft Denoise Zone."),
                io.Mask.Input("mask",
                    tooltip="WHERE the fragment belongs. Single mask = whole clip; a per-frame batch (SAM2 video segmentation) follows the subject through time. Same input as the Soft Denoise Zone — share the wire."),
            ],
            outputs=[io.Model.Output(
                tooltip="Wire to the sampler in place of the plain model (stack after the Soft Denoise Zone if both are used).")],
        )

    @classmethod
    def execute(cls, model, clip, conditioning, region_text="", strength=1.5,
                containment=0.5, mask_feather=0.1, mask=None) -> io.NodeOutput:
        if strength <= 0.0:
            logging.info("H3RegionalPrompt: strength 0 — passing the model through unpatched.")
            return io.NodeOutput(model)
        frag = (region_text or "").strip()
        if not frag:
            raise ValueError("H3 Regional Prompt: region_text is empty — paste the part "
                             "of the prompt that describes what belongs in the mask.")
        try:
            prompt = conditioning[0][1].get("minimax_prompt_text")
        except Exception:
            prompt = None
        if not prompt:
            raise ValueError(
                "H3 Regional Prompt: the conditioning does not carry its prompt text. "
                "Feed the MiniMax H3 Guide node's positive output (and re-run it once "
                "after updating this pack).")
        at = prompt.find(frag)
        if at < 0:
            raise ValueError(
                "H3 Regional Prompt: region_text is not part of the prompt. It must "
                "appear VERBATIM (the node biases the prompt's own tokens). Prompt "
                "starts: %r" % prompt[:120])
        text_ids = text_ids_fn(clip)
        if text_ids is None:
            raise ValueError("H3 Regional Prompt: this CLIP is not the MiniMax H3 text "
                             "encoder (no _text_ids); token spans can't be measured.")
        spans = beat_spans(text_ids, prompt, [(prompt[:at], prompt[:at + len(frag)])])
        if not spans:
            raise ValueError("H3 Regional Prompt: could not measure the fragment's "
                             "token span — try a slightly longer fragment.")
        start_from_end, stop_from_end = spans[0]
        expected_tokens = len(text_ids(prompt))

        m3 = (mask if mask.ndim == 3 else mask[None]).cpu()
        s_val = float(strength)
        c_val = float(containment)
        feather = float(mask_feather)

        orig_forward = model.get_model_object("diffusion_model.forward")

        def _build_bias(layout, video, device, payload):
            segs = layout.segments
            text_seg = next(((a, b) for a, b, k in segs if k == "text"), None)
            video_seg = next(((a, b) for a, b, k in segs if k == "video"), None)
            if text_seg is None or video_seg is None:
                return None
            t_lat = int(video.shape[2])
            h_r = (int(video.shape[-2]) + 1) // 2 * 2
            w_r = (int(video.shape[-1]) + 1) // 2 * 2
            ph, pw = h_r // 2, w_r // 2
            rows_per_frame = (video_seg[1] - video_seg[0]) // max(1, t_lat)
            if rows_per_frame != ph * pw:
                logging.warning("H3RegionalPrompt: layout video rows (%d/frame) don't "
                                "match the %dx%d patch grid — core layout drift, bias "
                                "skipped.", rows_per_frame, ph, pw)
                return None
            total = text_seg[1] - text_seg[0]
            tok0 = text_seg[0] + (total - start_from_end)
            tok1 = text_seg[0] + (total - stop_from_end)
            if not (text_seg[0] <= tok0 < tok1 <= text_seg[1]):
                logging.warning("H3RegionalPrompt: fragment span [%d,%d) escapes the "
                                "text segment — bias skipped.", tok0, tok1)
                return None
            zone = prepare_zone_mask(m3, t_lat, ph, pw, feather)   # [Tm, ph, pw]
            if zone.shape[0] == 1 and t_lat > 1:
                zone = zone.expand(t_lat, ph, pw)
            zvals = zone.reshape(-1)                               # [t_lat * ph * pw]
            if float(zvals.max()) <= 0.0:
                logging.warning("H3RegionalPrompt: the mask is entirely black — "
                                "no region to bias, skipped.")
                return None
            S = int(layout.position_ids.shape[0])
            # b(z): +strength at z=1, -strength*containment at z=0, linear between
            bvals = (s_val * ((1.0 + c_val) * zvals - c_val)).to(torch.float32)
            bias = torch.zeros(S, S, dtype=torch.bfloat16, device=device)
            v0 = video_seg[0]
            bias[v0:v0 + zvals.shape[0], tok0:tok1] = \
                bvals.view(-1, 1).to(device, torch.bfloat16)
            logging.info("H3RegionalPrompt: bias armed — %d zone rows x %d fragment "
                         "tokens (S=%d, ~%.0f MB), +%.2f / -%.2f logits.",
                         int((zvals > 0.5).sum()), tok1 - tok0, S,
                         S * S * 2 / 1e6, s_val, s_val * c_val)
            return bias

        def _override(bias):
            def ov(func, q, k, v, *a, mask=None, **kw):
                if mask is None and q.ndim == 4 and q.shape[2] == bias.shape[-1]:
                    m4 = bias
                    if m4.dtype != q.dtype:
                        m4 = m4.to(q.dtype)
                    mask = m4.view(1, 1, *bias.shape)
                return func(q, k, v, *a, mask=mask, **kw)
            return ov

        def wrapped(*args, **kwargs):
            payload = kwargs.get("minimax_payload")
            if not isinstance(payload, dict):
                return orig_forward(*args, **kwargs)
            layout = payload.get("layout")
            if layout is None:
                return orig_forward(*args, **kwargs)
            text_seg = next(((a, b) for a, b, k in layout.segments if k == "text"), None)
            if text_seg is None or (text_seg[1] - text_seg[0]) != expected_tokens:
                return orig_forward(*args, **kwargs)   # uncond / foreign prompt
            if "_h3_regional_bias" not in payload:
                x = args[0]
                video = x[0] if isinstance(x, (list, tuple)) else x
                try:
                    payload["_h3_regional_bias"] = _build_bias(
                        layout, video, video.device, payload)
                except Exception:
                    logging.exception("H3RegionalPrompt: bias build failed — "
                                      "continuing unbiased.")
                    payload["_h3_regional_bias"] = None
            bias = payload["_h3_regional_bias"]
            if bias is None:
                return orig_forward(*args, **kwargs)
            to = dict(kwargs.get("transformer_options") or {})
            to["optimized_attention_override"] = _override(bias)
            kwargs = dict(kwargs)
            kwargs["transformer_options"] = to
            return orig_forward(*args, **kwargs)

        m = model.clone()
        m.add_object_patch("diffusion_model.forward", wrapped)
        logging.info("H3RegionalPrompt: armed — %r (%d..%d tokens from end), "
                     "strength %.2f, containment %.2f.",
                     frag[:60], start_from_end, stop_from_end, s_val, c_val)
        return io.NodeOutput(m)
