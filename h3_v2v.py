"""Video-to-video support for MiniMax H3.

build_v2v_latent   -- the encoder: footage (+ audio) -> the H3 AV latent. The
    Guide node's own V2V row calls this, so the whole restyle flow is drivable
    from the timeline editor.

H3BasicScheduler   -- core's BasicScheduler with denoise as a SOCKET, so the
    Guide node's v2v_denoise output can drive the restyle amount over a wire.

Restyling is EXPERIMENTAL, and not a trained H3 task: it should behave like
img2img does everywhere, but the audio stream runs a shifted sigma schedule
internally and partial denoise across that dual schedule is untested territory.
"""

import logging
import math

import torch

import comfy.nested_tensor
import comfy.samplers
from comfy_api.latest import io

from .h3_studio import (FPS_HINT, _resize, encode_ref_audio, CANVAS_MULTIPLE)

DENOISE_MODES = ["slice (core)", "rescale"]

AUDIO_LATENT_FPS = 40
TRAINED_MAX_FRAMES = 362   # the empty-latent tooltip's stated trained range


def build_v2v_latent(vae, audio_vae, images, audio=None, fps=24.0, megapixels=0.0,
                     label="v2v source"):
    """Footage (+ soundtrack) -> ({"samples": NestedTensor}, frame_count).

    Frames trim to the 17k+5 grid (logged), dims round to the 32 grid (an
    optional MP cap scales down first, aspect-preserving). The audio stream is
    built to EXACTLY the length _empty_av_latent would give this frame count, or
    the sampler's pack shapes mismatch. fps only affects the audio-length
    mapping: H3 renders 24fps, so non-24 footage plays retimed (logged).
    """
    n = images.shape[0]
    if n < 5:
        raise ValueError("MiniMax H3 v2v: %s needs at least 5 frames (~0.2s at 24fps), "
                         "got %d." % (label, n))
    while n % 17 != 5:
        n -= 1
    if n != images.shape[0]:
        logging.info("H3Studio v2v: %s trimmed from %d to %d frames (17k+5 grid).",
                     label, images.shape[0], n)
    if n > TRAINED_MAX_FRAMES:
        logging.warning("H3Studio v2v: %s is %d frames (~%.1fs) -- past the "
                        "trained range of ~%d frames; results may degrade.",
                        label, n, n / FPS_HINT, TRAINED_MAX_FRAMES)
    if abs(fps - FPS_HINT) > 0.01:
        logging.warning("H3Studio v2v: %s is %gfps but H3 renders at %dfps -- the "
                        "restyled clip will play retimed. Resample the footage to 24fps "
                        "first if timing matters.", label, fps, FPS_HINT)
    frames = images[:n]
    h, w = frames.shape[1], frames.shape[2]
    if megapixels > 0.0:
        s = min(1.0, math.sqrt((megapixels * 1_000_000.0) / (w * h)))
        w, h = w * s, h * s
    tw = max(CANVAS_MULTIPLE, round(w / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    th = max(CANVAS_MULTIPLE, round(h / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
    if (tw, th) != (frames.shape[2], frames.shape[1]):   # skip the identity resample
        frames = _resize(frames, tw, th, "disabled")
    video = vae.encode(frames)

    audio_t = round((n / FPS_HINT) * AUDIO_LATENT_FPS)
    have_audio = (audio is not None
                  and audio.get("waveform") is not None
                  and audio["waveform"].shape[-1] > 0)
    if audio is not None and not have_audio:
        logging.info("H3Studio v2v: %s audio is empty -- using a silent stream.",
                     label)
    if have_audio:
        if audio_vae is None:
            raise ValueError("MiniMax H3 v2v: %s has audio but audio_vae is empty. Wire "
                             "the MiniMax H3 audio VAE in, or disconnect the audio."
                             % label)
        z, t = encode_ref_audio(audio_vae, audio)
        if t >= audio_t:
            audio_lat = z[..., :audio_t]
        else:
            audio_lat = torch.zeros(z.shape[0], z.shape[1], z.shape[2], audio_t,
                                    dtype=z.dtype, device=z.device)
            audio_lat[..., :t] = z
            logging.info("H3Studio v2v: %s audio shorter than the clip "
                         "(%d < %d latent frames) -- zero-padded.", label, t, audio_t)
    else:
        audio_lat = torch.zeros(1, 32, 2, audio_t, dtype=video.dtype, device=video.device)

    latent = {"samples": comfy.nested_tensor.NestedTensor((video, audio_lat))}
    return latent, n


class H3BasicScheduler(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3BasicScheduler",
            display_name="H3 Basic Scheduler (wired denoise)",
            category="sampling/custom_sampling/schedulers",
            description=(
                "Core's BasicScheduler with denoise as a SOCKET, so the Guide "
                "node's v2v_denoise output can drive the restyle amount over a "
                "wire: Guide.v2v_denoise -> here -> SamplerCustom's sigmas. "
                "'slice' is core's maths exactly; 'rescale' fixes core's dead "
                "zone at high denoise and suits distilled/turbo schedules."),
            inputs=[
                io.Model.Input("model"),
                io.Combo.Input("scheduler", options=comfy.samplers.SCHEDULER_NAMES),
                io.Int.Input("steps", default=20, min=1, max=10000),
                io.Float.Input("denoise", default=1.0, min=0.0, max=1.0,
                               force_input=True,
                               tooltip="Wire the Guide node's v2v_denoise output here (or any float)."),
                io.Combo.Input("denoise_mode", options=DENOISE_MODES, default="slice (core)",
                    tooltip=(
                        "How denoise becomes a schedule.\n\n"
                        "'slice (core)': what every stock scheduler does — build a "
                        "denser int(steps/denoise)-step schedule and keep its tail. "
                        "Because int() truncates, ANY denoise above steps/(steps+1) "
                        "is silently identical to 1.0 (at 3 steps that's everything "
                        "above 0.75; at 20 steps, above 0.95), and the step waypoints "
                        "move — which distilled/turbo LoRAs, trained for one exact "
                        "trajectory, tend to undershoot (residual noise, flicker).\n\n"
                        "'rescale': keep the schedule's own shape and step count, "
                        "compressed into [denoise, 0]. In flow matching sigma IS the "
                        "noise fraction, so starting at sigma=denoise is exact. The "
                        "dial then means what it says at every step count, and a "
                        "turbo trajectory keeps its distilled spacing. Recommended "
                        "for few-step/turbo v2v and for denoise above ~0.85.")),
            ],
            outputs=[io.Sigmas.Output()],
        )

    @classmethod
    def execute(cls, model, scheduler, steps, denoise, denoise_mode="slice (core)") -> io.NodeOutput:
        if denoise <= 0.0:
            return io.NodeOutput(torch.FloatTensor([]))
        ms = model.get_model_object("model_sampling")
        if denoise_mode == "rescale" and denoise < 1.0:
            # Same trajectory, compressed into [denoise, 0]: full step count,
            # distilled spacing preserved, and no truncation dead zone. Valid
            # because a flow model's sigma is literally the noise fraction of
            # x = sigma*noise + (1-sigma)*latent, which is what the sampler
            # seeds with at sigmas[0].
            sigmas = comfy.samplers.calculate_sigmas(ms, scheduler, steps).cpu()
            s0 = float(sigmas[0])
            if s0 <= 0:
                return io.NodeOutput(sigmas)
            scaled = sigmas * (float(denoise) / s0)
            scaled[-1] = sigmas[-1]      # keep the endpoint exactly (0 stays 0)
            logging.info("H3BasicScheduler: rescale denoise %.3f — %d steps from "
                         "sigma %.4f (core's slice would have started at %.4f)",
                         denoise, steps, float(scaled[0]),
                         float(comfy.samplers.calculate_sigmas(
                             ms, scheduler, int(steps / denoise)).cpu()[-(steps + 1)]))
            return io.NodeOutput(scaled)
        total_steps = steps
        if denoise < 1.0:
            total_steps = int(steps / denoise)
            if total_steps == steps:
                logging.info("H3BasicScheduler: denoise %.3f at %d steps rounds to a "
                             "FULL denoise (core's int(steps/denoise) truncation — "
                             "anything above %.3f does). Switch denoise_mode to "
                             "'rescale' if you wanted a partial restyle.",
                             denoise, steps, steps / (steps + 1.0))
        sigmas = comfy.samplers.calculate_sigmas(ms, scheduler, total_steps).cpu()
        sigmas = sigmas[-(steps + 1):]
        return io.NodeOutput(sigmas)
