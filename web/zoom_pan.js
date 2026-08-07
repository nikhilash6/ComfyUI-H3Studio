// Load Image (Zoom & Pan) — live framing widget.
//
// Numeric zoom/centre fields are unusable for framing: you cannot judge a crop
// without seeing it. This attaches a canvas DOM widget to the node that renders
// exactly what the node will output — drag to pan, scroll to zoom, double-click
// to reset — and writes the result back into the zoom / center_x / center_y
// widgets so the graph stays the single source of truth.
//
// The crop maths here MUST mirror crop_box() in load_image_zoom_pan.py, including
// the edge clamping, or the preview lies. Kept deliberately in one function below.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "LoadImageZoomPan";
const MIN_ZOOM = 1.0;
const MAX_ZOOM = 32.0;

// --- geometry: mirrors load_image_zoom_pan.py -------------------------------

function resolveOutputSize(outW, outH, srcW, srcH) {
    if (outW <= 0 && outH <= 0) return [srcW, srcH];
    if (outW <= 0) return [Math.max(1, Math.round(outH * (srcW / srcH))), outH];
    if (outH <= 0) return [outW, Math.max(1, Math.round(outW * (srcH / srcW)))];
    return [outW, outH];
}

function cropBox(srcW, srcH, outW, outH, zoom, centerX, centerY) {
    const aspect = outW / outH;
    let baseW, baseH;
    if (srcW / srcH > aspect) {
        baseW = srcH * aspect;
        baseH = srcH;
    } else {
        baseW = srcW;
        baseH = srcW / aspect;
    }
    const losslessZoom = Math.min(baseW / outW, baseH / outH);
    let cropW = baseW / zoom;
    let cropH = baseH / zoom;
    // floor jointly so extreme zoom on tiny sources can't break the aspect lock
    const k = Math.max(1 / cropW, 1 / cropH, 1);
    cropW *= k;
    cropH *= k;
    const cx = Math.min(Math.max(centerX * srcW, cropW / 2), srcW - cropW / 2);
    const cy = Math.min(Math.max(centerY * srcH, cropH / 2), srcH - cropH / 2);
    return { x: cx - cropW / 2, y: cy - cropH / 2, w: cropW, h: cropH, losslessZoom };
}

// --- helpers ---------------------------------------------------------------

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function widgetValue(node, name, fallback) {
    const w = getWidget(node, name);
    return w ? w.value : fallback;
}

function setWidget(node, name, value) {
    const w = getWidget(node, name);
    if (!w) return;
    w.value = value;
    // fire the widget's own callback so anything else listening stays in sync
    w.callback?.(value, app.canvas, node);
}

// widgets carry their Python min/max in options; respect them when snapping
function clampToWidget(node, name, value) {
    const opts = getWidget(node, name)?.options || {};
    const lo = typeof opts.min === "number" ? opts.min : 0;
    const hi = typeof opts.max === "number" ? opts.max : Number.MAX_SAFE_INTEGER;
    return Math.round(Math.min(hi, Math.max(lo, value)));
}

function imageUrl(value) {
    if (!value || typeof value !== "string") return null;
    let name = value;
    let type = "input";
    const annotated = /^(.*)\s*\[(\w+)\]\s*$/.exec(name);
    if (annotated) {
        name = annotated[1].trim();
        type = annotated[2];
    }
    let subfolder = "";
    const slash = name.lastIndexOf("/");
    if (slash > -1) {
        subfolder = name.substring(0, slash);
        name = name.substring(slash + 1);
    }
    // the pack's preview endpoint re-encodes server-side at the SAME resolution
    // (a huge wallpaper PNG arrives in a fraction of the time) AND applies
    // EXIF orientation, matching this node's python loader. Raw /view fallback
    // lives in loadImage. Python still reads the original file.
    return api.apiURL(
        `/h3guide/preview?filename=${encodeURIComponent(name)}&type=${type}` +
        `&subfolder=${encodeURIComponent(subfolder)}&t=${Date.now()}`
    );
}

// --- the widget ------------------------------------------------------------

function attachFramingCanvas(node) {
    const container = document.createElement("div");
    Object.assign(container.style, {
        width: "100%", height: "100%", position: "relative",
        background: "#161616", borderRadius: "4px", overflow: "hidden",
    });

    const canvas = document.createElement("canvas");
    Object.assign(canvas.style, {
        width: "100%", height: "100%", display: "block",
        cursor: "grab", touchAction: "none",
    });
    container.appendChild(canvas);

    const state = { img: null, loadedFor: null, error: null, dragging: false };
    node._zoomPanState = state;

    // ---- rendering
    function render() {
        const ctx = canvas.getContext("2d");
        const rect = canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        const cw = Math.max(1, Math.round(rect.width * dpr));
        const ch = Math.max(1, Math.round(rect.height * dpr));
        if (canvas.width !== cw || canvas.height !== ch) {
            canvas.width = cw;
            canvas.height = ch;
        }

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.fillStyle = "#161616";
        ctx.fillRect(0, 0, cw, ch);

        if (!state.img) {
            ctx.fillStyle = "#777";
            ctx.font = `${12 * dpr}px sans-serif`;
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(state.error || "select an image…", cw / 2, ch / 2);
            return;
        }

        const srcW = state.img.naturalWidth;
        const srcH = state.img.naturalHeight;
        const [outW, outH] = resolveOutputSize(
            widgetValue(node, "output_width", 0) | 0,
            widgetValue(node, "output_height", 0) | 0,
            srcW, srcH
        );
        const zoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(widgetValue(node, "zoom", 1)) || 1));
        const box = cropBox(srcW, srcH, outW, outH,
            zoom,
            Number(widgetValue(node, "center_x", 0.5)),
            Number(widgetValue(node, "center_y", 0.5)));

        // letterbox a viewport of the true output aspect inside the canvas
        const outAspect = outW / outH;
        let vw = cw, vh = cw / outAspect;
        if (vh > ch) { vh = ch; vw = ch * outAspect; }
        const vx = (cw - vw) / 2;
        const vy = (ch - vh) / 2;
        state.viewport = { x: vx, y: vy, w: vw, h: vh, dpr, box, srcW, srcH, outW, outH };

        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = "high";
        ctx.drawImage(state.img, box.x, box.y, box.w, box.h, vx, vy, vw, vh);

        // thirds guide while dragging
        if (state.dragging) {
            ctx.strokeStyle = "rgba(255,255,255,0.28)";
            ctx.lineWidth = Math.max(1, dpr);
            ctx.beginPath();
            for (let i = 1; i < 3; i++) {
                ctx.moveTo(vx + (vw * i) / 3, vy);
                ctx.lineTo(vx + (vw * i) / 3, vy + vh);
                ctx.moveTo(vx, vy + (vh * i) / 3);
                ctx.lineTo(vx + vw, vy + (vh * i) / 3);
            }
            ctx.stroke();
        }

        // frame edge
        ctx.strokeStyle = "rgba(255,255,255,0.35)";
        ctx.lineWidth = Math.max(1, dpr);
        ctx.strokeRect(vx + 0.5, vy + 0.5, vw - 1, vh - 1);

        // HUD
        const upscaling = zoom > box.losslessZoom + 1e-6;
        const label = `${zoom.toFixed(2)}×  ·  ${Math.round(box.w)}×${Math.round(box.h)} → ${outW}×${outH}`;
        ctx.font = `${11 * dpr}px monospace`;
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        const pad = 5 * dpr;
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = "rgba(0,0,0,0.62)";
        ctx.fillRect(pad, pad, tw + pad * 2, 15 * dpr + pad);
        ctx.fillStyle = upscaling ? "#ff9f6b" : "#d8d8d8";
        ctx.fillText(label, pad * 2, pad * 1.6);

        if (upscaling) {
            const warn = `upscaling — sharp to ${box.losslessZoom.toFixed(2)}×`;
            const ww = ctx.measureText(warn).width;
            ctx.fillStyle = "rgba(0,0,0,0.62)";
            ctx.fillRect(pad, pad * 2 + 15 * dpr, ww + pad * 2, 15 * dpr + pad);
            ctx.fillStyle = "#ff9f6b";
            ctx.fillText(warn, pad * 2, pad * 2.6 + 15 * dpr);
        }

        // transient confirmation from the snap buttons
        if (state.flash && performance.now() < state.flash.until) {
            const t = state.flash.text;
            const tww = ctx.measureText(t).width;
            ctx.textBaseline = "bottom";
            ctx.fillStyle = "rgba(0,0,0,0.72)";
            ctx.fillRect(pad, ch - pad - 18 * dpr, tww + pad * 2, 18 * dpr);
            ctx.fillStyle = "#9ee493";
            ctx.fillText(t, pad * 2, ch - pad - 3 * dpr);
        }
    }
    node._zoomPanRender = render;

    // ---- image loading
    function loadImage() {
        const value = widgetValue(node, "image", null);
        if (!value) { state.img = null; state.error = "select an image…"; render(); return; }
        if (state.loadedFor === value && state.img) { render(); return; }
        const url = imageUrl(value);
        if (!url) return;
        // big files take a moment even as webp — say so instead of sitting blank
        state.img = null;
        state.error = "loading…";
        render();
        const img = new Image();
        const want = value;   // a slow earlier load must not clobber a later pick
        img.onload = () => {
            if (widgetValue(node, "image", null) !== want) return;
            state.img = img;
            state.loadedFor = value;
            state.error = null;
            render();
        };
        img.onerror = () => {
            if (widgetValue(node, "image", null) !== want) return;
            if (!img._h3Raw) {
                // preview endpoint refused it — the raw file may still decode
                // (browser applies EXIF itself on raw URLs, so parity holds)
                img._h3Raw = true;
                let raw = value, rtype = "input", rsub = "";
                const ann = /^(.*)\s*\[(\w+)\]\s*$/.exec(raw);
                if (ann) { raw = ann[1].trim(); rtype = ann[2]; }
                const slash = raw.lastIndexOf("/");
                if (slash > -1) { rsub = raw.substring(0, slash); raw = raw.substring(slash + 1); }
                img.src = api.apiURL(`/view?filename=${encodeURIComponent(raw)}&type=${rtype}` +
                    `&subfolder=${encodeURIComponent(rsub)}&t=${Date.now()}`);
                return;
            }
            state.img = null;
            state.error = "could not load image";
            render();
        };
        img.src = url;
    }
    node._zoomPanReload = loadImage;

    // ---- interaction
    function canvasToSource(ev) {
        const rect = canvas.getBoundingClientRect();
        const vp = state.viewport;
        if (!vp) return null;
        const px = (ev.clientX - rect.left) * vp.dpr;
        const py = (ev.clientY - rect.top) * vp.dpr;
        // normalised position within the letterboxed viewport
        const u = (px - vp.x) / vp.w;
        const v = (py - vp.y) / vp.h;
        return { u, v, px, py };
    }

    let drag = null;

    canvas.addEventListener("pointerdown", (ev) => {
        if (!state.img || ev.button !== 0) return;
        ev.stopPropagation();
        ev.preventDefault();
        canvas.setPointerCapture(ev.pointerId);
        const vp = state.viewport;
        drag = {
            startX: ev.clientX, startY: ev.clientY,
            cx: Number(widgetValue(node, "center_x", 0.5)),
            cy: Number(widgetValue(node, "center_y", 0.5)),
            cropW: vp.box.w, cropH: vp.box.h,
            vw: vp.w / vp.dpr, vh: vp.h / vp.dpr,
            srcW: vp.srcW, srcH: vp.srcH,
        };
        state.dragging = true;
        canvas.style.cursor = "grabbing";
        render();
    });

    canvas.addEventListener("pointermove", (ev) => {
        if (!drag) return;
        ev.stopPropagation();
        // drag the image with the cursor: moving right shows what is to the left
        const dxSrc = ((ev.clientX - drag.startX) / drag.vw) * drag.cropW;
        const dySrc = ((ev.clientY - drag.startY) / drag.vh) * drag.cropH;
        const nx = drag.cx - dxSrc / drag.srcW;
        const ny = drag.cy - dySrc / drag.srcH;
        setWidget(node, "center_x", Math.min(1, Math.max(0, Number(nx.toFixed(4)))));
        setWidget(node, "center_y", Math.min(1, Math.max(0, Number(ny.toFixed(4)))));
        render();
        app.graph?.setDirtyCanvas(true, false);
    });

    function endDrag(ev) {
        if (!drag) return;
        drag = null;
        state.dragging = false;
        canvas.style.cursor = "grab";
        try { canvas.releasePointerCapture(ev.pointerId); } catch (e) { /* already released */ }
        render();
    }
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);

    canvas.addEventListener("wheel", (ev) => {
        if (!state.img || !state.viewport) return;
        ev.stopPropagation();
        ev.preventDefault();

        const vp = state.viewport;
        const p = canvasToSource(ev);
        // source pixel currently under the cursor — keep it pinned across the zoom
        const anchorX = vp.box.x + Math.min(1, Math.max(0, p.u)) * vp.box.w;
        const anchorY = vp.box.y + Math.min(1, Math.max(0, p.v)) * vp.box.h;

        const old = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Number(widgetValue(node, "zoom", 1)) || 1));
        const factor = Math.exp(-ev.deltaY * (ev.shiftKey ? 0.00015 : 0.0006));
        const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, old * factor));
        if (Math.abs(next - old) < 1e-6) return;

        const after = cropBox(vp.srcW, vp.srcH, vp.outW, vp.outH, next, 0.5, 0.5);
        const newX = anchorX - Math.min(1, Math.max(0, p.u)) * after.w;
        const newY = anchorY - Math.min(1, Math.max(0, p.v)) * after.h;

        setWidget(node, "zoom", Number(next.toFixed(3)));
        setWidget(node, "center_x",
            Math.min(1, Math.max(0, Number(((newX + after.w / 2) / vp.srcW).toFixed(4)))));
        setWidget(node, "center_y",
            Math.min(1, Math.max(0, Number(((newY + after.h / 2) / vp.srcH).toFixed(4)))));
        render();
        app.graph?.setDirtyCanvas(true, false);
    }, { passive: false });

    canvas.addEventListener("dblclick", (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
        setWidget(node, "zoom", 1.0);
        setWidget(node, "center_x", 0.5);
        setWidget(node, "center_y", 0.5);
        render();
        app.graph?.setDirtyCanvas(true, false);
    });

    // keep LiteGraph from treating canvas drags as node drags
    canvas.addEventListener("mousedown", (ev) => ev.stopPropagation());
    canvas.addEventListener("contextmenu", (ev) => ev.stopPropagation());

    // ---- snap buttons
    function flash(text) {
        state.flash = { text, until: performance.now() + 1400 };
        render();
        setTimeout(render, 1500);
    }

    function snapToSize() {
        if (!state.img) { flash("no image loaded"); return; }
        // clamp width FIRST, then derive height from the clamped value, or an
        // oversized source breaks the aspect it claims to match (bug-hunt finding)
        const srcW = state.img.naturalWidth, srcH = state.img.naturalHeight;
        const w = clampToWidget(node, "output_width", srcW);
        const h = clampToWidget(node, "output_height",
            w === srcW ? srcH : Math.max(8, Math.round((w * srcH) / srcW / 8) * 8));
        setWidget(node, "output_width", w);
        setWidget(node, "output_height", h);
        flash(`output set to ${w}×${h}`);
        app.graph?.setDirtyCanvas(true, false);
    }

    function snapToAspect() {
        if (!state.img) { flash("no image loaded"); return; }
        const srcW = state.img.naturalWidth, srcH = state.img.naturalHeight;
        let w = Number(widgetValue(node, "output_width", 0)) | 0;
        if (w <= 0) w = srcW;
        w = clampToWidget(node, "output_width", w);
        // round to the widgets' step so the numbers stay tidy; the aspect shift is <0.5%
        const h = clampToWidget(node, "output_height",
            Math.max(8, Math.round((w * srcH) / srcW / 8) * 8));
        setWidget(node, "output_width", w);
        setWidget(node, "output_height", h);
        flash(`aspect matched — ${w}×${h}`);
        app.graph?.setDirtyCanvas(true, false);
    }

    const btnSize = node.addWidget("button", "⤢  snap to image size", null, snapToSize);
    const btnAspect = node.addWidget("button", "⇔  match image aspect", null, snapToAspect);
    // Do NOT set .serialize = false on these: the frontend serializes
    // widgets_values by ORIGINAL index (leaving holes for serialize:false) but
    // restores COMPACTLY — mid-array holes shifted every later value, so
    // `resample` came back null on every save/load (bug-hunt finding). Dense
    // serialization of two button placeholders is harmless and symmetric.
    // sit them directly under output_height rather than at the end of the node
    const anchor = node.widgets.findIndex((w) => w.name === "output_height");
    if (anchor > -1) {
        for (const b of [btnSize, btnAspect]) {
            const at = node.widgets.indexOf(b);
            if (at > -1) node.widgets.splice(at, 1);
        }
        node.widgets.splice(anchor + 1, 0, btnSize, btnAspect);
    }

    const widget = node.addDOMWidget("zoom_pan_preview", "zoom_pan_canvas", container, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 220,
    });

    if (typeof ResizeObserver !== "undefined") {
        const ro = new ResizeObserver(() => render());
        ro.observe(container);
        node._zoomPanObserver = ro;
    }

    // redraw whenever any input that affects the framing changes
    for (const name of ["zoom", "center_x", "center_y", "output_width", "output_height"]) {
        const w = getWidget(node, name);
        if (!w) continue;
        const prev = w.callback;
        w.callback = function (...args) {
            const r = prev?.apply(this, args);
            requestAnimationFrame(render);
            return r;
        };
    }
    const imageWidget = getWidget(node, "image");
    if (imageWidget) {
        const prev = imageWidget.callback;
        imageWidget.callback = function (...args) {
            const r = prev?.apply(this, args);
            requestAnimationFrame(loadImage);
            return r;
        };
    }

    loadImage();
    requestAnimationFrame(render);
    return widget;
}

app.registerExtension({
    name: "ComfyUI.MiniMaxH3Guide.LoadImageZoomPan",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            attachFramingCanvas(this);
            if (this.size[1] < 420) this.setSize([Math.max(this.size[0], 320), 420]);
            return r;
        };

        // reloading a saved workflow restores widget values after creation
        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = onConfigure?.apply(this, arguments);
            requestAnimationFrame(() => this._zoomPanReload?.());
            return r;
        };

        const onRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            this._zoomPanObserver?.disconnect();
            return onRemoved?.apply(this, arguments);
        };
    },
});
