// MiniMax H3 Image to Video (Guide) — timeline editor.
//
// Two surfaces:
//   - NODE SUMMARY (read-only): micro-thumbs in time order, entity counts, error
//     badge. Its only job is "does this node have stuff, and is it broken?" —
//     click it (or the button) to open the editor.
//   - FULLSCREEN EDITOR: DOM shell with a filmstrip of large keyframe cards, a
//     canvas timeline track, a references panel, a persistent right-hand
//     inspector (large preview + editable fields + plain-language explanation of
//     what the current strength value means), and a help strip. Add via [+]
//     cards / picker (input folder + upload); remove via the ✕ on every card,
//     right-click, or the Delete key. Click a thumb for fullscreen preview.
//
// Design notes from the UX review this implements:
//   - markers hit-test BEFORE lanes (reverse-order hit search) — clicking a beat
//     used to fall into the lane rect and create a duplicate on top of it
//   - marker drag moves TIME ONLY; strength has its own handle (stem cap), plus
//     scroll, arrow keys, and the inspector slider — no more diagonal coupling
//   - the strength bands from the README finally reach the UI as live captions
//
// The spec/file text widgets REMAIN the source of truth — parsed on load,
// rewritten on every interaction. API workflows, old saves and headless use are
// untouched; the "raw text specs" toggle reveals the fields.
//
// The parsers below MUST mirror minimax_h3_guide.py (parse_middle_spec,
// parse_timed_text, parse_ref_spec), including comma handling, clamping and
// python's banker's rounding — they are cross-verified headlessly.

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_NAME = "MiniMaxH3ImageToVideoGuide";

// --- pure: spec parsing/formatting, mirrors the Python node -----------------
// (self-contained: everything in this block is extracted and cross-verified
// against the Python parsers headlessly — keep dependencies inside it)

const FPS = 24;
const AUTO_MIDDLE_STRENGTH = 1.0;   // MUST match minimax_h3_guide.py

function snapFrameCount(length) {
    let n = Math.max(5, Math.round(length));
    while (n % 17 !== 5) n++;
    return n;
}

function videoLatentT(frameCount) {
    // mirrors comfy_extras.nodes_minimax_h3.video_latent_t
    return frameCount <= 5 ? 2 : Math.floor((frameCount - 5) / 17) * 5 + 2;
}

function splitN(line, maxParts) {
    // python str.split(",", maxsplit): remainder kept intact in the last part
    const parts = [];
    let rest = line;
    for (let i = 0; i < maxParts - 1; i++) {
        const at = rest.indexOf(",");
        if (at < 0) break;
        parts.push(rest.slice(0, at));
        rest = rest.slice(at + 1);
    }
    parts.push(rest);
    return parts.map((p) => p.trim());
}

function specLines(text) {
    return String(text ?? "").split(/\r?\n/).map((l) => l.trim())
        .filter((l) => l && !l.startsWith("#"));
}

function roundHalfEven(x) {
    // half-UP, matching python's int(floor(x+0.5)) — banker's rounding collapsed
    // half-frame positions a whole frame apart into spurious duplicates
    return Math.floor(x + 0.5);
}

function posToIndex(pos, frameCount) {
    return roundHalfEven(pos <= 1.0 ? pos * (frameCount - 1) : pos);
}

function parseMiddleSpec(text, count, frameCount) {
    // -> {entries: [{frac, strength, desc}], auto} in SLOT order, or {error}
    if (!count) return { entries: [], auto: true };
    let lines = specLines(text);
    let auto = false;
    if (count && !lines.length) {
        auto = true;
        lines = Array.from({ length: count },
            (_, i) => `${((i + 1) / (count + 1)).toFixed(4)}, ${AUTO_MIDDLE_STRENGTH}`);
    }
    const lo = 1, hi = Math.max(1, frameCount - 2);
    const entries = [];
    for (const line of lines) {
        const p = splitN(line, 3);
        if (p.length < 2) return { error: `needs 'position, strength': ${line}` };
        const pos = parseFloat(p[0]), strength = parseFloat(p[1]);
        if (!isFinite(pos) || !isFinite(strength)) return { error: `not a number: ${line}` };
        if (strength < 0 || strength > 2) return { error: `strength out of range: ${line}` };
        const index = Math.min(Math.max(posToIndex(pos, frameCount), lo), hi);
        entries.push({ index, strength, desc: p[2] || "" });
    }
    if (entries.length !== count) {
        // pure COUNT mismatch with well-formed lines: recoverable — the caller
        // pairs what parsed and lets reconcile rewrite the spec (a socket
        // rewire used to lock the editor into a permanent error here)
        return { entries: entries.map((e) => ({ frac: e.index / (frameCount - 1),
            strength: e.strength, desc: e.desc })),
            error: `${entries.length} line(s) for ${count} frame(s)`, countOnly: true };
    }
    if (new Set(entries.map((e) => e.index)).size !== entries.length) {
        if (!auto) return { error: "two middle frames on the same frame index" };
        const used = new Set();
        for (const e of entries) {   // mirror python: nudge auto collisions apart
            while (used.has(e.index)) e.index++;
            if (e.index > hi) return { error: `clip too short for ${entries.length} middle frames` };
            used.add(e.index);
        }
    }
    return { entries: entries.map((e) => ({ frac: e.index / (frameCount - 1), strength: e.strength, desc: e.desc })), auto };
}

function formatMiddleSpec(entries, frameCount) {
    // unique indices, python raises on duplicates — nudge collisions apart,
    // bounded to the interior (an unbounded nudge could write frac 1.0)
    const used = new Set();
    const hi = Math.max(1, frameCount - 2);
    return entries.map((e) => {
        let idx = Math.min(Math.max(roundHalfEven(e.frac * (frameCount - 1)), 1), hi);
        while (used.has(idx) && idx < hi) idx++;
        while (used.has(idx) && idx > 1) idx--;
        used.add(idx);
        const base = `${(idx / (frameCount - 1)).toFixed(4)}, ${e.strength.toFixed(2)}`;
        // a newline in a description would split the spec line and corrupt it
        const desc = (e.desc || "").replace(/\s*\r?\n\s*/g, " ").trim();
        return desc ? `${base}, ${desc}` : base;
    }).join("\n");
}

function parseTimedText(text, frameCount) {
    const out = [];
    for (const line of specLines(text)) {
        const p = splitN(line, 2);
        if (p.length < 2 || !p[1]) return { error: `needs 'position, text': ${line}` };
        const pos = parseFloat(p[0]);
        if (!isFinite(pos)) return { error: `position not a number: ${line}` };
        const index = Math.min(Math.max(posToIndex(pos, frameCount), 0), frameCount - 1);
        out.push({ frac: index / (frameCount - 1), text: p[1] });
    }
    out.sort((a, b) => a.frac - b.frac);
    return { entries: out };
}

function formatTimedText(entries, frameCount) {
    return entries.filter((e) => e.text.trim())
        .map((e) => `${(roundHalfEven(e.frac * (frameCount - 1)) / (frameCount - 1)).toFixed(4)}, ${e.text.replace(/\s*\r?\n\s*/g, " ").trim()}`)
        .join("\n");
}

function parseRefSpec(text, count) {
    if (!count) return { entries: [], auto: true };
    let lines = specLines(text);
    if (count && !lines.length) return { entries: Array(count).fill(1.0), auto: true };
    const entries = [];
    for (const line of lines) {
        const s = parseFloat(splitN(line, 2)[0]);
        if (!isFinite(s)) return { error: `not a number: ${line}` };
        if (s < 0 || s > 2) return { error: `out of range: ${line}` };
        entries.push(s);
    }
    if (entries.length !== count)
        return { entries, error: `${entries.length} line(s) for ${count} ref(s)`, countOnly: true };
    return { entries, auto: false };
}

function formatRefSpec(entries) {
    return entries.map((s) => s.toFixed(2)).join("\n");
}

// --- end pure ---------------------------------------------------------------

let ACTIVE_EDITOR_CLOSE = null;   // one fullscreen editor at a time, across nodes

const COL = {
    bg: "#161616", panel: "#1b1b1b", input: "#0d0d0d",
    axis: "#3a3a3a", tick: "#2a2a2a", text: "#9a9a9a",
    bright: "#d8d8d8", cap: "#6ea8fe", mid: "#e8b45a",
    green: "#9ee493", red: "#e06c60", sel: "#ffffff", slider: "#555",
    border: "#333", divider: "#2a2a2a",
};

function getWidget(node, name) {
    return node.widgets?.find((w) => w.name === name);
}

function widgetValue(node, name, fallback) {
    const w = getWidget(node, name);
    return w ? w.value : fallback;
}

function setWidgetVisible(node, w, visible) {
    if (!w) return;
    if (!visible) {
        if (w.type !== "hidden") {
            w._h3Type = w.type;
            w._h3Compute = w.computeSize;
            w.type = "hidden";
            w.computeSize = () => [0, -4];
        }
        w.hidden = true;
        if (w.element) w.element.style.display = "none";
    } else if (w._h3Type) {
        w.type = w._h3Type;
        w.computeSize = w._h3Compute;
        w.hidden = false;
        if (w.element) w.element.style.display = "";
    }
}

function upstreamImage(node, inputName) {
    const inp = (node.inputs || []).find((i) => i.name === inputName);
    if (!inp || inp.link == null) return null;
    const link = (node.graph || app.graph)?.links?.[inp.link];
    if (!link) return null;
    const src = (node.graph || app.graph)?.getNodeById?.(link.origin_id);
    if (!src) return null;
    const img = src.imgs?.[0] || src._zoomPanState?.img || null;
    return img && img.naturalWidth > 0 ? img : null;
}

// crop window maths — MUST mirror crop_box() in load_image_zoom_pan.py (the
// Python side applies these crops via that exact function; verified earlier
// across 400 parameter combinations for the Zoom & Pan loader)
function cropBoxJS(srcW, srcH, outW, outH, zoom, centerX, centerY) {
    const aspect = outW / outH;
    let baseW, baseH;
    if (srcW / srcH > aspect) { baseW = srcH * aspect; baseH = srcH; }
    else { baseW = srcW; baseH = srcW / aspect; }
    const cropW = Math.max(1, baseW / zoom);
    const cropH = Math.max(1, baseH / zoom);
    const cx = Math.min(Math.max(centerX * srcW, cropW / 2), srcW - cropW / 2);
    const cy = Math.min(Math.max(centerY * srcH, cropH / 2), srcH - cropH / 2);
    return { x: cx - cropW / 2, y: cy - cropH / 2, w: cropW, h: cropH };
}

function parseCropSpec(text, count) {
    // mirror python parse_crop_spec: '-' = none, pad/truncate to count, clamp
    const out = [];
    for (const line of specLines(text).slice(0, count)) {
        if (line === "-" || line === "none") { out.push(null); continue; }
        const p = line.split(",").map((x) => parseFloat(x.trim()));
        if (p.length !== 3 || p.some((v) => !isFinite(v))) { out.push(null); continue; }
        out.push({ cx: Math.min(Math.max(p[0], 0), 1), cy: Math.min(Math.max(p[1], 0), 1),
            z: Math.max(1, p[2]) });
    }
    while (out.length < count) out.push(null);
    return out;
}

function formatCropSpec(crops) {
    return crops.map((c) => c
        ? `${c.cx.toFixed(4)}, ${c.cy.toFixed(4)}, ${c.z.toFixed(3)}` : "-").join("\n");
}

function inputFileUrl(name) {
    // annotated names ("frame.png [output]") resolve to the matching folder —
    // the same syntax folder_paths accepts server-side, so a picked output-folder
    // frame draws AND loads with zero special-casing anywhere else
    let filename = name, type = "input", subfolder = "";
    const ann = /^(.*?)\s*\[(input|output|temp)\]\s*$/.exec(filename);
    if (ann) { filename = ann[1].trim(); type = ann[2]; }
    const slash = filename.lastIndexOf("/");
    if (slash > -1) { subfolder = filename.slice(0, slash); filename = filename.slice(slash + 1); }
    return api.apiURL(`/view?filename=${encodeURIComponent(filename)}&type=${type}` +
        `&subfolder=${encodeURIComponent(subfolder)}`);
}

function previewFileUrl(name) {
    // same name resolution as inputFileUrl, but through the pack's own preview
    // endpoint: core's /view?preview strips EXIF orientation, ours transposes
    // first so the display agrees with what python actually loads
    let filename = name, type = "input", subfolder = "";
    const ann = /^(.*?)\s*\[(input|output|temp)\]\s*$/.exec(filename);
    if (ann) { filename = ann[1].trim(); type = ann[2]; }
    const slash = filename.lastIndexOf("/");
    if (slash > -1) { subfolder = filename.slice(0, slash); filename = filename.slice(slash + 1); }
    return api.apiURL(`/h3guide/preview?filename=${encodeURIComponent(filename)}&type=${type}` +
        `&subfolder=${encodeURIComponent(subfolder)}`);
}

function connectedSlots(node, prefix) {
    const out = [];
    for (const inp of node.inputs || []) {
        if (inp.name?.startsWith(prefix) && inp.link != null) {
            const n = parseInt(inp.name.slice(prefix.length), 10);
            if (!isNaN(n)) out.push({ slot: n, name: inp.name });
        }
    }
    return out.sort((a, b) => a.slot - b.slot);
}

function inputConnected(node, name) {
    return (node.inputs || []).some((i) => i.name === name && i.link != null);
}

// strength band copy — the README's table, finally reaching the UI
function strengthCaption(s, isRef) {
    if (isRef) {
        if (s >= 1.05) return "Overdrive — identity pushed harder than stock.";
        if (s >= 0.85) return "Locked — a firm identity anchor for the whole clip.";
        if (s >= 0.5) return "Likeness hint — model may adapt pose and lighting.";
        if (s >= 0.2) return "Faint influence — loose resemblance at best.";
        return "Barely there — reference is nearly ignored.";
    }
    if (s >= 1.05) return "Overdrive — amplifies the latent, pushes adherence past stock.";
    if (s >= 0.85) return "Pinned — hits this frame (almost) exactly.";
    if (s >= 0.55) return "Sweet spot — keeps composition and colour, frees the motion.";
    if (s >= 0.25) return "Loose hint — mood and palette survive, geometry doesn't.";
    return "Barely there — near-ignored; below 0.3 gets unpredictable.";
}

// everything a setup file captures — deliberately the full conditioning recipe,
// so a saved setup resumes on a fresh node (or a fresh workflow) as-was
const SETUP_FIELDS = [
    "prompt", "width", "height", "length",
    "first_frame_strength", "last_frame_strength",
    "first_frame_file", "last_frame_file", "middle_frame_files", "ref_image_files",
    "first_frame_crop", "last_frame_crop", "middle_frame_crops", "ref_image_crops",
    "middle_frame_spec", "timed_text", "timed_text_mode",
    "ref_spec", "ref_image_size", "ref_megapixels", "ref_audio_strength",
    "ref_audio_files", "ref_video_spec", "ref_video_files", "ref_video_megapixels",
    "ref_video_crops", "v2v_video_file", "v2v_start_seconds", "v2v_end_seconds",
    "v2v_crop",
    "mask_ref_pixels",
];

// schema defaults: importSetup resets fields ABSENT from older setup files to
// these, so a pre-feature setup can't leave stale current values behind
const SETUP_DEFAULTS = {
    prompt: "", width: 1344, height: 768, length: 124,
    first_frame_strength: 1.0, last_frame_strength: 1.0,
    first_frame_file: "", last_frame_file: "", middle_frame_files: "", ref_image_files: "",
    first_frame_crop: "", last_frame_crop: "", middle_frame_crops: "", ref_image_crops: "",
    middle_frame_spec: "", timed_text: "", timed_text_mode: "text only",
    ref_spec: "", ref_image_size: "match", ref_megapixels: 0.0, ref_audio_strength: 1.0,
    ref_audio_files: "", ref_video_spec: "", ref_video_files: "", ref_video_megapixels: 0.0,
    ref_video_crops: "", v2v_video_file: "", v2v_start_seconds: 0.0, v2v_end_seconds: 0.0,
    v2v_crop: "",
    mask_ref_pixels: false,
};

const VIDEO_EXT = /\.(mp4|webm|mov|mkv|m4v|avi)(\s*\[\w+\])?\s*$/i;

async function fetchInternalFiles(type) {
    // /internal is a root-app subapp, NOT aliased under /api — api.fetchApi
    // 404s on it (the output tab showed empty). Try both, newest-first list.
    try {
        const r = await api.fetchApi(`/internal/files/${type}`);
        if (r.ok) return await r.json();
    } catch (e) { /* fall through */ }
    try {
        const base = (api.api_base ?? "").replace(/\/$/, "");
        const r = await fetch(`${base}/internal/files/${type}`);
        if (r.ok) return await r.json();
    } catch (e) { /* unreachable server */ }
    return [];
}

function adaptCanvasJS(w, h) {
    // mirrors core adapt_canvas: 768 short edge, 768*1344 area cap, round to 32
    const ratio = w / h;
    let nw, nh;
    if (ratio >= 1) { nw = 768 * ratio; nh = 768; } else { nw = 768; nh = 768 / ratio; }
    if (nw * nh > 768 * 1344) {
        const s = Math.sqrt((768 * 1344) / (nw * nh));
        nw *= s; nh *= s;
    }
    return [Math.max(32, Math.round(nw / 32) * 32), Math.max(32, Math.round(nh / 32) * 32)];
}
const SETUP_FORMAT = "h3guide-setup";
const SETUP_VERSION = 1;
const CAST_FORMAT = "h3guide-cast";
const CAST_VERSION = 1;
const CAST_PREFIX = "h3cast-";

function buildCastJson(name, images, audio) {
    // images: [{file, strength, crop|null}], audio: [{file}]
    return JSON.stringify({ format: CAST_FORMAT, version: CAST_VERSION,
        name, images, audio }, null, 2);
}

function parseCastJson(text) {
    let j;
    try { j = JSON.parse(text); } catch (e) { return null; }
    if (j?.format !== CAST_FORMAT || !Array.isArray(j.images)) return null;
    return {
        name: String(j.name || "unnamed"),
        images: j.images.filter((e) => e && typeof e.file === "string")
            .map((e) => {
                const s = Number(e.strength);
                const c = e.crop;
                const cropOk = c && Number.isFinite(Number(c.cx))
                    && Number.isFinite(Number(c.cy)) && Number.isFinite(Number(c.z));
                return { file: e.file,
                    strength: Number.isFinite(s) ? Math.min(2, Math.max(0, s)) : 1,
                    crop: cropOk ? { cx: Number(c.cx), cy: Number(c.cy),
                        z: Math.max(1, Number(c.z)) } : null };
            }),
        audio: (Array.isArray(j.audio) ? j.audio : [])
            .filter((e) => e && typeof e.file === "string").map((e) => ({ file: e.file })),
    };
}

function castSlug(name) {
    const base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
    if (base) return base;
    // all-unicode names would otherwise collapse onto one shared "cast" file
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return "cast-" + h.toString(36);
}

const HELP_COPY = [
    ["Waypoints vs beats", "A waypoint is an image the clip should pass through at a moment. A beat is text pinned to a moment — no image."],
    ["Strength", "How hard a frame is enforced. 1.0 = hit it exactly. 0.6–0.8 keeps composition but frees the motion. Above 1.0 = overdrive."],
    ["References", "Not on the timeline — they define the subject for the whole clip. 1.0 locks identity; 0.7 is a likeness hint."],
    ["Auto placement", "Unplaced waypoints are spaced evenly at FULL strength and shown dimmed. Drag one and its placement becomes yours; lower to ~0.6 to free the motion."],
];

// ============================================================================

function attachTimeline(node) {
    const state = {
        mids: [], beats: [], refs: [], midsAuto: true, refsAuto: true,
        videoRefs: [], videoRefsAuto: true, videoMeta: new Map(),
        firstCrop: null, lastCrop: null, midCrops: [], refCrops: [], videoCrops: [],
        sel: null,           // {kind:'first'|'last'|'mid'|'beat'|'ref', i?}
        drag: null, syncing: false, specError: null,
        thumbTry: 0, thumbTimer: null,
        imgCache: new Map(),
        fs: null,            // fullscreen editor DOM refs
        modal: null,
        dragReadout: null,
    };
    node._h3State = state;

    const fc = () => snapFrameCount(Number(widgetValue(node, "length", 124)) || 124);
    const fileLinesOf = (name) => specLines(widgetValue(node, name, ""));

    function setWidget(name, value) {
        const w = getWidget(node, name);
        if (!w || w.value === value) return;
        state.syncing = true;
        try {
            w.value = value;
            w.callback?.(value, app.canvas, node);
        } finally {
            state.syncing = false;
        }
    }

    function cachedImg(name) {
        // display copies come through the pack's preview endpoint: a 7680px
        // wallpaper PNG is tens of MB raw but ~1–2MB as same-resolution webp
        // (EXIF-transposed, so orientation matches the python loader). If the
        // preview 500s, fall back to the raw file before declaring failure.
        const url = previewFileUrl(name);
        let img = state.imgCache.get(url);
        if (!img) {
            img = new Image();
            img.onload = () => { state.fs?.fill?.(); renderSummary(); };
            img.onerror = () => {
                if (!img._h3Raw) {
                    img._h3Raw = true;
                    img.src = inputFileUrl(name);   // raw: browser applies EXIF itself
                } else {
                    img._h3Failed = true;
                    state.fs?.fill?.(); renderSummary();
                }
            };
            img.src = url;
            state.imgCache.set(url, img);
        }
        return img.naturalWidth > 0 ? img : null;
    }
    function cachedImgFailed(name) {
        return !!state.imgCache.get(previewFileUrl(name))?._h3Failed;
    }

    function ensureVideoMeta(name) {
        // videoMeta is normally filled by the reference-video cards; the v2v bar
        // needs dims for footage that has no card. undefined = not started,
        // null = loading, object = known.
        const have = state.videoMeta.get(name);
        if (have !== undefined) return (have && have.w) ? have : null;
        state.videoMeta.set(name, null);
        const vv = document.createElement("video");
        vv.muted = true;
        vv.preload = "metadata";
        vv.src = inputFileUrl(name);
        vv.addEventListener("loadedmetadata", () => {
            state.videoMeta.set(name, { dur: vv.duration, w: vv.videoWidth, h: vv.videoHeight });
            vv.removeAttribute("src"); vv.load();
            state.fs?.fill?.();
        }, { once: true });
        vv.addEventListener("error", () => {
            // marked failed (not deleted): consumers treat it as no-meta, and the
            // card path below may still overwrite it with a successful read
            state.videoMeta.set(name, { failed: true });
            state.fs?.fill?.();
        }, { once: true });
        return null;
    }

    // ---- widget <-> state sync -------------------------------------------
    function pullFromWidgets() {
        const F = fc();
        const midSlots = connectedSlots(node, "middle_frame_");
        const midFiles = fileLinesOf("middle_frame_files");
        const m = parseMiddleSpec(widgetValue(node, "middle_frame_spec", ""),
            midSlots.length + midFiles.length, F);
        const midSrcs = [
            ...midSlots.map((s) => ({ type: "socket", slot: s.slot })),
            ...midFiles.map((name, idx) => ({ type: "file", idx, name })),
        ];
        state.midSpecError = !!m.error && !m.countOnly;
        if (m.error && m.countOnly) {
            // recoverable count mismatch. Sockets sit at the HEAD of the combined
            // order, so a socket connect/disconnect shifts the head — pairing
            // from the TAIL keeps file entities matched to their own lines
            state.specError = null;
            const k = Math.min(m.entries.length, midSrcs.length);
            const ents = m.entries.slice(-k), srcs2 = midSrcs.slice(-k);
            state.mids = ents.map((e, i) => ({ ...e, src: srcs2[i] }));
            state.midsAuto = false;
        } else if (m.error) {
            state.specError = "middle spec: " + m.error;
            state.mids = [];
        } else {
            state.specError = null;
            state.mids = m.entries.map((e, i) => ({ ...e, src: midSrcs[i] }));
            state.midsAuto = m.auto;
        }
        const t = parseTimedText(widgetValue(node, "timed_text", ""), F);
        state.timedTextError = !!t.error;
        if (!t.error) {
            const drafts = state.beats.filter((b) => !b.text.trim());
            // keep drafts, but sort the whole lane so rebuild order matches the
            // local sorts and selection indices stay meaningful
            state.beats = [...t.entries, ...drafts].sort((a, b) => a.frac - b.frac);
        } else state.specError = (state.specError ? state.specError + " · " : "") + "timed text: " + t.error;
        const refSlots = connectedSlots(node, "ref_image_");
        const refFiles = fileLinesOf("ref_image_files");
        const refSrcs = [
            ...refSlots.map((s) => ({ type: "socket", slot: s.slot })),
            ...refFiles.map((name, idx) => ({ type: "file", idx, name })),
        ];
        const r = parseRefSpec(widgetValue(node, "ref_spec", ""), refSrcs.length);
        state.refSpecError = !!r.error && !r.countOnly;
        if (r.error && r.countOnly) {
            const k = Math.min(r.entries.length, refSrcs.length);
            const ents = r.entries.slice(-k), srcs2 = refSrcs.slice(-k);
            state.refs = ents.map((s, i) => ({ strength: s, src: srcs2[i] }));
            state.refsAuto = false;
        } else if (r.error) {
            state.specError = (state.specError ? state.specError + " · " : "") + "ref spec: " + r.error;
        } else {
            state.refs = r.entries.map((s, i) => ({ strength: s, src: refSrcs[i] }));
            state.refsAuto = r.auto;
        }
        const vidSlots = connectedSlots(node, "ref_video_");
        const vidFiles = fileLinesOf("ref_video_files");
        const vidSrcs = [
            ...vidSlots.map((s) => ({ type: "socket", slot: s.slot })),
            ...vidFiles.map((name, idx) => ({ type: "file", idx, name })),
        ];
        const vr = parseRefSpec(widgetValue(node, "ref_video_spec", ""), vidSrcs.length);
        state.videoSpecError = !!vr.error && !vr.countOnly;
        if (vr.error && vr.countOnly) {
            const k = Math.min(vr.entries.length, vidSrcs.length);
            const ents = vr.entries.slice(-k), srcs2 = vidSrcs.slice(-k);
            state.videoRefs = ents.map((s, i) => ({ strength: s, src: srcs2[i] }));
            state.videoRefsAuto = false;
        } else if (vr.error) {
            state.specError = (state.specError ? state.specError + " · " : "") + "video spec: " + vr.error;
        } else {
            state.videoRefs = vr.entries.map((s, i) => ({ strength: s, src: vidSrcs[i] }));
            state.videoRefsAuto = vr.auto;
        }
        state.firstCrop = parseCropSpec(widgetValue(node, "first_frame_crop", ""), 1)[0];
        state.lastCrop = parseCropSpec(widgetValue(node, "last_frame_crop", ""), 1)[0];
        state.midCrops = parseCropSpec(widgetValue(node, "middle_frame_crops", ""), state.mids.length);
        state.refCrops = parseCropSpec(widgetValue(node, "ref_image_crops", ""), state.refs.length);
        state.videoCrops = parseCropSpec(widgetValue(node, "ref_video_crops", ""), state.videoRefs.length);
    }

    function pushCrops() {
        setWidget("first_frame_crop", state.firstCrop
            ? formatCropSpec([state.firstCrop]) : "");
        setWidget("last_frame_crop", state.lastCrop
            ? formatCropSpec([state.lastCrop]) : "");
        if (!state.midSpecError)   // error state: crop array length is not authoritative
            setWidget("middle_frame_crops", state.midCrops.some(Boolean)
                ? formatCropSpec(state.midCrops) : "");
        if (!state.refSpecError)   // error state: crop array length is not authoritative
            setWidget("ref_image_crops", state.refCrops.some(Boolean)
                ? formatCropSpec(state.refCrops) : "");
        if (!state.videoSpecError)   // error state: crop array length is not authoritative
            setWidget("ref_video_crops", state.videoCrops.some(Boolean)
                ? formatCropSpec(state.videoCrops) : "");
    }

    // banker's-rounded 32 grid — MUST mirror python's round(x/32)*32
    function snap32(v) {
        const q = v / 32, f = Math.floor(q), d = q - f;
        const r = d > 0.5 ? f + 1 : d < 0.5 ? f : (f % 2 === 0 ? f : f + 1);
        return Math.max(32, r * 32);
    }

    // the canvas conditioning actually conforms to: the widgets normally, but
    // with v2v active the footage (or the framed window) takes over — python
    // overrides width/height, so warnings and framers must use the same truth.
    // Returns null when the canvas is unknowable client-side (socket footage,
    // dims still loading) — callers should stay silent rather than guess.
    function effWH() {
        const vf = String(widgetValue(node, "v2v_video_file", "")).trim();
        const vSock = inputConnected(node, "v2v_images");
        if (!vSock && !vf) return outWH();
        if (cropOf({ kind: "v2v" })) {
            const [w, h] = outWH();
            return [snap32(w), snap32(h)];
        }
        if (vf) {
            const m = state.videoMeta.get(vf);
            if (m?.w) return [snap32(m.w), snap32(m.h)];
        }
        return null;
    }

    // canvas aspect for keyframe crop windows — the whole point: what the model
    // sees is framed in the OUTPUT aspect, so there is no mismatch to resolve
    const outWH = () => [Number(widgetValue(node, "width", 1344)) || 1344,
        Number(widgetValue(node, "height", 768)) || 768];

    function cropOf(sel) {
        if (!sel) return null;
        if (sel.kind === "first") return state.firstCrop;
        if (sel.kind === "last") return state.lastCrop;
        if (sel.kind === "mid") return state.midCrops[sel.i] || null;
        if (sel.kind === "ref") return state.refCrops[sel.i] || null;
        if (sel.kind === "video") return state.videoCrops[sel.i] || null;
        if (sel.kind === "v2v")
            return parseCropSpec(widgetValue(node, "v2v_crop", ""), 1)[0];
        return null;
    }
    function setCropOf(sel, crop) {
        if (sel.kind === "first") state.firstCrop = crop;
        else if (sel.kind === "last") state.lastCrop = crop;
        else if (sel.kind === "mid") state.midCrops[sel.i] = crop;
        else if (sel.kind === "ref") state.refCrops[sel.i] = crop;
        else if (sel.kind === "video") state.videoCrops[sel.i] = crop;
        else if (sel.kind === "v2v") {
            // single-line widget, not part of the entity crop arrays
            setWidget("v2v_crop", crop ? formatCropSpec([crop]) : "");
            return;
        }
        pushCrops();
    }

    function pushMids() {
        state.midsAuto = false;
        setWidget("middle_frame_spec", formatMiddleSpec(state.mids, fc()));
    }
    function pushBeats() {
        if (state.timedTextError) {
            toast("fix the timed_text error first (✎ raw text specs) — editing beats now would overwrite your lines", true);
            return;
        }
        setWidget("timed_text", formatTimedText(state.beats, fc()));
    }
    function pushRefs() {
        state.refsAuto = false;
        setWidget("ref_spec", formatRefSpec(state.refs.map((r) => r.strength)));
    }
    function pushVideoRefs() {
        state.videoRefsAuto = false;
        setWidget("ref_video_spec", formatRefSpec(state.videoRefs.map((r) => r.strength)));
    }
    function addFileVideo(name) {
        const socketCount = connectedSlots(node, "ref_video_").length;
        const lines = fileLinesOf("ref_video_files");
        if (socketCount + lines.length >= 3) { toast("H3 supports at most 3 reference videos", true); return; }
        lines.push(name);
        state.videoCrops.push(null);
        pushCrops();
        if (!state.videoRefsAuto && state.videoRefs.length) {
            state.videoRefs.push({ strength: 1.0, src: { type: "file", idx: lines.length - 1, name } });
            pushVideoRefs();
        }
        setWidget("ref_video_files", lines.join("\n"));
        refresh(true);
    }
    function removeFileVideo(i) {
        const v = state.videoRefs[i];
        if (v?.src.type !== "file") return;
        const lines = fileLinesOf("ref_video_files");
        lines.splice(v.src.idx, 1);
        state.videoRefs.splice(i, 1);
        state.videoCrops.splice(i, 1);
        pushCrops();
        if (!state.videoRefsAuto && state.videoRefs.length) pushVideoRefs();
        else if (!state.videoRefs.length) setWidget("ref_video_spec", "");
        setWidget("ref_video_files", lines.join("\n"));
        refresh(true);
    }

    function reconcileCounts() {
        // rebuild each list by matching entities to their surviving SOURCE, so a
        // vanished socket removes ITS entry — popping the tail misattributed the
        // socket's spec line to a file entity (bug-hunt finding)
        const rebuild = (list, prefix, filesWidget, filler) => {
            const slots = connectedSlots(node, prefix).map((s) => s.slot);
            const files = fileLinesOf(filesWidget);
            const next = [];
            for (const s of slots) {
                const e = list.find((x) => x.src?.type === "socket" && x.src.slot === s);
                next.push(e || filler("socket", s, null));
            }
            files.forEach((name, idx) => {
                const e = list.find((x) => x.src?.type === "file" && x.src.name === name
                    && !next.includes(x));
                const ent = e || filler("file", idx, name);
                ent.src = { type: "file", idx, name };
                next.push(ent);
            });
            const changed = next.length !== list.length || next.some((e, i) => e !== list[i]);
            return { next, changed };
        };
        if (!state.midsAuto) {
            const before = state.mids;
            const { next, changed } = rebuild(state.mids, "middle_frame_", "middle_frame_files",
                (type, slot, name) => ({ frac: 0.5, strength: AUTO_MIDDLE_STRENGTH, desc: "",
                    src: type === "socket" ? { type, slot } : { type, idx: slot, name } }));
            if (changed) {
                // framings follow their image through the rebuild
                state.midCrops = next.map((e) => {
                    const oi = before.indexOf(e);
                    return oi > -1 ? (state.midCrops[oi] ?? null) : null;
                });
                state.mids = next;
                next.length ? pushMids() : setWidget("middle_frame_spec", "");
                pushCrops();
            }
        }
        if (!state.videoRefsAuto) {
            const before = state.videoRefs;
            const { next, changed } = rebuild(state.videoRefs, "ref_video_", "ref_video_files",
                (type, slot, name) => ({ strength: 1.0,
                    src: type === "socket" ? { type, slot } : { type, idx: slot, name } }));
            if (changed) {
                state.videoCrops = next.map((e) => {
                    const oi = before.indexOf(e);
                    return oi > -1 ? (state.videoCrops[oi] ?? null) : null;
                });
                state.videoRefs = next;
                next.length ? pushVideoRefs() : setWidget("ref_video_spec", "");
                pushCrops();
            }
        }
        if (!state.refsAuto) {
            const before = state.refs;
            const { next, changed } = rebuild(state.refs, "ref_image_", "ref_image_files",
                (type, slot, name) => ({ strength: 1.0,
                    src: type === "socket" ? { type, slot } : { type, idx: slot, name } }));
            if (changed) {
                state.refCrops = next.map((e) => {
                    const oi = before.indexOf(e);
                    return oi > -1 ? (state.refCrops[oi] ?? null) : null;
                });
                state.refs = next;
                next.length ? pushRefs() : setWidget("ref_spec", "");
                pushCrops();
            }
        }
    }

    // ---- entity helpers ---------------------------------------------------
    const hasFirst = () => inputConnected(node, "first_frame") || !!widgetValue(node, "first_frame_file", "").trim();
    const hasLast = () => inputConnected(node, "last_frame") || !!widgetValue(node, "last_frame_file", "").trim();

    function capInfo(which) {
        const connected = inputConnected(node, which + "_frame");
        const file = widgetValue(node, which + "_frame_file", "").trim();
        if (!connected && !file) return null;
        return {
            connected, file: connected ? null : file,
            img: connected ? upstreamImage(node, which + "_frame") : cachedImg(file),
            strength: Number(widgetValue(node, which + "_frame_strength", 1.0)),
        };
    }

    function midImg(m) {
        return m.src.type === "socket"
            ? upstreamImage(node, "middle_frame_" + m.src.slot) : cachedImg(m.src.name);
    }
    function refImg(r) {
        return r.src.type === "socket"
            ? upstreamImage(node, "ref_image_" + r.src.slot) : cachedImg(r.src.name);
    }
    const midName = (m) => m.src.type === "socket" ? `socket m${m.src.slot}` : m.src.name;
    const refName = (r) => r.src.type === "socket" ? `socket ref${r.src.slot}` : r.src.name;

    // one place owns the numbering rule: keyframes (time order) then refs get
    // <Picture N>; audio (sockets then files) gets <Audio N>
    function numberedEntities() {
        const ents = entityList();          // assigns e.pic and ref._pic
        // soundtracks take <Audio N> numbers BEFORE standalone audio (core's
        // presentation order: each soundtrack label precedes its <Video k>)
        const videos = [];
        let aN = 1;
        state.videoRefs.forEach((v, i) => {
            const snd = v.src.type === "socket"
                ? inputConnected(node, "ref_video_audio_" + v.src.slot)
                : "maybe";   // file videos: embedded audio unknown until the server decodes
            videos.push({ num: i + 1, v, snd, aNum: snd ? aN++ : null });
        });
        const uncertain = videos.some((x) => x.snd === "maybe");
        const audio = [];
        for (const a of connectedSlots(node, "ref_audio_"))
            audio.push({ num: aN++, label: `socket audio${a.slot}`, uncertain });
        for (const f of fileLinesOf("ref_audio_files"))
            audio.push({ num: aN++, label: f, uncertain });
        return { ents, refs: state.refs, videos, audio, uncertain };
    }

    // filmstrip entities in time order; picture numbers follow this order + refs
    function entityList() {
        const out = [];
        const first = capInfo("first");
        if (first) out.push({ kind: "first", ...first, frac: 0 });
        const midsSorted = state.mids.map((m, i) => ({ m, i })).sort((a, b) => a.m.frac - b.m.frac);
        for (const { m, i } of midsSorted)
            out.push({ kind: "mid", i, frac: m.frac, strength: m.strength,
                img: midImg(m), file: m.src.type === "file" ? m.src.name : null,
                connected: m.src.type === "socket", desc: m.desc });
        const last = capInfo("last");
        if (last) out.push({ kind: "last", ...last, frac: 1 });
        let p = 1;
        for (const e of out) e.pic = p++;
        state.refs.forEach((r, i) => { r._pic = p++; });
        return out;
    }

    const timeOf = (frac) => (frac * (fc() - 1) / FPS);
    const frameOf = (frac) => roundHalfEven(frac * (fc() - 1));

    // ---- add/remove -------------------------------------------------------
    function addFileMid(name) {
        const capacity = Math.max(1, fc() - 2);   // distinct interior frames
        const current = connectedSlots(node, "middle_frame_").length
            + fileLinesOf("middle_frame_files").length;
        if (current >= capacity) {
            toast(`clip too short for another waypoint — only ${capacity} interior frame(s) at this length`, true);
            return false;
        }
        const lines = fileLinesOf("middle_frame_files");
        lines.push(name);
        state.midCrops.push(null);
        pushCrops();
        if (!state.midsAuto && state.mids.length) {
            state.mids.push({ frac: 0.5, strength: AUTO_MIDDLE_STRENGTH, desc: "",
                src: { type: "file", idx: lines.length - 1, name } });
            pushMids();
        }
        setWidget("middle_frame_files", lines.join("\n"));
        refresh(true);
        return true;
    }
    function removeFileMid(i) {
        const m = state.mids[i];
        if (m?.src.type !== "file") return;
        const lines = fileLinesOf("middle_frame_files");
        lines.splice(m.src.idx, 1);
        state.mids.splice(i, 1);
        state.midCrops.splice(i, 1);
        pushCrops();
        if (!state.midsAuto && state.mids.length) pushMids();
        else if (!state.mids.length) setWidget("middle_frame_spec", "");
        setWidget("middle_frame_files", lines.join("\n"));
        state.sel = null;
        refresh(true);
    }
    function addFileRef(name) {
        const lines = fileLinesOf("ref_image_files");
        lines.push(name);
        state.refCrops.push(null);
        pushCrops();
        if (!state.refsAuto && state.refs.length) {
            state.refs.push({ strength: 1.0, src: { type: "file", idx: lines.length - 1, name } });
            pushRefs();
        }
        setWidget("ref_image_files", lines.join("\n"));
        refresh(true);
    }
    function removeFileRef(i) {
        const r = state.refs[i];
        if (r?.src.type !== "file") return;
        const lines = fileLinesOf("ref_image_files");
        lines.splice(r.src.idx, 1);
        state.refs.splice(i, 1);
        state.refCrops.splice(i, 1);
        pushCrops();
        if (!state.refsAuto && state.refs.length) pushRefs();
        else if (!state.refs.length) setWidget("ref_spec", "");
        setWidget("ref_image_files", lines.join("\n"));
        state.sel = null;
        refresh(true);
    }
    function removeBeat(i) {
        state.beats.splice(i, 1);
        if (state.sel?.kind === "beat") state.sel = null;
        pushBeats();
        refresh(true);
    }
    function deleteSelected() {
        const s = state.sel;
        if (!s) return;
        if (s.kind === "beat") removeBeat(s.i);
        else if (s.kind === "mid" && state.mids[s.i]?.src.type === "file") removeFileMid(s.i);
        else if (s.kind === "ref" && state.refs[s.i]?.src.type === "file") removeFileRef(s.i);
        else if ((s.kind === "first" || s.kind === "last")
            && !inputConnected(node, s.kind + "_frame")
            && widgetValue(node, s.kind + "_frame_file", "").trim()) {
            setWidget(s.kind + "_frame_file", "");
            setWidget(s.kind + "_frame_crop", "");
            state.sel = null;
            refresh(true);
        }
    }

    // ---- DOM helpers ------------------------------------------------------
    function el(tag, style, text) {
        const e = document.createElement(tag);
        if (style) Object.assign(e.style, style);
        if (text != null) e.textContent = text;
        return e;
    }
    const btnStyle = {
        background: "#2a2a2a", color: COL.bright, border: `1px solid ${COL.border}`,
        borderRadius: "3px", cursor: "pointer", padding: "3px 12px", fontSize: "12px",
    };

    function thumbEl(img, w, h, label, crop, aspectWH) {
        if (img?.src && crop) {
            // show what the model will actually see: the framed window, not the file
            const cnv = el("canvas", {
                width: w + "px", height: h + "px", borderRadius: "4px",
                display: "block", background: "#222",
            });
            cnv.width = w * 2; cnv.height = h * 2;   // 2x for sharpness
            const [aw, ah] = aspectWH || [img.naturalWidth, img.naturalHeight];
            const box = cropBoxJS(img.naturalWidth, img.naturalHeight, aw, ah,
                crop.z, crop.cx, crop.cy);
            const ctx = cnv.getContext("2d");
            const s = Math.max(cnv.width / box.w, cnv.height / box.h);
            try {
                ctx.drawImage(img, box.x + (box.w - cnv.width / s) / 2,
                    box.y + (box.h - cnv.height / s) / 2,
                    cnv.width / s, cnv.height / s, 0, 0, cnv.width, cnv.height);
            } catch (e) { /* decode raced us */ }
            return cnv;
        }
        if (img?.src) {
            const im = el("img");
            im.src = img.src;
            Object.assign(im.style, {
                width: w + "px", height: h + "px", objectFit: "cover",
                borderRadius: "4px", display: "block", background: "#222",
            });
            return im;
        }
        const ph = el("div", {
            width: w + "px", height: h + "px", borderRadius: "4px", background: "#222",
            display: "flex", alignItems: "center", justifyContent: "center",
            color: "#666", fontSize: "11px", fontFamily: "monospace",
        }, label || "no preview");
        if ((label || "").includes("loading")) {
            // big files decode for a while — a static label reads as "stuck"
            if (!document.getElementById("h3-timeline-css")) {
                const st = document.createElement("style");
                st.id = "h3-timeline-css";
                st.textContent = "@keyframes h3pulse{0%{opacity:.35}50%{opacity:1}100%{opacity:.35}}";
                document.head.appendChild(st);
            }
            ph.style.animation = "h3pulse 1.2s ease-in-out infinite";
            ph.textContent = "⏳ " + label;
        }
        return ph;
    }

    function picChip(n, color) {
        return el("span", {
            color, fontFamily: "monospace", fontSize: "11px",
            border: `1px solid ${color}`, borderRadius: "3px", padding: "0 4px",
        }, `Pic ${n}`);
    }

    // ---- modal scaffold (picker + lightbox) -------------------------------
    function closeModal() {
        const m = state.modal;
        if (!m) return;
        state.modal = null;
        window.removeEventListener("keydown", m.onKey, true);
        try { m.onClose?.(); } catch (e) { /* cleanup only */ }
        m.root.remove();
    }
    function openModal(build, onClose) {
        closeModal();
        const root = el("div", {
            position: "fixed", inset: "0", zIndex: "10020",
            background: "rgba(0,0,0,0.6)", display: "flex",
            alignItems: "center", justifyContent: "center",
        });
        root.addEventListener("pointerdown", (ev) => { if (ev.target === root) closeModal(); });
        const onKey = (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); closeModal(); } };
        window.addEventListener("keydown", onKey, true);
        state.modal = { root, onKey, onClose };
        build(root);
        document.body.appendChild(root);
    }

    function openLightbox(img) {
        if (!img?.src) return;
        openModal((root) => {
            const big = el("img");
            big.src = img.src;
            Object.assign(big.style, {
                maxWidth: "94vw", maxHeight: "94vh", borderRadius: "6px",
                boxShadow: "0 12px 48px rgba(0,0,0,0.7)", cursor: "zoom-out",
            });
            big.addEventListener("click", closeModal);
            root.appendChild(big);
        });
    }

    // ---- audio: file entries, picker, mic recorder ------------------------
    function addFileAudio(name) {
        const socketCount = connectedSlots(node, "ref_audio_").length;
        const lines = fileLinesOf("ref_audio_files");
        if (socketCount + lines.length >= 3) {
            toast("H3 supports at most 3 reference audios", true);
            return;
        }
        lines.push(name);
        setWidget("ref_audio_files", lines.join("\n"));
        refresh(true);
    }
    function removeFileAudio(idx) {
        const lines = fileLinesOf("ref_audio_files");
        lines.splice(idx, 1);
        setWidget("ref_audio_files", lines.join("\n"));
        refresh(true);
    }

    async function uploadBlob(blob, filename, overwrite) {
        const fd = new FormData();
        fd.append("image", new File([blob], filename, { type: blob.type }));
        if (overwrite) fd.append("overwrite", "true");
        const resp = await api.fetchApi("/upload/image", { method: "POST", body: fd });
        if (!resp.ok) throw new Error(`upload rejected (${resp.status})`);
        const j = await resp.json();
        if (!j?.name) throw new Error("upload returned no filename");
        return j.subfolder ? `${j.subfolder}/${j.name}` : j.name;
    }

    function encodeWav(audioBuffer) {
        // 16-bit PCM WAV — nothing downstream has to guess at a codec
        const ch = Math.min(2, audioBuffer.numberOfChannels);
        const len = audioBuffer.length, sr = audioBuffer.sampleRate;
        const data = new DataView(new ArrayBuffer(44 + len * ch * 2));
        const wstr = (o, s) => { for (let i = 0; i < s.length; i++) data.setUint8(o + i, s.charCodeAt(i)); };
        wstr(0, "RIFF"); data.setUint32(4, 36 + len * ch * 2, true); wstr(8, "WAVE");
        wstr(12, "fmt "); data.setUint32(16, 16, true); data.setUint16(20, 1, true);
        data.setUint16(22, ch, true); data.setUint32(24, sr, true);
        data.setUint32(28, sr * ch * 2, true); data.setUint16(32, ch * 2, true);
        data.setUint16(34, 16, true); wstr(36, "data"); data.setUint32(40, len * ch * 2, true);
        let o = 44;
        const chans = Array.from({ length: ch }, (_, c) => audioBuffer.getChannelData(c));
        for (let i = 0; i < len; i++)
            for (let c = 0; c < ch; c++) {
                const v = Math.max(-1, Math.min(1, chans[c][i]));
                data.setInt16(o, v < 0 ? v * 0x8000 : v * 0x7fff, true);
                o += 2;
            }
        return new Blob([data.buffer], { type: "audio/wav" });
    }

    async function recordMic(button) {
        if (state.recorder) {                       // second press = stop
            state.recorder.stop();
            return;
        }
        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        } catch (e) {
            if (e.name === "NotAllowedError") {
                // a DISMISSED prompt re-asks on the next click; a hard DENIAL can't
                // be re-triggered from a page — the user must flip the site setting
                let hard = true;
                try {
                    const st = await navigator.permissions.query({ name: "microphone" });
                    hard = st.state === "denied";
                } catch (q) { /* permissions API unavailable */ }
                toast(hard
                    ? "microphone blocked for this site — click the 🔒/camera icon in the address bar, allow the microphone, then press record again"
                    : "microphone prompt dismissed — press record again to re-ask", true);
            } else {
                toast("microphone unavailable — " + e.message, true);
            }
            return;
        }
        state.recorderStream = stream;
        const rec = new MediaRecorder(stream);
        const chunks = [];
        rec.ondataavailable = (ev) => chunks.push(ev.data);
        rec.onstop = async () => {
            state.recorder = null;
            state.recorderStream = null;
            stream.getTracks().forEach((t) => t.stop());
            button.textContent = "● record mic";
            button.style.color = COL.bright;
            try {
                const raw = new Blob(chunks, { type: rec.mimeType });
                const buf = await new AudioContext().decodeAudioData(await raw.arrayBuffer());
                const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
                const name = await uploadBlob(encodeWav(buf), `h3-mic-${stamp}.wav`);
                addFileAudio(name);
                toast(`recorded ${buf.duration.toFixed(1)}s → ${name}`);
            } catch (e) {
                toast("recording failed: " + e.message, true);
            }
        };
        rec.start();
        state.recorder = rec;
        button.textContent = "⏹ stop recording";
        button.style.color = COL.red;
    }

    async function openAudioPicker(onPick) {
        let files = [];
        try {
            const resp = await api.fetchApi("/object_info/LoadAudio");
            const j = await resp.json();
            files = j?.LoadAudio?.input?.required?.audio?.[0] || [];
        } catch (e) { /* upload still works */ }
        openModal((root) => {
            const panel = el("div", {
                width: "min(560px, 92vw)", maxHeight: "80vh", background: COL.bg,
                border: `1px solid ${COL.border}`, borderRadius: "8px", display: "flex",
                flexDirection: "column", overflow: "hidden", fontFamily: "sans-serif",
            });
            const head = el("div", {
                display: "flex", gap: "8px", alignItems: "center",
                padding: "8px 10px", borderBottom: `1px solid ${COL.divider}`,
            });
            head.appendChild(el("span", { color: COL.bright, fontSize: "13px", flex: "1" },
                "Pick reference audio"));
            const webBtn = el("button", btnStyle, "🌐 web…");
            webBtn.title = "search free Creative Commons sounds (Openverse: Freesound/Jamendo, all licenses — each result shows its own) and pull them into the input folder";
            const up = el("button", btnStyle, "upload…");
            const fi = el("input");
            fi.type = "file";
            fi.accept = "audio/*";
            fi.style.display = "none";
            up.addEventListener("click", () => fi.click());
            fi.addEventListener("change", async () => {
                const f = fi.files?.[0];
                if (!f) return;
                try {
                    const name = await uploadBlob(f, f.name);
                    closeModal(); onPick(name);
                } catch (e) { toast("upload failed: " + e.message, true); }
            });
            const closeB = el("button", btnStyle, "✕");
            closeB.addEventListener("click", closeModal);
            head.append(webBtn, up, fi, closeB);
            const list = el("div", { overflowY: "auto", padding: "6px" });
            if (!files.length) list.appendChild(el("div", { color: COL.text, fontSize: "12px", padding: "8px" },
                "no audio in the input folder — use upload… or the mic recorder"));
            for (const f of files.slice(0, 300)) {
                const row = el("div", {
                    display: "flex", alignItems: "center", gap: "10px",
                    padding: "5px 8px", borderRadius: "4px", cursor: "pointer",
                });
                row.addEventListener("mouseenter", () => row.style.background = "#232323");
                row.addEventListener("mouseleave", () => row.style.background = "transparent");
                row.appendChild(el("span", { color: COL.green }, "♪"));
                row.appendChild(el("span", { color: COL.bright, fontSize: "12px", flex: "1" }, f));
                const play = el("audio");
                play.controls = true;
                play.preload = "none";
                play.src = inputFileUrl(f);
                Object.assign(play.style, { height: "26px", width: "180px" });
                play.addEventListener("click", (ev) => ev.stopPropagation());
                row.appendChild(play);
                row.addEventListener("click", () => { closeModal(); onPick(f); });
                list.appendChild(row);
            }
            webBtn.addEventListener("click", () => {
                list.textContent = "";
                const bar = el("div", { display: "flex", gap: "8px", padding: "6px 8px" });
                const out = el("div", { overflowY: "auto" });
                const q = el("input");
                q.placeholder = "search free sounds… (Enter)";
                Object.assign(q.style, {
                    flex: "1", background: COL.input, color: COL.bright,
                    border: `1px solid ${COL.border}`, borderRadius: "3px",
                    padding: "4px 8px", fontSize: "12px",
                });
                q.addEventListener("keydown", async (ev) => {
                    ev.stopPropagation();
                    if (ev.key !== "Enter" || !q.value.trim()) return;
                    out.textContent = "";
                    out.appendChild(el("div", { color: COL.text, fontSize: "12px", padding: "6px 8px" }, "searching…"));
                    try {
                        const results = await webSearch(q.value.trim(), "audio", 1);
                        out.textContent = "";
                        if (!results.length) {
                            out.appendChild(el("div", { color: COL.text, fontSize: "12px", padding: "6px 8px" }, "no results"));
                            return;
                        }
                        for (const res of results) {
                            const row = el("div", {
                                display: "flex", alignItems: "center", gap: "10px",
                                padding: "5px 8px", borderRadius: "4px",
                            });
                            row.append(el("span", { color: COL.green }, "♪"),
                                el("span", { color: COL.bright, fontSize: "12px", flex: "1",
                                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
                                    (res.title || "untitled")
                                    + (res.duration ? ` (${Math.round(res.duration)}s)` : "")),
                                el("span", { color: "#666", fontSize: "10px", whiteSpace: "nowrap" },
                                    res.license + (res.creator ? " · " + res.creator.slice(0, 16) : "")));
                            const play = el("audio");
                            play.controls = true;
                            play.preload = "none";
                            play.src = res.url;
                            Object.assign(play.style, { height: "26px", width: "170px" });
                            const add = el("button", { ...btnStyle, color: COL.green, padding: "2px 10px" }, "add");
                            add.addEventListener("click", async () => {
                                add.disabled = true;
                                add.textContent = "…";
                                try {
                                    const name = await webFetch(res, "audio");
                                    closeModal();
                                    onPick(name);
                                } catch (e) {
                                    add.disabled = false;
                                    add.textContent = "add";
                                    toast("download failed: " + e.message, true);
                                }
                            });
                            row.append(play, add);
                            out.appendChild(row);
                        }
                    } catch (e) {
                        out.textContent = "";
                        out.appendChild(el("div", { color: COL.red, fontSize: "12px", padding: "6px 8px" },
                            "search failed: " + e.message));
                    }
                });
                bar.appendChild(q);
                list.append(bar, out);
                q.focus();
            });
            panel.append(head, list);
            root.appendChild(panel);
        });
    }

    // ---- context menu -----------------------------------------------------
    function closeCtxMenu() {
        state.ctxMenu?.remove();
        state.ctxMenu = null;
        window.removeEventListener("pointerdown", onCtxAway, true);
    }
    function onCtxAway(ev) {
        if (state.ctxMenu && !state.ctxMenu.contains(ev.target)) closeCtxMenu();
    }
    function openCtxMenu(x, y, items) {
        closeCtxMenu();
        const menu = el("div", {
            position: "fixed", left: x + "px", top: y + "px", zIndex: "10030",
            background: "#1f1f1f", border: `1px solid ${COL.border}`, borderRadius: "6px",
            boxShadow: "0 8px 28px rgba(0,0,0,0.6)", padding: "4px", minWidth: "230px",
            fontFamily: "sans-serif",
        });
        for (const it of items) {
            if (it === "-") {
                menu.appendChild(el("div", { borderTop: `1px solid ${COL.divider}`, margin: "4px 6px" }));
                continue;
            }
            const row = el("div", {
                padding: "6px 10px", borderRadius: "4px", fontSize: "12px",
                color: it.disabled ? "#555" : (it.danger ? COL.red : COL.bright),
                cursor: it.disabled ? "default" : "pointer",
                display: "flex", justifyContent: "space-between", gap: "12px",
            });
            row.appendChild(el("span", null, it.label));
            if (it.hint) row.appendChild(el("span", { color: "#666", fontSize: "11px" }, it.hint));
            if (!it.disabled) {
                row.addEventListener("mouseenter", () => row.style.background = "#2a2a2a");
                row.addEventListener("mouseleave", () => row.style.background = "transparent");
                row.addEventListener("click", () => { closeCtxMenu(); it.action(); });
            }
            menu.appendChild(row);
        }
        document.body.appendChild(menu);
        // keep it on-screen
        const r = menu.getBoundingClientRect();
        if (r.right > window.innerWidth - 8) menu.style.left = (window.innerWidth - r.width - 8) + "px";
        if (r.bottom > window.innerHeight - 8) menu.style.top = (window.innerHeight - r.height - 8) + "px";
        state.ctxMenu = menu;
        window.addEventListener("pointerdown", onCtxAway, true);
    }

    // ---- asset replacement: new image, same placement/strength/desc/framing --
    function replaceImage(sel) {
        openPicker((name) => {
            if (sel.kind === "first" || sel.kind === "last") {
                setWidget(sel.kind + "_frame_file", name);
            } else if (sel.kind === "mid") {
                const m = state.mids[sel.i];
                if (m?.src.type !== "file") return;
                const lines = fileLinesOf("middle_frame_files");
                lines[m.src.idx] = name;
                setWidget("middle_frame_files", lines.join("\n"));
            } else if (sel.kind === "ref") {
                const r = state.refs[sel.i];
                if (r?.src.type !== "file") return;
                const lines = fileLinesOf("ref_image_files");
                lines[r.src.idx] = name;
                setWidget("ref_image_files", lines.join("\n"));
            }
            refresh(true);
            // the old framing still applies (coordinates are normalized), but a new
            // image usually wants its own window — reopen the framer to confirm it
            if (cropOf(sel)) {
                toast("image replaced — settings kept. Check the framing on the new image.");
                const wait = () => {
                    const img = sel.kind === "mid" ? midImg(state.mids[sel.i])
                        : sel.kind === "ref" ? refImg(state.refs[sel.i])
                        : capInfo(sel.kind)?.img;
                    if (img) openFramer(sel);
                    else setTimeout(wait, 250);   // new file still decoding
                };
                setTimeout(wait, 100);
            } else {
                toast("image replaced — placement, strength and settings kept");
            }
        });
    }

    // ---- final-frame extraction: chain from any footage in one click --------
    // Decodes the video's last frame in the browser, uploads it as a PNG and
    // hands back the input-folder name. File videos only (sockets have no URL).
    function extractLastFrame(name, onDone) {
        toast("extracting final frame…");
        const vv = document.createElement("video");
        vv.muted = true;
        vv.preload = "auto";
        vv.src = inputFileUrl(name);
        const stop = () => { vv.removeAttribute("src"); vv.load(); };
        const fail = (why) => {
            stop();
            toast("could not extract the final frame — " + why
                + ". Graph fallback: Load Video → Get Video Components → Image From Batch.", true);
        };
        vv.addEventListener("error", () => fail("the browser can't decode this file"));
        vv.addEventListener("loadedmetadata", () => {
            if (!isFinite(vv.duration) || vv.duration <= 0) return fail("no duration reported");
            // a hair before the end: seeking to exactly duration can land past
            // the last decodable frame in some containers
            vv.currentTime = Math.max(0, vv.duration - 0.001);
        }, { once: true });
        vv.addEventListener("seeked", async () => {
            try {
                if (!vv.videoWidth) return fail("no video track");
                const cnv = document.createElement("canvas");
                cnv.width = vv.videoWidth;
                cnv.height = vv.videoHeight;
                cnv.getContext("2d").drawImage(vv, 0, 0);
                const blob = await new Promise((res, rej) =>
                    cnv.toBlob((b) => b ? res(b) : rej(new Error("frame not decodable")), "image/png"));
                const base = name.replace(/\s*\[\w+\]\s*$/, "").split("/").pop().replace(/\.[^.]+$/, "");
                const fname = await uploadBlob(blob, `h3-final-${base}.png`, true);
                // deterministic name + overwrite: a cached thumb would show the OLD frame
                state.imgCache.delete(previewFileUrl(fname));
                state.imgCache.delete(inputFileUrl(fname));
                stop();
                onDone(fname);
            } catch (e) {
                fail(e.message);
            }
        }, { once: true });
    }

    function finalFrameToFirst(videoName) {
        if (inputConnected(node, "first_frame")) {
            toast("the first_frame SOCKET is connected and wins over file inputs — disconnect it in the graph, then press ⏭ again", true);
            return;
        }
        extractLastFrame(videoName, (fname) => {
            setWidget("first_frame_file", fname);
            setWidget("first_frame_crop", "");   // old image's framing doesn't apply
            refresh(true);
            const [oW, oH] = outWH();
            const meta = state.videoMeta.get(videoName);
            const mismatch = meta && Math.abs(meta.w / meta.h - oW / oH) > 0.01;
            toast(`final frame set as first frame (${fname})`
                + (mismatch ? " — aspect differs from the output, ⛶ it on the first-frame card" : ""),
                !!mismatch);
        });
    }

    // beat -> waypoint: the beat's moment and words become a keyframe's time and
    // description; waypoint -> beat: drop the image, keep the intent as text
    function beatToWaypoint(i) {
        const b = state.beats[i];
        if (!b) return;
        openPicker((name) => {
            const frac = b.frac, text = b.text.trim();
            if (!addFileMid(name)) return;          // appends a mid + refresh
            const m = state.mids[state.mids.length - 1];
            if (m) {
                m.frac = frac;
                m.desc = text;
                pushMids();
            }
            // refresh(true) rebuilt state.beats with NEW objects — indexOf(b)
            // would miss and leave a duplicate beat behind (bug-hunt finding)
            const bi = state.beats.findIndex((x) => Math.abs(x.frac - frac) < 1e-6 && x.text.trim() === text);
            if (bi > -1) state.beats.splice(bi, 1);
            pushBeats();
            refresh(true);
            const mi = state.mids.findIndex((x) => Math.abs(x.frac - frac) < 1e-6 && x.desc === text);
            state.sel = { kind: "mid", i: mi > -1 ? mi : state.mids.length - 1 };
            state.fs?.fill?.();
            toast("beat upgraded to a waypoint — same moment, text kept as its description");
        });
    }

    function waypointToBeat(i) {
        const m = state.mids[i];
        if (m?.src.type !== "file") return;
        const frac = m.frac, text = m.desc.trim();
        removeFileMid(i);                           // clears selection + refresh
        state.beats.push({ frac, text });
        state.beats.sort((a, b) => a.frac - b.frac);
        pushBeats();
        refresh(true);
        // re-resolve AFTER the rebuild — draft ordering could shift indices
        state.sel = { kind: "beat",
            i: state.beats.findIndex((b) => Math.abs(b.frac - frac) < 1e-6 && b.text.trim() === text.trim()) };
        state.fs?.fill?.();
        toast(text ? "image stripped — kept as a timed beat"
            : "image stripped — give the beat some text or it won't be saved", !text);
    }

    function imageMenu(sel, ev) {
        const isCap = sel.kind === "first" || sel.kind === "last";
        const entity = sel.kind === "mid" ? state.mids[sel.i]
            : sel.kind === "ref" ? state.refs[sel.i] : null;
        const isFile = isCap
            ? (!inputConnected(node, sel.kind + "_frame") && !!widgetValue(node, sel.kind + "_frame_file", "").trim())
            : entity?.src.type === "file";
        const img = sel.kind === "mid" ? midImg(entity)
            : sel.kind === "ref" ? refImg(entity) : capInfo(sel.kind)?.img;
        const crop = cropOf(sel);
        const items = [
            { label: "🔍 view full size", action: () => openLightbox(img), disabled: !img },
            { label: crop ? "⛶ adjust framing…" : "⛶ frame image…",
              hint: crop ? crop.z.toFixed(2) + "×" : "", action: () => openFramer(sel), disabled: !img },
        ];
        if (crop) items.push({ label: "⛶ clear framing", action: () => { setCropOf(sel, null); refresh(true); } });
        if (sel.kind === "mid" && isFile)
            items.push({ label: "◦ strip image, keep as beat", hint: "text survives",
                action: () => waypointToBeat(sel.i) });
        items.push("-");
        items.push({
            label: "⇄ replace image…",
            hint: "keeps placement + settings",
            disabled: !isFile,
            ...(isFile ? {} : { hint: "fed by graph socket" }),
            action: () => replaceImage(sel),
        });
        items.push("-");
        items.push({
            label: "✕ remove",
            danger: true,
            disabled: !isFile,
            ...(isFile ? {} : { hint: "disconnect in the graph" }),
            action: () => {
                if (isCap) {
                    setWidget(sel.kind + "_frame_file", "");
                    setWidget(sel.kind + "_frame_crop", "");
                    state.sel = null; refresh(true);
                } else if (sel.kind === "mid") removeFileMid(sel.i);
                else removeFileRef(sel.i);
            },
        });
        openCtxMenu(ev.clientX, ev.clientY, items);
    }

    // ---- setup save/load --------------------------------------------------
    function exportSetup() {
        const fields = {};
        for (const f of SETUP_FIELDS) {
            const w = getWidget(node, f);
            if (w) fields[f] = w.value;
        }
        const setup = {
            format: SETUP_FORMAT, version: SETUP_VERSION,
            // socket inputs can't travel in a file — record them so the loader can
            // say what a resumed setup is still missing
            sockets: {
                first: inputConnected(node, "first_frame"),
                last: inputConnected(node, "last_frame"),
                mids: connectedSlots(node, "middle_frame_").map((s) => s.slot),
                refs: connectedSlots(node, "ref_image_").map((s) => s.slot),
                audios: connectedSlots(node, "ref_audio_").map((s) => s.slot),
            },
            fields,
        };
        const blob = new Blob([JSON.stringify(setup, null, 2)], { type: "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        const stamp = new Date().toISOString().slice(0, 16).replace(/[T:]/g, "-");
        a.download = `h3-setup-${stamp}.json`;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
        toast("setup saved to your downloads");
    }

    function importSetup(json) {
        let setup;
        try { setup = JSON.parse(json); } catch (e) { toast("not valid JSON", true); return; }
        if (setup?.format !== SETUP_FORMAT || !setup.fields) {
            toast("not an H3 setup file", true); return;
        }
        for (const f of SETUP_FIELDS) {
            if (f in setup.fields) setWidget(f, setup.fields[f]);
            else if (f in SETUP_DEFAULTS) setWidget(f, SETUP_DEFAULTS[f]);
        }
        refresh(true);
        // honest resume report: what the file expected that this graph doesn't have
        const s = setup.sockets || {};
        const missing = [];
        if (s.first && !inputConnected(node, "first_frame")) missing.push("first_frame socket");
        if (s.last && !inputConnected(node, "last_frame")) missing.push("last_frame socket");
        const midNow = connectedSlots(node, "middle_frame_").map((x) => x.slot);
        for (const sl of s.mids || []) if (!midNow.includes(sl)) missing.push(`middle_frame_${sl}`);
        const refNow = connectedSlots(node, "ref_image_").map((x) => x.slot);
        for (const sl of s.refs || []) if (!refNow.includes(sl)) missing.push(`ref_image_${sl}`);
        const audNow = connectedSlots(node, "ref_audio_").map((x) => x.slot);
        for (const sl of s.audios || []) if (!audNow.includes(sl)) missing.push(`ref_audio_${sl}`);
        toast(missing.length
            ? "setup loaded — but it used sockets not connected here: " + missing.join(", ")
            : "setup loaded", missing.length > 0);
    }

    async function webSearch(q, kind, page) {
        const r = await api.fetchApi("/h3guide/websearch?q=" + encodeURIComponent(q)
            + "&kind=" + kind + "&page=" + (page || 1));
        const j = await r.json();
        if (j.error) throw new Error(j.error);
        return j.results || [];
    }
    async function webFetch(res, kind) {
        const ps = new URLSearchParams({ url: res.url, kind, title: res.title,
            creator: res.creator, license: res.license, source: res.source, id: res.id });
        const r = await api.fetchApi("/h3guide/webfetch?" + ps.toString());
        const j = await r.json();
        if (j.error) throw new Error(j.error);
        toast(`pulled "${(res.title || j.name).slice(0, 40)}" — ${res.license}`
            + (res.creator ? ` by ${res.creator}` : "")
            + " · credit logged to input/web/credits.txt");
        return j.name;
    }

    function toast(msg, warn) {
        state.toastMsg = { msg, warn: !!warn };
        clearTimeout(state.toastTimer);
        state.toastTimer = setTimeout(() => { state.toastMsg = null; state.fs?.fill(); }, 6000);
        state.fs?.fill();
    }

    // ---- cast library: named person/asset bundles persisted server-side ------
    function openCastModal() {
        openModal(async (root) => {
            const panel = el("div", {
                width: "min(1040px, 94vw)", maxHeight: "88vh", background: COL.bg,
                border: `1px solid ${COL.border}`, borderRadius: "8px", display: "flex",
                flexDirection: "column", overflow: "hidden", fontFamily: "sans-serif",
            });
            const head = el("div", {
                display: "flex", gap: "8px", alignItems: "center",
                padding: "10px 14px", borderBottom: `1px solid ${COL.divider}`,
            });
            head.appendChild(el("span", { color: COL.bright, fontSize: "15px", flex: "1" },
                "\u{1F3AD} Cast — save people/assets once, reuse them in any clip or workflow"));
            const closeB = el("button", btnStyle, "✕");
            closeB.addEventListener("click", closeModal);
            head.appendChild(closeB);
            const bodyEl = el("div", { overflowY: "auto", padding: "12px", display: "flex",
                flexDirection: "column", gap: "14px" });
            panel.append(head, bodyEl);
            root.appendChild(panel);

            // --- SAVE pane: current FILE-sourced refs + audio become a cast member
            const saveBox = el("div", {
                border: `1px solid ${COL.divider}`, borderRadius: "6px", padding: "10px",
            });
            saveBox.appendChild(el("div", { color: COL.bright, fontSize: "12px", marginBottom: "6px" },
                "Save the current references as a cast member"));
            const savables = state.refs
                .map((r, i) => ({ r, i })).filter((x) => x.r.src.type === "file");
            const savableAudio = fileLinesOf("ref_audio_files");
            if (!savables.length && !savableAudio.length) {
                saveBox.appendChild(el("div", { color: COL.text, fontSize: "12px" },
                    "Nothing file-sourced to save — socket references can't travel in a cast file; pick images via + reference first."));
            } else {
                const checks = [];
                const rowWrap = el("div", { display: "flex", flexWrap: "wrap", gap: "8px", margin: "6px 0" });
                for (const { r, i } of savables) {
                    const lab = el("label", { display: "flex", flexDirection: "column",
                        alignItems: "center", gap: "4px", cursor: "pointer", width: "116px" });
                    const cb = el("input");
                    cb.type = "checkbox"; cb.checked = true;
                    checks.push({ cb, kind: "image", r, i });
                    lab.append(thumbEl(refImg(r), 112, 112, "", state.refCrops[i], null),
                        el("span", { color: COL.text, fontSize: "11px", fontFamily: "monospace" },
                            r.strength.toFixed(2) + (state.refCrops[i] ? " · ⛶" : "")),
                        cb);
                    rowWrap.appendChild(lab);
                }
                savableAudio.forEach((f) => {
                    const lab = el("label", { display: "flex", alignItems: "center", gap: "5px",
                        cursor: "pointer", fontSize: "11px", color: COL.text });
                    const cb = el("input");
                    cb.type = "checkbox"; cb.checked = true;
                    checks.push({ cb, kind: "audio", file: f });
                    lab.append(cb, el("span", { color: COL.green }, "♪"), el("span", null, f));
                    rowWrap.appendChild(lab);
                });
                saveBox.appendChild(rowWrap);
                const nameRow = el("div", { display: "flex", gap: "8px", alignItems: "center" });
                const nameIn = el("input");
                nameIn.placeholder = "name (e.g. Sarah)";
                Object.assign(nameIn.style, {
                    flex: "1", background: COL.input, color: COL.bright,
                    border: `1px solid ${COL.border}`, borderRadius: "3px",
                    padding: "4px 8px", fontSize: "12px",
                });
                nameIn.addEventListener("keydown", (ev) => ev.stopPropagation());
                const saveB = el("button", { ...btnStyle, color: COL.green }, "\u{1F4BE} save cast member");
                saveB.addEventListener("click", async () => {
                    const nm = nameIn.value.trim();
                    if (!nm) { toast("give the cast member a name", true); return; }
                    const fname = CAST_PREFIX + castSlug(nm) + ".json";
                    if (saveB.dataset.confirm !== fname) {
                        const existing = (await fetchInternalFiles("input"))
                            .map((f) => f.replace(/\s*\[\w+\]\s*$/, ""));
                        if (existing.includes(fname)) {
                            saveB.dataset.confirm = fname;
                            saveB.textContent = "⚠ overwrite \"" + nm + "\"?";
                            saveB.style.color = COL.mid;
                            return;
                        }
                    }
                    const images = checks.filter((c) => c.cb.checked && c.kind === "image")
                        .map((c) => ({ file: c.r.src.name, strength: c.r.strength,
                            crop: state.refCrops[c.i] || null }));
                    const audio = checks.filter((c) => c.cb.checked && c.kind === "audio")
                        .map((c) => ({ file: c.file }));
                    if (!images.length && !audio.length) { toast("nothing selected", true); return; }
                    try {
                        await uploadBlob(new Blob([buildCastJson(nm, images, audio)],
                            { type: "application/json" }), CAST_PREFIX + castSlug(nm) + ".json", true);
                        closeModal();
                        toast(`cast member "${nm}" saved — available in every workflow`);
                    } catch (e) { toast("cast save failed: " + e.message, true); }
                });
                nameRow.append(nameIn, saveB);
                saveBox.appendChild(nameRow);
            }
            bodyEl.appendChild(saveBox);

            // --- LOAD pane: every saved cast member, thumbnails, one-click add
            const loadBox = el("div", {
                border: `1px solid ${COL.divider}`, borderRadius: "6px", padding: "10px",
            });
            loadBox.appendChild(el("div", { color: COL.bright, fontSize: "12px", marginBottom: "6px" },
                "Add a saved cast member to this clip"));
            const listEl = el("div", { display: "flex", flexWrap: "wrap", gap: "12px",
                alignItems: "stretch" });
            loadBox.appendChild(listEl);
            loadBox.appendChild(el("div", { color: "#666", fontSize: "10px", marginTop: "8px" },
                "to remove a cast member, delete its h3cast-….json from the input folder"));
            bodyEl.appendChild(loadBox);

            listEl.appendChild(el("div", { color: COL.text, fontSize: "12px" }, "loading…"));
            const myRoot = root;
            const files = (await fetchInternalFiles("input"))
                .map((f) => f.replace(/\s*\[\w+\]\s*$/, ""))
                .filter((f) => f.startsWith(CAST_PREFIX) && f.endsWith(".json"));
            if (state.modal?.root !== myRoot) return;   // closed mid-fetch
            listEl.textContent = "";
            if (!files.length)
                listEl.appendChild(el("div", { color: COL.text, fontSize: "12px" },
                    "no cast members saved yet"));
            for (const f of files) {
                let cast = null;
                try {
                    const resp = await fetch(inputFileUrl(f));
                    if (resp.ok) cast = parseCastJson(await resp.text());
                } catch (e) { /* unreadable */ }
                if (state.modal?.root !== myRoot) return;   // closed mid-fetch
                const row = el("div", {
                    display: "flex", flexDirection: "column", gap: "8px", width: "236px",
                    background: COL.panel, border: `1px solid ${COL.border}`,
                    borderRadius: "6px", padding: "10px",
                });
                row.addEventListener("mouseenter", () => row.style.borderColor = COL.slider);
                row.addEventListener("mouseleave", () => row.style.borderColor = COL.border);
                if (!cast) {
                    row.append(el("span", { color: COL.red, fontSize: "12px" },
                        `⚠ ${f} is not a valid cast file`));
                    listEl.appendChild(row);
                    continue;
                }
                const nameLine = el("div", { display: "flex", alignItems: "baseline", gap: "8px" });
                nameLine.append(
                    el("span", { color: COL.bright, fontSize: "15px", flex: "1",
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, cast.name),
                    el("span", { color: COL.text, fontSize: "11px" },
                        cast.images.length + " ref" + (cast.images.length === 1 ? "" : "s")
                        + (cast.audio.length ? " · ♪×" + cast.audio.length : "")));
                row.appendChild(nameLine);
                const thumbs = el("div", { display: "flex", flexWrap: "wrap", gap: "6px" });
                for (const im of cast.images.slice(0, 4)) {
                    const cell = el("div", { display: "flex", flexDirection: "column",
                        alignItems: "center", gap: "2px" });
                    cell.append(thumbEl(cachedImg(im.file), 100, 100, "", im.crop, null),
                        el("span", { color: "#666", fontSize: "10px", fontFamily: "monospace" },
                            im.strength.toFixed(2) + (im.crop ? " · ⛶" : "")));
                    thumbs.appendChild(cell);
                }
                if (cast.images.length > 4)
                    thumbs.appendChild(el("div", {
                        width: "100px", height: "100px", display: "flex", alignItems: "center",
                        justifyContent: "center", color: COL.text, fontSize: "13px",
                        background: "#101010", borderRadius: "4px",
                    }, "+" + (cast.images.length - 4) + " more"));
                row.appendChild(thumbs);
                const addB = el("button", { ...btnStyle, color: COL.green, padding: "7px 12px",
                    fontSize: "13px", marginTop: "auto" }, "➕ add to clip");
                addB.title = "add this cast member's references (with strengths and framings) to the clip";
                addB.addEventListener("click", () => {
                    if (state.refSpecError) {
                        toast("fix the ref spec error first (✎ raw text specs) — adding now would scramble strengths", true);
                        return;
                    }
                    // ONE batched write per widget: per-image addFileRef would
                    // refresh between items and clobber earlier strength/crop sets
                    const lines = fileLinesOf("ref_image_files");
                    for (const im of cast.images) lines.push(im.file);
                    setWidget("ref_image_files", lines.join("\n"));
                    const strengths = state.refs.map((r) => r.strength)
                        .concat(cast.images.map((im) => im.strength));
                    setWidget("ref_spec", formatRefSpec(strengths));
                    const crops = [...state.refCrops, ...cast.images.map((im) => im.crop || null)];
                    setWidget("ref_image_crops", crops.some(Boolean) ? formatCropSpec(crops) : "");
                    const socketA = connectedSlots(node, "ref_audio_").length;
                    const aLines = fileLinesOf("ref_audio_files");
                    let audioAdded = 0;
                    for (const au of cast.audio) {
                        if (socketA + aLines.length >= 3) {
                            toast("audio limit (3) reached — some cast audio skipped", true);
                            break;
                        }
                        aLines.push(au.file);
                        audioAdded++;
                    }
                    if (audioAdded) setWidget("ref_audio_files", aLines.join("\n"));
                    refresh(true);
                    closeModal();
                    toast(`"${cast.name}" added — ${cast.images.length} reference(s)${audioAdded ? ` + ${audioAdded} audio` : ""}`);
                });
                row.appendChild(addB);
                listEl.appendChild(row);
            }
        });
    }

    // ---- framing tool: aspect-locked crop window over the source image -------
    function openFramer(sel, _media) {
        // v2v frames like a KEYFRAME (window locked to the widget canvas — the
        // footage will be resized to exactly width×height) but scrubs like a video
        const isRef = sel.kind === "ref" || sel.kind === "video";
        const isVideo = sel.kind === "video" || sel.kind === "v2v";
        let img = null;
        if (sel.kind === "first" || sel.kind === "last") img = capInfo(sel.kind)?.img;
        else if (sel.kind === "mid") img = midImg(state.mids[sel.i]);
        else if (sel.kind === "ref") img = refImg(state.refs[sel.i]);
        else if (sel.kind === "video" || sel.kind === "v2v") {
            const name = sel.kind === "v2v"
                ? String(widgetValue(node, "v2v_video_file", "")).trim()
                : (state.videoRefs[sel.i]?.src.type === "file" ? state.videoRefs[sel.i].src.name : "");
            if (name) {
                img = _media || document.createElement("video");
                if (!_media) {
                    img.muted = true;
                    img.preload = "auto";
                    img.src = inputFileUrl(name);
                }
            }
        }
        if (!img) { openModal((r) => r.appendChild(el("div", { color: COL.text, fontSize: "13px" },
            "No preview available to frame — socket inputs need a prior run (file-picked ones frame instantly)."))); return; }
        if (isVideo && _media && !_media.videoWidth) {
            // re-entry after the wait, but the source vanished or never decoded
            _media.removeAttribute("src"); _media.load();
            toast("couldn't read that footage (removed, or a codec the browser can't decode)", true);
            return;
        }
        if (isVideo && !img.videoWidth) {
            // metadata never exists synchronously after src-set: hand the SAME
            // element back to ourselves once it's ready (recursing without it
            // created a fresh 0-width element every cycle — an infinite loop)
            img.addEventListener("loadedmetadata", () => openFramer(sel, img), { once: true });
            img.addEventListener("error", () => openFramer(sel, img), { once: true });
            return;
        }
        // normalize media dims: <video> exposes videoWidth/Height, <img> natural*
        if (isVideo) {
            Object.defineProperty(img, "naturalWidth", { value: img.videoWidth });
            Object.defineProperty(img, "naturalHeight", { value: img.videoHeight });
        }

        const [ow, oh] = isRef ? [img.naturalWidth, img.naturalHeight]
            : (sel.kind === "v2v" ? outWH().map(snap32) : (effWH() || outWH()));
        let crop = cropOf(sel) || { cx: 0.5, cy: 0.5, z: 1.0 };

        openModal((root) => {
            const panel = el("div", {
                background: COL.bg, border: `1px solid ${COL.border}`, borderRadius: "8px",
                display: "flex", flexDirection: "column", overflow: "hidden",
                fontFamily: "sans-serif", maxWidth: "92vw", maxHeight: "92vh",
            });
            const head = el("div", {
                display: "flex", alignItems: "center", gap: "10px",
                padding: "8px 12px", borderBottom: `1px solid ${COL.divider}`,
            });
            head.appendChild(el("span", { color: COL.bright, fontSize: "13px", flex: "1" },
                sel.kind === "v2v"
                    ? `Frame v2v footage — window locked to ${ow}×${oh}: the restyled clip becomes exactly this canvas`
                    : isRef ? "Frame reference — zoom/pan chooses what the model sees (source aspect)"
                    : `Frame — window locked to the output aspect ${ow}×${oh}, so no stretch and no blind crop`));
            const resetB = el("button", btnStyle, "reset");
            const clearB = el("button", btnStyle, "no framing");
            const doneB = el("button", { ...btnStyle, color: COL.green }, "done");
            head.append(resetB, clearB, doneB);

            const cnv = el("canvas", { display: "block", cursor: "grab", touchAction: "none" });
            const maxW = Math.min(window.innerWidth * 0.86, 1200);
            const maxH = Math.min(window.innerHeight * 0.74, 820);
            const fit = Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight);
            const dw = Math.round(img.naturalWidth * fit), dh = Math.round(img.naturalHeight * fit);
            cnv.style.width = dw + "px"; cnv.style.height = dh + "px";
            const dpr = window.devicePixelRatio || 1;
            cnv.width = Math.round(dw * dpr); cnv.height = Math.round(dh * dpr);
            const hint = el("div", { padding: "6px 12px", color: COL.text, fontSize: "11px" },
                "drag to move · drag a corner for exact size · scroll = zoom (Shift = fine) · dimmed = not seen by the model");
            panel.append(head, cnv);
            if (isVideo) {
                const scrubRow = el("div", { display: "flex", gap: "8px", alignItems: "center", padding: "4px 12px 0" });
                const scrub = el("input");
                scrub.type = "range"; scrub.min = "0"; scrub.max = "1000"; scrub.value = "0";
                Object.assign(scrub.style, { flex: "1", accentColor: COL.green, cursor: "pointer" });
                const tLabel = el("span", { color: COL.text, fontSize: "11px", fontFamily: "monospace", width: "48px" }, "0.0s");
                scrub.addEventListener("input", () => {
                    const t = (scrub.value / 1000) * (img.duration || 0);
                    img.currentTime = t;
                    tLabel.textContent = t.toFixed(1) + "s";
                });
                img.addEventListener("seeked", () => draw());
                scrubRow.append(el("span", { color: COL.text, fontSize: "11px" }, "scrub"), scrub, tLabel);
                panel.append(scrubRow);
            }
            panel.append(hint);
            root.appendChild(panel);

            function draw() {
                const ctx = cnv.getContext("2d");
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
                ctx.clearRect(0, 0, dw, dh);
                try { ctx.drawImage(img, 0, 0, dw, dh); } catch (e) { /* raced */ }
                const box = cropBoxJS(img.naturalWidth, img.naturalHeight, ow, oh,
                    crop.z, crop.cx, crop.cy);
                const bx = box.x * fit, bY = box.y * fit, bw = box.w * fit, bh = box.h * fit;
                ctx.fillStyle = "rgba(0,0,0,0.62)";
                ctx.fillRect(0, 0, dw, bY);
                ctx.fillRect(0, bY + bh, dw, dh - bY - bh);
                ctx.fillRect(0, bY, bx, bh);
                ctx.fillRect(bx + bw, bY, dw - bx - bw, bh);
                ctx.strokeStyle = COL.sel;
                ctx.lineWidth = 1.5;
                ctx.strokeRect(bx + 0.75, bY + 0.75, bw - 1.5, bh - 1.5);
                ctx.fillStyle = COL.sel;
                for (const [hx, hy] of [[bx, bY], [bx + bw, bY], [bx, bY + bh], [bx + bw, bY + bh]])
                    ctx.fillRect(hx - 4, hy - 4, 8, 8);
                // thirds
                ctx.strokeStyle = "rgba(255,255,255,0.25)";
                ctx.lineWidth = 1;
                ctx.beginPath();
                for (let i = 1; i < 3; i++) {
                    ctx.moveTo(bx + bw * i / 3, bY); ctx.lineTo(bx + bw * i / 3, bY + bh);
                    ctx.moveTo(bx, bY + bh * i / 3); ctx.lineTo(bx + bw, bY + bh * i / 3);
                }
                ctx.stroke();
                ctx.font = "12px monospace";
                ctx.fillStyle = COL.bright;
                ctx.textAlign = "left"; ctx.textBaseline = "top";
                ctx.fillText(`${crop.z.toFixed(2)}×  ·  ${Math.round(box.w)}×${Math.round(box.h)}px`,
                    bx + 6, bY + 6);
            }

            const save = () => {
                setCropOf(sel, (isRef && crop.z <= 1.001) ? null : crop);
                refresh(true);
            };
            let dragging = null;
            const cornerAt = (px, py) => {
                // returns {fx, fy}: the FIXED (opposite) corner in source px, or null
                const box = cropBoxJS(img.naturalWidth, img.naturalHeight, ow, oh,
                    crop.z, crop.cx, crop.cy);
                const corners = [
                    [box.x, box.y, box.x + box.w, box.y + box.h],
                    [box.x + box.w, box.y, box.x, box.y + box.h],
                    [box.x, box.y + box.h, box.x + box.w, box.y],
                    [box.x + box.w, box.y + box.h, box.x, box.y],
                ];
                for (const [cx0, cy0, fx, fy] of corners)
                    if (Math.abs(px - cx0 * fit) < 10 && Math.abs(py - cy0 * fit) < 10)
                        return { fx, fy };
                return null;
            };
            cnv.addEventListener("pointerdown", (ev) => {
                if (ev.button !== 0) return;
                ev.preventDefault();
                cnv.setPointerCapture(ev.pointerId);
                const r = cnv.getBoundingClientRect();
                const corner = cornerAt(ev.clientX - r.left, ev.clientY - r.top);
                if (corner) {
                    dragging = { corner };
                    cnv.style.cursor = "nwse-resize";
                } else {
                    dragging = { x: ev.clientX, y: ev.clientY, cx: crop.cx, cy: crop.cy };
                    cnv.style.cursor = "grabbing";
                }
            });
            cnv.addEventListener("pointermove", (ev) => {
                if (!dragging) return;
                if (dragging.corner) {
                    // resize about the opposite corner: exact window sizing by hand
                    const r = cnv.getBoundingClientRect();
                    const sx = (ev.clientX - r.left) / fit, sy = (ev.clientY - r.top) / fit;
                    const { fx, fy } = dragging.corner;
                    const aspect = ow / oh;
                    const wPx = Math.max(Math.abs(sx - fx), Math.abs(sy - fy) * aspect);
                    const box0 = cropBoxJS(img.naturalWidth, img.naturalHeight, ow, oh, 1, 0.5, 0.5);
                    const z = Math.min(16, Math.max(1, box0.w / Math.max(8, wPx)));
                    const w2 = box0.w / z, h2 = box0.h / z;
                    const ncx = fx + Math.sign(sx - fx) * w2 / 2;
                    const ncy = fy + Math.sign(sy - fy) * h2 / 2;
                    crop = { cx: Math.min(1, Math.max(0, ncx / img.naturalWidth)),
                        cy: Math.min(1, Math.max(0, ncy / img.naturalHeight)), z };
                    draw();
                    return;
                }
                crop = { ...crop,
                    cx: Math.min(1, Math.max(0, dragging.cx + (ev.clientX - dragging.x) / fit / img.naturalWidth)),
                    cy: Math.min(1, Math.max(0, dragging.cy + (ev.clientY - dragging.y) / fit / img.naturalHeight)) };
                draw();
            });
            const stop = (ev) => {
                if (!dragging) return;
                dragging = null;
                cnv.style.cursor = "grab";
                try { cnv.releasePointerCapture(ev.pointerId); } catch (e) { /* released */ }
                save();
            };
            cnv.addEventListener("pointerup", stop);
            cnv.addEventListener("pointercancel", stop);
            cnv.addEventListener("wheel", (ev) => {
                ev.preventDefault();
                // ~6%/notch (Shift: ~1.5%) — the old 16% couldn't reach values
                // between 100% and 122%
                const k = ev.shiftKey ? 0.00015 : 0.0006;
                const next = Math.min(16, Math.max(1, crop.z * Math.exp(-ev.deltaY * k)));
                crop = { ...crop, z: next };
                draw(); save();
            }, { passive: false });

            resetB.addEventListener("click", () => { crop = { cx: 0.5, cy: 0.5, z: 1.0 }; draw(); save(); });
            clearB.addEventListener("click", () => { setCropOf(sel, null); refresh(true); closeModal(); });
            doneB.addEventListener("click", () => { save(); closeModal(); });
            draw();
            if (isVideo && state.modal)
                // stop the buffering download the moment the framer closes
                state.modal.onClose = () => {
                    try { img.pause(); img.removeAttribute("src"); img.load(); } catch (e) { /* torn down */ }
                };
        });
    }

    // ---- image picker: real folder explorer over /h3guide/browse ----------
    const FAV_KEY = "h3guide.favPath";
    function readFav() {
        try { return JSON.parse(localStorage.getItem(FAV_KEY)) || null; }
        catch (e) { return null; }
    }
    async function fetchBrowse(tab, folder, q) {
        const ps = new URLSearchParams({ type: tab, path: folder || "", q: q || "", kind: "images" });
        const r = await api.fetchApi("/h3guide/browse?" + ps.toString());
        const j = await r.json();
        if (j.error) throw new Error(j.error);
        const ann = tab === "output" ? " [output]" : "";
        return { dirs: j.dirs || [], files: (j.files || []).map((f) => f + ann) };
    }

    async function openPicker(onPick) {
        const fav = readFav();
        let tab = fav?.tab === "output" ? "output" : "input";
        let folder = (fav && fav.tab === tab && fav.folder) || "";
        let listing = { dirs: [], files: [] };
        let webMode = false, webQ = "", webPage = 1, webAcc = [];
        let searchTimer = null, loadGen = 0;
        openModal((root) => {
            const panel = el("div", {
                width: "min(980px, 92vw)", maxHeight: "84vh", background: COL.bg,
                border: `1px solid ${COL.border}`, borderRadius: "8px", display: "flex",
                flexDirection: "column", overflow: "hidden", fontFamily: "sans-serif",
            });
            const head = el("div", {
                display: "flex", gap: "8px", alignItems: "center",
                padding: "8px 10px", borderBottom: `1px solid ${COL.divider}`,
            });
            const tabBtn = (name, label) => {
                const b = el("button", { ...btnStyle }, label);
                b.addEventListener("click", () => {
                    if (webMode) return;
                    tab = name;
                    folder = "";
                    styleTabs(); styleFav();
                    load();
                });
                return b;
            };
            const tabIn = tabBtn("input", "input folder");
            const tabOut = tabBtn("output", "output folder");
            const styleTabs = () => {
                tabIn.style.borderColor = tab === "input" ? COL.bright : COL.border;
                tabIn.style.color = tab === "input" ? COL.bright : COL.text;
                tabOut.style.borderColor = tab === "output" ? COL.bright : COL.border;
                tabOut.style.color = tab === "output" ? COL.bright : COL.text;
            };
            const crumb = el("span", {
                color: COL.text, fontSize: "12px", fontFamily: "monospace",
                whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                maxWidth: "220px",
            });
            const favBtn = el("button", { ...btnStyle, padding: "3px 8px" }, "☆");
            const styleFav = () => {
                const f = readFav();
                const here = f && f.tab === tab && (f.folder || "") === (folder || "");
                favBtn.textContent = here ? "⭐" : "☆";
                favBtn.style.color = here ? COL.mid : COL.text;
                favBtn.title = here
                    ? "this is your favourite location — the picker opens here. Click to clear."
                    : "make this tab+folder the favourite location the picker opens at";
                crumb.textContent = "/" + (folder || "");
                crumb.title = (tab === "output" ? "output/" : "input/") + (folder || "");
            };
            favBtn.addEventListener("click", () => {
                const f = readFav();
                const here = f && f.tab === tab && (f.folder || "") === (folder || "");
                try {
                    if (here) localStorage.removeItem(FAV_KEY);
                    else localStorage.setItem(FAV_KEY, JSON.stringify({ tab, folder }));
                } catch (e) { /* storage unavailable */ }
                styleFav();
            });
            const search = el("input");
            search.placeholder = "search this folder + below…";
            Object.assign(search.style, {
                flex: "1", background: COL.input, color: COL.bright,
                border: `1px solid ${COL.border}`, borderRadius: "3px",
                padding: "3px 8px", fontSize: "12px",
            });
            search.addEventListener("keydown", (ev) => {
                ev.stopPropagation();
                if (ev.key === "Enter" && webMode && search.value.trim()) {
                    webQ = search.value.trim();
                    runWebSearch(false);
                }
            });
            search.addEventListener("input", () => {
                if (webMode) return;
                clearTimeout(searchTimer);
                searchTimer = setTimeout(load, 250);
            });
            const webBtn = el("button", btnStyle, "🌐 web…");
            webBtn.title = "search free Creative Commons images (Openverse, all licenses — each result shows its own) and pull them straight into the input folder";
            webBtn.addEventListener("click", () => {
                webMode = !webMode;
                webBtn.style.color = webMode ? COL.mid : COL.bright;
                webBtn.style.borderColor = webMode ? COL.mid : COL.border;
                favBtn.style.display = webMode ? "none" : "";
                crumb.style.display = webMode ? "none" : "";
                search.placeholder = webMode ? "search the free web… (Enter)" : "search this folder + below…";
                search.value = "";
                if (webMode) {
                    grid.textContent = "";
                    grid.appendChild(el("div", { color: COL.text, fontSize: "12px" },
                        "type a search and press Enter — results are Creative Commons / public domain (license shown per result); picks download into input/web/"));
                    search.focus();
                } else {
                    load();
                }
            });
            const webCell = (res) => {
                const cell = el("div", { cursor: "pointer", textAlign: "center", width: "110px" });
                const im = el("img");
                im.loading = "lazy";
                im.src = res.thumb || res.url;
                Object.assign(im.style, {
                    width: "110px", height: "74px", objectFit: "cover",
                    borderRadius: "4px", border: `1px solid ${COL.border}`, display: "block",
                });
                cell.append(im,
                    el("div", { color: COL.text, fontSize: "10px", overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap" },
                        res.title || "untitled"),
                    el("div", { color: "#666", fontSize: "9px", overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap" },
                        res.license + (res.creator ? " · " + res.creator : "")));
                cell.title = `${res.title}\n${res.width}×${res.height} · ${res.license}`
                    + (res.creator ? ` by ${res.creator}` : "");
                cell.addEventListener("click", async () => {
                    cell.style.opacity = "0.4";
                    try {
                        const name = await webFetch(res, "images");
                        closeModal();
                        onPick(name);
                    } catch (e) {
                        cell.style.opacity = "1";
                        toast("download failed: " + e.message, true);
                    }
                });
                return cell;
            };
            const runWebSearch = async (append) => {
                if (!append) {
                    webPage = 1; webAcc = [];
                    grid.textContent = "";
                    grid.appendChild(el("div", { color: COL.text, fontSize: "12px" }, "searching…"));
                }
                try {
                    const results = await webSearch(webQ, "images", webPage);
                    if (!webMode) return;
                    webAcc = webAcc.concat(results);
                    grid.textContent = "";
                    if (!webAcc.length) {
                        grid.appendChild(el("div", { color: COL.text, fontSize: "12px" }, "no results"));
                        return;
                    }
                    for (const res of webAcc) grid.appendChild(webCell(res));
                    if (results.length >= 20) {
                        const more = el("button", { ...btnStyle, alignSelf: "center" }, "more…");
                        more.addEventListener("click", () => { webPage++; runWebSearch(true); });
                        grid.appendChild(more);
                    }
                } catch (e) {
                    grid.textContent = "";
                    grid.appendChild(el("div", { color: COL.red, fontSize: "12px" },
                        "search failed: " + e.message));
                }
            };
            const up = el("button", btnStyle, "upload…");
            const fileInput = el("input");
            fileInput.type = "file";
            fileInput.accept = "image/*";
            fileInput.style.display = "none";
            up.addEventListener("click", () => fileInput.click());
            fileInput.addEventListener("change", async () => {
                const f = fileInput.files?.[0];
                if (!f) return;
                try {
                    const name = await uploadBlob(f, f.name);
                    closeModal();
                    onPick(name);
                } catch (e) { toast("upload failed: " + e.message, true); }
            });
            const closeB = el("button", btnStyle, "✕");
            closeB.addEventListener("click", closeModal);
            head.append(tabIn, tabOut, crumb, favBtn, webBtn, search, up, fileInput, closeB);

            const caption = el("div", { padding: "6px 10px 0", color: COL.text, fontSize: "11px" });
            const grid = el("div", {
                display: "flex", flexWrap: "wrap", gap: "6px", padding: "10px",
                overflowY: "auto",
            });
            panel.append(head, caption, grid);
            root.appendChild(panel);

            const goTo = (target) => { folder = target; styleFav(); load(); };
            const folderTile = (label, target) => {
                const c = el("div", {
                    width: "110px", height: "74px", display: "flex",
                    flexDirection: "column", alignItems: "center",
                    justifyContent: "center", gap: "2px",
                    border: `2px dashed ${COL.slider}`, borderRadius: "4px",
                    color: COL.bright, fontSize: "11px", cursor: "pointer",
                    overflow: "hidden", padding: "2px",
                });
                c.append(el("span", { fontSize: "20px" }, "📁"),
                    el("span", { maxWidth: "100px", overflow: "hidden",
                        textOverflow: "ellipsis", whiteSpace: "nowrap" }, label));
                c.title = target || "(root)";
                c.addEventListener("click", () => goTo(target));
                return c;
            };
            const fileCell = (f) => {
                const cell = el("div", { cursor: "pointer", textAlign: "center", width: "110px" });
                const im = el("img");
                im.loading = "lazy";
                im.src = previewFileUrl(f);
                im.addEventListener("error", () => { im.src = inputFileUrl(f); }, { once: true });
                Object.assign(im.style, {
                    width: "110px", height: "74px", objectFit: "cover",
                    borderRadius: "4px", border: `1px solid ${COL.border}`, display: "block",
                });
                const shown = f.replace(/\s*\[\w+\]\s*$/, "").split("/").pop();
                cell.append(im, el("div", { color: COL.text, fontSize: "10px", overflow: "hidden" },
                    shown.length > 18 ? shown.slice(0, 16) + "…" : shown));
                cell.title = f;
                cell.addEventListener("click", () => { closeModal(); onPick(f); });
                return cell;
            };
            const fill = () => {
                const q = search.value.trim();
                caption.textContent = tab === "output" && !folder && !q
                    ? "newest first — chain a clip by picking the last frame of your previous render"
                    : q ? `search results under /${folder || ""}` : "";
                caption.style.display = caption.textContent ? "" : "none";
                grid.textContent = "";
                let tiles = 0;
                if (!q) {
                    if (folder) {
                        grid.appendChild(folderTile("⬑ up",
                            folder.includes("/") ? folder.slice(0, folder.lastIndexOf("/")) : ""));
                        tiles++;
                    }
                    for (const d of listing.dirs) {
                        grid.appendChild(folderTile(d.split("/").pop(), d));
                        tiles++;
                    }
                }
                for (const f of listing.files) grid.appendChild(fileCell(f));
                if (!listing.files.length && !tiles)
                    grid.appendChild(el("div", { color: COL.text, fontSize: "12px" },
                        q ? "no matches"
                            : tab === "output" ? "this folder is empty — render something first"
                            : "this folder is empty — use upload…"));
            };
            const load = async () => {
                const gen = ++loadGen;
                grid.textContent = "";
                grid.appendChild(el("div", { color: COL.text, fontSize: "12px" }, "loading…"));
                try {
                    const got = await fetchBrowse(tab, folder, search.value.trim());
                    if (gen !== loadGen || webMode) return;
                    listing = got;
                    fill();
                } catch (e) {
                    if (gen !== loadGen) return;
                    if (folder) { folder = ""; styleFav(); load(); return; }   // stale fav etc.
                    grid.textContent = "";
                    grid.appendChild(el("div", { color: COL.red, fontSize: "12px" },
                        "listing failed: " + e.message));
                }
            };
            styleTabs();
            styleFav();
            load();
        });
    }

    // ======================================================================
    // FULLSCREEN EDITOR
    // ======================================================================
    function stopRecorder() {
        if (!state.recorder) return;
        try { state.recorder.onstop = null; state.recorder.stop(); } catch (e) { /* already stopped */ }
        state.recorderStream?.getTracks().forEach((t) => t.stop());
        state.recorder = null;
        state.recorderStream = null;
    }

    function closeFullscreen() {
        closeCtxMenu();
        stopRecorder();
        if (ACTIVE_EDITOR_CLOSE === closeFullscreen) ACTIVE_EDITOR_CLOSE = null;
        const f = state.fs;
        if (!f) return;
        state.fs = null;
        window.removeEventListener("keydown", f.onKey, true);
        f.root.remove();
        renderSummary();
    }
    node._h3CloseFS = closeFullscreen;

    function openFullscreen() {
        if (state.fs) return;
        if (ACTIVE_EDITOR_CLOSE && ACTIVE_EDITOR_CLOSE !== closeFullscreen) ACTIVE_EDITOR_CLOSE();
        ACTIVE_EDITOR_CLOSE = closeFullscreen;
        const root = el("div", {
            position: "fixed", inset: "12px", zIndex: "9999",
            background: COL.bg, border: `1px solid ${COL.border}`, borderRadius: "8px",
            boxShadow: "0 16px 64px rgba(0,0,0,0.7)", display: "flex",
            flexDirection: "column", overflow: "hidden", fontFamily: "sans-serif",
        });

        // header
        const header = el("div", {
            display: "flex", alignItems: "center", gap: "14px", padding: "0 16px",
            height: "44px", flex: "0 0 auto", borderBottom: `1px solid ${COL.divider}`,
        });
        const title = el("span", { color: COL.bright, fontSize: "15px" },
            "H3 Timeline — " + (node.title || "MiniMax H3 (Guide)"));
        // inline-editable clip dimensions: the timeline's own parameters shouldn't
        // require leaving the editor. Width/height snap to 32; length shows the
        // 17k+5 snapped truth beside it.
        const dimField = (w) => {
            const f = el("input");
            Object.assign(f.style, {
                width: w + "px", background: COL.input, color: COL.bright,
                border: `1px solid ${COL.border}`, borderRadius: "3px",
                padding: "2px 6px", fontSize: "12px", fontFamily: "monospace",
                textAlign: "center",
            });
            f.addEventListener("keydown", (ev) => {
                ev.stopPropagation();
                if (ev.key === "Enter") f.blur();
            });
            return f;
        };
        const wField = dimField(52), hField = dimField(52), lenField = dimField(58);
        const snapNote = el("span", { color: COL.text, fontSize: "12px", fontFamily: "monospace" });
        // aspect lock: editing one axis derives the other from the ratio captured
        // at lock time, both snapped to the model's 32px grid
        let dimLock = false, lockRatio = 1344 / 768;
        const lockBtn = el("button", { ...btnStyle, padding: "1px 7px" }, "🔓");
        lockBtn.title = "lock aspect: editing width derives height (and vice versa), both on the 32px grid";
        lockBtn.addEventListener("click", () => {
            dimLock = !dimLock;
            const [w, h] = outWH();
            lockRatio = w / h;
            lockBtn.textContent = dimLock ? "🔒" : "🔓";
            lockBtn.style.color = dimLock ? COL.green : COL.bright;
        });
        const wMax = () => getWidget(node, "width")?.options?.max ?? 16384;
        const r32 = (v) => Math.min(wMax(), Math.max(32, Math.round(v / 32) * 32));
        const commitDims = () => {
            const curW = Number(widgetValue(node, "width", 1344));
            const curH = Number(widgetValue(node, "height", 768));
            let w = parseFloat(wField.value);
            let h = parseFloat(hField.value);
            w = isFinite(w) ? r32(w) : curW;
            h = isFinite(h) ? r32(h) : curH;
            if (dimLock) {
                if (w !== curW && h === curH) h = r32(w / lockRatio);
                else if (h !== curH) w = r32(h * lockRatio);
            }
            setWidget("width", w);
            setWidget("height", h);
            // length reads in SECONDS ("5.2" or "5.2s"); frames still accepted as "124f"
            const raw = lenField.value.trim().toLowerCase();
            const cur = Number(widgetValue(node, "length", 124));
            let n;
            if (raw.endsWith("f")) n = parseInt(raw, 10);
            else n = Math.round(parseFloat(raw) * FPS);
            n = isFinite(n) && n > 0 ? Math.min(3600, Math.max(5, n)) : cur;
            setWidget("length", n);
            if (n !== cur && (state.mids.length || state.beats.length)) {
                const places = state.mids.length + state.beats.length;
                toast(`${places} placement(s) keep their RELATIVE position — halfway is now ${(snapFrameCount(n) / 2 / FPS).toFixed(1)}s`);
            }
            fill();
        };
        for (const f of [wField, hField, lenField]) f.addEventListener("blur", commitDims);
        const stats = el("span", {
            color: COL.text, fontSize: "12px", fontFamily: "monospace", flex: "1",
            display: "flex", alignItems: "center", gap: "5px",
        });
        stats.append(wField, el("span", null, "×"), hField, lockBtn, el("span", null, "px ·"),
            lenField, snapNote);

        const queueBtn = el("button", { ...btnStyle, color: COL.green }, "▶ queue");
        queueBtn.title = "queue the workflow without leaving the editor";
        queueBtn.addEventListener("click", async () => {
            try {
                await app.queuePrompt(0);
                toast("queued — rendering with the current setup");
            } catch (e) {
                toast("queue failed: " + (e?.message || e), true);
            }
        });
        const swapBtn = el("button", btnStyle, "⇄ reverse");
        swapBtn.addEventListener("click", () => {
            if (swapBtn.disabled) return;
            const pairs = [["first_frame_file", "last_frame_file"],
                ["first_frame_crop", "last_frame_crop"],
                ["first_frame_strength", "last_frame_strength"]];
            for (const [a, b] of pairs) {
                const va = widgetValue(node, a, ""), vb = widgetValue(node, b, "");
                setWidget(a, vb);
                setWidget(b, va);
            }
            refresh(true);
            toast("first and last swapped — the shot now runs in reverse");
        });

        const saveBtn = el("button", btnStyle, "💾 save setup");
        saveBtn.title = "download this node's whole conditioning setup as a JSON file";
        saveBtn.addEventListener("click", exportSetup);
        const loadBtn = el("button", btnStyle, "📂 load setup");
        loadBtn.title = "resume a saved setup (overwrites the current fields)";
        const loadInput = el("input");
        loadInput.type = "file";
        loadInput.accept = ".json,application/json";
        loadInput.style.display = "none";
        loadBtn.addEventListener("click", () => loadInput.click());
        loadInput.addEventListener("change", () => {
            const f = loadInput.files?.[0];
            if (!f) return;
            f.text().then(importSetup);
            loadInput.value = "";
        });
        const helpBtn = el("button", btnStyle, "?");
        const closeBtn = el("button", btnStyle, "✕");
        closeBtn.addEventListener("click", closeFullscreen);
        header.append(title, stats, queueBtn, swapBtn, saveBtn, loadBtn, loadInput, helpBtn, closeBtn);

        // body: main column + inspector
        const body = el("div", { display: "flex", flex: "1 1 auto", minHeight: "0" });
        const main = el("div", {
            flex: "1 1 auto", minWidth: "0", display: "flex", flexDirection: "column",
            overflowY: "auto",
        });
        const inspector = el("div", {
            width: "360px", flex: "0 0 360px", borderLeft: `1px solid ${COL.divider}`,
            background: COL.panel, overflowY: "auto", padding: "16px",
            boxSizing: "border-box",
        });

        // main sections
        const promptHead = el("div", sectionHeadStyle(),
            "PROMPT — click a chip to cite that image or audio at the cursor");
        const promptTA = el("textarea");
        promptTA.rows = 3;
        Object.assign(promptTA.style, {
            margin: "8px 16px 4px", background: COL.input, color: COL.bright,
            border: `1px solid ${COL.border}`, borderRadius: "4px", padding: "8px",
            fontSize: "13px", lineHeight: "1.45", resize: "vertical",
            fontFamily: "sans-serif", flex: "0 0 auto",
        });
        promptTA.placeholder = "describe the clip…";
        promptTA.addEventListener("keydown", (ev) => ev.stopPropagation());
        promptTA.addEventListener("input", () => setWidget("prompt", promptTA.value));
        const chipRow = el("div", {
            display: "flex", flexWrap: "wrap", gap: "6px", padding: "0 16px 4px",
            flex: "0 0 auto",
        });
        const insertAtCaret = (text) => {
            promptTA.focus();
            const s = promptTA.selectionStart ?? promptTA.value.length;
            const e = promptTA.selectionEnd ?? s;
            promptTA.setRangeText(text, s, e, "end");
            setWidget("prompt", promptTA.value);
        };
        // small shared control builders for section-header settings
        // the tag you actually type in the prompt, shown under each thumbnail
        const tagCaption = (text, color) => el("div", {
            color, fontFamily: "monospace", fontSize: "11px", textAlign: "center",
            padding: "1px 0 2px", background: "#101010", letterSpacing: "0.03em",
            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
        }, text);

        const miniSelect = (widget, options, tooltip) => {
            const s = el("select", {
                background: COL.input, color: COL.bright, border: `1px solid ${COL.border}`,
                borderRadius: "3px", fontSize: "13px", padding: "3px 8px", cursor: "pointer",
            });
            for (const o of options) {
                const opt = el("option", null, o);
                opt.value = o;
                s.appendChild(opt);
            }
            s.title = tooltip;
            s.addEventListener("change", () => { setWidget(widget, s.value); fill(); });
            s.addEventListener("pointerdown", (ev) => ev.stopPropagation());
            return s;
        };
        const miniNum = (widget, tooltip, width) => {
            const n = el("input", {
                width: (width || 56) + "px", background: COL.input, color: COL.bright,
                border: `1px solid ${COL.border}`, borderRadius: "3px",
                fontSize: "13px", padding: "3px 6px", fontFamily: "monospace", textAlign: "center",
            });
            n.title = tooltip;
            n.addEventListener("keydown", (ev) => { ev.stopPropagation(); if (ev.key === "Enter") n.blur(); });
            n.addEventListener("blur", () => {
                const v = parseFloat(n.value);
                setWidget(widget, isFinite(v) && v >= 0 ? v : 0);
                fill();
            });
            return n;
        };

        // v2v bar: drive the whole restyle flow from the editor — pick footage
        // (output tab = restyle a previous render), optional section, done.
        const v2vBar = el("div", {
            display: "flex", gap: "8px", alignItems: "center", padding: "6px 16px",
            flex: "0 0 auto", borderBottom: `1px solid ${COL.divider}`,
        });
        v2vBar.appendChild(el("span", { color: COL.text, fontSize: "11px", letterSpacing: "0.06em" },
            "🎞 V2V"));
        const v2vLabel = el("span", { color: COL.text, fontSize: "12px", flex: "1",
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
        const v2vPick = el("button", btnStyle, "pick footage…");
        v2vPick.title = "restyle existing footage: the node's latent becomes this video encoded for partial denoise (sampler denoise 0.3-0.7). Output tab = restyle a previous render.";
        v2vPick.addEventListener("click", () => openVideoPicker((n) => {
            setWidget("v2v_video_file", n);
            refresh(true);
        }));
        const v2vStart = dimField(46), v2vEnd = dimField(46);
        v2vStart.title = "section start (seconds); 0 = clip start";
        v2vEnd.title = "section end (seconds); 0 = to the end";
        const commitV2v = () => {
            const s = parseFloat(v2vStart.value), e = parseFloat(v2vEnd.value);
            setWidget("v2v_start_seconds", isFinite(s) && s >= 0 ? s : 0);
            setWidget("v2v_end_seconds", isFinite(e) && e >= 0 ? e : 0);
            fill();
        };
        for (const f of [v2vStart, v2vEnd]) f.addEventListener("blur", commitV2v);
        const v2vClear = el("button", { ...btnStyle, color: COL.red, padding: "1px 7px" }, "✕");
        v2vClear.title = "clear the v2v source (back to a normal empty latent)";
        v2vClear.addEventListener("click", () => {
            setWidget("v2v_video_file", "");
            setWidget("v2v_start_seconds", 0);
            setWidget("v2v_end_seconds", 0);
            setWidget("v2v_crop", "");   // a framing must not outlive its footage
            refresh(true);
        });
        const v2vNote = el("span", { fontSize: "11px", display: "none", whiteSpace: "nowrap" });
        const v2vScrub = el("button", btnStyle, "✂ section…");
        v2vScrub.title = "scrub the footage and set the in/out points visually";
        v2vScrub.addEventListener("click", () => openV2vSection());
        const v2vFrame = el("button", btnStyle, "⛶");
        v2vFrame.title = "frame the footage: window locked to the width×height widgets — the restyled clip becomes exactly that canvas (reframe landscape into vertical, etc.)";
        v2vFrame.addEventListener("click", () => {
            const vSock = inputConnected(node, "v2v_images");
            if (vSock) {
                // no preview to frame — offer a two-click clear of the stale crop
                if (v2vFrame.dataset.confirm === "1") {
                    delete v2vFrame.dataset.confirm;
                    setWidget("v2v_crop", "");
                    refresh(true);
                    toast("v2v framing cleared — canvas follows the socket footage again");
                } else {
                    v2vFrame.dataset.confirm = "1";
                    toast("a v2v framing is set but socket footage can't be previewed — click ⛶ again to CLEAR it", true);
                }
                return;
            }
            delete v2vFrame.dataset.confirm;
            openFramer({ kind: "v2v" });
        });
        const ghostWrap = el("span", { display: "none", alignItems: "center", gap: "4px" });
        ghostWrap.appendChild(el("span", { color: COL.text, fontSize: "11px" }, "ghost"));
        const ghostSl = el("input");
        ghostSl.type = "range"; ghostSl.min = "0"; ghostSl.max = "80"; ghostSl.step = "5";
        Object.assign(ghostSl.style, { width: "64px", accentColor: COL.slider, cursor: "pointer" });
        ghostSl.title = "opacity of the footage filmstrip behind the timeline (0 hides it)";
        ghostSl.addEventListener("input", () => {
            node.properties = node.properties || {};
            node.properties.h3_ghost_opacity = parseInt(ghostSl.value, 10) / 100;
            renderTrack();
        });
        ghostSl.addEventListener("pointerdown", (ev) => ev.stopPropagation());
        ghostWrap.appendChild(ghostSl);
        const v2vDenoise = el("span", {
            color: COL.mid, fontSize: "12px", display: "none", whiteSpace: "nowrap",
        }, "→ set denoise 0.3–0.7 on your KSampler");
        v2vDenoise.title = "the restyle amount lives on the SAMPLER, not this node: denoise 1.0 ignores the footage entirely, ~0.3 barely touches it, 0.4–0.7 restyles while keeping the motion";
        v2vBar.append(v2vLabel, v2vNote,
            el("span", { color: COL.text, fontSize: "11px" }, "section"),
            v2vStart, el("span", { color: COL.text, fontSize: "11px" }, "→"), v2vEnd,
            el("span", { color: COL.text, fontSize: "11px" }, "s"),
            v2vScrub, v2vFrame, ghostWrap, v2vPick, v2vClear, v2vDenoise);

        // ---- motion path: Ken Burns through the model -----------------------
        // Two windows (A=start, B=end) over one image; the chosen curve places
        // N tween waypoints automatically — same file, interpolated framings —
        // so the camera path AND speed are pinned while the model adds life.
        const MP_EASE = {
            "linear": (t) => t,
            "ease-in-out": (t) => t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2,
            "ease-in": (t) => t * t,
            "ease-out": (t) => 1 - (1 - t) * (1 - t),
        };
        function mpLerpWin(A, B, u) {
            return { cx: A.cx + (B.cx - A.cx) * u,
                     cy: A.cy + (B.cy - A.cy) * u,
                     // geometric zoom: linear lerp reads as acceleration
                     z: Math.exp(Math.log(A.z) + (Math.log(B.z) - Math.log(A.z)) * u) };
        }
        function placeMotionPath(name, A, B, count, easeName, strength) {
            if (inputConnected(node, "first_frame") || inputConnected(node, "last_frame")) {
                toast("first/last frame sockets are connected and win over file inputs — disconnect them to place a motion path", true);
                return false;
            }
            if (state.midSpecError) {
                toast("fix the middle spec error first (✎ raw text specs) — placing a path now would overwrite your lines", true);
                return false;
            }
            const F = fc();
            const existing = connectedSlots(node, "middle_frame_").length
                + fileLinesOf("middle_frame_files").length;
            if (existing + count > Math.max(1, F - 2)) {
                toast(`clip too short for ${count} more waypoint(s) at this length`, true);
                return false;
            }
            const E = MP_EASE[easeName] || MP_EASE["ease-in-out"];
            setWidget("first_frame_file", name);
            setWidget("first_frame_crop", formatCropSpec([A]));
            setWidget("last_frame_file", name);
            setWidget("last_frame_crop", formatCropSpec([B]));
            // batched writes (per-item adds would refresh between and clobber)
            const lines = fileLinesOf("middle_frame_files");
            const ents = state.mids.map((m) => ({ frac: m.frac, strength: m.strength, desc: m.desc }));
            const crops = state.midCrops.slice();
            for (let i = 0; i < count; i++) {
                const frac = (i + 1) / (count + 1);           // even in TIME…
                const win = mpLerpWin(A, B, E(frac));          // …curve moves the WINDOW
                lines.push(name);
                ents.push({ frac, strength, desc: "" });
                crops.push(win);
            }
            setWidget("middle_frame_files", lines.join("\n"));
            setWidget("middle_frame_spec", formatMiddleSpec(ents, F));
            setWidget("middle_frame_crops", formatCropSpec(crops));
            refresh(true);
            toast(`motion path placed — ${count} tween waypoint(s), ${easeName}, strength ${strength.toFixed(2)}. Drag any waypoint to hand-tune the curve.`);
            return true;
        }
        function openMotionPath() {
            openPicker((name) => {
                const img = cachedImg(name);
                if (!img) {   // still decoding — retry briefly
                    const wait = () => cachedImg(name) ? mpModal(name, cachedImg(name)) : setTimeout(wait, 200);
                    setTimeout(wait, 150);
                    return;
                }
                mpModal(name, img);
            });
        }
        function mpModal(name, img) {
            const [oW, oH] = outWH();
            // seed from existing framings when re-opening on the same image
            let A = (String(widgetValue(node, "first_frame_file", "")).trim() === name && state.firstCrop)
                ? { ...state.firstCrop } : { cx: 0.3, cy: 0.5, z: 1.0 };
            let B = (String(widgetValue(node, "last_frame_file", "")).trim() === name && state.lastCrop)
                ? { ...state.lastCrop } : { cx: 0.7, cy: 0.5, z: 1.0 };
            let active = "A";
            let anim = null;
            openModal((root) => {
                const panel = el("div", {
                    background: COL.bg, border: `1px solid ${COL.border}`, borderRadius: "8px",
                    display: "flex", flexDirection: "column", overflow: "hidden",
                    fontFamily: "sans-serif", maxWidth: "94vw", maxHeight: "94vh",
                });
                const head = el("div", {
                    display: "flex", alignItems: "center", gap: "10px",
                    padding: "8px 12px", borderBottom: `1px solid ${COL.divider}`,
                });
                head.appendChild(el("span", { color: COL.bright, fontSize: "13px", flex: "1" },
                    `Motion path — drag the A (start) and B (end) windows, wheel zooms · both locked to ${oW}×${oH}`));
                const closeB = el("button", btnStyle, "✕");
                closeB.addEventListener("click", closeModal);
                head.appendChild(closeB);

                const body = el("div", { display: "flex", gap: "10px", padding: "10px" });
                const cnv = el("canvas", { display: "block", cursor: "grab", touchAction: "none",
                    borderRadius: "4px" });
                const maxW = Math.min(window.innerWidth * 0.66, 1100);
                const maxH = Math.min(window.innerHeight * 0.72, 800);
                const fit = Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight);
                const dw = Math.round(img.naturalWidth * fit), dh = Math.round(img.naturalHeight * fit);
                cnv.style.width = dw + "px"; cnv.style.height = dh + "px";
                const dpr = window.devicePixelRatio || 1;
                cnv.width = Math.round(dw * dpr); cnv.height = Math.round(dh * dpr);

                const side = el("div", { display: "flex", flexDirection: "column", gap: "10px", width: "220px" });
                const lens = el("canvas", { display: "block", borderRadius: "4px", background: "#000",
                    border: `1px solid ${COL.border}` });
                lens.width = 352; lens.height = 198;
                lens.style.width = "176px"; lens.style.height = "99px";
                side.appendChild(el("div", { color: COL.text, fontSize: "11px" }, "through the lens:"));
                side.appendChild(lens);
                const mkRow = (label, ctl) => {
                    const r = el("div", { display: "flex", alignItems: "center", gap: "8px" });
                    r.append(el("span", { color: COL.text, fontSize: "12px", width: "72px" }, label), ctl);
                    return r;
                };
                const cntSel = el("select", {
                    background: COL.input, color: COL.bright, border: `1px solid ${COL.border}`,
                    borderRadius: "3px", fontSize: "13px", padding: "3px 8px", cursor: "pointer",
                });
                for (const n of [2, 3, 4, 5, 6, 7, 8]) {
                    const o = el("option", null, String(n)); o.value = String(n); cntSel.appendChild(o);
                }
                cntSel.value = "4";
                const easeSel = el("select", {
                    background: COL.input, color: COL.bright, border: `1px solid ${COL.border}`,
                    borderRadius: "3px", fontSize: "13px", padding: "3px 8px", cursor: "pointer",
                });
                for (const k of Object.keys(MP_EASE)) {
                    const o = el("option", null, k); o.value = k; easeSel.appendChild(o);
                }
                easeSel.value = "ease-in-out";
                const strSl = el("input");
                strSl.type = "range"; strSl.min = "0.2"; strSl.max = "1"; strSl.step = "0.05"; strSl.value = "0.55";
                Object.assign(strSl.style, { flex: "1", accentColor: COL.mid, cursor: "pointer" });
                const strVal = el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "12px" }, "0.55");
                strSl.addEventListener("input", () => strVal.textContent = Number(strSl.value).toFixed(2));
                const strRow = el("div", { display: "flex", alignItems: "center", gap: "8px" });
                strRow.append(el("span", { color: COL.text, fontSize: "12px", width: "72px" }, "tween str"), strSl, strVal);
                // numeric zoom per window — Deforum-style pushes want exact values
                const zField = (get, set, color) => {
                    const f = el("input");
                    Object.assign(f.style, {
                        width: "52px", background: COL.input, color, border: `1px solid ${COL.border}`,
                        borderRadius: "3px", fontSize: "13px", padding: "2px 4px",
                        fontFamily: "monospace", textAlign: "center",
                    });
                    f.value = get().toFixed(2);
                    f.addEventListener("keydown", (ev) => { ev.stopPropagation(); if (ev.key === "Enter") f.blur(); });
                    f.addEventListener("blur", () => {
                        const v = parseFloat(f.value);
                        set(Math.max(1, isFinite(v) ? v : 1));
                        f.value = get().toFixed(2);
                        draw();
                    });
                    return f;
                };
                const zA = zField(() => A.z, (v) => A.z = v, COL.cap);
                const zB = zField(() => B.z, (v) => B.z = v, COL.green);
                const zRow = el("div", { display: "flex", alignItems: "center", gap: "6px" });
                zRow.append(el("span", { color: COL.text, fontSize: "12px", width: "72px" }, "zoom"),
                    el("span", { color: COL.cap, fontSize: "12px" }, "A"), zA,
                    el("span", { color: COL.green, fontSize: "12px" }, "B"), zB,
                    el("span", { color: "#666", fontSize: "11px" }, "×"));
                // anchor strengths, right where the zoom decision is made: a big
                // zoom means a small upscaled end crop — often wants softening
                const capDial = (widget, label) => {
                    const sl = el("input");
                    sl.type = "range"; sl.min = "0"; sl.max = "2"; sl.step = "0.05";
                    sl.value = String(Number(widgetValue(node, widget, 1.0)));
                    Object.assign(sl.style, { flex: "1", accentColor: COL.cap, cursor: "pointer" });
                    const val = el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "12px" },
                        Number(sl.value).toFixed(2));
                    sl.addEventListener("input", () => val.textContent = Number(sl.value).toFixed(2));
                    const r = el("div", { display: "flex", alignItems: "center", gap: "8px" });
                    r.append(el("span", { color: COL.text, fontSize: "12px", width: "72px" }, label), sl, val);
                    return { row: r, sl };
                };
                const startD = capDial("first_frame_strength", "start str");
                const endD = capDial("last_frame_strength", "end str");
                const hint = el("div", { color: "#666", fontSize: "11px", lineHeight: "1.45" },
                    "low strength = the model adds parallax and life along your path; high = a mechanical crop-pan. Ends anchor at the first/last frame strengths.");
                const prevB = el("button", btnStyle, "▶ preview move");
                const placeB = el("button", { ...btnStyle, color: COL.green }, "✦ place on timeline");
                side.append(mkRow("waypoints", cntSel), mkRow("curve", easeSel), zRow,
                    strRow, startD.row, endD.row, prevB, placeB, hint);
                body.append(cnv, side);
                panel.append(head, body);
                root.appendChild(panel);

                const ctx = cnv.getContext("2d");
                const winRect = (w) => {
                    const b = cropBoxJS(img.naturalWidth, img.naturalHeight, oW, oH, w.z, w.cx, w.cy);
                    const k = cnv.width / img.naturalWidth;
                    return { x: b.x * k, y: b.y * k, w: b.w * k, h: b.h * k, src: b };
                };
                const drawLens = (w) => {
                    const b = cropBoxJS(img.naturalWidth, img.naturalHeight, oW, oH, w.z, w.cx, w.cy);
                    const lc = lens.getContext("2d");
                    lc.drawImage(img, b.x, b.y, b.w, b.h, 0, 0, lens.width, lens.height);
                };
                const draw = (movingWin) => {
                    ctx.clearRect(0, 0, cnv.width, cnv.height);
                    ctx.drawImage(img, 0, 0, cnv.width, cnv.height);
                    ctx.fillStyle = "rgba(0,0,0,0.45)";
                    ctx.fillRect(0, 0, cnv.width, cnv.height);
                    for (const [w2, tag, color] of [[A, "A", COL.cap], [B, "B", COL.green]]) {
                        const r = winRect(w2);
                        ctx.drawImage(img,
                            r.src.x, r.src.y, r.src.w, r.src.h, r.x, r.y, r.w, r.h);
                        ctx.strokeStyle = color;
                        ctx.lineWidth = active === tag && !movingWin ? 3 : 1.5;
                        ctx.setLineDash(active === tag && !movingWin ? [8, 5] : []);
                        ctx.strokeRect(r.x, r.y, r.w, r.h);
                        ctx.setLineDash([]);
                        ctx.font = "bold 16px monospace";
                        ctx.fillStyle = color;
                        ctx.fillText(tag, r.x + 8, r.y + 22);
                    }
                    // the path the window's CENTER travels
                    const ra = winRect(A), rb = winRect(B);
                    ctx.strokeStyle = "rgba(255,255,255,0.5)";
                    ctx.setLineDash([4, 6]);
                    ctx.beginPath();
                    ctx.moveTo(ra.x + ra.w / 2, ra.y + ra.h / 2);
                    ctx.lineTo(rb.x + rb.w / 2, rb.y + rb.h / 2);
                    ctx.stroke();
                    ctx.setLineDash([]);
                    // tween markers along the curve
                    const E = MP_EASE[easeSel.value];
                    const n = parseInt(cntSel.value, 10);
                    for (let i = 1; i <= n; i++) {
                        const w2 = mpLerpWin(A, B, E(i / (n + 1)));
                        const r = winRect(w2);
                        ctx.fillStyle = COL.mid;
                        ctx.beginPath();
                        ctx.arc(r.x + r.w / 2, r.y + r.h / 2, 4, 0, Math.PI * 2);
                        ctx.fill();
                    }
                    if (movingWin) {
                        const r = winRect(movingWin);
                        ctx.strokeStyle = COL.sel;
                        ctx.lineWidth = 2.5;
                        ctx.strokeRect(r.x, r.y, r.w, r.h);
                        drawLens(movingWin);
                    } else {
                        drawLens(active === "A" ? A : B);
                    }
                };
                cntSel.addEventListener("change", () => draw());
                easeSel.addEventListener("change", () => draw());

                let dragW = null;
                const evPt = (ev) => {
                    const r = cnv.getBoundingClientRect();
                    return { x: (ev.clientX - r.left) / r.width * cnv.width,
                             y: (ev.clientY - r.top) / r.height * cnv.height };
                };
                const winAt = (p) => {
                    const inside = (w2) => {
                        const r = winRect(w2);
                        return p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h;
                    };
                    const inA = inside(A), inB = inside(B);
                    if (inA && inB) {   // overlapping: nearer center wins
                        const ra = winRect(A), rb = winRect(B);
                        const da = (p.x - ra.x - ra.w / 2) ** 2 + (p.y - ra.y - ra.h / 2) ** 2;
                        const db = (p.x - rb.x - rb.w / 2) ** 2 + (p.y - rb.y - rb.h / 2) ** 2;
                        return da <= db ? "A" : "B";
                    }
                    return inA ? "A" : inB ? "B" : null;
                };
                cnv.addEventListener("pointerdown", (ev) => {
                    stopAnim();
                    const p = evPt(ev);
                    const tag = winAt(p);
                    if (!tag) return;
                    active = tag;
                    const w2 = tag === "A" ? A : B;
                    dragW = { tag, cx: w2.cx, cy: w2.cy, x0: p.x, y0: p.y };
                    cnv.setPointerCapture(ev.pointerId);
                    draw();
                });
                cnv.addEventListener("pointermove", (ev) => {
                    if (!dragW) return;
                    const p = evPt(ev);
                    const w2 = dragW.tag === "A" ? A : B;
                    w2.cx = Math.min(1, Math.max(0, dragW.cx + (p.x - dragW.x0) / cnv.width));
                    w2.cy = Math.min(1, Math.max(0, dragW.cy + (p.y - dragW.y0) / cnv.height));
                    draw();
                });
                const endW = (ev) => {
                    if (!dragW) return;
                    dragW = null;
                    try { cnv.releasePointerCapture(ev.pointerId); } catch (e) { /* released */ }
                };
                cnv.addEventListener("pointerup", endW);
                cnv.addEventListener("pointercancel", endW);
                cnv.addEventListener("wheel", (ev) => {
                    ev.preventDefault();
                    stopAnim();
                    const tag = winAt(evPt(ev)) || active;
                    active = tag;
                    const w2 = tag === "A" ? A : B;
                    w2.z = Math.max(1, w2.z * Math.exp(-ev.deltaY * (ev.shiftKey ? 0.00015 : 0.0006)));
                    (tag === "A" ? zA : zB).value = w2.z.toFixed(2);
                    draw();
                }, { passive: false });

                const stopAnim = () => {
                    if (anim) { cancelAnimationFrame(anim); anim = null; prevB.textContent = "▶ preview move"; }
                };
                prevB.addEventListener("click", () => {
                    if (anim) { stopAnim(); draw(); return; }
                    prevB.textContent = "⏹ stop";
                    const durMs = Math.min(6000, Math.max(1500, fc() / FPS * 1000));
                    const t0 = performance.now();
                    const stepA = (now) => {
                        const t = ((now - t0) % durMs) / durMs;
                        draw(mpLerpWin(A, B, MP_EASE[easeSel.value](t)));
                        anim = requestAnimationFrame(stepA);
                    };
                    anim = requestAnimationFrame(stepA);
                });
                placeB.addEventListener("click", () => {
                    stopAnim();
                    setWidget("first_frame_strength", Number(startD.sl.value));
                    setWidget("last_frame_strength", Number(endD.sl.value));
                    if (placeMotionPath(name, A, B, parseInt(cntSel.value, 10),
                        easeSel.value, Number(strSl.value))) closeModal();
                });
                draw();
            }, () => { if (anim) cancelAnimationFrame(anim); });
        }

        // scrub the v2v footage and set in/out points visually; widgets stay
        // the source of truth (every set writes v2v_start/end_seconds)
        function openV2vSection() {
            const vf = String(widgetValue(node, "v2v_video_file", "")).trim();
            if (!vf) return;
            const vv = document.createElement("video");
            vv.preload = "auto";
            vv.volume = 0.5;
            vv.src = inputFileUrl(vf);
            openModal((root) => {
                const panel = el("div", {
                    background: COL.bg, border: `1px solid ${COL.border}`, borderRadius: "8px",
                    display: "flex", flexDirection: "column", overflow: "hidden",
                    fontFamily: "sans-serif", maxWidth: "92vw",
                });
                const head = el("div", {
                    display: "flex", alignItems: "center", gap: "10px",
                    padding: "8px 12px", borderBottom: `1px solid ${COL.divider}`,
                });
                head.appendChild(el("span", { color: COL.bright, fontSize: "13px", flex: "1" },
                    "Pick the section to restyle — scrub, then set the in/out points"));
                const doneB = el("button", { ...btnStyle, color: COL.green }, "done");
                doneB.addEventListener("click", closeModal);
                head.appendChild(doneB);

                Object.assign(vv.style, {
                    display: "block", maxWidth: "min(880px, 88vw)", maxHeight: "56vh",
                    background: "#000",
                });
                const strip = el("div", {
                    position: "relative", height: "34px", margin: "10px 12px 4px",
                    background: "#101010", border: `1px solid ${COL.border}`,
                    borderRadius: "4px", cursor: "pointer",
                });
                const selBand = el("div", {
                    position: "absolute", top: "0", bottom: "0",
                    background: "rgba(158,228,147,0.25)",
                    borderLeft: `2px solid ${COL.green}`, borderRight: `2px solid ${COL.green}`,
                    pointerEvents: "none",
                });
                const playHead = el("div", {
                    position: "absolute", top: "0", bottom: "0", width: "2px",
                    background: COL.bright, pointerEvents: "none",
                });
                strip.append(selBand, playHead);

                const bar = el("div", {
                    display: "flex", gap: "8px", alignItems: "center",
                    padding: "6px 12px 12px", flexWrap: "wrap",
                });
                const playB = el("button", btnStyle, "▶");
                const inB = el("button", btnStyle, "⟦ start here");
                const outB = el("button", btnStyle, "end here ⟧");
                const wholeB = el("button", btnStyle, "whole clip");
                const readout = el("span", { color: COL.text, fontSize: "12px", flex: "1" });
                bar.append(playB, inB, outB, wholeB, readout);
                panel.append(head, vv, strip, bar);
                root.appendChild(panel);

                const dur = () => (isFinite(vv.duration) && vv.duration > 0) ? vv.duration : 0;
                const getS = () => Number(widgetValue(node, "v2v_start_seconds", 0)) || 0;
                const getE = () => Number(widgetValue(node, "v2v_end_seconds", 0)) || 0;
                const paint = () => {
                    const d = dur();
                    const s = getS(), e0 = getE();
                    const e = e0 <= 0 ? d : Math.min(e0, d);
                    if (d) {
                        selBand.style.left = (s / d * 100) + "%";
                        selBand.style.width = (Math.max(0, e - s) / d * 100) + "%";
                        playHead.style.left = (vv.currentTime / d * 100) + "%";
                    }
                    readout.textContent = d
                        ? `${vv.currentTime.toFixed(1)}s / ${d.toFixed(1)}s · section ${s.toFixed(1)}s → ${e0 <= 0 ? "end" : e0.toFixed(1) + "s"} (${Math.max(0, e - s).toFixed(1)}s ≈ ${Math.round(Math.max(0, e - s) * FPS)}f)`
                        : "reading footage…";
                };
                const seekTo = (ev) => {
                    const r = strip.getBoundingClientRect();
                    if (dur()) vv.currentTime = Math.min(Math.max((ev.clientX - r.left) / r.width, 0), 1) * dur();
                };
                strip.addEventListener("pointerdown", (ev) => {
                    seekTo(ev);
                    strip.setPointerCapture(ev.pointerId);
                });
                strip.addEventListener("pointermove", (ev) => { if (ev.buttons) seekTo(ev); });
                vv.addEventListener("timeupdate", paint);
                vv.addEventListener("loadedmetadata", paint);
                vv.addEventListener("error", () => {
                    readout.textContent = "couldn't decode this footage in the browser — set the section by typing seconds instead";
                });
                playB.addEventListener("click", () => {
                    if (vv.paused) { vv.play(); playB.textContent = "⏸"; }
                    else { vv.pause(); playB.textContent = "▶"; }
                });
                inB.addEventListener("click", () => {
                    const t = Math.round(vv.currentTime * 10) / 10;
                    setWidget("v2v_start_seconds", t);
                    const e0 = getE();
                    if (e0 > 0 && e0 <= t) setWidget("v2v_end_seconds", 0);
                    paint(); refresh(false);
                });
                outB.addEventListener("click", () => {
                    const t = Math.round(vv.currentTime * 10) / 10;
                    if (t < 0.1) {
                        // 0 is the "to the end" sentinel — an out-point here would
                        // silently select the whole clip
                        readout.textContent = "scrub forward first — an end point at 0s would mean 'to the end'";
                        return;
                    }
                    setWidget("v2v_end_seconds", t);
                    if (getS() >= t) setWidget("v2v_start_seconds", 0);
                    paint(); refresh(false);
                });
                wholeB.addEventListener("click", () => {
                    setWidget("v2v_start_seconds", 0);
                    setWidget("v2v_end_seconds", 0);
                    paint(); refresh(false);
                });
                paint();
            }, () => {
                vv.pause();
                vv.removeAttribute("src");
                vv.load();
                state.fs?.fill?.();
            });
        }

        const stripHead = el("div", {
            ...sectionHeadStyle(), display: "flex", justifyContent: "space-between",
            gap: "16px", alignItems: "center",
        });
        stripHead.appendChild(el("span", null, "KEYFRAMES — the images the clip passes through"));
        const pathBtn = el("button", btnStyle, "✦ motion path…");
        pathBtn.title = "cinematic pan/zoom from ONE image: set a start and end window, pick a speed curve, and tween waypoints are placed on the timeline automatically";
        pathBtn.addEventListener("click", () => openMotionPath());
        stripHead.appendChild(pathBtn);
        const strip = el("div", {
            display: "flex", gap: "12px", padding: "8px 16px 16px", overflowX: "auto",
            flex: "0 0 auto", alignItems: "flex-start",
        });
        const trackHead = el("div", {
            ...sectionHeadStyle(), display: "flex", justifyContent: "space-between",
            gap: "16px", alignItems: "baseline",
        });
        trackHead.appendChild(el("span", null,
            "TIMELINE — markers move in time; the square cap on a stem sets strength"));
        const trackCtl = el("span", { display: "inline-flex", gap: "6px", alignItems: "center" });
        trackCtl.appendChild(el("span", { fontSize: "11px", color: COL.text }, "beats mode"));
        const modeSel = miniSelect("timed_text_mode", ["text only", "rope + text", "rope only"],
            "How timed beats reach the model. 'text only' (safe): plain prose like 'At 2.0 seconds: …'. "
            + "'rope + text': the same prose AND the tokens are moved onto the video timeline (experimental). "
            + "'rope only': positions only, no time words — the purest test, most likely to fail. Try text only first.");
        trackCtl.appendChild(modeSel);
        trackHead.appendChild(trackCtl);
        const track = el("canvas", { width: "100%", height: "210px", display: "block", flex: "0 0 auto", touchAction: "none" });
        const refsHead = el("div", {
            ...sectionHeadStyle(), display: "flex", justifyContent: "space-between",
            gap: "16px", alignItems: "baseline",
        });
        refsHead.appendChild(el("span", null,
            "REFERENCES — not on the timeline; they define the subject for the whole clip. 1.0 locks identity, 0.7 is a likeness hint"));
        const refsCtl = el("span", { display: "inline-flex", gap: "6px", alignItems: "center", whiteSpace: "nowrap" });
        const castB = el("button", { ...btnStyle, padding: "1px 8px", fontSize: "11px" }, "\u{1F3AD} cast");
        castB.title = "save the current references as a named cast member, or add a saved one — persists across workflows";
        castB.addEventListener("click", openCastModal);
        refsCtl.appendChild(castB);
        refsCtl.appendChild(el("span", { fontSize: "12px", color: COL.text }, "sizing"));
        const sizeSel = miniSelect("ref_image_size", ["match", "max"],
            "'match' scales references to the generation's pixel area; 'max' keeps a 2048px short edge for best identity — several times slower, since reference rows ride every sampling step. Ignored when the MP cap is set.");
        refsCtl.appendChild(sizeSel);
        refsCtl.appendChild(el("span", { fontSize: "12px", color: COL.text }, "MP cap"));
        const mpNum = miniNum("ref_megapixels",
            "Cap each reference at this many megapixels (e.g. 0.4). Overrides sizing when above 0; scales down only. 0 = off.");
        refsCtl.appendChild(mpNum);
        const maskChk = el("input");
        maskChk.type = "checkbox";
        maskChk.style.cursor = "pointer";
        maskChk.title = "mask_ref_pixels: when ON, a reference mask also greys the image the text encoder sees, so it describes only the kept region. OFF (default): only the condition latent is masked.";
        maskChk.addEventListener("change", () => { setWidget("mask_ref_pixels", maskChk.checked); fill(); });
        const maskWrap = el("label", { display: "inline-flex", gap: "5px", alignItems: "center", fontSize: "12px", color: COL.text, cursor: "pointer" });
        maskWrap.append(maskChk, el("span", null, "mask→pixels"));
        refsCtl.appendChild(maskWrap);
        const costMeter = el("span", { fontFamily: "monospace", fontSize: "11px", whiteSpace: "nowrap" });
        refsCtl.appendChild(costMeter);
        refsHead.appendChild(refsCtl);

        // per-step cost estimate: reference rows ride through EVERY sampling step.
        // Mirrors encode_ref_image sizing + PackedLayout row counts (verified
        // earlier: match ≈1014 rows, max ≈5828 vs a 37296-row 5s target).
        function updateCostMeter() {
            const [oW, oH] = outWH();
            const F = fc();
            const targetRows = videoLatentT(F) * Math.floor(oH / 16 / 2) * Math.floor(oW / 16 / 2);
            const sizeMode = widgetValue(node, "ref_image_size", "match");
            const mp = Number(widgetValue(node, "ref_megapixels", 0)) || 0;
            let rows = 0, approx = false;
            for (const r of state.refs) {
                const img = refImg(r);
                let w = img?.naturalWidth, h = img?.naturalHeight;
                if (!w) { w = oW; h = oH; approx = true; }  // no preview: assume match-size
                let scale;
                if (mp > 0) scale = Math.min(1, Math.sqrt((mp * 1e6) / (w * h)));
                else if (sizeMode === "match") scale = Math.min(1, Math.sqrt((oW * oH) / (w * h)));
                else scale = Math.min(1, 2048 / Math.min(w, h));
                const tw = Math.max(32, Math.round(w * scale / 32) * 32);
                const th = Math.max(32, Math.round(h * scale / 32) * 32);
                rows += Math.floor(th / 16 / 2) * Math.floor(tw / 16 / 2);
            }
            let vidRows = 0;
            const vidMpCap = Number(widgetValue(node, "ref_video_megapixels", 0)) || 0;
            for (const [vi, v] of state.videoRefs.entries()) {
                const meta = v.src.type === "file" ? state.videoMeta.get(v.src.name) : null;
                const dur = meta?.dur || 5;
                let vw = meta?.w || oW, vh = meta?.h || oH;
                const vc = state.videoCrops[vi];
                if (vc) {
                    const box = cropBoxJS(vw, vh, vw, vh, vc.z, vc.cx, vc.cy);
                    vw = box.w; vh = box.h;
                }
                approx = true;
                let n = Math.min(Math.floor(Math.min(dur, 15) * FPS), F);
                while (n % 17 !== 5 && n > 5) n--;
                const vt = videoLatentT(n);
                let cw, ch;
                if (vidMpCap > 0) {
                    const s = Math.min(1, Math.sqrt((vidMpCap * 1e6) / (vw * vh)));
                    cw = Math.max(32, Math.round(vw * s / 32) * 32);
                    ch = Math.max(32, Math.round(vh * s / 32) * 32);
                } else {
                    [cw, ch] = adaptCanvasJS(vw, vh);
                    if (vw * vh < cw * ch) {
                        cw = Math.max(32, Math.round(vw / 32) * 32);
                        ch = Math.max(32, Math.round(vh / 32) * 32);
                    }
                }
                vidRows += vt * Math.floor(ch / 16 / 2) * Math.floor(cw / 16 / 2);
            }
            rows += vidRows;
            if (!rows) { costMeter.textContent = ""; return; }
            const pct = rows / targetRows * 100;
            costMeter.textContent = `refs add ${approx ? "≈" : ""}${rows.toLocaleString()} rows (+${pct.toFixed(0)}% per step)`;
            costMeter.style.color = pct > 25 ? COL.mid : COL.text;
            costMeter.title = pct > 25
                ? "references ride through every sampling step — try ref_image_size 'match' or a ref_megapixels cap to cut this"
                : "reference rows processed at every sampling step, on top of the "
                  + targetRows.toLocaleString() + "-row video target (keyframes add a fixed "
                  + Math.floor(oH / 16 / 2) * Math.floor(oW / 16 / 2) + " rows each)";
        }
        const refsRow = el("div", {
            display: "flex", gap: "12px", padding: "8px 16px 16px", overflowX: "auto",
            flex: "0 0 auto", alignItems: "flex-start",
        });
        const vidHead = el("div", {
            ...sectionHeadStyle(), display: "flex", justifyContent: "space-between",
            gap: "16px", alignItems: "baseline",
        });
        vidHead.appendChild(el("span", null,
            "VIDEO REFERENCES — motion + identity for the whole clip (2-15s at 24fps). The heaviest reference type: every frame's rows ride every sampling step"));
        // grouped pill: a bare label+box floating in the header read as stranded
        const vidCtl = el("span", {
            display: "inline-flex", gap: "8px", alignItems: "center", whiteSpace: "nowrap",
            background: COL.panel, border: `1px solid ${COL.border}`,
            borderRadius: "5px", padding: "4px 12px", alignSelf: "center",
        });
        vidCtl.appendChild(el("span", { fontSize: "12px", color: COL.bright }, "⚡ MP cap"));
        const vidMp = miniNum("ref_video_megapixels",
            "Cap reference video frames at this many megapixels (e.g. 0.4). Aspect-preserving, down-only, 32-grid — never a squish. 0 = the model's 768-short-edge canvas rule. The single biggest speed dial in the pack.");
        vidCtl.appendChild(vidMp);
        vidCtl.appendChild(el("span", { fontSize: "11px", color: "#666" }, "0 = auto · the big speed dial"));
        vidHead.appendChild(vidCtl);
        const vidRow = el("div", {
            display: "flex", gap: "12px", padding: "8px 16px 16px", overflowX: "auto",
            flex: "0 0 auto", alignItems: "flex-start",
        });

        async function openVideoPicker(onPick) {
            const fetchVids = async (type) =>
                (await fetchInternalFiles(type)).filter((f) => VIDEO_EXT.test(f));
            let tab = "input";
            let files = await fetchVids(tab);
            openModal((root) => {
                const panel = el("div", {
                    width: "min(680px, 92vw)", maxHeight: "82vh", background: COL.bg,
                    border: `1px solid ${COL.border}`, borderRadius: "8px", display: "flex",
                    flexDirection: "column", overflow: "hidden", fontFamily: "sans-serif",
                });
                const head = el("div", {
                    display: "flex", gap: "8px", alignItems: "center",
                    padding: "8px 10px", borderBottom: `1px solid ${COL.divider}`,
                });
                const list = el("div", { overflowY: "auto", padding: "6px" });
                const tabBtn = (name, label) => {
                    const b = el("button", btnStyle, label);
                    b.addEventListener("click", async () => {
                        tab = name;
                        files = await fetchVids(tab);
                        styleTabs(); fillList();
                    });
                    return b;
                };
                const tIn = tabBtn("input", "input"), tOut = tabBtn("output", "output — chain a render");
                const styleTabs = () => {
                    tIn.style.color = tab === "input" ? COL.bright : COL.text;
                    tOut.style.color = tab === "output" ? COL.bright : COL.text;
                };
                const up = el("button", btnStyle, "upload…");
                const fi = el("input");
                fi.type = "file"; fi.accept = "video/*"; fi.style.display = "none";
                up.addEventListener("click", () => fi.click());
                fi.addEventListener("change", async () => {
                    const f = fi.files?.[0];
                    if (!f) return;
                    try {
                        const name = await uploadBlob(f, f.name);
                        closeModal(); onPick(name);
                    } catch (e) { toast("upload failed: " + e.message, true); }
                });
                const closeB = el("button", btnStyle, "✕");
                closeB.addEventListener("click", closeModal);
                head.append(tIn, tOut, el("span", { flex: "1" }), up, fi, closeB);
                const fillList = () => {
                    list.textContent = "";
                    if (!files.length) list.appendChild(el("div", { color: COL.text, fontSize: "12px", padding: "8px" },
                        tab === "output" ? "no videos in the output folder yet" : "no videos in the input folder — use upload…"));
                    for (const f of files.slice(0, 200)) {
                        const row = el("div", {
                            display: "flex", alignItems: "center", gap: "10px",
                            padding: "5px 8px", borderRadius: "4px", cursor: "pointer",
                        });
                        row.addEventListener("mouseenter", () => row.style.background = "#232323");
                        row.addEventListener("mouseleave", () => row.style.background = "transparent");
                        const vv = el("video");
                        vv.muted = true; vv.preload = "metadata";
                        vv.src = inputFileUrl(f);
                        Object.assign(vv.style, {
                            width: "96px", height: "54px", objectFit: "cover",
                            borderRadius: "3px", background: "#222", pointerEvents: "none",
                        });
                        row.append(vv, el("span", { color: COL.bright, fontSize: "12px", flex: "1" },
                            f.replace(/\s*\[\w+\]\s*$/, "")));
                        row.addEventListener("click", () => { closeModal(); onPick(f); });
                        list.appendChild(row);
                    }
                };
                styleTabs(); fillList();
                panel.append(head, list);
                root.appendChild(panel);
            });
        }

        const audioHead = el("div", sectionHeadStyle(),
            "AUDIO REFERENCES — a voice, room tone or music bed for the whole clip. H3 applies audio references completely clean by default; this strength dial is the only way to soften one");
        const audioRow = el("div", {
            display: "flex", gap: "12px", padding: "8px 16px 16px", overflowX: "auto",
            flex: "0 0 auto", alignItems: "center",
        });
        const helpStrip = el("div", {
            flex: "0 0 auto", borderTop: `1px solid ${COL.divider}`, padding: "6px 16px",
            fontSize: "12px", color: COL.text,
        });
        main.append(promptHead, chipRow, promptTA, v2vBar, stripHead, strip, trackHead, track,
            refsHead, refsRow, vidHead, vidRow, audioHead, audioRow, helpStrip);
        body.append(main, inspector);
        root.append(header, body);

        let helpOpen = false;
        helpBtn.addEventListener("click", () => { helpOpen = !helpOpen; fill(); });

        // OS drag-and-drop: drop images on the filmstrip (waypoints) or refs row,
        // audio on the audio row. Uploaded via the same endpoint the pickers use.
        function makeDropTarget(target, accept, route, color) {
            target.addEventListener("dragover", (ev) => {
                ev.preventDefault(); ev.stopPropagation();
                target.style.outline = `2px dashed ${color}`;
                target.style.outlineOffset = "-4px";
            });
            target.addEventListener("dragleave", () => { target.style.outline = ""; });
            target.addEventListener("drop", async (ev) => {
                ev.preventDefault(); ev.stopPropagation();
                target.style.outline = "";
                const files = [...(ev.dataTransfer?.files || [])];
                if (!files.length) return;
                let taken = 0;
                for (const f of files) {
                    if (!f.type.startsWith(accept)) continue;
                    try {
                        route(await uploadBlob(f, f.name));
                        taken++;
                    } catch (e) { toast("upload failed: " + e.message, true); return; }
                }
                if (!taken) toast(`drop ${accept === "image/" ? "image" : "audio"} files here`, true);
            });
        }
        makeDropTarget(strip, "image/", addFileMid, COL.mid);
        makeDropTarget(refsRow, "image/", addFileRef, COL.green);
        makeDropTarget(vidRow, "video/", addFileVideo, COL.green);
        makeDropTarget(audioRow, "audio/", addFileAudio, COL.green);

        function sectionHeadStyle() {
            return {
                padding: "12px 16px 0", color: COL.text, fontSize: "11px",
                letterSpacing: "0.06em", flex: "0 0 auto",
            };
        }

        // ---- filmstrip cards
        function card(entity) {
            const color = entity.kind === "mid" ? COL.mid : COL.cap;
            const seld = state.sel && (entity.kind === "mid"
                ? (state.sel.kind === "mid" && state.sel.i === entity.i)
                : (entity.kind === state.sel.kind));
            const c = el("div", {
                width: "180px", flex: "0 0 auto", background: COL.panel,
                border: `2px solid ${seld ? COL.sel : COL.border}`, borderRadius: "6px",
                overflow: "hidden", cursor: "pointer",
                opacity: entity.kind === "mid" && state.midsAuto ? "0.65" : "1",
            });
            const entCrop = entity.kind === "mid" ? state.midCrops[entity.i]
                : (entity.kind === "first" ? state.firstCrop : state.lastCrop);
            const th = thumbEl(entity.img, 176, 99,
                entity.connected ? "socket — run to preview"
                    : (entity.file && cachedImgFailed(entity.file) ? "⚠ couldn't load" : "loading…"),
                entCrop, outWH());
            const foot = el("div", { padding: "6px 8px", display: "flex", flexDirection: "column", gap: "3px" });
            const row1 = el("div", { display: "flex", justifyContent: "space-between", alignItems: "center" });
            row1.append(
                el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "13px" },
                    entity.kind === "first" ? "0.0s" : timeOf(entity.frac).toFixed(1) + "s"),
                picChip(entity.pic, color));
            const row2 = el("div", { display: "flex", alignItems: "center", gap: "6px" });
            const sVal = el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "12px" },
                entity.strength.toFixed(2));
            const sSl = el("input");
            sSl.type = "range"; sSl.min = "0"; sSl.max = "2"; sSl.step = "0.05";
            sSl.value = String(entity.strength);
            Object.assign(sSl.style, { flex: "1", accentColor: color, cursor: "pointer" });
            sSl.title = "strength — how hard this frame is enforced (also draggable as the stem on the track)";
            sSl.addEventListener("input", () => {
                const v = Math.round(parseFloat(sSl.value) * 20) / 20;
                sVal.textContent = v.toFixed(2);
                if (entity.kind === "mid") { state.mids[entity.i].strength = v; pushMids(); }
                else setWidget(entity.kind + "_frame_strength", v);
                refresh(false);
            });
            sSl.addEventListener("change", () => fill());
            sSl.addEventListener("pointerdown", (ev) => ev.stopPropagation());
            row2.append(sVal, sSl);
            const row3 = el("div", { display: "flex", justifyContent: "space-between", alignItems: "center" });
            const srcTxt = entity.connected
                ? (entity.kind === "mid" ? `SOCKET m${state.mids[entity.i].src.slot}` : "SOCKET")
                : `FILE ${entity.file?.length > 12 ? entity.file.slice(0, 10) + "…" : entity.file || ""}`;
            row3.appendChild(el("span", { color: COL.text, fontSize: "10px", textTransform: "uppercase" }, srcTxt));
            const fr = el("button", { ...btnStyle, padding: "0 6px", fontSize: "11px",
                color: entCrop ? COL.green : COL.bright }, "⛶");
            fr.title = entCrop ? "framed — click to adjust" : "frame this image (zoom/crop what the model sees)";
            fr.addEventListener("click", (ev) => {
                ev.stopPropagation();
                openFramer(entity.kind === "mid" ? { kind: "mid", i: entity.i } : { kind: entity.kind });
            });
            row3.appendChild(fr);
            if (!entity.connected) {
                const x = el("button", { ...btnStyle, color: COL.red, padding: "0 6px", fontSize: "11px" }, "✕");
                x.title = "remove this frame";
                x.addEventListener("click", (ev) => {
                    ev.stopPropagation();
                    if (entity.kind === "mid") removeFileMid(entity.i);
                    else {
                        setWidget(entity.kind + "_frame_file", "");
                        setWidget(entity.kind + "_frame_crop", "");
                        state.sel = null; refresh(true);
                    }
                });
                row3.appendChild(x);
            } else row3.appendChild(el("span", { color: "#555", fontSize: "10px" }, "via graph"));
            foot.append(row1, row2, row3);
            // unframed + aspect mismatch = silent distortion at encode time
            // (first frame stretches, waypoints/last center-crop — core's own
            // conventions). Announce it and make the fix one click.
            if (!entCrop && entity.img?.naturalWidth && effWH()) {
                const [oW, oH] = effWH();
                const ia = entity.img.naturalWidth / entity.img.naturalHeight;
                if (Math.abs(ia / (oW / oH) - 1) > 0.02) {
                    const warn = el("div", {
                        color: COL.mid, fontSize: "10px", cursor: "pointer",
                        whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                    }, entity.kind === "first"
                        ? "⚠ aspect differs — will STRETCH · ⛶ to choose"
                        : "⚠ aspect differs — will edge-crop · ⛶ to choose");
                    warn.title = "this image is "
                        + entity.img.naturalWidth + "×" + entity.img.naturalHeight
                        + " but the clip is " + oW + "×" + oH + " — without a framing the model "
                        + (entity.kind === "first"
                            ? "stretches it to fit (squish)."
                            : "center-crops it (edges lost).")
                        + " Click to frame it yourself.";
                    warn.addEventListener("click", (ev) => {
                        ev.stopPropagation();
                        openFramer(entity.kind === "mid" ? { kind: "mid", i: entity.i } : { kind: entity.kind });
                    });
                    foot.appendChild(warn);
                }
            }
            if (entity.kind === "mid" && entity.desc)
                foot.appendChild(el("div", {
                    color: COL.text, fontSize: "11px", whiteSpace: "nowrap",
                    overflow: "hidden", textOverflow: "ellipsis",
                }, entity.desc));
            if (entity.kind === "mid" && state.midsAuto)
                foot.appendChild(el("div", { color: COL.text, fontSize: "10px" }, "AUTO — drag to own it"));
            c.append(th, tagCaption(`<Picture ${entity.pic}>`,
                entity.kind === "mid" ? COL.mid : COL.cap), foot);
            c.addEventListener("click", () => {
                state.sel = entity.kind === "mid" ? { kind: "mid", i: entity.i } : { kind: entity.kind };
                fill();
            });
            th.addEventListener("dblclick", () => openLightbox(entity.img));
            c.addEventListener("contextmenu", (ev) => {
                ev.preventDefault(); ev.stopPropagation();
                imageMenu(entity.kind === "mid" ? { kind: "mid", i: entity.i } : { kind: entity.kind }, ev);
            });
            return c;
        }

        function ghostCard(label, w, h, onClick) {
            const g = el("div", {
                width: w + "px", height: h + "px", flex: "0 0 auto",
                border: `2px dashed ${COL.slider}`, borderRadius: "6px",
                display: "flex", alignItems: "center", justifyContent: "center",
                color: COL.text, fontSize: "12px", cursor: "pointer", textAlign: "center",
            }, label);
            g.addEventListener("click", onClick);
            return g;
        }

        // ---- references cards
        function refCard(r, i) {
            const seld = state.sel?.kind === "ref" && state.sel.i === i;
            const c = el("div", {
                width: "140px", flex: "0 0 auto", background: COL.panel,
                border: `2px solid ${seld ? COL.sel : COL.border}`, borderRadius: "6px",
                overflow: "hidden", cursor: "pointer",
                opacity: state.refsAuto ? "0.8" : "1",
            });
            const th = thumbEl(refImg(r), 136, 136,
                r.src.type === "socket" ? "socket"
                    : (cachedImgFailed(r.src.name) ? "⚠ couldn't load" : "loading…"),
                state.refCrops[i], null);
            const foot = el("div", { padding: "6px 8px", display: "flex", flexDirection: "column", gap: "4px" });
            const row1 = el("div", { display: "flex", justifyContent: "space-between", alignItems: "center" });
            const frB = el("button", { ...btnStyle, padding: "0 6px", fontSize: "11px",
                color: state.refCrops[i] ? COL.green : COL.bright }, "⛶");
            frB.title = state.refCrops[i] ? "framed — click to adjust" : "frame this reference";
            frB.addEventListener("click", (ev) => { ev.stopPropagation(); openFramer({ kind: "ref", i }); });
            const rv = el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "12px" }, r.strength.toFixed(2));
            row1.append(picChip(r._pic, COL.green), frB, rv);
            const slider = el("input");
            slider.type = "range"; slider.min = "0"; slider.max = "2"; slider.step = "0.05";
            slider.value = String(r.strength);
            Object.assign(slider.style, { width: "100%", accentColor: COL.green, cursor: "pointer" });
            // fill() on input would destroy the slider mid-drag (bug-hunt finding)
            slider.addEventListener("input", () => {
                r.strength = parseFloat(slider.value);
                rv.textContent = r.strength.toFixed(2);
                pushRefs(); refresh(false);
            });
            slider.addEventListener("change", () => fill());
            slider.addEventListener("pointerdown", (ev) => ev.stopPropagation());
            const row3 = el("div", { display: "flex", justifyContent: "space-between", alignItems: "center" });
            const srcLabel = el("span", { color: COL.text, fontSize: "10px", textTransform: "uppercase" },
                r.src.type === "socket" ? `SOCKET ref${r.src.slot}` : "FILE");
            row3.appendChild(srcLabel);
            if (r.src.type === "socket" && inputConnected(node, "ref_mask_" + r.src.slot)) {
                const mB = el("span", {
                    color: COL.mid, fontSize: "10px", border: `1px solid ${COL.mid}`,
                    borderRadius: "3px", padding: "0 3px",
                }, "M");
                mB.title = "a mask is connected to this reference (white keeps, black drops)";
                row3.appendChild(mB);
            }
            if (r.src.type === "file") {
                const x = el("button", { ...btnStyle, color: COL.red, padding: "0 6px", fontSize: "11px" }, "✕");
                x.addEventListener("click", (ev) => { ev.stopPropagation(); removeFileRef(i); });
                row3.appendChild(x);
            }
            foot.append(row1, slider, row3);
            c.append(th, tagCaption(`<Picture ${r._pic}>`, COL.green), foot);
            c.addEventListener("click", () => { state.sel = { kind: "ref", i }; fill(); });
            th.addEventListener("dblclick", () => openLightbox(refImg(r)));
            c.addEventListener("contextmenu", (ev) => {
                ev.preventDefault(); ev.stopPropagation();
                imageMenu({ kind: "ref", i }, ev);
            });
            return c;
        }

        // ---- inspector
        function fieldLabel(t) { return el("div", { color: COL.text, fontSize: "12px", margin: "16px 0 4px" }, t); }

        function strengthField(get, set, isRef) {
            const wrap = el("div");
            const row = el("div", { display: "flex", alignItems: "center", gap: "8px" });
            const slider = el("input");
            slider.type = "range"; slider.min = "0"; slider.max = "2"; slider.step = "0.05";
            slider.value = String(get());
            Object.assign(slider.style, { flex: "1", accentColor: COL.mid, cursor: "pointer" });
            const val = el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "13px", width: "38px" },
                get().toFixed(2));
            const cap = el("div", { color: COL.text, fontSize: "12px", marginTop: "6px", lineHeight: "1.4" },
                strengthCaption(get(), isRef));
            slider.addEventListener("input", () => {
                set(parseFloat(slider.value));
                val.textContent = parseFloat(slider.value).toFixed(2);
                cap.textContent = strengthCaption(parseFloat(slider.value), isRef);
                refresh(false);   // live, but don't rebuild the DOM under the slider
            });
            slider.addEventListener("change", () => fill());
            row.append(slider, val);
            const scale = el("div", {
                display: "flex", justifyContent: "space-between", color: "#666",
                fontSize: "10px", fontFamily: "monospace",
            });
            scale.append(el("span", null, "0"), el("span", null, "1.0"), el("span", null, "2"));
            wrap.append(row, scale, cap);
            return wrap;
        }

        function timeField(getFrac, setFrac, lockLabel) {
            const wrap = el("div", { display: "flex", gap: "8px", alignItems: "center" });
            if (lockLabel) {
                wrap.appendChild(el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "13px" }, lockLabel));
                return wrap;
            }
            const input = el("input");
            input.value = timeOf(getFrac()).toFixed(2) + "s";
            Object.assign(input.style, {
                width: "90px", background: COL.input, color: COL.bright,
                border: `1px solid ${COL.border}`, borderRadius: "3px",
                padding: "3px 8px", fontSize: "13px", fontFamily: "monospace",
            });
            const frame = el("span", { color: COL.text, fontSize: "12px", fontFamily: "monospace" },
                "f" + frameOf(getFrac()));
            input.addEventListener("keydown", (ev) => {
                ev.stopPropagation();
                if (ev.key !== "Enter") return;
                const raw = input.value.trim().toLowerCase();
                const F = fc();
                let idx;
                if (raw.endsWith("s")) idx = Math.round(parseFloat(raw) * FPS);
                else if (raw.includes(".") && parseFloat(raw) <= 1) idx = Math.round(parseFloat(raw) * (F - 1));
                else idx = Math.round(parseFloat(raw));
                if (!isFinite(idx)) return;
                setFrac(Math.min(1, Math.max(0, idx / (F - 1))));
                fill();
            });
            input.addEventListener("blur", () => { input.value = timeOf(getFrac()).toFixed(2) + "s"; });
            wrap.append(input, frame, el("span", { color: "#666", fontSize: "11px" }, "type 1.7s or a frame № + Enter"));
            return wrap;
        }

        function buildInspector() {
            inspector.textContent = "";
            const s = state.sel;
            const ents = state._ents || [];
            if (!s) {
                // idle: the legend + help live here, so the space always earns its keep
                inspector.appendChild(el("div", { color: COL.bright, fontSize: "13px", marginBottom: "6px" },
                    "Picture numbers"));
                inspector.appendChild(el("div", { color: COL.text, fontSize: "12px", lineHeight: "1.4", marginBottom: "12px" },
                    "Every image gets a number, in this order. Write <Picture 2> in your prompt to talk about that image."));
                const list = el("div", { display: "flex", flexDirection: "column", gap: "6px" });
                for (const e of ents) {
                    const row = el("div", { display: "flex", gap: "8px", alignItems: "center" });
                    row.append(thumbEl(e.img, 40, 26, ""), picChip(e.pic, e.kind === "mid" ? COL.mid : COL.cap),
                        el("span", { color: COL.text, fontSize: "12px", flex: "1" },
                            e.kind === "mid" ? `waypoint @ ${timeOf(e.frac).toFixed(1)}s` : e.kind + " frame"));
                    list.appendChild(row);
                }
                state.refs.forEach((r) => {
                    const row = el("div", { display: "flex", gap: "8px", alignItems: "center" });
                    row.append(thumbEl(refImg(r), 40, 26, ""), picChip(r._pic, COL.green),
                        el("span", { color: COL.text, fontSize: "12px", flex: "1" }, "reference (whole clip)"));
                    list.appendChild(row);
                });
                (state._numbered?.videos || []).forEach((vch) => {
                    const row = el("div", { display: "flex", gap: "8px", alignItems: "center" });
                    row.append(el("span", { width: "40px", textAlign: "center", color: COL.green }, "▶"),
                        picChip("V" + vch.num, COL.green),
                        el("span", { color: COL.text, fontSize: "12px", flex: "1" },
                            "video reference" + (vch.aNum ? ` (soundtrack A${vch.aNum})` : "")));
                    list.appendChild(row);
                });
                for (const a of (state._numbered?.audio || [])) {
                    const row = el("div", { display: "flex", gap: "8px", alignItems: "center" });
                    row.append(el("span", { width: "40px", textAlign: "center", color: COL.green }, "♪"),
                        picChip(`A${a.num}${a.uncertain ? "~" : ""}`, COL.green),
                        el("span", { color: COL.text, fontSize: "12px" }, a.label));
                    list.appendChild(row);
                }
                if (!ents.length && !state.refs.length)
                    list.appendChild(el("div", { color: "#666", fontSize: "12px" },
                        "Nothing yet — add keyframes and references on the left."));
                inspector.appendChild(list);
                inspector.appendChild(el("div", { borderTop: `1px solid ${COL.divider}`, margin: "14px 0" }));
                for (const [h, t] of HELP_COPY) {
                    inspector.appendChild(el("div", { color: COL.bright, fontSize: "12px", marginTop: "10px" }, h));
                    inspector.appendChild(el("div", { color: COL.text, fontSize: "12px", lineHeight: "1.45" }, t));
                }
                return;
            }

            const addPreview = (img, crop, aspect) => {
                const p = thumbEl(img, 328, 200, "no preview yet", crop, aspect);
                p.style.cursor = "zoom-in";
                p.addEventListener("click", () => openLightbox(img));
                inspector.appendChild(p);
            };

            const framingField = (sel2, isRef) => {
                const wrap = el("div", { display: "flex", gap: "8px", alignItems: "center" });
                const crop = cropOf(sel2);
                const b = el("button", { ...btnStyle, color: crop ? COL.green : COL.bright },
                    crop ? `⛶ framed ${crop.z.toFixed(2)}×` : "⛶ frame image");
                b.addEventListener("click", () => openFramer(sel2));
                wrap.appendChild(b);
                if (crop) {
                    const c = el("button", { ...btnStyle, color: COL.red }, "clear");
                    c.addEventListener("click", () => { setCropOf(sel2, null); refresh(true); });
                    wrap.appendChild(c);
                }
                wrap.appendChild(el("span", { color: "#666", fontSize: "11px" },
                    isRef ? "zoom/pan — chooses what the model sees"
                        : "window locked to the output aspect — no stretch, no blind crop"));
                return wrap;
            };

            if (s.kind === "first" || s.kind === "last") {
                const info = capInfo(s.kind);
                if (!info) { state.sel = null; buildInspector(); return; }
                addPreview(info.img, cropOf(s), outWH());
                const e = ents.find((x) => x.kind === s.kind);
                inspector.appendChild(el("div", { color: COL.bright, fontSize: "14px", margin: "10px 0 0" },
                    `${s.kind === "first" ? "First" : "Last"} frame  ·  Picture ${e?.pic ?? "?"}`));
                inspector.appendChild(fieldLabel("time"));
                inspector.appendChild(timeField(null, null, s.kind === "first" ? "0.0s (start)" : timeOf(1).toFixed(1) + "s (end)"));
                inspector.appendChild(fieldLabel("strength"));
                inspector.appendChild(strengthField(
                    () => Number(widgetValue(node, s.kind + "_frame_strength", 1.0)),
                    (v) => setWidget(s.kind + "_frame_strength", Math.round(v * 20) / 20), false));
                inspector.appendChild(fieldLabel("framing"));
                inspector.appendChild(framingField({ kind: s.kind }, false));
                inspector.appendChild(fieldLabel("source"));
                inspector.appendChild(el("div", { color: COL.bright, fontSize: "12px" },
                    info.connected ? "graph socket (disconnect in the graph to remove)" : "file: " + info.file));
                if (!info.connected) {
                    const rep = el("button", { ...btnStyle, marginTop: "16px", marginRight: "8px" }, "⇄ replace image…");
                    rep.title = "new image, same strength and framing";
                    rep.addEventListener("click", () => replaceImage({ kind: s.kind }));
                    const d = el("button", { ...btnStyle, color: COL.red, marginTop: "16px" }, "remove this frame");
                    d.addEventListener("click", deleteSelected);
                    inspector.append(rep, d);
                }
            } else if (s.kind === "mid") {
                const m = state.mids[s.i];
                if (!m) { state.sel = null; buildInspector(); return; }
                addPreview(midImg(m), state.midCrops[s.i], outWH());
                const e = ents.find((x) => x.kind === "mid" && x.i === s.i);
                inspector.appendChild(el("div", { color: COL.bright, fontSize: "14px", margin: "10px 0 0" },
                    `Waypoint  ·  Picture ${e?.pic ?? "?"}`));
                inspector.appendChild(fieldLabel("time"));
                inspector.appendChild(timeField(() => m.frac, (f) => {
                    m.frac = Math.min(Math.max(f, 1 / (fc() - 1)), (fc() - 2) / (fc() - 1));
                    pushMids();
                }));
                inspector.appendChild(fieldLabel("strength"));
                inspector.appendChild(strengthField(() => m.strength,
                    (v) => { m.strength = Math.round(v * 20) / 20; pushMids(); }, false));
                inspector.appendChild(fieldLabel("description — appended to the prompt with its Picture number"));
                const ta = el("textarea");
                ta.value = m.desc;
                ta.rows = 3;
                Object.assign(ta.style, {
                    width: "100%", boxSizing: "border-box", background: COL.input, color: COL.bright,
                    border: `1px solid ${COL.border}`, borderRadius: "3px", padding: "6px",
                    fontSize: "12px", resize: "vertical", fontFamily: "sans-serif",
                });
                ta.addEventListener("keydown", (ev) => ev.stopPropagation());
                ta.addEventListener("input", () => { m.desc = ta.value; pushMids(); refresh(false); });
                inspector.appendChild(ta);
                inspector.appendChild(fieldLabel("framing"));
                inspector.appendChild(framingField({ kind: "mid", i: s.i }, false));
                inspector.appendChild(fieldLabel("source"));
                inspector.appendChild(el("div", { color: COL.bright, fontSize: "12px" }, midName(m)));
                if (m.src.type === "file") {
                    const rep = el("button", { ...btnStyle, marginTop: "16px", marginRight: "8px" }, "⇄ replace image…");
                    rep.title = "new image, same time, strength, description and framing";
                    rep.addEventListener("click", () => replaceImage({ kind: "mid", i: s.i }));
                    const d = el("button", { ...btnStyle, color: COL.red, marginTop: "16px" }, "delete waypoint");
                    d.addEventListener("click", deleteSelected);
                    inspector.append(rep, d);
                }
            } else if (s.kind === "beat") {
                const b = state.beats[s.i];
                if (!b) { state.sel = null; buildInspector(); return; }
                inspector.appendChild(el("div", { color: COL.bright, fontSize: "14px" }, "Beat — timed prompt"));
                inspector.appendChild(el("div", { color: COL.text, fontSize: "12px", lineHeight: "1.4", marginTop: "4px" },
                    "Text pinned to a moment. No image — the words steer what happens here."));
                inspector.appendChild(fieldLabel("time"));
                inspector.appendChild(timeField(() => b.frac, (f) => { b.frac = f; pushBeats(); }));
                inspector.appendChild(fieldLabel("text (required — a red beat isn't saved yet)"));
                const ta = el("textarea");
                ta.value = b.text;
                ta.rows = 3;
                Object.assign(ta.style, {
                    width: "100%", boxSizing: "border-box", background: COL.input, color: COL.bright,
                    border: `1px solid ${b.text.trim() ? COL.border : COL.red}`, borderRadius: "3px",
                    padding: "6px", fontSize: "12px", resize: "vertical", fontFamily: "sans-serif",
                });
                ta.addEventListener("keydown", (ev) => ev.stopPropagation());
                ta.addEventListener("input", () => {
                    b.text = ta.value; pushBeats();
                    ta.style.borderColor = b.text.trim() ? COL.border : COL.red;
                    refresh(false);
                });
                inspector.appendChild(ta);
                const d = el("button", { ...btnStyle, color: COL.red, marginTop: "16px" }, "delete beat");
                d.addEventListener("click", deleteSelected);
                inspector.appendChild(d);
                requestAnimationFrame(() => ta.focus());
            } else if (s.kind === "ref") {
                const r = state.refs[s.i];
                if (!r) { state.sel = null; buildInspector(); return; }
                addPreview(refImg(r), state.refCrops[s.i], null);
                inspector.appendChild(el("div", { color: COL.bright, fontSize: "14px", margin: "10px 0 0" },
                    `Reference  ·  Picture ${r._pic}`));
                inspector.appendChild(el("div", { color: COL.text, fontSize: "12px", lineHeight: "1.4", marginTop: "4px" },
                    "Applies to the whole clip — it defines the subject, not a moment."));
                inspector.appendChild(fieldLabel("strength"));
                inspector.appendChild(strengthField(() => r.strength,
                    (v) => { r.strength = Math.round(v * 20) / 20; pushRefs(); }, true));
                inspector.appendChild(fieldLabel("framing"));
                inspector.appendChild(framingField({ kind: "ref", i: s.i }, true));
                inspector.appendChild(fieldLabel("source"));
                inspector.appendChild(el("div", { color: COL.bright, fontSize: "12px" }, refName(r)));
                if (r.src.type === "file") {
                    const rep = el("button", { ...btnStyle, marginTop: "16px", marginRight: "8px" }, "⇄ replace image…");
                    rep.title = "new image, same strength and framing";
                    rep.addEventListener("click", () => replaceImage({ kind: "ref", i: s.i }));
                    const d = el("button", { ...btnStyle, color: COL.red, marginTop: "16px" }, "delete reference");
                    d.addEventListener("click", deleteSelected);
                    inspector.append(rep, d);
                }
            }
        }

        // ---- track canvas (time + strength handles + beats)
        const trackPad = 40;
        function trackLayout(w) {
            return { x0: trackPad, x1: w - trackPad, ruler: 24, ky: 108, stem: 60, by: 168 };
        }
        const fracToX = (T, f) => T.x0 + f * (T.x1 - T.x0);
        const xToFrac = (T, x) => Math.min(1, Math.max(0, (x - T.x0) / (T.x1 - T.x0)));

        // v2v filmstrip ghost: frames sampled across the selected section,
        // drawn dimmed UNDER the lanes so the timeline shows the footage it is
        // restyling. NLE-style center crops; opacity dial lives in the v2v bar
        // and persists in node.properties (a UI preference, not a widget).
        // exact-frame preview while scrubbing/dragging over v2v footage.
        // Sized like a filmstrip card (176x99, 2x backing store), floats above
        // the track's right edge (over the beats-mode corner), pointer-through.
        let scrubFrac = null;
        const framePrev = el("div", {
            position: "fixed", zIndex: "10015", display: "none",
            background: COL.panel, border: `1px solid ${COL.border}`,
            borderRadius: "6px", overflow: "hidden", pointerEvents: "none",
            boxShadow: "0 6px 24px rgba(0,0,0,0.55)",
        });
        const framePrevCnv = el("canvas", { display: "block", width: "176px", height: "99px", background: "#000" });
        framePrevCnv.width = 352; framePrevCnv.height = 198;
        const framePrevCap = el("div", {
            color: COL.bright, fontSize: "11px", fontFamily: "monospace",
            textAlign: "center", padding: "2px 4px",
        });
        framePrev.append(framePrevCnv, framePrevCap);
        const scrub = { vid: null, key: "", ready: false, pending: null, seeking: false };
        function scrubSource() {
            const vf = String(widgetValue(node, "v2v_video_file", "")).trim();
            return (vf && !inputConnected(node, "v2v_images")) ? vf : null;
        }
        function ensureScrubVid(vf) {
            if (scrub.key === vf && scrub.vid) return scrub.vid;
            if (scrub.vid) { scrub.vid.removeAttribute("src"); scrub.vid.load(); }
            const vv = document.createElement("video");
            scrub.vid = vv; scrub.key = vf;
            scrub.ready = false; scrub.pending = null; scrub.seeking = false;
            vv.muted = true;
            vv.preload = "auto";
            vv.src = inputFileUrl(vf);
            vv.addEventListener("loadedmetadata", () => {
                scrub.ready = true;
                if (scrub.pending != null) { const t = scrub.pending; scrub.pending = null; seekScrub(t); }
            });
            vv.addEventListener("seeked", () => {
                scrub.seeking = false;
                drawScrubFrame();
                if (scrub.pending != null) { const t = scrub.pending; scrub.pending = null; seekScrub(t); }
            });
            vv.addEventListener("error", () => { framePrevCap.textContent = "can't decode footage"; });
            return vv;
        }
        function seekScrub(t) {
            const vv = scrub.vid;
            if (!vv) return;
            if (!scrub.ready || scrub.seeking) { scrub.pending = t; return; }   // coalesce
            if (Math.abs(vv.currentTime - t) < 1 / (2 * FPS)) { drawScrubFrame(); return; }
            scrub.seeking = true;
            vv.currentTime = t;
        }
        function drawScrubFrame() {
            const vv = scrub.vid;
            if (!vv || !vv.videoWidth) return;
            const c = framePrevCnv;
            const sc = Math.max(c.width / vv.videoWidth, c.height / vv.videoHeight);
            const cw = c.width / sc, ch2 = c.height / sc;
            c.getContext("2d").drawImage(vv,
                (vv.videoWidth - cw) / 2, (vv.videoHeight - ch2) / 2, cw, ch2,
                0, 0, c.width, c.height);
        }
        function showFramePreview(frac) {
            const vf = scrubSource();
            if (!vf) return;
            const vv = ensureScrubVid(vf);
            const gs = Number(widgetValue(node, "v2v_start_seconds", 0)) || 0;
            const ge0 = Number(widgetValue(node, "v2v_end_seconds", 0)) || 0;
            const D = scrub.ready && isFinite(vv.duration) ? vv.duration : 0;
            const ge = ge0 > 0 ? ge0 : D;
            const srcT = gs + frac * Math.max(0, (ge || gs) - gs);
            seekScrub(D ? Math.min(srcT, D - 0.001) : srcT);
            framePrevCap.textContent =
                `${timeOf(frac).toFixed(2)}s · f${frameOf(frac)} · src ${srcT.toFixed(2)}s`;
            if (!framePrev.parentNode) (state.fs?.root || document.body).appendChild(framePrev);
            const r = track.getBoundingClientRect();
            framePrev.style.left = Math.min(window.innerWidth - 186, Math.max(8, r.right - 184)) + "px";
            let by = r.top - 132;   // above the track header (beats-mode corner)
            if (by < 8) by = r.top + 8;
            framePrev.style.top = by + "px";
            framePrev.style.display = "";
        }
        function hideFramePreview() {
            framePrev.style.display = "none";
            // scrubFrac deliberately survives: the playhead persists where you
            // left it (cleared automatically when the v2v source goes away)
        }

        const ghost = { key: "", thumbs: [] };
        const GHOST_N = 14;
        const ghostOpacity = () =>
            Number.isFinite(node.properties?.h3_ghost_opacity) ? node.properties.h3_ghost_opacity : 0.35;
        function buildGhost(key, vf, s0, e0) {
            ghost.key = key;
            ghost.thumbs = [];
            const vv = document.createElement("video");
            vv.muted = true;
            vv.preload = "auto";
            vv.src = inputFileUrl(vf);
            const bail = () => { vv.removeAttribute("src"); vv.load(); };
            vv.addEventListener("error", bail, { once: true });
            vv.addEventListener("loadedmetadata", () => {
                const D = isFinite(vv.duration) ? vv.duration : 0;
                if (!D || !vv.videoWidth) return bail();
                const s = Math.min(s0, D);
                const e = Math.max(s + 0.01, Math.min(e0 > 0 ? e0 : D, D));
                let i = 0;
                vv.addEventListener("seeked", () => {
                    if (!state.fs || ghost.key !== key) return bail();   // superseded/closed
                    const c = document.createElement("canvas");
                    c.width = 96; c.height = 140;   // tallish slice, cover-cropped
                    const sc = Math.max(c.width / vv.videoWidth, c.height / vv.videoHeight);
                    const cw = c.width / sc, ch2 = c.height / sc;
                    try {
                        c.getContext("2d").drawImage(vv,
                            (vv.videoWidth - cw) / 2, (vv.videoHeight - ch2) / 2, cw, ch2,
                            0, 0, c.width, c.height);
                    } catch (err) { return bail(); }
                    ghost.thumbs[i] = c;
                    renderTrack();
                    i++;
                    if (i < GHOST_N) vv.currentTime = s + (i + 0.5) / GHOST_N * (e - s);
                    else bail();
                });
                vv.currentTime = s + 0.5 / GHOST_N * (e - s);
            }, { once: true });
        }

        function renderTrack() {
            const rect = track.getBoundingClientRect();
            if (rect.width < 4) return;
            const dpr = window.devicePixelRatio || 1;
            track.width = Math.round(rect.width * dpr);
            track.height = Math.round(rect.height * dpr);
            const ctx = track.getContext("2d");
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, rect.width, rect.height);
            const T = trackLayout(rect.width);
            const hits = [];
            track._h3T = T;
            track._h3Hits = hits;
            const F = fc(), dur = F / FPS;

            // v2v ghost strip (under all lanes)
            {
                const vf = String(widgetValue(node, "v2v_video_file", "")).trim();
                if (vf && !inputConnected(node, "v2v_images")) {
                    const gs = Number(widgetValue(node, "v2v_start_seconds", 0)) || 0;
                    const ge = Number(widgetValue(node, "v2v_end_seconds", 0)) || 0;
                    const key = `${vf}|${gs}|${ge}`;
                    if (ghost.key !== key) buildGhost(key, vf, gs, ge);
                    const op = ghostOpacity();
                    if (op > 0.01 && ghost.thumbs.length) {
                        const y0 = T.ruler + 10, y1 = T.by - 16, hgt = y1 - y0;
                        const w = (T.x1 - T.x0) / GHOST_N;
                        ctx.globalAlpha = op;
                        for (let k = 0; k < GHOST_N; k++) {
                            const th = ghost.thumbs[k];
                            if (!th) continue;
                            // cover-crop the cached slice to the drawn slice aspect
                            const sc = Math.max(w / th.width, hgt / th.height);
                            const cw = w / sc, ch2 = hgt / sc;
                            ctx.drawImage(th, (th.width - cw) / 2, (th.height - ch2) / 2,
                                cw, ch2, T.x0 + k * w, y0, w, hgt);
                        }
                        ctx.globalAlpha = 1;
                    }
                } else if (ghost.key) {
                    ghost.key = "";
                    ghost.thumbs = [];
                }
            }

            // ruler
            ctx.font = "12px monospace";
            ctx.textAlign = "center"; ctx.textBaseline = "middle";
            const step = dur > 16 ? 2 : dur >= 8 ? 1 : dur >= 2 ? 0.5 : Math.max(dur / 4, 1 / FPS);
            for (let s = 0; s <= dur + 1e-6; s += step) {
                const x = fracToX(T, s / dur);
                ctx.strokeStyle = COL.tick;
                ctx.beginPath(); ctx.moveTo(x, T.ruler); ctx.lineTo(x, T.by + 8); ctx.stroke();
                ctx.fillStyle = COL.text;
                ctx.fillText(Number.isInteger(s) ? s + "s" : s.toFixed(1), x, T.ruler - 10);
            }

            // keyframe lane
            ctx.strokeStyle = COL.axis;
            ctx.beginPath(); ctx.moveTo(T.x0, T.ky); ctx.lineTo(T.x1, T.ky); ctx.stroke();
            const stemH = (s) => (s / 2) * T.stem;

            // lanes get pushed FIRST so markers (pushed later) win the reverse hit-test
            hits.push({ x: T.x0, y: T.by - 12, w: T.x1 - T.x0, h: 26, kind: "beatlane" });

            const drawKf = (frac, strength, color, shape, selKind, selIdx, present) => {
                const x = fracToX(T, frac);
                const h = stemH(strength);
                const seld = state.sel && state.sel.kind === selKind
                    && (selIdx == null || state.sel.i === selIdx);
                ctx.globalAlpha = present ? 1 : 0.3;
                ctx.fillStyle = ctx.strokeStyle = color;
                ctx.fillRect(x - 1.5, T.ky - h, 3, h);
                // strength handle: a square cap at the stem top — vertical drag only
                ctx.fillRect(x - 5, T.ky - h - 5, 10, 10);
                if (shape === "diamond") {
                    ctx.beginPath();
                    ctx.moveTo(x, T.ky - 6); ctx.lineTo(x + 6, T.ky); ctx.lineTo(x, T.ky + 6); ctx.lineTo(x - 6, T.ky);
                    ctx.closePath(); ctx.fill();
                } else {
                    ctx.beginPath(); ctx.arc(x, T.ky, 5, 0, Math.PI * 2); ctx.fill();
                }
                if (seld) {
                    ctx.strokeStyle = COL.sel;
                    ctx.lineWidth = 2;
                    ctx.beginPath(); ctx.arc(x, T.ky, 9, 0, Math.PI * 2); ctx.stroke();
                    ctx.lineWidth = 1;
                }
                ctx.fillStyle = COL.bright;
                ctx.font = "12px monospace";
                ctx.fillText(strength.toFixed(2), x, T.ky - h - 14);
                ctx.globalAlpha = 1;
                if (present) {
                    hits.push({ x: x - 8, y: T.ky - h - 12, w: 16, h: 16, kind: "stem", selKind, selIdx });
                    hits.push({ x: x - 9, y: T.ky - 9, w: 18, h: 18, kind: "marker", selKind, selIdx });
                } else if (selKind === "first" || selKind === "last") {
                    // a dim stem used to be a dead zone — clicking it now offers
                    // to CREATE the missing frame instead of silently ignoring you
                    hits.push({ x: x - 9, y: T.ky - h - 12, w: 18, h: T.ky - (T.ky - h - 12) + 9,
                        kind: "ghostcap", selKind });
                }
            };

            drawKf(0, Number(widgetValue(node, "first_frame_strength", 1.0)), COL.cap, "circle", "first", null, !!capInfo("first"));
            state.mids.forEach((m, i) => drawKf(m.frac, m.strength, COL.mid, "diamond", "mid", i, true));
            drawKf(1, Number(widgetValue(node, "last_frame_strength", 1.0)), COL.cap, "circle", "last", null, !!capInfo("last"));

            // beat lane with staggered full-text labels
            ctx.strokeStyle = COL.tick;
            ctx.beginPath(); ctx.moveTo(T.x0, T.by); ctx.lineTo(T.x1, T.by); ctx.stroke();
            ctx.fillStyle = COL.text; ctx.font = "11px monospace"; ctx.textAlign = "left";
            ctx.fillText(state.beats.length ? "beats" : "beats — click here to add a timed prompt", T.x0, T.by + 22);
            ctx.textAlign = "center";
            const rowEnd = [-1e9, -1e9];   // greedy 2-row stagger, no overprint
            state.beats.forEach((b, i) => {
                const x = fracToX(T, b.frac);
                const draft = !b.text.trim();
                const seld = state.sel?.kind === "beat" && state.sel.i === i;
                ctx.fillStyle = draft ? COL.red : COL.green;
                ctx.beginPath();
                ctx.moveTo(x, T.by - 7); ctx.lineTo(x + 6, T.by + 4); ctx.lineTo(x - 6, T.by + 4);
                ctx.closePath(); ctx.fill();
                if (seld) { ctx.strokeStyle = COL.sel; ctx.lineWidth = 2; ctx.stroke(); ctx.lineWidth = 1; }
                const label = (draft ? "(no text yet)" : b.text);
                const short = label.length > 34 ? label.slice(0, 32) + "…" : label;
                ctx.font = "12px sans-serif";
                const tw = ctx.measureText(short).width;
                const startX = x - tw / 2;
                const row = rowEnd[0] + 8 < startX ? 0
                    : rowEnd[1] + 8 < startX ? 1
                    : (rowEnd[0] <= rowEnd[1] ? 0 : 1);
                rowEnd[row] = x + tw / 2;
                ctx.fillStyle = draft ? COL.red : (seld ? COL.bright : COL.text);
                ctx.fillText(short, x, T.by + 22 + row * 15);
                ctx.font = "11px monospace";
                ctx.fillStyle = "#666";
                ctx.fillText(timeOf(b.frac).toFixed(1) + "s", x, T.by - 15);
                hits.push({ x: x - 9, y: T.by - 20, w: 18, h: 28, kind: "beat", i });
            });

            // drag readout chip
            if (scrubFrac != null && !scrubSource()) scrubFrac = null;
            if (scrubFrac != null) {
                const x = fracToX(T, scrubFrac);
                ctx.globalAlpha = state.drag?.kind === "scrub" ? 1 : 0.55;
                ctx.strokeStyle = COL.bright;
                ctx.lineWidth = 2;
                ctx.beginPath(); ctx.moveTo(x, T.ruler - 4); ctx.lineTo(x, T.by + 8); ctx.stroke();
                ctx.lineWidth = 1;
                ctx.globalAlpha = 1;
            }
            if (state.dragReadout) {
                const { x, text } = state.dragReadout;
                ctx.font = "12px monospace";
                const tw = ctx.measureText(text).width;
                ctx.fillStyle = "rgba(0,0,0,0.8)";
                ctx.fillRect(x - tw / 2 - 6, 34, tw + 12, 20);
                ctx.fillStyle = COL.bright;
                ctx.fillText(text, x, 44);
            }
        }

        // track interaction — reverse hit order so markers beat lanes
        const hitAt = (p) => {
            const hs = track._h3Hits || [];
            for (let i = hs.length - 1; i >= 0; i--) {
                const h = hs[i];
                if (p.x >= h.x && p.x <= h.x + h.w && p.y >= h.y && p.y <= h.y + h.h) return h;
            }
            return null;
        };
        const evPos = (ev) => {
            const r = track.getBoundingClientRect();
            return { x: ev.clientX - r.left, y: ev.clientY - r.top };
        };

        track.addEventListener("pointerdown", (ev) => {
            if (ev.button !== 0) return;
            ev.preventDefault();
            const p = evPos(ev);
            const h = hitAt(p);
            const T = track._h3T;
            if (!h && scrubSource() && p.y <= T.by - 14) {
                // press anywhere on the empty track = playhead scrub over the
                // v2v footage. A lane press that never moves is still treated
                // as the old deselect-click (maybeClick, resolved on release).
                track.setPointerCapture(ev.pointerId);
                state.drag = { kind: "scrub", T, x0: p.x, maybeClick: p.y > T.ruler + 6 };
                scrubFrac = xToFrac(T, p.x);
                if (!state.drag.maybeClick) showFramePreview(scrubFrac);
                renderTrack();
                return;
            }
            if (!h) { state.sel = null; fill(); return; }
            track.setPointerCapture(ev.pointerId);
            if (h.kind === "ghostcap") {
                openPicker((n) => {
                    setWidget(h.selKind + "_frame_file", n);
                    refresh(true);
                });
                return;
            }
            if (h.kind === "marker") {
                state.sel = h.selIdx != null ? { kind: h.selKind, i: h.selIdx } : { kind: h.selKind };
                if (h.selKind === "mid")
                    state.drag = { kind: "midtime", i: h.selIdx, frac: state.mids[h.selIdx].frac, x: p.x, T };
                fill();
            } else if (h.kind === "stem") {
                const get = h.selKind === "mid"
                    ? () => state.mids[h.selIdx].strength
                    : () => Number(widgetValue(node, h.selKind + "_frame_strength", 1.0));
                state.sel = h.selIdx != null ? { kind: h.selKind, i: h.selIdx } : { kind: h.selKind };
                state.drag = { kind: "strength", selKind: h.selKind, i: h.selIdx, strength: get(), y: p.y, T };
                fill();
            } else if (h.kind === "beat") {
                state.sel = { kind: "beat", i: h.i };
                state.drag = { kind: "beattime", i: h.i, frac: state.beats[h.i].frac, x: p.x, T };
                fill();
            } else if (h.kind === "beatlane") {
                const frac = xToFrac(T, p.x);
                state.beats.push({ frac, text: "" });
                state.beats.sort((a, b) => a.frac - b.frac);
                state.sel = { kind: "beat", i: state.beats.findIndex((b) => b.frac === frac && !b.text) };
                fill();
            }
        });

        track.addEventListener("pointermove", (ev) => {
            const d = state.drag;
            if (!d) return;
            const p = evPos(ev);
            const T = d.T;
            const F = fc();
            if (d.kind === "scrub") {
                if (d.maybeClick && Math.abs(p.x - d.x0) > 3) d.maybeClick = false;
                scrubFrac = xToFrac(T, p.x);
                if (!d.maybeClick) showFramePreview(scrubFrac);
                renderTrack();
                return;   // nothing written — no refresh needed
            }
            if (d.kind === "midtime") {
                const m = state.mids[d.i];
                const lo = 1 / (F - 1), hi = (F - 2) / (F - 1);
                m.frac = Math.min(hi, Math.max(lo, d.frac + (p.x - d.x) / (T.x1 - T.x0)));
                // show the SNAPPED value that will actually be written
                m.frac = roundHalfEven(m.frac * (F - 1)) / (F - 1);
                state.dragReadout = { x: fracToX(T, m.frac),
                    text: `${timeOf(m.frac).toFixed(2)}s · f${frameOf(m.frac)} · ${m.strength.toFixed(2)}` };
                if (scrubSource()) showFramePreview(m.frac);   // the frame it will anchor over
                pushMids();
            } else if (d.kind === "beattime") {
                const b = state.beats[d.i];
                b.frac = Math.min(1, Math.max(0, d.frac + (p.x - d.x) / (T.x1 - T.x0)));
                b.frac = roundHalfEven(b.frac * (F - 1)) / (F - 1);
                state.dragReadout = { x: fracToX(T, b.frac),
                    text: `${timeOf(b.frac).toFixed(2)}s · f${frameOf(b.frac)}` };
                if (scrubSource()) showFramePreview(b.frac);
                pushBeats();
            } else if (d.kind === "strength") {
                const v = Math.min(2, Math.max(0,
                    Math.round((d.strength + (d.y - p.y) / T.stem * 2) * 20) / 20));
                if (d.selKind === "mid") { state.mids[d.i].strength = v; pushMids(); }
                else setWidget(d.selKind + "_frame_strength", v);
                const x = d.selKind === "mid" ? fracToX(T, state.mids[d.i].frac)
                    : (d.selKind === "first" ? T.x0 : T.x1);
                state.dragReadout = { x, text: v.toFixed(2) };
            }
            refresh(false);
        });

        const endDrag = (ev) => {
            if (!state.drag) return;
            const wasScrub = state.drag.kind === "scrub";
            const wasClick = wasScrub && state.drag.maybeClick;
            state.drag = null;
            state.dragReadout = null;
            hideFramePreview();
            try { track.releasePointerCapture(ev.pointerId); } catch (e) { /* released */ }
            if (wasClick) { state.sel = null; fill(); }   // lane click = deselect (playhead moves too)
            else if (wasScrub) renderTrack();
            else refresh(true);   // re-pull: the spec's nudged/quantized values win
        };
        track.addEventListener("pointerup", endDrag);
        track.addEventListener("pointercancel", endDrag);

        track.addEventListener("contextmenu", (ev) => {
            const h = hitAt(evPos(ev));
            if (!h) return;
            ev.preventDefault();
            if (h.kind === "beat") {
                openCtxMenu(ev.clientX, ev.clientY, [
                    { label: "✎ edit text", action: () => { state.sel = { kind: "beat", i: h.i }; fill(); } },
                    { label: "🖼 give this beat an image…", hint: "becomes a waypoint",
                      action: () => beatToWaypoint(h.i) },
                    "-",
                    { label: "✕ delete beat", danger: true, action: () => removeBeat(h.i) },
                ]);
            } else if (h.kind === "marker" && (h.selKind === "mid" || h.selKind === "first" || h.selKind === "last")) {
                imageMenu(h.selKind === "mid" ? { kind: "mid", i: h.selIdx } : { kind: h.selKind }, ev);
            }
        });

        track.addEventListener("wheel", (ev) => {
            const h = hitAt(evPos(ev));
            if (!h || (h.kind !== "marker" && h.kind !== "stem")) return;
            ev.preventDefault();
            const delta = ev.deltaY < 0 ? 0.05 : -0.05;
            const clamp = (v) => Math.min(2, Math.max(0, Math.round(v * 20) / 20));
            if (h.selKind === "mid") {
                state.mids[h.selIdx].strength = clamp(state.mids[h.selIdx].strength + delta);
                pushMids();
            } else {
                const cur = Number(widgetValue(node, h.selKind + "_frame_strength", 1.0));
                setWidget(h.selKind + "_frame_strength", clamp(cur + delta));
            }
            refresh(false);
            clearTimeout(state.wheelT);
            state.wheelT = setTimeout(() => state.fs?.fill?.(), 200);
        }, { passive: false });

        track.addEventListener("dblclick", (ev) => {
            const p = evPos(ev);
            const T = track._h3T;
            // double-click empty keyframe-lane space: pick an image for a waypoint there
            if (p.y > T.ruler && p.y < T.by - 20 && !hitAt(p)) {
                const frac = xToFrac(T, p.x);
                openPicker((name) => {
                    addFileMid(name);
                    const m = state.mids[state.mids.length - 1];
                    if (m) { m.frac = frac; pushMids(); refresh(true); }
                });
            }
        });

        // ---- keyboard
        const onKey = (ev) => {
            if (state.modal) return;                      // picker/lightbox owns keys
            if (state.ctxMenu) {                          // menu owns Esc; swallow the rest
                if (ev.key === "Escape") { ev.stopPropagation(); closeCtxMenu(); }
                return;
            }
            const tag = document.activeElement?.tagName;
            if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
            if (ev.key === "Escape") {
                ev.stopPropagation();
                if (state.sel) { state.sel = null; fill(); }
                else closeFullscreen();
            } else if (ev.key === "Delete" || ev.key === "Backspace") {
                deleteSelected();
            } else if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
                const s = state.sel;
                if (!s) return;
                const F = fc();
                const stepF = (ev.shiftKey ? FPS : 1) / (F - 1) * (ev.key === "ArrowLeft" ? -1 : 1);
                if (s.kind === "mid") {
                    const m = state.mids[s.i];
                    if (!m) { state.sel = null; return; }
                    m.frac = Math.min((F - 2) / (F - 1), Math.max(1 / (F - 1), m.frac + stepF));
                    pushMids(); fill();
                } else if (s.kind === "beat") {
                    const b = state.beats[s.i];
                    if (!b) { state.sel = null; return; }
                    b.frac = Math.min(1, Math.max(0, b.frac + stepF));
                    pushBeats(); fill();
                }
            } else if (ev.key === "ArrowUp" || ev.key === "ArrowDown") {
                const s = state.sel;
                if (!s) return;
                const d = ev.key === "ArrowUp" ? 0.05 : -0.05;
                const clamp = (v) => Math.min(2, Math.max(0, Math.round(v * 20) / 20));
                if (s.kind === "mid") {
                    if (!state.mids[s.i]) { state.sel = null; return; }
                    state.mids[s.i].strength = clamp(state.mids[s.i].strength + d); pushMids();
                } else if (s.kind === "ref") {
                    if (!state.refs[s.i]) { state.sel = null; return; }
                    state.refs[s.i].strength = clamp(state.refs[s.i].strength + d); pushRefs();
                }
                else if (s.kind === "first" || s.kind === "last") {
                    const cur = Number(widgetValue(node, s.kind + "_frame_strength", 1.0));
                    setWidget(s.kind + "_frame_strength", clamp(cur + d));
                }
                fill();
            }
        };
        window.addEventListener("keydown", onKey, true);

        // ---- fill: rebuild all DOM sections from state
        function fill() {
            const F = fc();
            const [oW, oH] = outWH();
            if (document.activeElement !== wField) wField.value = String(oW);
            if (document.activeElement !== hField) hField.value = String(oH);
            if (document.activeElement !== lenField)
                lenField.value = (Number(widgetValue(node, "length", 124)) / FPS).toFixed(1) + "s";
            snapNote.textContent = `= ${F}f @ ${FPS}fps`;
            {
                const vf = String(widgetValue(node, "v2v_video_file", "")).trim();
                const vSock = inputConnected(node, "v2v_images");
                const active = vSock || !!vf;
                v2vLabel.textContent = vSock ? "socket footage connected"
                    : vf ? vf.replace(/\s*\[\w+\]\s*$/, "") : "none — normal empty latent";
                v2vLabel.style.color = active ? COL.green : COL.text;
                v2vClear.style.display = vf ? "" : "none";
                if (document.activeElement !== v2vStart)
                    v2vStart.value = String(Number(widgetValue(node, "v2v_start_seconds", 0)) || 0);
                if (document.activeElement !== v2vEnd)
                    v2vEnd.value = String(Number(widgetValue(node, "v2v_end_seconds", 0)) || 0);
                v2vStart.disabled = v2vEnd.disabled = !active;
                if (active) snapNote.textContent = "· length follows v2v footage";
                // with v2v the FOOTAGE defines the canvas (python overrides the
                // widgets) — UNLESS a ⛶ framing pins it back to width×height.
                // Surface whichever is true instead of letting w/h fields mislead.
                const v2vCrop = cropOf({ kind: "v2v" });
                v2vScrub.style.display = (!vSock && vf) ? "" : "none";
                ghostWrap.style.display = (!vSock && vf) ? "inline-flex" : "none";
                if (document.activeElement !== ghostSl)
                    ghostSl.value = String(Math.round(ghostOpacity() * 100));
                // socket footage can't be framed here (no preview), but a crop
                // set earlier STILL applies python-side — keep ⛶ visible as a
                // two-click clear so it can never pin the canvas invisibly
                v2vFrame.style.display = ((!vSock && vf) || (vSock && v2vCrop)) ? "" : "none";
                v2vFrame.style.color = v2vCrop ? COL.green : COL.bright;
                v2vDenoise.style.display = active ? "" : "none";
                v2vNote.style.display = active ? "" : "none";
                if (active && v2vCrop) {
                    v2vNote.style.color = COL.green;
                    v2vNote.textContent = `· canvas = your ⛶ window → ${snap32(oW)}×${snap32(oH)}`;
                    v2vNote.title = "framed v2v: the footage is windowed to the width×height aspect and resized to exactly that canvas";
                } else if (vSock) {
                    v2vNote.style.color = COL.text;
                    v2vNote.textContent = "· canvas + length follow the footage (w/h/length widgets ignored)";
                } else if (active) {
                    const meta = ensureVideoMeta(vf);
                    if (state.videoMeta.get(vf)?.failed) {
                        v2vNote.style.color = COL.mid;
                        v2vNote.textContent = "· couldn't read the footage dims (codec?) — canvas follows footage";
                    } else if (meta) {
                        const fw = snap32(meta.w), fh = snap32(meta.h);
                        const differs = fw !== oW || fh !== oH;
                        v2vNote.style.color = differs ? COL.mid : COL.text;
                        v2vNote.textContent = differs
                            ? `· canvas follows footage → ${fw}×${fh} (w/h widgets ${oW}×${oH} ignored)`
                            : `· canvas follows footage (${fw}×${fh})`;
                        v2vNote.title = "keyframes and framing windows conform to the footage canvas while v2v is active";
                    } else {
                        v2vNote.style.color = COL.text;
                        v2vNote.textContent = "· canvas follows the footage (reading dims…)";
                    }
                }
            }
            const firstSock = inputConnected(node, "first_frame");
            const lastSock = inputConnected(node, "last_frame");
            swapBtn.disabled = firstSock || lastSock;
            swapBtn.style.opacity = swapBtn.disabled ? "0.4" : "1";
            swapBtn.title = swapBtn.disabled
                ? "a socket-fed frame can't be swapped from here — files only"
                : "swap first and last frames (files, framings and strengths) — the shot runs in reverse";
            const numbered = numberedEntities();
            const ents = numbered.ents;
            state._ents = ents;
            state._numbered = numbered;

            // prompt: don't clobber the caret while the user is typing here
            if (document.activeElement !== promptTA)
                promptTA.value = widgetValue(node, "prompt", "");
            chipRow.textContent = "";
            const chip = (label, insert, color) => {
                const b = el("button", {
                    ...btnStyle, padding: "1px 8px", fontSize: "11px",
                    color, borderColor: color,
                }, label);
                b.title = "insert " + insert.trim() + " at the cursor";
                b.addEventListener("click", () => insertAtCaret(insert));
                chipRow.appendChild(b);
            };
            for (const e of ents)
                chip(`P${e.pic} ${e.kind === "mid" ? "waypoint" : e.kind}`,
                    `<Picture ${e.pic}> `, e.kind === "mid" ? COL.mid : COL.cap);
            for (const r of numbered.refs) chip(`P${r._pic} ref`, `<Picture ${r._pic}> `, COL.green);
            for (const vch of numbered.videos) chip(`V${vch.num} video`, `<Video ${vch.num}> `, COL.green);
            for (const vch of numbered.videos)
                if (vch.aNum) chip(`A${vch.aNum} ♪`, `<Audio ${vch.aNum}> `, COL.green);
            for (const a of numbered.audio) chip(`A${a.num}${a.uncertain ? "~" : ""} ♪`, `<Audio ${a.num}> `, COL.green);
            if (!chipRow.childElementCount)
                chipRow.appendChild(el("span", { color: "#666", fontSize: "11px" },
                    "chips appear here as you add images and audio"));

            strip.textContent = "";
            const capGhost = (which) => {
                const g = ghostCard(`+ pick ${which} frame`, 180, 145, () =>
                    openPicker((n) => { setWidget(which + "_frame_file", n); refresh(true); }));
                makeDropTarget(g, "image/",
                    (n) => { setWidget(which + "_frame_file", n); refresh(true); }, COL.cap);
                return g;
            };
            if (!hasFirst()) strip.appendChild(capGhost("first"));
            for (const e of ents) strip.appendChild(card(e));
            if (!hasLast()) strip.appendChild(capGhost("last"));
            strip.appendChild(ghostCard("+ waypoint", 180, 145, () => openPicker(addFileMid)));

            refsRow.textContent = "";
            state.refs.forEach((r, i) => refsRow.appendChild(refCard(r, i)));
            refsRow.appendChild(ghostCard("+ reference", 140, 180, () => openPicker(addFileRef)));

            // audio references: sockets only (no audio picker), so the editor's job
            // is visibility + the one strength dial + playback when resolvable
            if (document.activeElement !== vidMp)
                vidMp.value = String(Number(widgetValue(node, "ref_video_megapixels", 0)) || 0);
            vidRow.textContent = "";
            numbered.videos.forEach(({ num, v, snd, aNum }, i) => {
                const c = el("div", {
                    width: "180px", flex: "0 0 auto", background: COL.panel,
                    border: `1px solid ${COL.border}`, borderRadius: "6px",
                    overflow: "hidden",
                });
                if (v.src.type === "file") {
                    const vv = el("video");
                    vv.controls = true; vv.muted = true; vv.preload = "metadata";
                    vv.src = inputFileUrl(v.src.name);
                    Object.assign(vv.style, { width: "180px", height: "101px", objectFit: "cover", background: "#222", display: "block" });
                    vv.addEventListener("loadedmetadata", () => {
                        const cur = state.videoMeta.get(v.src.name);
                        if (!cur || !cur.w) {   // fill placeholders/failures too
                            state.videoMeta.set(v.src.name,
                                { dur: vv.duration, w: vv.videoWidth, h: vv.videoHeight });
                            updateCostMeter();
                        }
                    });
                    c.appendChild(vv);
                } else {
                    c.appendChild(el("div", {
                        width: "180px", height: "101px", background: "#222", display: "flex",
                        alignItems: "center", justifyContent: "center", color: "#666",
                        fontSize: "11px", fontFamily: "monospace",
                    }, `socket video${v.src.slot}`));
                }
                const foot = el("div", { padding: "6px 8px", display: "flex", flexDirection: "column", gap: "4px" });
                const row1 = el("div", { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "6px" });
                row1.appendChild(picChip("V" + num, COL.green));
                const vfr = el("button", { ...btnStyle, padding: "0 6px", fontSize: "11px",
                    color: state.videoCrops[i] ? COL.green : COL.bright }, "⛶");
                vfr.title = state.videoCrops[i]
                    ? "framed — click to adjust (window keeps the source aspect)"
                    : "frame this video — choose the crop when continuing footage of a different aspect";
                vfr.addEventListener("click", (ev) => { ev.stopPropagation(); openFramer({ kind: "video", i }); });
                row1.appendChild(vfr);
                const sndB = el("span", { color: COL.text, fontSize: "10px" },
                    snd === "maybe" ? "♪ auto" : (snd ? "♪ A" + aNum : "silent"));
                sndB.title = snd === "maybe"
                    ? "an embedded soundtrack is used automatically if the file has one (exact <Audio N> numbers in the server log)"
                    : (snd ? "paired soundtrack — cite as <Audio " + aNum + ">" : "no soundtrack");
                row1.appendChild(sndB);
                const vv2 = el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "12px" },
                    v.strength.toFixed(2));
                row1.appendChild(vv2);
                const slider = el("input");
                slider.type = "range"; slider.min = "0"; slider.max = "2"; slider.step = "0.05";
                slider.value = String(v.strength);
                Object.assign(slider.style, { width: "100%", accentColor: COL.green, cursor: "pointer" });
                slider.addEventListener("input", () => {
                    v.strength = parseFloat(slider.value);
                    vv2.textContent = v.strength.toFixed(2);
                    pushVideoRefs(); refresh(false);
                });
                slider.addEventListener("change", () => fill());
                slider.addEventListener("pointerdown", (ev) => ev.stopPropagation());
                const row3 = el("div", { display: "flex", justifyContent: "space-between", alignItems: "center" });
                row3.appendChild(el("span", { color: COL.text, fontSize: "10px", textTransform: "uppercase" },
                    v.src.type === "socket" ? `SOCKET video${v.src.slot}`
                        : "FILE " + (v.src.name.length > 12 ? v.src.name.slice(0, 10) + "…" : v.src.name)));
                if (state.videoCrops[i])
                    row3.appendChild(el("span", { color: COL.green, fontSize: "10px" },
                        "⛶ " + state.videoCrops[i].z.toFixed(2) + "×"));
                if (v.src.type === "file") {
                    const ff = el("button", { ...btnStyle, padding: "0 6px", fontSize: "11px" }, "⏭");
                    ff.title = "use this video's FINAL frame as the clip's first frame — continue the footage exactly";
                    ff.addEventListener("click", (ev) => { ev.stopPropagation(); finalFrameToFirst(v.src.name); });
                    row3.appendChild(ff);
                    const x = el("button", { ...btnStyle, color: COL.red, padding: "0 6px", fontSize: "11px" }, "✕");
                    x.addEventListener("click", () => removeFileVideo(i));
                    row3.appendChild(x);
                }
                c.addEventListener("contextmenu", (ev) => {
                    ev.preventDefault();
                    const isFile = v.src.type === "file";
                    const items = [
                        { label: "⏭ final frame → first frame", hint: "continue this footage",
                          disabled: !isFile, ...(isFile ? {} : { hint: "fed by graph socket" }),
                          action: () => finalFrameToFirst(v.src.name) },
                        { label: state.videoCrops[i] ? "⛶ adjust framing…" : "⛶ frame video…",
                          hint: state.videoCrops[i] ? state.videoCrops[i].z.toFixed(2) + "×" : "",
                          disabled: !isFile, action: () => openFramer({ kind: "video", i }) },
                    ];
                    if (state.videoCrops[i])
                        items.push({ label: "⛶ clear framing",
                            action: () => { setCropOf({ kind: "video", i }, null); refresh(true); } });
                    items.push("-");
                    items.push({ label: "✕ remove", danger: true, disabled: !isFile,
                        ...(isFile ? {} : { hint: "disconnect in the graph" }),
                        action: () => removeFileVideo(i) });
                    openCtxMenu(ev.clientX, ev.clientY, items);
                });
                foot.append(row1, slider, row3);
                // informational, not a warning: refs are never distorted (sizing is
                // aspect-preserving) — but an unwindowed off-aspect video means the
                // model studies the full frame, which may not be the region you care
                // about when continuing footage
                if (v.src.type === "file" && !state.videoCrops[i] && effWH()) {
                    const meta = state.videoMeta.get(v.src.name);
                    if (meta?.w && Math.abs((meta.w / meta.h) / (effWH()[0] / effWH()[1]) - 1) > 0.02) {
                        const info = el("div", {
                            color: COL.text, fontSize: "10px", cursor: "pointer",
                            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                        }, "aspect differs from clip — model sees full frame · ⛶ to window");
                        info.title = "this video is " + meta.w + "×" + meta.h + " vs the "
                            + outWH().join("×") + " clip. No distortion happens (reference sizing "
                            + "keeps aspect) — but framing it focuses the model on your region.";
                        info.addEventListener("click", (ev) => {
                            ev.stopPropagation();
                            openFramer({ kind: "video", i });
                        });
                        foot.appendChild(info);
                    }
                }
                c.append(tagCaption(`<Video ${num}>` + (aNum ? ` · ♪ <Audio ${aNum}>` : ""),
                    COL.green), foot);
                vidRow.appendChild(c);
            });
            const addV = el("button", btnStyle, "+ video…");
            addV.title = "pick a reference video from the input or output folder — the output tab chains motion from a previous render";
            addV.addEventListener("click", () => openVideoPicker(addFileVideo));
            vidRow.appendChild(addV);

            audioRow.textContent = "";
            const audios = connectedSlots(node, "ref_audio_");
            const audioFiles = fileLinesOf("ref_audio_files");
            audioHead.style.display = "";
            audioRow.style.display = "flex";
            {
                const audioNums = numbered.audio;   // soundtracks already consumed A1..
                let aIdx = 0;
                audios.forEach((a) => {
                    const c = el("div", {
                        background: COL.panel, border: `1px solid ${COL.border}`,
                        borderRadius: "6px", padding: "8px 12px", display: "flex",
                        alignItems: "center", gap: "10px", flex: "0 0 auto",
                    });
                    c.append(el("span", { color: COL.green, fontSize: "16px" }, "♪"),
                        picChip("A" + (audioNums[aIdx] ? audioNums[aIdx].num : ++aIdx) + (audioNums[aIdx]?.uncertain ? "~" : ""), COL.green),
                        el("span", { color: COL.text, fontSize: "12px" }, `socket audio${a.slot}`));
                    aIdx++;
                    // playable when the upstream node is a LoadAudio-style file loader
                    const inp = (node.inputs || []).find((x) => x.name === a.name);
                    const link = inp && (node.graph || app.graph)?.links?.[inp.link];
                    const srcNode = link && (node.graph || app.graph)?.getNodeById?.(link.origin_id);
                    const fileW = srcNode?.widgets?.find((w) => w.name === "audio" && typeof w.value === "string");
                    if (fileW?.value) {
                        const player = el("audio");
                        player.controls = true;
                        player.preload = "none";
                        player.src = inputFileUrl(fileW.value);
                        Object.assign(player.style, { height: "28px", maxWidth: "260px" });
                        c.appendChild(player);
                    }
                    audioRow.appendChild(c);
                });
                audioFiles.forEach((f, idx) => {
                    const c = el("div", {
                        background: COL.panel, border: `1px solid ${COL.border}`,
                        borderRadius: "6px", padding: "8px 12px", display: "flex",
                        alignItems: "center", gap: "10px", flex: "0 0 auto",
                    });
                    const an = audioNums[aIdx + idx];
                    c.append(el("span", { color: COL.green, fontSize: "16px" }, "♪"),
                        picChip("A" + (an ? an.num : aIdx + idx + 1) + (an?.uncertain ? "~" : ""), COL.green),
                        el("span", { color: COL.bright, fontSize: "12px" },
                            f.length > 22 ? f.slice(0, 20) + "…" : f));
                    const player = el("audio");
                    player.controls = true;
                    player.preload = "none";
                    player.src = inputFileUrl(f);
                    Object.assign(player.style, { height: "28px", maxWidth: "260px" });
                    const x = el("button", { ...btnStyle, color: COL.red, padding: "0 7px", fontSize: "11px" }, "✕");
                    x.title = "remove this audio";
                    x.addEventListener("click", () => removeFileAudio(idx));
                    c.append(player, x);
                    audioRow.appendChild(c);
                });
                const addA = el("button", btnStyle, "+ audio…");
                addA.addEventListener("click", () => openAudioPicker(addFileAudio));
                const recB = el("button", btnStyle,
                    state.recorder ? "⏹ stop recording" : "● record mic");
                if (state.recorder) recB.style.color = COL.red;
                recB.title = "record from your microphone straight into the node (saved as WAV in the input folder)";
                recB.addEventListener("click", () => recordMic(recB));
                audioRow.append(addA, recB);
            }
            if (audios.length + audioFiles.length) {
                // the shared strength dial (one value for all connected audio, by design)
                const sWrap = el("div", {
                    background: COL.panel, border: `1px solid ${COL.border}`,
                    borderRadius: "6px", padding: "8px 12px", display: "flex",
                    alignItems: "center", gap: "10px", flex: "1 1 260px", minWidth: "260px",
                });
                const cur = Number(widgetValue(node, "ref_audio_strength", 1.0));
                const lab = el("span", { color: COL.text, fontSize: "12px", whiteSpace: "nowrap" }, "strength (all audio)");
                const slider = el("input");
                slider.type = "range"; slider.min = "0"; slider.max = "2"; slider.step = "0.05";
                slider.value = String(cur);
                Object.assign(slider.style, { flex: "1", accentColor: COL.green, cursor: "pointer" });
                const val = el("span", { color: COL.bright, fontFamily: "monospace", fontSize: "13px", width: "36px" },
                    cur.toFixed(2));
                const cap = el("span", { color: "#666", fontSize: "11px", whiteSpace: "nowrap" },
                    strengthCaption(cur, true));
                slider.addEventListener("input", () => {
                    const v = Math.round(parseFloat(slider.value) * 20) / 20;
                    setWidget("ref_audio_strength", v);
                    val.textContent = v.toFixed(2);
                    cap.textContent = strengthCaption(v, true);
                });
                sWrap.append(lab, slider, val, cap);
                audioRow.appendChild(sWrap);
                if (!inputConnected(node, "audio_vae"))
                    audioRow.appendChild(el("div", {
                        color: COL.red, fontSize: "12px", alignSelf: "center", flex: "0 0 auto",
                    }, "⚠ audio_vae not connected — the run will fail"));
            }
            // recorder state must not leak across fills
            if (!state.recorder) { /* no-op: button rebuilt fresh above */ }

            helpStrip.textContent = "";
            if (state.specError) {
                Object.assign(helpStrip.style, { background: "#2a1a18", color: COL.red });
                helpStrip.textContent = "⚠ " + state.specError + " — fix via ✎ raw text specs on the node";
            } else if (state.toastMsg) {
                Object.assign(helpStrip.style, {
                    background: state.toastMsg.warn ? "#2a2418" : "#182a1c",
                    color: state.toastMsg.warn ? COL.mid : COL.green,
                });
                helpStrip.textContent = state.toastMsg.msg;
            } else {
                Object.assign(helpStrip.style, { background: "transparent", color: COL.text });
                if (helpOpen) {
                    const grid = el("div", { display: "flex", gap: "24px", flexWrap: "wrap", padding: "4px 0" });
                    for (const [h, t] of HELP_COPY) {
                        const cell = el("div", { flex: "1 1 300px" });
                        cell.append(el("div", { color: COL.bright, fontSize: "12px" }, h),
                            el("div", { fontSize: "12px", lineHeight: "1.4" }, t));
                        grid.appendChild(cell);
                    }
                    helpStrip.appendChild(grid);
                } else {
                    helpStrip.textContent =
                        "?  strength, beats and Picture numbers explained   ·   drag = move · stem cap = strength · Del = remove · ←→ nudge (⇧=1s) · ↑↓ strength · Esc = close";
                }
            }

            if (document.activeElement !== modeSel)
                modeSel.value = widgetValue(node, "timed_text_mode", "text only");
            if (document.activeElement !== sizeSel)
                sizeSel.value = widgetValue(node, "ref_image_size", "match");
            if (document.activeElement !== mpNum)
                mpNum.value = String(Number(widgetValue(node, "ref_megapixels", 0)) || 0);
            maskChk.checked = !!widgetValue(node, "mask_ref_pixels", false);
            const anyMask = connectedSlots(node, "ref_mask_").length > 0;
            maskWrap.style.display = anyMask ? "inline-flex" : "none";
            updateCostMeter();
            buildInspector();
            renderTrack();
        }

        const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => renderTrack()) : null;
        ro?.observe(main);
        document.body.appendChild(root);
        state.fs = { root, onKey, fill, renderTrack, ro };
        fill();
    }
    node._h3OpenFS = openFullscreen;

    // ======================================================================
    // NODE SUMMARY (read-only)
    // ======================================================================
    const container = el("div", {
        width: "100%", height: "100%", display: "flex", flexDirection: "column",
        background: COL.bg, borderRadius: "4px", overflow: "hidden",
        fontFamily: "sans-serif", cursor: "pointer",
    });
    const summary = el("canvas", { width: "100%", flex: "1 1 auto", display: "block" });
    container.appendChild(summary);
    container.title = "click to open the timeline editor";
    container.addEventListener("click", openFullscreen);
    container.addEventListener("mousedown", (ev) => ev.stopPropagation());

    function renderSummary() {
        const rect = summary.getBoundingClientRect();
        if (rect.width < 4) return;
        const dpr = window.devicePixelRatio || 1;
        summary.width = Math.round(rect.width * dpr);
        summary.height = Math.round(rect.height * dpr);
        const ctx = summary.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const W = rect.width, H = rect.height;
        ctx.clearRect(0, 0, W, H);

        const ents = entityList();
        // micro-thumb row
        let x = 10;
        const ty = 8, tw = 46, th = 26;
        for (const e of ents.slice(0, 8)) {
            const color = e.kind === "mid" ? COL.mid : COL.cap;
            if (e.img) {
                try {
                    const s = Math.max(tw / e.img.naturalWidth, th / e.img.naturalHeight);
                    ctx.save();
                    ctx.beginPath(); ctx.roundRect(x, ty, tw, th, 3); ctx.clip();
                    ctx.drawImage(e.img, (e.img.naturalWidth - tw / s) / 2, (e.img.naturalHeight - th / s) / 2,
                        tw / s, th / s, x, ty, tw, th);
                    ctx.restore();
                } catch (err) { /* not decoded yet */ }
            } else {
                ctx.fillStyle = "#222";
                ctx.beginPath(); ctx.roundRect(x, ty, tw, th, 3); ctx.fill();
            }
            ctx.strokeStyle = color;
            ctx.beginPath(); ctx.roundRect(x + 0.5, ty + 0.5, tw - 1, th - 1, 3); ctx.stroke();
            x += tw + 5;
        }
        if (!ents.length) {
            ctx.fillStyle = COL.text;
            ctx.font = "11px sans-serif";
            ctx.textAlign = "left"; ctx.textBaseline = "middle";
            ctx.fillText("no keyframes yet — click to open the editor", 10, ty + th / 2);
        }

        // duration bar with marker dots
        const by = ty + th + 12, bx0 = 10, bx1 = W - 10;
        ctx.strokeStyle = COL.axis;
        ctx.beginPath(); ctx.moveTo(bx0, by); ctx.lineTo(bx1, by); ctx.stroke();
        const dot = (frac, color, r) => {
            ctx.fillStyle = color;
            ctx.beginPath(); ctx.arc(bx0 + frac * (bx1 - bx0), by, r, 0, Math.PI * 2); ctx.fill();
        };
        if (hasFirst()) dot(0, COL.cap, 3.5);
        for (const m of state.mids) dot(m.frac, COL.mid, 3);
        if (hasLast()) dot(1, COL.cap, 3.5);
        for (const b of state.beats) dot(b.frac, b.text.trim() ? COL.green : COL.red, 2.5);

        // counts + error badge
        ctx.font = "11px sans-serif";
        ctx.textAlign = "left"; ctx.textBaseline = "middle";
        const kf = ents.length, bt = state.beats.length, rf = state.refs.length;
        ctx.fillStyle = COL.text;
        ctx.fillText(`${kf} keyframe${kf === 1 ? "" : "s"} · ${bt} beat${bt === 1 ? "" : "s"} · ${rf} ref${rf === 1 ? "" : "s"}  —  open editor ⤢`,
            10, by + 16);
        if (state.specError) {
            ctx.fillStyle = COL.red;
            ctx.textAlign = "right";
            ctx.fillText("⚠ spec error", W - 10, by + 16);
        }

        // retry for late-arriving upstream previews
        const missing = ents.some((e) => !e.img && e.connected);
        if (missing && state.thumbTry < 10) {
            state.thumbTry++;
            clearTimeout(state.thumbTimer);
            state.thumbTimer = setTimeout(renderSummary, 900);
        }
    }

    // ---- shared refresh ---------------------------------------------------
    // full=true re-pulls from widgets (structure changed); false just repaints
    function refresh(full) {
        if (full !== false) {
            state.thumbTry = 0;
            // pull FIRST: reconcile against fresh state, or a reloaded workflow's
            // specs get clobbered by stale pre-configure state (bug-hunt finding).
            // Never reconcile while a spec is unparseable — counts aren't authoritative.
            pullFromWidgets();
            // count-only mismatches are recovered during pull and NEED reconcile
            // to rewrite the spec; only malformed content skips it
            if (!state.specError) reconcileCounts();
        }
        renderSummary();
        if (state.fs) (full !== false ? state.fs.fill : state.fs.renderTrack)();
        app.graph?.setDirtyCanvas(true, false);
    }
    node._h3Refresh = () => refresh(true);

    // ---- assembly ---------------------------------------------------------
    const HIDDEN_WIDGETS = ["middle_frame_spec", "timed_text", "ref_spec",
        "first_frame_file", "last_frame_file", "middle_frame_files", "ref_image_files",
        "first_frame_crop", "last_frame_crop", "middle_frame_crops", "ref_image_crops",
        "ref_audio_files", "ref_video_spec", "ref_video_files", "ref_video_crops",
        "v2v_video_file", "v2v_start_seconds", "v2v_end_seconds", "v2v_crop"];
    let rawVisible = false;
    for (const name of HIDDEN_WIDGETS) setWidgetVisible(node, getWidget(node, name), false);
    node.addWidget("button", "⤢ open timeline editor", null, openFullscreen);
    node.addWidget("button", "✎ raw text specs", null, () => {
        rawVisible = !rawVisible;
        for (const name of HIDDEN_WIDGETS) setWidgetVisible(node, getWidget(node, name), rawVisible);
        node.setSize(node.computeSize());
        app.graph?.setDirtyCanvas(true, true);
    });

    const domWidget = node.addDOMWidget("h3_timeline", "h3_timeline", container, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 92,
    });

    if (typeof ResizeObserver !== "undefined") {
        const ro = new ResizeObserver(() => renderSummary());
        ro.observe(container);
        node._h3Observer = ro;
    }

    for (const name of ["prompt", "length", "width", "height", "first_frame_strength", "last_frame_strength",
            "timed_text_mode", "ref_image_size", "ref_megapixels", "mask_ref_pixels",
            "ref_video_spec", "ref_video_files", "ref_video_megapixels", ...HIDDEN_WIDGETS]) {
        const w = getWidget(node, name);
        if (!w) continue;
        const prev = w.callback;
        w.callback = function (...args) {
            const r = prev?.apply(this, args);
            if (!state.syncing) requestAnimationFrame(() => refresh(true));
            else requestAnimationFrame(() => refresh(false));
            return r;
        };
    }

    refresh(true);
    return domWidget;
}

app.registerExtension({
    name: "ComfyUI.MiniMaxH3Guide.Timeline",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== NODE_NAME) return;

        const onCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = onCreated?.apply(this, arguments);
            try { attachTimeline(this); } catch (e) { console.error("[H3Guide] timeline failed:", e); }
            if (this.size[1] < 420) this.setSize([Math.max(this.size[0], 340), 420]);
            return r;
        };

        const onSerialize = nodeType.prototype.onSerialize;
        nodeType.prototype.onSerialize = function (o) {
            const r = onSerialize?.apply(this, arguments);
            try {
                // widgets_values is POSITIONAL in the frontend: any widget added
                // to the schema shifts older saves. Mirror values by NAME into
                // properties (which serialize as an object) so configure can
                // repair any positional scramble.
                o.properties = o.properties || {};
                const m = {};
                for (const w of this.widgets || [])
                    if (w.name && w.type !== "button" && w.name !== "h3_timeline"
                        && typeof w.value !== "undefined" && typeof w.value !== "object")
                        m[w.name] = w.value;
                o.properties.h3_widget_values = m;
            } catch (e) { /* best-effort armor */ }
            return r;
        };

        const onConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (o) {
            const r = onConfigure?.apply(this, arguments);
            try {
                const m = this.properties?.h3_widget_values;
                if (m) for (const w of this.widgets || [])
                    if (w.name in m) w.value = m[w.name];
            } catch (e) { /* positional restore stands */ }
            requestAnimationFrame(() => this._h3Refresh?.());
            return r;
        };

        const onConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const r = onConnectionsChange?.apply(this, arguments);
            requestAnimationFrame(() => this._h3Refresh?.());
            return r;
        };

        const onRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            this._h3Observer?.disconnect();
            this._h3CloseFS?.();
            const st = this._h3State;
            if (st) {
                clearTimeout(st.thumbTimer);
                clearTimeout(st.toastTimer);
                if (st.recorder) {
                    try { st.recorder.onstop = null; st.recorder.stop(); } catch (e) { /* stopped */ }
                    st.recorderStream?.getTracks().forEach((t) => t.stop());
                }
                st.ctxMenu?.remove();
                st.modal?.root.remove();
                if (st.modal) window.removeEventListener("keydown", st.modal.onKey, true);
            }
            return onRemoved?.apply(this, arguments);
        };
    },
});
