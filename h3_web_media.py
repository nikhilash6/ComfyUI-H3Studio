"""Free image + audio search for the editor's pickers, via Openverse (the
Creative Commons search API — keyless, licensed results only; it aggregates
Flickr/Wikimedia for images and Freesound/Jamendo for audio).

Routes (registered at import when a PromptServer exists):

  /h3guide/websearch?q=...&kind=images|audio&page=N   proxy the search
  /h3guide/webfetch?url=...&kind=...&...              download a pick into
                                                      input/web/ + log credit

Search defaults to commercial-use licenses (CC0/PD/BY/BY-SA); every result
carries license + creator, the UI shows them, and each download appends a
JSON line to input/web/credits.txt so attribution survives the session.
"""

import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urlsplit

OPENVERSE = {"images": "https://api.openverse.org/v1/images/",
             "audio": "https://api.openverse.org/v1/audio/"}
MAX_BYTES = 80 * 1024 * 1024
SUBDIR = "web"
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
AUD_EXT = (".wav", ".mp3", ".ogg", ".oga", ".flac", ".m4a", ".opus")


def _host_is_private(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True  # unresolvable: refuse
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return True
    return False


def _safe_name(title, url, uid, kind):
    base = (title or "").strip() or os.path.basename(urlsplit(url).path) or "media"
    base = re.sub(r"[^A-Za-z0-9._ -]+", "", base).strip().replace(" ", "-")[:48] or "media"
    base = base.rsplit(".", 1)[0]
    ext = os.path.splitext(urlsplit(url).path)[1].lower()
    if kind == "audio":
        if ext not in AUD_EXT:
            ext = ".mp3"
    elif ext not in IMG_EXT:
        ext = ".jpg"
    tag = (uid or "")[:8] or format(int(time.time()), "x")
    return "%s-%s%s" % (base, tag, ext)


def register():
    try:
        import aiohttp
        from aiohttp import web
        import folder_paths
        from PIL import Image
        from server import PromptServer
    except Exception:
        return  # headless import: nothing to attach to

    try:
        routes = PromptServer.instance.routes
    except Exception:
        return  # server module importable but no live instance (tests)

    @routes.get("/h3guide/websearch")
    async def h3guide_websearch(request):
        q = request.rel_url.query.get("q", "").strip()
        kind = request.rel_url.query.get("kind", "images")
        endpoint = OPENVERSE.get(kind)
        if not q or endpoint is None:
            return web.json_response({"results": []})
        page = request.rel_url.query.get("page", "1")
        params = {
            "q": q, "page_size": "24",
            "page": page if page.isdigit() else "1",
            # no license prefilter: every CC/PD result shows, with its license
            # on the card and in credits.txt — the call is the user's
        }
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(endpoint, params=params,
                                    headers={"User-Agent": "ComfyUI-MiniMaxH3Guide"}) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {"error": "openverse returned %d" % resp.status}, status=502)
                    data = await resp.json()
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=502)
        out = []
        for r in data.get("results", []):
            if not r.get("url"):
                continue
            out.append({
                "id": r.get("id", ""),
                "title": r.get("title") or "",
                "thumb": r.get("thumbnail") or "",
                "url": r.get("url"),
                "license": ("%s %s" % (r.get("license", ""),
                                       r.get("license_version") or "")).strip(),
                "creator": r.get("creator") or "",
                "source": r.get("foreign_landing_url") or "",
                "width": r.get("width") or 0,
                "height": r.get("height") or 0,
                "duration": (r.get("duration") or 0) / 1000.0,   # audio: ms -> s
            })
        return web.json_response({"results": out,
                                  "count": data.get("result_count", len(out))})

    @routes.get("/h3guide/webfetch")
    async def h3guide_webfetch(request):
        q = request.rel_url.query
        url = q.get("url", "")
        kind = q.get("kind", "images")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return web.json_response({"error": "bad url"}, status=400)
        if _host_is_private(parts.hostname):
            return web.json_response({"error": "refusing private address"}, status=403)
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(url, headers={"User-Agent": "ComfyUI-MiniMaxH3Guide"},
                                    max_redirects=5) as resp:
                    if resp.status != 200:
                        return web.json_response(
                            {"error": "source returned %d" % resp.status}, status=502)
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                    body = b""
                    async for chunk in resp.content.iter_chunked(1 << 16):
                        body += chunk
                        if len(body) > MAX_BYTES:
                            return web.json_response({"error": "file too large"}, status=413)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=502)
        if kind == "images":
            # must actually be an image PIL can open — guards mislabeled content
            try:
                import io
                with Image.open(io.BytesIO(body)) as im:
                    im.verify()
            except Exception:
                return web.json_response({"error": "not a decodable image"}, status=415)
        else:
            ext_ok = os.path.splitext(urlsplit(url).path)[1].lower() in AUD_EXT
            if not (ctype.startswith("audio/") or ctype == "application/ogg" or ext_ok):
                return web.json_response({"error": "not audio (%s)" % ctype}, status=415)
        out_dir = os.path.join(folder_paths.get_input_directory(), SUBDIR)
        os.makedirs(out_dir, exist_ok=True)
        name = _safe_name(q.get("title", ""), url, q.get("id", ""), kind)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as f:
            f.write(body)
        credit = {
            "file": SUBDIR + "/" + name,
            "title": q.get("title", ""),
            "creator": q.get("creator", ""),
            "license": q.get("license", ""),
            "source": q.get("source", "") or url,
        }
        try:
            with open(os.path.join(out_dir, "credits.txt"), "a", encoding="utf-8") as f:
                f.write(json.dumps(credit, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return web.json_response({"name": SUBDIR + "/" + name})


register()
