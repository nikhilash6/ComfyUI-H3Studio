"""Load Image (Zoom & Pan) -- a LoadImage variant with a virtual camera.

Built for the MiniMax H3 keyframe workflow. Feed the same source image into two or
three of these, vary only zoom / centre, and the keyframes become different framings
of one photograph: a push-in, a pan, a reveal. Because every frame is literally the
same pixels, the scene cannot drift -- no second subject, no changed lighting, no
wandering background. That is exactly what mid-strength keyframes need, since a
weakened anchor lets the model reinterpret content, and you want it reinterpreting
*motion*, not *what the scene is*.

The crop is taken from the full-resolution original and resized once, so zooming
costs no detail until the crop region falls below the output size (the node logs the
zoom at which that happens). Contrast with zooming a pre-resized image, which
throws away detail and then upscales the remains.

Output defaults match the H3 node's canvas, so the conditioning node's internal
resize becomes a no-op and the pixels are resampled exactly once end to end.
"""

import hashlib
import logging
import os

import numpy as np
import torch
from PIL import Image, ImageOps, ImageSequence

import comfy.model_management
import comfy.utils
import folder_paths
import node_helpers
import nodes
from comfy_api.latest import ComfyExtension, io

RESAMPLE_METHODS = ["lanczos", "bicubic", "bilinear", "area", "nearest-exact"]


def resolve_output_size(out_w, out_h, src_w, src_h):
    """0 means 'take it from the source'; one axis given derives the other."""
    if out_w <= 0 and out_h <= 0:
        return src_w, src_h
    if out_w <= 0:
        return max(1, round(out_h * (src_w / src_h))), out_h
    if out_h <= 0:
        return out_w, max(1, round(out_w * (src_h / src_w)))
    return out_w, out_h


def crop_box(src_w, src_h, out_w, out_h, zoom, center_x, center_y):
    """Pixel box of aspect out_w:out_h, shrunk by `zoom`, centred and kept in bounds.

    Returns (x0, y0, x1, y1) and the zoom at which the box stops covering the output
    resolution (i.e. where real upscaling would begin).
    """
    aspect = out_w / out_h
    # largest window of the target aspect that fits inside the source
    if src_w / src_h > aspect:
        base_w, base_h = src_h * aspect, float(src_h)
    else:
        base_w, base_h = float(src_w), src_w / aspect

    lossless_zoom = min(base_w / out_w, base_h / out_h)

    crop_w = base_w / zoom
    crop_h = base_h / zoom
    # floor jointly so extreme zoom on tiny sources can't break the aspect lock
    k = max(1.0 / crop_w, 1.0 / crop_h, 1.0)
    crop_w *= k
    crop_h *= k
    cx = min(max(center_x * src_w, crop_w / 2.0), src_w - crop_w / 2.0)
    cy = min(max(center_y * src_h, crop_h / 2.0), src_h - crop_h / 2.0)

    x0 = int(round(cx - crop_w / 2.0))
    y0 = int(round(cy - crop_h / 2.0))
    x1 = min(src_w, x0 + max(1, int(round(crop_w))))
    y1 = min(src_h, y0 + max(1, int(round(crop_h))))
    x0 = max(0, min(x0, x1 - 1))
    y0 = max(0, min(y0, y1 - 1))
    return (x0, y0, x1, y1), lossless_zoom


def _resample(bhwc, out_w, out_h, method):
    # [B, H, W, C] -> resized [B, H, W, C]; identity size skips the filter entirely
    if bhwc.shape[1] == out_h and bhwc.shape[2] == out_w:
        return bhwc
    s = bhwc.movedim(-1, 1)
    s = comfy.utils.common_upscale(s, out_w, out_h, method, "disabled")
    return s.movedim(1, -1)


def _resample_mask(bhw, out_w, out_h, method):
    """[B, H, W] -> resized [B, H, W].

    Not just _resample with a dummy channel: comfy.utils.lanczos squeezes the channel
    dimension for single-channel input ("the below API is strict and expects grayscale
    to be squeezed") while every other method keeps it. Feeding the result through a
    fixed movedim would transpose the mask. Normalise the rank instead.
    """
    if bhw.shape[1] == out_h and bhw.shape[2] == out_w:
        return bhw
    # masks always resample bilinear: lanczos round-trips through uint8 PIL and
    # quantizes alpha to 8-bit levels (edge-hunt finding)
    s = comfy.utils.common_upscale(bhw.unsqueeze(1), out_w, out_h, "bilinear", "disabled")
    return s[:, 0] if s.ndim == 4 else s


class LoadImageZoomPan(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        input_dir = folder_paths.get_input_directory()
        os.makedirs(input_dir, exist_ok=True)
        files = folder_paths.filter_files_content_types(
            [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))],
            ["image"])
        return io.Schema(
            node_id="LoadImageZoomPan",
            display_name="Load Image (Zoom & Pan)",
            category="image",
            search_aliases=["load image zoom", "pan image", "crop image loader",
                            "ken burns", "keyframe framing", "minimax h3 framing"],
            description="Load an image and frame it with a virtual camera. Crops from the full-resolution original, so zooming costs no detail until the crop drops below the output size. Point several of these at one image with different zoom/centre values to build scene-consistent H3 keyframes.",
            inputs=[
                io.Combo.Input("image", upload=io.UploadType.image, options=sorted(files)),
                io.Float.Input("zoom", default=1.0, min=1.0, max=32.0, step=0.01,
                    tooltip="1.0 frames the whole image. 2.0 shows half the width and height. The crop always keeps the output aspect ratio, and is clamped so it never runs off the edge of the source."),
                io.Float.Input("center_x", default=0.5, min=0.0, max=1.0, step=0.001,
                    tooltip="Horizontal centre of the crop, 0.0 = left edge, 1.0 = right edge. Ignored at zoom 1.0, where the crop already fills the frame."),
                io.Float.Input("center_y", default=0.5, min=0.0, max=1.0, step=0.001,
                    tooltip="Vertical centre of the crop, 0.0 = top, 1.0 = bottom. Ignored at zoom 1.0."),
                io.Int.Input("output_width", default=1344, min=0, max=nodes.MAX_RESOLUTION, step=8,
                    tooltip="Output width. Set to 0 to take it from the source image (or to derive it from output_height, preserving the source aspect). Match this to the H3 node's width so its internal resize does nothing and the pixels are resampled only once."),
                io.Int.Input("output_height", default=768, min=0, max=nodes.MAX_RESOLUTION, step=8,
                    tooltip="Output height. Set to 0 to take it from the source image, or to derive it from output_width."),
                io.Combo.Input("resample", options=RESAMPLE_METHODS, default="lanczos",
                    tooltip="Filter used for the single resize from crop to output. lanczos is sharpest for downscaling, which is the normal case here."),
            ],
            outputs=[io.Image.Output(), io.Mask.Output()],
        )

    @classmethod
    def execute(cls, image, zoom, center_x, center_y, output_width, output_height,
                resample) -> io.NodeOutput:
        image_path = folder_paths.get_annotated_filepath(image)
        img = node_helpers.pillow(Image.open, image_path)

        dtype = comfy.model_management.intermediate_dtype()
        device = comfy.model_management.intermediate_device()

        frames, masks = [], []
        src_w = src_h = None
        box = None
        lossless = None

        for frame in ImageSequence.Iterator(img):
            frame = node_helpers.pillow(ImageOps.exif_transpose, frame)
            rgb = frame.convert("RGB")
            if src_w is None:
                src_w, src_h = rgb.size
                out_w, out_h = resolve_output_size(output_width, output_height, src_w, src_h)
                box, lossless = crop_box(src_w, src_h, out_w, out_h, zoom, center_x, center_y)
            elif rgb.size != (src_w, src_h):
                continue  # ragged sequence: keep only frames matching the first

            arr = torch.from_numpy(np.array(rgb).astype(np.float32) / 255.0)[None]
            frames.append(arr[:, box[1]:box[3], box[0]:box[2], :])

            if "A" in frame.getbands():
                a = torch.from_numpy(np.array(frame.getchannel("A")).astype(np.float32) / 255.0)
                masks.append((1.0 - a)[None, box[1]:box[3], box[0]:box[2]])
            else:
                masks.append(None)

        if not frames:
            raise ValueError("Load Image (Zoom & Pan): no readable frames in %s" % image)

        out_w, out_h = resolve_output_size(output_width, output_height, src_w, src_h)
        if zoom > lossless + 1e-6:
            logging.warning(
                "Load Image (Zoom & Pan): zoom %.2f on a %dx%d source crops to %dx%d, below the "
                "%dx%d output -- this upscales. Detail is preserved up to zoom %.2f.",
                zoom, src_w, src_h, box[2] - box[0], box[3] - box[1], out_w, out_h, lossless)

        out_image = _resample(torch.cat(frames, dim=0), out_w, out_h, resample)
        if any(m is not None for m in masks):
            ref = next(m for m in masks if m is not None)
            filled = [m if m is not None else torch.zeros_like(ref) for m in masks]
            out_mask = _resample_mask(torch.cat(filled, dim=0), out_w, out_h, resample)
        else:
            out_mask = torch.zeros((out_image.shape[0], out_h, out_w), dtype=torch.float32)

        return io.NodeOutput(out_image.to(device=device, dtype=dtype).clamp(0.0, 1.0),
                             out_mask.to(device=device, dtype=dtype).clamp(0.0, 1.0))

    @classmethod
    def fingerprint_inputs(cls, image, zoom, center_x, center_y, output_width, output_height,
                           resample):
        m = hashlib.sha256()
        with open(folder_paths.get_annotated_filepath(image), "rb") as f:
            m.update(f.read())
        return m.digest().hex()

    @classmethod
    def validate_inputs(cls, image, zoom, center_x, center_y, output_width, output_height,
                        resample):
        if not folder_paths.exists_annotated_filepath(image):
            return "Invalid image file: {}".format(image)
        return True
