"""Video-to-video and section-splice nodes for MiniMax H3.

build_v2v_latent -- the shared encoder: footage (+ audio) -> the H3 AV latent.
    Used by the H3VideoToLatent node AND by the Guide node's v2v inputs, so the
    whole restyle flow is drivable from the timeline editor.

H3VideoToLatent  -- EXPERIMENTAL: encode existing footage (+ audio) into the H3
    AV latent so a normal KSampler at denoise < 1.0 restyles it, img2img-style.
    Not a trained H3 task: video restyling should behave like img2img does
    everywhere, but the audio stream runs a shifted sigma schedule internally
    and partial denoise across that dual schedule is untested territory.

H3FrameRange     -- slice a section (frames + sample-accurate audio) out of a
    clip by seconds, for restyling just that section.

H3Splice         -- bake a restyled section back into the untouched original at
    pixel level: exact frame splice, sample-accurate audio with an optional
    linear crossfade at both joins.

Together with core's LoadVideo / GetVideoComponents / CreateVideo these make the
"section restyle" recipe in the README.
"""

import logging
import math

import torch

import comfy.nested_tensor
import comfy.samplers
from comfy_api.latest import io

from .minimax_h3_guide import (FPS_HINT, _resize, encode_ref_audio, CANVAS_MULTIPLE)

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
        logging.info("MiniMaxH3Guide v2v: %s trimmed from %d to %d frames (17k+5 grid).",
                     label, images.shape[0], n)
    if n > TRAINED_MAX_FRAMES:
        logging.warning("MiniMaxH3Guide v2v: %s is %d frames (~%.1fs) -- past the "
                        "trained range of ~%d frames; results may degrade.",
                        label, n, n / FPS_HINT, TRAINED_MAX_FRAMES)
    if abs(fps - FPS_HINT) > 0.01:
        logging.warning("MiniMaxH3Guide v2v: %s is %gfps but H3 renders at %dfps -- the "
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
        logging.info("MiniMaxH3Guide v2v: %s audio is empty -- using a silent stream.",
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
            logging.info("MiniMaxH3Guide v2v: %s audio shorter than the clip "
                         "(%d < %d latent frames) -- zero-padded.", label, t, audio_t)
    else:
        audio_lat = torch.zeros(1, 32, 2, audio_t, dtype=video.dtype, device=video.device)

    latent = {"samples": comfy.nested_tensor.NestedTensor((video, audio_lat))}
    return latent, n


class H3VideoToLatent(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3VideoToLatent",
            display_name="MiniMax H3 Video To Latent (v2v)",
            category="model/latent/minimax",
            is_experimental=True,
            description="Encode existing footage into the H3 AV latent for video-to-video: sample it with denoise 0.3-0.7 to restyle while keeping the motion (1.0 ignores the source entirely). Frames are expected at 24fps; count trims to the model's 17k+5 grid; dimensions round to the 32px grid (cap them with megapixels -- raw 1080p is ~2x the trained area). EXPERIMENTAL -- v2v is not a trained H3 task, and the audio stream's behaviour under partial denoise is untested. Note: with KSamplerAdvanced use add_noise=enable, or use SamplerCustomAdvanced -- core's add_noise=disable path doesn't understand AV latents.",
            inputs=[
                io.Image.Input("images", tooltip="Source frames at 24fps (e.g. from Get Video Components or H3 Frame Range). At least 5 frames; the count trims down to the 17k+5 grid."),
                io.Vae.Input("vae"),
                io.Audio.Input("audio", optional=True,
                    tooltip="The footage's soundtrack. Encoded into the audio stream so partial denoise restyles it alongside the video; omit for a silent/zeroed audio stream."),
                io.Vae.Input("audio_vae", optional=True,
                    tooltip="The MiniMax H3 audio VAE -- required only when audio is connected."),
                io.Float.Input("fps", default=24.0, min=1.0, max=120.0, step=1.0,
                    tooltip="The footage's real frame rate (wire Get Video Components' fps output). H3 renders at 24fps, so non-24 footage plays retimed -- this input keeps the AUDIO aligned to the frames either way."),
                io.Float.Input("megapixels", default=1.0, min=0.0, max=4.0, step=0.05,
                    tooltip="Cap the encoded frame area (aspect-preserving, down-only, 32px grid). Default 1.0 MP ~= the trained 768x1344 area; 0 = keep the source resolution (raw 1080p is ~2x trained and slow)."),
            ],
            outputs=[io.Latent.Output(),
                     io.Int.Output(display_name="frame_count")],
        )

    @classmethod
    def execute(cls, images, vae, audio=None, audio_vae=None, fps=24.0,
                megapixels=1.0) -> io.NodeOutput:
        latent, n = build_v2v_latent(vae, audio_vae, images, audio, fps, megapixels,
                                     "Video To Latent input")
        return io.NodeOutput(latent, n)


class H3FrameRange(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3FrameRange",
            display_name="MiniMax H3 Frame Range",
            category="image/video",
            description="Cut a section (frames + sample-accurate audio) out of a clip by seconds -- the front half of the section-restyle recipe. Feed the section to H3 Video To Latent, restyle it, then bake it back with H3 Splice.",
            inputs=[
                io.Image.Input("images"),
                io.Float.Input("start_seconds", default=0.0, min=0.0, max=10000.0, step=0.1),
                io.Float.Input("end_seconds", default=0.0, min=0.0, max=10000.0, step=0.1,
                    tooltip="0 = to the end of the clip."),
                io.Float.Input("fps", default=24.0, min=1.0, max=120.0, step=1.0,
                    tooltip="The clip's frame rate (H3 renders at 24; wire Get Video Components' fps for other footage)."),
                io.Audio.Input("audio", optional=True),
            ],
            outputs=[io.Image.Output(display_name="images"),
                     io.Audio.Output(display_name="audio"),
                     io.Int.Output(display_name="start_frame"),
                     io.Int.Output(display_name="frame_count")],
        )

    @classmethod
    def execute(cls, images, start_seconds, end_seconds, fps, audio=None) -> io.NodeOutput:
        total = images.shape[0]
        s = max(0, min(total, int(round(start_seconds * fps))))
        e = total if end_seconds <= 0 else max(0, min(total, int(round(end_seconds * fps))))
        if e <= s:
            raise ValueError("MiniMax H3 Frame Range: empty range -- start %.2fs (frame %d) "
                             "to end %.2fs (frame %d) of a %d-frame clip."
                             % (start_seconds, s, end_seconds, e, total))
        out_images = images[s:e]

        if audio is not None:
            wf = audio["waveform"]
            sr = audio["sample_rate"]
            a0 = int(round(s / fps * sr))
            a1 = int(round(e / fps * sr))
            sl = wf[..., a0:a1]
            want = a1 - a0
            if sl.shape[-1] < want:
                # soundtrack shorter than the video is a normal container state --
                # zero-pad so downstream encode never sees an empty/short slice
                pad = torch.zeros(*sl.shape[:-1], want - sl.shape[-1],
                                  dtype=wf.dtype, device=wf.device)
                sl = torch.cat([sl, pad], dim=-1)
                logging.info("MiniMaxH3Guide range: audio ends %.2fs before the section "
                             "does -- zero-padded.", (want - sl.shape[-1]) / sr)
            out_audio = {"waveform": sl, "sample_rate": sr}
        else:
            out_audio = {"waveform": torch.zeros(1, 2, max(1, int(round((e - s) / fps * 44100)))),
                         "sample_rate": 44100}
        return io.NodeOutput(out_images, out_audio, s, e - s)


class H3Splice(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3Splice",
            display_name="MiniMax H3 Splice",
            category="image/video",
            description="Bake a (restyled) section back into the untouched original: exact frame replacement starting at start_seconds, sample-accurate audio with a linear crossfade at both joins. The back half of the section-restyle recipe.",
            inputs=[
                io.Image.Input("original_images"),
                io.Image.Input("section_images"),
                io.Float.Input("start_seconds", default=0.0, min=0.0, max=10000.0, step=0.1,
                    tooltip="Where the section starts in the original (use H3 Frame Range's start_frame / fps)."),
                io.Float.Input("fps", default=24.0, min=1.0, max=120.0, step=1.0),
                io.Audio.Input("original_audio", optional=True),
                io.Audio.Input("section_audio", optional=True),
                io.Int.Input("audio_crossfade_ms", default=30, min=0, max=1000, step=5,
                    tooltip="Linear crossfade at both audio joins to hide clicks. 0 = hard cut (and a bit-exact round-trip for an unmodified section)."),
            ],
            outputs=[io.Image.Output(display_name="images"),
                     io.Audio.Output(display_name="audio")],
        )

    @classmethod
    def execute(cls, original_images, section_images, start_seconds, fps,
                original_audio=None, section_audio=None,
                audio_crossfade_ms=30) -> io.NodeOutput:
        # alpha is meaningless post-splice; comparing RGB-only also stops a
        # channel mismatch masquerading as a "resize to match" error
        original_images = original_images[..., :3]
        section_images = section_images[..., :3]
        total = original_images.shape[0]
        s = max(0, min(total, int(round(start_seconds * fps))))
        seg = section_images
        if s + seg.shape[0] > total:
            keep = max(0, total - s)
            logging.warning("MiniMaxH3Guide splice: section overruns the original by %d "
                            "frame(s) -- trimming the section to fit.",
                            seg.shape[0] - keep)
            seg = seg[:keep]
        if seg.shape[0] == 0:
            raise ValueError("MiniMax H3 Splice: start_seconds %.2f puts the whole section "
                             "past the end of a %d-frame original." % (start_seconds, total))
        if seg.shape[1:3] != original_images.shape[1:3]:
            raise ValueError("MiniMax H3 Splice: section frames are %dx%d but the original "
                             "is %dx%d -- resize the section to match before splicing."
                             % (seg.shape[2], seg.shape[1],
                                original_images.shape[2], original_images.shape[1]))
        images = torch.cat([original_images[:s], seg, original_images[s + seg.shape[0]:]], dim=0)

        audio = original_audio
        if original_audio is not None and section_audio is not None:
            wf = original_audio["waveform"].clone()
            sr = original_audio["sample_rate"]
            sw = section_audio["waveform"]
            if section_audio["sample_rate"] != sr:
                import torchaudio
                sw = torchaudio.functional.resample(sw, section_audio["sample_rate"], sr)
            sw = sw.to(wf.device)
            a0 = int(round(s / fps * sr))
            a_len = min(sw.shape[-1], max(0, wf.shape[-1] - a0))
            sw = sw[..., :a_len]
            if sw.shape[-2] != wf.shape[-2]:
                sw = sw.mean(dim=-2, keepdim=True).expand(*sw.shape[:-2], wf.shape[-2], sw.shape[-1]).contiguous()
            xf = min(int(sr * audio_crossfade_ms / 1000), a_len // 2)
            wf[..., a0:a0 + a_len] = sw
            if xf > 0:
                ramp = torch.linspace(0.0, 1.0, xf, dtype=wf.dtype, device=wf.device)
                orig = original_audio["waveform"].to(wf.device)
                # no join to hide at a clip edge -- fading there would just bleed
                # the old soundtrack over the restyled audio's attack
                if a0 > 0:
                    wf[..., a0:a0 + xf] = (sw[..., :xf] * ramp
                                           + orig[..., a0:a0 + xf] * (1.0 - ramp))
                e0 = a0 + a_len - xf
                if a0 + a_len < wf.shape[-1]:
                    wf[..., e0:e0 + xf] = (sw[..., a_len - xf:] * (1.0 - ramp)
                                           + orig[..., e0:e0 + xf] * ramp)
            audio = {"waveform": wf, "sample_rate": sr}
        elif section_audio is not None:
            logging.warning("MiniMaxH3Guide splice: section audio given without "
                            "original_audio -- passing the section audio through unspliced.")
            audio = section_audio
        elif original_audio is not None:
            logging.warning("MiniMaxH3Guide splice: no section audio -- the original "
                            "soundtrack is kept over the restyled section.")
        else:
            audio = {"waveform": torch.zeros(1, 2, max(1, int(round(images.shape[0] / fps * 44100)))),
                     "sample_rate": 44100}
        return io.NodeOutput(images, audio)


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
