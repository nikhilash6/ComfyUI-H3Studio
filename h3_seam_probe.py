"""Measure a join, in the graph, where the seam's position is known exactly.

Everything this pack does for continuation -- pinning a tail, continuing a
waveform, correcting brightness, rescaling the clock -- has been judged by
watching the result. That is a bad way to judge a chain, because the failures
are cumulative and small per link: nobody notices a 4% artifact growth or an
8 ms drift on one join, and by the time it is obvious there are twelve clips
to re-render.

So: numbers. Four of them, each answering a different question, because a join
can fail any one on its own.

    LAG           is the new clip's audio the same waveform continued, or a
                  sound-alike? Cross-correlation of the overlap against the
                  source it was pinned from. A real continuation sits within a
                  couple of milliseconds; a cover version wanders.

    CORRELATION   how confident that lag is. Above ~0.9 is continuation. Around
                  0.4-0.6 means the model wrote something that merely resembles
                  the source -- the failure mode that motivated putting pinned
                  audio on the target timeline in the first place.

    LEVEL STEP    does the loudness jump at the cut? Broadband RMS either side.

    FLOOR STEP    does the room tone jump? The 10th-percentile envelope either
                  side, which hears a change of space that the broadband level
                  can hide.

The in-graph position matters. Measuring from decoded files means inferring
where the seam is from clip lengths, and H3 rounds its audio grid up, so every
clip carries a few ms more sound than picture -- an inference error that reads
as a real lag. Here the seam is a frame index we were told, so it is exact.

Credit: the measurement set, the thresholds and the finding that made them
necessary are ComfyUI-H3-Motion-Context's (see the README).
"""

import logging

import torch

from comfy_api.latest import io

FPS = 24.0

# thresholds, for the verdict lines only -- the numbers are always reported
LAG_OK_MS = 3.0
LAG_POOR_MS = 12.0
CORR_GOOD = 0.90
CORR_WEAK = 0.60
STEP_OK_DB = 1.0
STEP_POOR_DB = 3.0


def _mono(audio):
    """[C, L] float -> [L] mono float32, or None."""
    if audio is None:
        return None, 0
    wf = audio.get("waveform")
    if wf is None:
        return None, 0
    x = wf.to(torch.float32)
    while x.ndim > 2:
        x = x[0]
    if x.ndim == 2:
        x = x.mean(dim=0)
    return x, int(audio.get("sample_rate") or 0)


def _rms(x):
    if x.numel() == 0:
        return 0.0
    return float(torch.sqrt(torch.clamp((x * x).mean(), min=0.0)))


def _floor(x, sr):
    """10th-percentile short-window RMS: the noise floor under the content."""
    win = max(1, int(sr * 0.02))
    n = x.numel() // win
    if n < 4:
        return _rms(x)
    frames = x[:n * win].reshape(n, win)
    env = torch.sqrt(torch.clamp((frames * frames).mean(dim=1), min=0.0))
    return float(env.sort().values[max(0, int(n * 0.1))])


def _db_step(before, after):
    """after relative to before, in dB. Large finite value when one side is silent."""
    eps = 1e-9
    return 20.0 * torch.log10(torch.tensor((after + eps) / (before + eps))).item()


def _best_lag(a, b, max_lag):
    """Lag (samples, positive = b is late) maximising normalised correlation.

    Brute force over +-max_lag on the overlap. The windows here are a second or
    two, so this is milliseconds of work and needs no FFT.
    """
    n = min(a.numel(), b.numel())
    if n <= 4 * max_lag:
        max_lag = max(0, n // 4)
    if n <= 2 or max_lag <= 0:
        return 0, 0.0
    a = a - a.mean()
    b = b - b.mean()
    best, best_c = 0, -2.0
    # the compared span leaves max_lag of slack at BOTH ends, so every lag in
    # [-max_lag, +max_lag] indexes inside the buffer. Sizing it n - max_lag
    # instead makes each positive lag run off the end and get skipped, which
    # reads as "no late joins exist" -- exactly the measurement being made.
    span = n - 2 * max_lag
    a_ref = a[max_lag:max_lag + span]
    a_norm = float(torch.sqrt(torch.clamp((a_ref * a_ref).sum(), min=1e-12)))
    for lag in range(-max_lag, max_lag + 1):
        seg = b[max_lag + lag:max_lag + lag + span]
        if seg.numel() != span:
            continue
        d = float(torch.sqrt(torch.clamp((seg * seg).sum(), min=1e-12)))
        c = float((a_ref * seg).sum()) / (a_norm * d)
        if c > best_c:
            best_c, best = c, lag
    return best, best_c


def measure(source, rendered, sr, seam_frames, window_s=1.0):
    """Compare a rendered clip's pinned head against the source it came from.

    `source`   mono tail of the clip that was continued FROM (its end is the join)
    `rendered` mono of the NEW clip, untrimmed, so its first seam_frames frames
               are the pinned head
    `seam_frames` how many frames were pinned (the node's motion_context_frames)

    Returns a dict of measurements; every value may be None when there was not
    enough audio to compute it.
    """
    out = {"sample_rate": sr, "seam_frames": seam_frames,
           "seam_seconds": seam_frames / FPS}
    seam = int(round(seam_frames / FPS * sr))
    win = int(round(window_s * sr))
    out["seam_sample"] = seam

    # --- continuation: the rendered head against the source tail --------------
    head = rendered[:seam]
    tail = source[-seam:] if source.numel() >= seam else source
    n = min(head.numel(), tail.numel())
    if n >= sr // 10:
        max_lag = max(1, int(sr * 0.03))    # +-30 ms is far past any real join
        lag, corr = _best_lag(tail[-n:], head[:n], max_lag)
        out["lag_ms"] = lag / sr * 1000.0
        out["correlation"] = corr
    else:
        out["lag_ms"] = out["correlation"] = None

    # --- continuity across the cut, inside the rendered clip alone ------------
    before = rendered[max(0, seam - win):seam]
    after = rendered[seam:seam + win]
    if before.numel() > sr // 20 and after.numel() > sr // 20:
        out["level_step_db"] = _db_step(_rms(before), _rms(after))
        out["floor_step_db"] = _db_step(_floor(before, sr), _floor(after, sr))
    else:
        out["level_step_db"] = out["floor_step_db"] = None
    return out


def _verdict(m):
    lines = []
    lag, corr = m.get("lag_ms"), m.get("correlation")
    if lag is None:
        lines.append("continuation  : not enough overlap to measure")
    else:
        if corr is not None and corr < CORR_WEAK:
            note = ("SOUND-ALIKE -- the model wrote something that resembles the "
                    "source rather than continuing it")
        elif corr is not None and corr < CORR_GOOD:
            note = "weak: partly continued, partly re-invented"
        elif abs(lag) <= LAG_OK_MS:
            note = "continuation, on time"
        elif abs(lag) <= LAG_POOR_MS:
            note = "continuation, slightly %s" % ("late" if lag > 0 else "early")
        else:
            note = "continuation, but %.0f ms %s" % (abs(lag),
                                                     "late" if lag > 0 else "early")
        lines.append("continuation  : lag %+.1f ms, correlation %.3f  -- %s"
                     % (lag, corr if corr is not None else float("nan"), note))
    for key, label in (("level_step_db", "level at cut"),
                       ("floor_step_db", "room tone   ")):
        v = m.get(key)
        if v is None:
            lines.append("%s  : not enough audio either side" % label)
            continue
        a = abs(v)
        note = ("inaudible" if a <= STEP_OK_DB else
                "audible step" if a <= STEP_POOR_DB else "clear step")
        lines.append("%s  : %+.2f dB  -- %s" % (label, v, note))
    return lines


def report(m):
    w = ["H3 Studio seam probe",
         "seam at frame %d (%.3f s), sample %d of %d Hz"
         % (m["seam_frames"], m["seam_seconds"], m["seam_sample"], m["sample_rate"]),
         ""]
    w += _verdict(m)
    w += ["",
          "lag is measured against the clip this one continues FROM, so it needs",
          "the same file the motion context used. Level and room tone are measured",
          "inside this clip alone, either side of the cut.",
          "",
          "Feed the UNTRIMMED audio: the pinned head has to still be there."]
    return "\n".join(w)


class H3SeamProbe(io.ComfyNode):
    """Measure a motion-context join without leaving the graph."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="H3SeamProbe",
            display_name="H3 Seam Probe",
            category="audio/analysis",
            description="Measure how well a continued clip joins the one before "
                        "it: is the audio the same waveform continued, and do the "
                        "level and room tone step at the cut. Passes the audio "
                        "through unchanged, so it sits inline.",
            inputs=[
                io.Audio.Input("clip_audio", tooltip="The NEW clip's audio, UNTRIMMED -- straight off the audio VAE decode, before the pinned head is removed."),
                io.Audio.Input("source_audio", tooltip="Audio of the clip this one continues FROM (the same file motion_context_file names). Its END is the join."),
                io.Int.Input("seam_frames", default=22, min=1, max=240,
                             tooltip="How many frames were pinned -- the same number as motion_context_frames. The cut sits exactly here."),
                io.Float.Input("window_seconds", default=1.0, min=0.1, max=10.0, step=0.1,
                               tooltip="How much audio either side of the cut to measure the level and room-tone steps over."),
            ],
            outputs=[io.Audio.Output(display_name="audio"),
                     io.String.Output(display_name="report")],
        )

    @classmethod
    def execute(cls, clip_audio, source_audio, seam_frames, window_seconds=1.0) -> io.NodeOutput:
        rendered, sr = _mono(clip_audio)
        source, ssr = _mono(source_audio)
        if rendered is None or source is None:
            raise ValueError("H3 Seam Probe: both audio inputs need a waveform.")
        if sr <= 0 or ssr <= 0:
            raise ValueError("H3 Seam Probe: an input has no sample rate.")
        if ssr != sr:
            # H3 emits 32 kHz; a 48 kHz source here would read as a huge lag
            import torchaudio
            source = torchaudio.functional.resample(source[None], ssr, sr)[0]
            logging.info("H3Studio: seam probe resampled the source from %d to "
                         "%d Hz to match the rendered clip.", ssr, sr)
        m = measure(source, rendered, sr, int(seam_frames), float(window_seconds))
        text = report(m)
        logging.info("H3Studio: seam probe --\n%s", text)
        return io.NodeOutput(clip_audio, text)
