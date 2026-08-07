"""Orientation-correct preview endpoint for the editor's thumbnails.

Core's /view?preview=webp re-encodes without ImageOps.exif_transpose, so an
orientation-tagged phone JPEG comes back sideways relative to what python
actually loads (minimax_h3_guide.load_input_image DOES transpose). Framing
coordinates are normalized over the displayed pixels, so the display and the
loader must agree on orientation -- this route is /view's preview branch plus
the transpose. Same-size re-encode only; python always reads the original file.

Registered at import when a PromptServer exists; silently absent headless.
"""

import os
from io import BytesIO


def register():
    try:
        from server import PromptServer
        from aiohttp import web
        import folder_paths
        from PIL import Image, ImageOps
    except Exception:
        return  # headless / test import: no server to attach to

    routes = PromptServer.instance.routes

    @routes.get("/h3guide/preview")
    async def h3guide_preview(request):
        q = request.rel_url.query
        filename = q.get("filename", "")
        if not filename or filename[0] in "/\\" or ".." in filename:
            return web.Response(status=400)
        type_dir = folder_paths.get_directory_by_type(q.get("type", "input"))
        if type_dir is None:
            return web.Response(status=400)
        subfolder = q.get("subfolder", "")
        full_dir = os.path.abspath(os.path.join(type_dir, subfolder))
        # containment: the resolved directory must stay inside the typed root
        root = os.path.abspath(type_dir)
        if os.path.commonpath([root, full_dir]) != root:
            return web.Response(status=403)
        file = os.path.join(full_dir, os.path.basename(filename))
        if not os.path.isfile(file):
            return web.Response(status=404)
        try:
            with Image.open(file) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                buf = BytesIO()
                img.save(buf, format="webp", quality=90)
        except Exception:
            return web.Response(status=415)  # not an image PIL can read
        return web.Response(body=buf.getvalue(), content_type="image/webp",
                            headers={"Cache-Control": "no-cache"})


register()
