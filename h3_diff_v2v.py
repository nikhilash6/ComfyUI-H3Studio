"""Soft spatial denoise for H3 v2v — differential diffusion on the video stream.

The ask: a v2v restyle where a soft zone (say, a feathered circle over the
subject) gets a HIGH effective denoise while the rest of the frame stays close
to the footage — no hard matte line, the strength itself fades across the
feather.

The mechanism is Differential Diffusion (Levin & Fried, 2023; core ComfyUI
ships it for image models): a per-pixel map in [0,1] is read as "what fraction
of the run this pixel participates in". At every model call the map is
thresholded against the run's progress into a binary mask; pixels not yet
participating are replaced with the ORIGINAL footage latent re-noised to the
current sigma (input side) and pinned to clean footage in the denoised
prediction (output side). A pixel with map value v therefore only denoises for
the last v fraction of the schedule — an effective per-pixel denoise of
v * (the sampler's global denoise), with soft maps giving seamless spatial
transitions because all blending happens in noise space, per step.

Core's node can't do this for H3: the AV latent is a video/audio pair that
ComfyUI packs into one flat tensor for sampling (comfy.utils.pack_latents in
CFGGuider.inner_sample), and the noise-mask plumbing was never wired for that.
So this node implements the whole loop itself in a model_function_wrapper:
unpack the flat sample with latent_shapes, blend the VIDEO stream, leave audio
untouched, repack. The run's starting sigma is captured live (a new run is
detected when sigma rises), so it composes with any scheduler including the
pack's wired-denoise H3BasicScheduler.

Semantics: the sampler's denoise stays the master dial. inner_denoise /
outer_denoise are FRACTIONS OF IT — inner 1.0 / outer 0.3 with the sampler at
0.6 restyles the zone at 0.6 and the surroundings at ~0.18.

EXPERIMENTAL: to our knowledge the first spatially-varying denoise on H3. The
model's attention is global, so a steep strength gradient could produce
content disagreement across the feather — widen the feather if it does.
"""

import logging
import math

import torch

import comfy.utils
from comfy_api.latest import io


def prepare_zone_mask(m3, t_lat, h, w, mask_feather):
    """[B,H,W] mask (single or per-frame batch) -> [Tm, h, w] on a latent-side
    grid, Tm = 1 (static) or t_lat (per-frame, pooled onto H3's (1,4,4,4,4)
    time grid). Feather grows-then-blurs with the original re-maximized in, so
    a hard matte's interior keeps full strength and the falloff extends
    strictly outward. Shared by the Soft Denoise Zone and Regional Prompt."""
    m3 = m3.to(torch.float32).clamp(0.0, 1.0)
    if m3.shape[0] > 1 and t_lat > 1:
        from .h3_studio import mc_pixel_frames, mc_step_offsets
        pixel_frames = mc_pixel_frames(t_lat)
        offsets = mc_step_offsets(t_lat)
        b0 = m3.shape[0]
        if b0 != pixel_frames:
            logging.info("H3Studio: %d mask frames onto a %d-frame clip — "
                         "resampling by index.", b0, pixel_frames)
        idx = [min(b0 - 1, round(p * (b0 - 1) / max(1, pixel_frames - 1)))
               for p in range(pixel_frames)]
        per_step = []
        for k in range(t_lat):
            n_k = mc_pixel_frames(k + 1) - offsets[k]
            grp = m3[[idx[p] for p in
                      range(offsets[k], min(offsets[k] + n_k, pixel_frames))]]
            per_step.append(grp.mean(dim=0))
        v = torch.stack(per_step)               # [T, H, W]
    else:
        v = m3[0:1]                             # [1, H, W] — whole clip
    v = torch.nn.functional.interpolate(
        v.unsqueeze(1), size=(h, w), mode="bilinear", align_corners=False)[:, 0]
    r = int(round(float(mask_feather) * min(h, w)))
    if r > 0:
        k2 = 2 * r + 1
        v4 = v.unsqueeze(1)
        v4 = torch.nn.functional.max_pool2d(v4, kernel_size=k2, stride=1, padding=r)
        for _ in range(3):   # triple box blur ~ gaussian
            v4 = torch.nn.functional.avg_pool2d(
                v4, kernel_size=k2, stride=1, padding=r, count_include_pad=False)
        v = torch.maximum(v4[:, 0], v)
    return v.clamp(0.0, 1.0)


def radial_map(h, w, cx, cy, radius, feather):
    """Feathered disc on the latent grid, circular in FRAME space.

    Coordinates are normalized so the frame's short side is 1 unit; radius is
    a fraction of the short side. Inside radius*(1-feather) the value is 1,
    at radius it reaches 0, smoothstepped between.
    """
    ys = (torch.arange(h, dtype=torch.float32) + 0.5) / h
    xs = (torch.arange(w, dtype=torch.float32) + 0.5) / w
    short = min(h, w)
    dy = (ys - cy).view(h, 1) * (h / short)
    dx = (xs - cx).view(1, w) * (w / short)
    dist = torch.sqrt(dx * dx + dy * dy)
    r0 = radius * (1.0 - min(0.999, feather))
    t = ((radius - dist) / max(radius - r0, 1e-6)).clamp(0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)   # smoothstep


class H3SoftDenoiseZone(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3SoftDenoiseZone",
            display_name="H3 Soft Denoise Zone (v2v)",
            category="model/conditioning/minimax",
            description="Spatially-varying denoise for H3 v2v: a feathered zone (circle or mask) restyles harder than the rest of the frame, with the strength itself fading across the feather — no matte lines. Differential diffusion, adapted to H3's packed AV latent.",
            inputs=[
                io.Model.Input("model"),
                io.Latent.Input("v2v_latent",
                    tooltip="The Guide node's LATENT output with a v2v source active — the SAME latent you feed the sampler. It is the clean footage the protected regions hold on to."),
                io.Float.Input("center_x", default=0.5, min=0.0, max=1.0, step=0.01,
                    tooltip="Zone centre, fraction of the frame width."),
                io.Float.Input("center_y", default=0.5, min=0.0, max=1.0, step=0.01,
                    tooltip="Zone centre, fraction of the frame height."),
                io.Float.Input("radius", default=0.35, min=0.05, max=1.5, step=0.01,
                    tooltip="Zone radius as a fraction of the frame's SHORT side (0.5 spans it edge to edge)."),
                io.Float.Input("feather", default=0.5, min=0.0, max=1.0, step=0.01,
                    tooltip="How much of the radius is the soft falloff. 0 = hard edge (matte-line territory), 0.5 = the outer half fades, 1 = fades from the centre out."),
                io.Float.Input("inner_denoise", default=1.0, min=0.0, max=1.0, step=0.05,
                    tooltip="Fraction of the SAMPLER'S denoise applied inside the zone. 1.0 = the full wired denoise; the sampler stays the master dial."),
                io.Float.Input("outer_denoise", default=0.3, min=0.0, max=1.0, step=0.05,
                    tooltip="Fraction of the sampler's denoise outside the zone. 0 = the surroundings reproduce the footage untouched."),
                io.Int.Input("noise_seed", default=77, min=0, max=2**31 - 1,
                    tooltip="Seed for the re-injection noise field (deterministic; change it only to reroll how protected regions shimmer)."),
                io.Float.Input("mask_feather", default=0.1, min=0.0, max=0.5, step=0.01,
                    tooltip="Softens a MASK input (radius as a fraction of the frame's short side): the mask is grown then blurred, so a hard segmentation matte (SAM2 etc.) fades outward instead of leaving a matte line. 0 = use the mask as-is. Ignored for the circle (it has its own feather)."),
                io.Mask.Input("mask", optional=True,
                    tooltip="Optional: replaces the circle. White areas get inner_denoise, black get outer_denoise, greys blend. A SINGLE mask applies to the whole clip; a mask BATCH (one per frame at 24 fps — e.g. SAM2 video segmentation tracking a person) follows the subject through time, pooled onto the latent grid. Frame counts that don't match the clip are resampled by index."),
            ],
            outputs=[io.Model.Output(
                tooltip="Wire to the sampler in place of the plain model.")],
        )

    @classmethod
    def execute(cls, model, v2v_latent, center_x=0.5, center_y=0.5, radius=0.35,
                feather=0.5, inner_denoise=1.0, outer_denoise=0.3,
                noise_seed=77, mask_feather=0.1, mask=None) -> io.NodeOutput:
        samples = v2v_latent["samples"]
        parts = list(samples.unbind()) if hasattr(samples, "unbind") else [samples]
        video = parts[0]
        if video.ndim == 4:
            video = video.unsqueeze(0)
        if video.ndim != 5:
            raise ValueError("H3 Soft Denoise Zone: expected an H3 AV latent "
                             "(video [B,C,T,H,W]); got shape %s. Feed the Guide "
                             "node's LATENT output with a v2v source active."
                             % (tuple(video.shape),))
        h, w = int(video.shape[-2]), int(video.shape[-1])

        if inner_denoise < outer_denoise:
            logging.info("H3SoftDenoiseZone: inner (%.2f) below outer (%.2f) — the "
                         "zone PROTECTS instead of restyling. Legal, just checking "
                         "it's intended.", inner_denoise, outer_denoise)

        if mask is not None:
            m3 = mask if mask.ndim == 3 else mask[None]
            # per-frame batches (SAM2 etc.) follow the subject through time;
            # single masks apply to the whole clip — see prepare_zone_mask
            v = prepare_zone_mask(m3, int(video.shape[2]), h, w, float(mask_feather))
        else:
            v = radial_map(h, w, float(center_x), float(center_y),
                           float(radius), float(feather)).unsqueeze(0)
        zone = (float(outer_denoise)
                + (float(inner_denoise) - float(outer_denoise)) * v)
        # [1,1,Tm,h,w] with Tm = 1 (static/circle) or the latent T (per-frame)
        map5 = zone.unsqueeze(0).unsqueeze(0).to(torch.float32)

        # footage in sampling space (H3's latent format is scale 1.0 today, but
        # go through the proper door) + a fixed re-injection noise field
        proc = model.model.process_latent_in(samples)
        pparts = list(proc.unbind()) if hasattr(proc, "unbind") else [proc]
        z0 = pparts[0].to(torch.float32)
        if z0.ndim == 4:
            z0 = z0.unsqueeze(0)
        gen = torch.Generator("cpu").manual_seed(int(noise_seed))
        noise = torch.randn(z0.shape, generator=gen, dtype=torch.float32)

        state = {"prev": None, "start": None}
        cache = {}

        def _on(dev):
            got = cache.get(dev)
            if got is None:
                got = (map5.to(dev), z0.to(dev), noise.to(dev))
                cache[dev] = got
            return got

        def wrapper(apply_model, args):
            x = args["input"]
            t = args["timestep"]
            c = args["c"]
            shapes = c.get("latent_shapes")
            if not shapes:
                return apply_model(x, t, **c)
            xparts = comfy.utils.unpack_latents(x, shapes)
            vx = xparts[0]
            if vx.ndim != 5 or vx.shape[-2] != h or vx.shape[-1] != w:
                # different canvas than the latent this node saw — stay out
                return apply_model(x, t, **c)
            s = float(t.max())
            if state["prev"] is None or s > state["prev"] + 1e-9:
                state["start"] = s   # sigma rose: a new run began
            state["prev"] = s
            # differential diffusion: progress threshold falls start -> 0; a
            # pixel with map value m participates once threshold <= m, i.e.
            # for the last m fraction of THIS run (flow timestep == sigma)
            thr = s / max(state["start"], 1e-9)
            mp, z, nz = _on(vx.device)
            mbin = (mp >= thr - 1e-6).to(torch.float32)
            # input side: not-yet-participating pixels carry the footage
            # re-noised to the current sigma (the flow forward process)
            noised = s * nz + (1.0 - s) * z
            vx2 = (vx.to(torch.float32) * mbin + noised * (1.0 - mbin)).to(vx.dtype)
            x2 = comfy.utils.pack_latents([vx2] + list(xparts[1:]))[0]
            out = apply_model(x2, t, **c)
            oparts = comfy.utils.unpack_latents(out, shapes)
            ov = oparts[0]
            # output side: their denoised prediction IS the clean footage, so
            # every integrator step pulls the running sample toward it
            ov2 = (ov.to(torch.float32) * mbin + z * (1.0 - mbin)).to(ov.dtype)
            return comfy.utils.pack_latents([ov2] + list(oparts[1:]))[0]

        m = model.clone()
        m.set_model_unet_function_wrapper(wrapper)
        logging.info("H3SoftDenoiseZone: armed — %s, inner %.2f / outer %.2f of the "
                     "sampler's denoise on a %dx%d latent grid.",
                     "mask input" if mask is not None
                     else "circle at (%.2f, %.2f) r %.2f feather %.2f"
                     % (center_x, center_y, radius, feather),
                     inner_denoise, outer_denoise, w, h)
        return io.NodeOutput(m)
