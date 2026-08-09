"""Orientation-correct preview endpoint for the editor's thumbnails.

Core's /view?preview=webp re-encodes without ImageOps.exif_transpose, so an
orientation-tagged phone JPEG comes back sideways relative to what python
actually loads (minimax_h3_guide.load_input_image DOES transpose). Framing
coordinates are normalized over the displayed pixels, so the display and the
loader must agree on orientation -- this route is /view's preview branch plus
the transpose. Same-size re-encode only; python always reads the original file.

Registered at import when a PromptServer exists; silently absent headless.
"""

import asyncio
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

    IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
    VID_EXT = (".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi")
    AUD_EXT = (".wav", ".mp3", ".ogg", ".oga", ".flac", ".m4a", ".opus")
    KINDS = {"images": IMG_EXT, "video": VID_EXT, "audio": AUD_EXT}

    # Preview encodes are CPU work and MUST stay off the aiohttp event loop:
    # a synchronous re-encode of one 7680px wallpaper blocked the whole server
    # for seconds — every other thumbnail, the websocket, and even queue POSTs
    # stalled behind it (the "gallery stuck on loading" / "queue takes 20s"
    # reports). Work runs in threads, a few at a time.
    _sem = asyncio.Semaphore(4)

    # display copies only — framing coordinates are normalized 0..1, so a
    # bounded preview keeps crops exact while encoding ~10x faster
    PREVIEW_MAX_EDGE = 2048

    @routes.get("/h3guide/browse")
    async def h3guide_browse(request):
        """Real directory listing for the picker: folders even when EMPTY (the
        file-derived approach showed nothing in a fresh tree) and folder
        navigation on the output tab (core's /internal listing is flat).
        q= recursively searches filenames under the current path (bounded)."""
        rq = request.rel_url.query
        type_dir = folder_paths.get_directory_by_type(rq.get("type", "input"))
        if type_dir is None:
            return web.json_response({"error": "bad type"}, status=400)
        rel = rq.get("path", "").replace("\\", "/").strip("/")
        if ".." in rel.split("/"):
            return web.json_response({"error": "bad path"}, status=400)
        root = os.path.abspath(type_dir)
        cur = os.path.abspath(os.path.join(root, rel)) if rel else root
        if os.path.commonpath([root, cur]) != root:
            return web.json_response({"error": "bad path"}, status=403)
        if not os.path.isdir(cur):
            return web.json_response({"error": "no such folder"}, status=404)
        exts = KINDS.get(rq.get("kind", "images"), IMG_EXT)
        q = rq.get("q", "").strip().lower()
        try:
            dirs, files = await asyncio.to_thread(_scan, cur, root, exts, q)
        except OSError as exc:
            return web.json_response({"error": str(exc)}, status=500)
        files.sort(key=lambda t: -t[0])          # newest first
        return web.json_response({"dirs": sorted(dirs),
                                  "files": [f for _, f in files[:800]]})

    def _scan(cur, root, exts, q):
        # filesystem walks stay off the event loop too — a deep tree on a slow
        # disk was another way to stall every request behind one listing
        dirs, files = [], []
        if q:
            # bounded recursive filename search under the current path
            budget = 1500
            for base, dnames, fnames in os.walk(cur):
                depth = os.path.relpath(base, cur).count(os.sep)
                if depth >= 6:
                    dnames[:] = []
                for fn in fnames:
                    if budget <= 0:
                        break
                    if fn.lower().endswith(exts) and q in fn.lower():
                        rp = os.path.relpath(os.path.join(base, fn), root)
                        files.append((os.path.getmtime(os.path.join(base, fn)),
                                      rp.replace("\\", "/")))
                        budget -= 1
                if budget <= 0:
                    break
        else:
            with os.scandir(cur) as it:
                for e in it:
                    rp = os.path.relpath(e.path, root).replace("\\", "/")
                    if e.is_dir():
                        dirs.append(rp)
                    elif e.name.lower().endswith(exts):
                        files.append((e.stat().st_mtime, rp))
        return dirs, files

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
            async with _sem:
                body = await asyncio.to_thread(_encode_preview, file)
        except Exception:
            return web.Response(status=415)  # not an image PIL can read
        return web.Response(body=body, content_type="image/webp",
                            headers={"Cache-Control": "no-cache"})

    def _encode_preview(file):
        with Image.open(file) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            if max(img.size) > PREVIEW_MAX_EDGE:
                img.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="webp", quality=88)
        return buf.getvalue()


register()
