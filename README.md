# ComfyUI MiniMax H3 Guide

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/lorasandlenses)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](https://opensource.org/licenses/MIT)

**MiniMax H3 first/last-frame conditioning with an independent strength dial on each keyframe — so they steer the motion instead of pinning it.**

> **In a hurry? Read the [Quick recipes guide](GUIDE.md)** — every feature as a one-breath "I want to…" answer. This README is the deep reference.

## What it does

Drop-in duplicate of ComfyUI's stock **MiniMax H3 Image to Video** node with two
extra widgets: `first_frame_strength` and `last_frame_strength`.

Stock H3 treats keyframes as hard targets. It will open on your first frame and
land on your last frame, exactly, and it will bend the motion however it has to
in order to do it. Great when those frames *are* the shot you want. Frustrating
when you just want the clip to head in that general direction.

Turn a strength down and that frame becomes a **guide**: the model picks up its
composition, colour and rough content, then finds its own way.

## Why you'd want it

- A keyframe is a rough comp, a different crop, or an approximate pose
- Stock H3 is warping the subject unnaturally to hit a target frame exactly
- You want the *mood* of a frame without its literal geometry
- **You want the first frame locked hard while the end frame stays soft** — or
  the reverse: a loose opening that resolves onto an exact final frame

Those last ones are the killer feature. The model's own strength knob
(`visual_cond_noise_aug`) is a **single global value** applied to every condition
latent, so it cannot weaken one keyframe without equally weakening the other.
This node dials them independently.

## Install

```bash
cd ComfyUI/custom_nodes
git clone <your-repo-url> ComfyUI-MiniMaxH3Guide
```

Restart ComfyUI (a **hard browser refresh** too, the first time — the pack ships a
frontend extension for the Zoom & Pan preview). **No additional Python
dependencies** — it only uses what ComfyUI already ships.

Requires a ComfyUI build with MiniMax H3 support (v0.30.0 / commit `57500fc`
onwards).

## Quick start

1. Take any working stock **MiniMax H3 Image to Video** workflow
2. Swap that node for **MiniMax H3 Image to Video (Guide)** — same inputs, same
   two outputs, wire it identically
3. Leave both strengths at `1.0` and re-run: output is **bit-identical** to the
   stock node
4. Now walk one down until the motion loosens up

Find it under `model/conditioning/minimax`, right next to the stock node.

## The strength dials

Both widgets use the same scale and are fully independent — each only affects its
own frame, and has no effect at all unless that frame is connected.

| Value | Behaviour |
|---|---|
| `1.0` | Identical to the stock node — hits that frame exactly |
| `0.8 – 0.9` | Slight give. Cleans up unnatural warping into the frame |
| `0.6 – 0.8` | **Sweet spot.** Keeps composition and colour, frees the motion |
| `0.3 – 0.5` | Loose hint. Mood and palette survive, geometry does not |
| `0.0` | Frame ignored entirely (same as not connecting it) |
| `> 1.0` | Overdrive — amplifies the latent and pushes adherence past stock |

**The two ends are not symmetrical in practice.** The first frame is H3's geometry
anchor — it's stretched to fill the canvas and establishes the shot, so it
generally wants a higher value than the end frame. The last frame is a follower,
cover-cropped to preserve aspect, and takes dilution far more gracefully. If
you're only going to loosen one, loosen the last.

Useful combinations:

| `first` | `last` | Effect |
|---|---|---|
| `1.0` | `0.6 – 0.8` | Locked opening, the ending drifts toward your target |
| `0.6 – 0.8` | `1.0` | Loose opening that resolves onto an exact final frame |
| `0.7` | `0.7` | Both frames as bookend suggestions; most freedom for the model |

## The timeline

The node shows a compact **read-only summary** (micro-thumbs in time order, a
duration bar with marker dots, entity counts, an error badge) — click it or the
**⤢ open timeline editor** button to open the real thing: a **fullscreen editor**.

The editor is organised as rooms, not a strip:

- **Keyframes filmstrip** — large cards (180px thumbs) in time order: time,
  `<Pic N>` chip, strength as number + mini-bar, source badge (`SOCKET` / `FILE`),
  ✕ on removable cards, description caption. Dashed ghost cards add via the
  built-in picker (input folder + upload). Double-click a thumb for a fullscreen
  view.
- **Timeline track** — big ruler, markers with 60px strength stems. **Dragging a
  marker moves time only**; the square cap on the stem is the strength handle
  (vertical drag), and scroll / ↑↓ also adjust it. While dragging, a readout chip
  shows the *snapped* value that will actually be written (`1.7s · f41 · 0.60`).
  Double-click empty lane space to drop a new waypoint right there.
- **Beats lane** — click to add, drag to move, right-click or Delete to remove.
  Full beat text is shown under each marker, staggered so labels never overlap.
- **References panel** — square 140px cards with a real slider each, clearly
  labelled as whole-clip identity (not on the timeline).
- **Inspector** (right panel) — the selected item at ~330px with every property
  editable: an exact time field (`1.7s` or a frame number), a full-width strength
  slider whose caption *explains the current value in plain words* ("Sweet spot —
  keeps composition and colour, frees the motion"), a proper textarea for
  descriptions/beat text, source info, delete. When nothing is selected it shows
  the **Picture-number legend with thumbnails** and the help copy — the space is
  never wasted.
- **Keyboard** — Esc deselects, then closes · Delete removes · ←→ nudges one
  frame (Shift = 1s) · ↑↓ strength ±0.05.

### Editor quality-of-life

- **Prompt lives in the editor** — a proper textarea at the top, with a **chip
  row**: click `P2 waypoint` or `A1 ♪` to insert `<Picture 2>` / `<Audio 1>` at
  the cursor. The numbering legend is now an input device, not documentation.
- **Clip chaining** — the image picker has an **output folder tab** (newest
  first): pick the last frame of your previous render as the next clip's first
  frame. Annotated `name [output]` paths load and cache-invalidate natively.
- **Drag & drop from your OS** — drop images on the filmstrip (waypoints), the
  refs row, or an empty first/last slot; drop audio on the audio row. Sections
  highlight while you hover.
- **Beat ⇄ waypoint** — right-click a beat → "give this beat an image…" (same
  moment, text becomes the description); right-click a file waypoint → "strip
  image, keep as beat".
- **Width / height / length editable in the header**, with the snapped frame
  count shown live (`→ 124f · 5.2s`); width/height round to the model's 32 grid.
- **⇄ reverse** — swap first and last (files, framings, strengths) to run the
  shot backwards. Disabled with a reason when a cap is socket-fed.
- **Conditioning cost meter** on the references header: estimated rows your refs
  add per sampling step and the % overhead vs the video target; turns amber past
  +25% with advice (`match` / `ref_megapixels`).

**Full parity with the node:** every conditioning control now lives in the editor,
placed with the section it affects — `beats mode` (the timed-text delivery switch)
on the timeline header, and `sizing` / `MP cap` / `mask→pixels` on the references
header right beside the cost meter they drive. The mask toggle only appears when a
mask is actually connected, and socket references carry a small `M` badge when
masked so the mask's existence is visible at a glance.

### Assets: right-click menu and replace-in-place

**Right-click any image** — filmstrip card, reference card, or its marker on the
track — for a context menu:

- 🔍 view full size
- ⛶ frame / adjust framing (shows the current zoom) · clear framing
- ⇄ **replace image…** — swap in a new image while keeping *everything else*:
  time, strength, description, `<Picture N>` slot and framing. If the entity had
  a framing, the framer reopens automatically on the new image so you can confirm
  or adjust the window — a new photo usually wants its own crop.
- ✕ remove

The same replace button lives in the inspector. Socket-fed entries show the menu
too, with the unavailable actions disabled and labelled with *why* ("fed by graph
socket", "disconnect in the graph") rather than hidden. Beats get their own menu
(edit text / delete). Right-click is never instantly destructive any more —
deletion always goes through a visible menu item or the Delete key on a selection.

### Save / load setups

**💾 save setup** in the editor header downloads the node's entire conditioning
recipe as one JSON file — prompt, resolution, length, every strength, all picked
files, all framings, waypoint spec, beats, reference settings. **📂 load setup**
resumes it, on this node or a fresh one in a different workflow.

Two honest caveats: **socket-fed images can't travel in a file** — the setup
records which sockets were connected at save time, and the loader tells you
exactly what's still missing ("it used sockets not connected here:
middle_frame_0"). And file-picked images are stored by *filename*, so a setup
moved to another machine needs those files in its input folder too.

### Framing — no more aspect mismatches

Every image card and the inspector carry a **⛶ frame** button. It opens the source
image large, with a draggable, zoomable crop window — and for keyframes that window
is **locked to the generation's output aspect** (the node's own `width`×`height`).
The dimmed area is exactly what the model will *not* see.

This closes a hole every keyframe workflow has: a mismatched first frame normally
gets silently **stretched** (distortion) and mids/last get a **blind centre-crop**.
With a framing set, the downstream resize is distortion-free *by construction* —
what you framed is what the model sees. Thumbnails and the inspector preview render
the framed view, not the raw file.

References get the same tool with the window in the **source aspect** (pure
zoom/pan): refs aren't resized to the canvas, so there's no aspect to match — only
content to choose.

Framings live in `first_frame_crop` / `last_frame_crop` / `middle_frame_crops` /
`ref_image_crops` as `center_x, center_y, zoom` lines (`-` = none), editable by
hand or API like every other spec. All model-facing dimensions remain multiples of
32 as H3 requires — the canvas steps by 32 and reference sizes round to 32; framing
chooses *content*, never dimensions.

Everything file-picked gets a guaranteed thumbnail (the node knows the file);
socket-fed images show the upstream preview when one exists. Sockets always win
over file fields, so Zoom & Pan and graph-generated frames work exactly as before.
Auto-placed waypoints render dimmed with an `AUTO` tag until you touch them.

The spec text fields are still the source of truth — the timeline parses them on
load and rewrites them on every interaction, so **API-format workflows, old saves
and headless use are completely unaffected**. A `✎ raw text specs` button reveals
the fields for hand editing; edits there flow straight back into the timeline. The
UI also keeps spec line counts reconciled when you connect/disconnect inputs, so
the count-mismatch error can't happen from the graph.

Saved workflows are armored against schema growth: ComfyUI stores widget values
*positionally*, so a node that gains widgets normally scrambles older saves. This
node also mirrors every widget value **by name** into the workflow's properties
on save and re-applies them by name on load — workflows saved with an older
version of the pack keep their settings after an update.

The sections below describe the underlying spec formats — you only need them for
raw-text editing or API workflows.

## Middle frames (waypoints)

Stock H3 accepts anchors at the first and last frame only — everything else is
rejected outright:

```
ValueError: only first/last keyframe anchors are supported
```

This node lifts that. Connect up to **four** `middle_frame_` inputs and describe
them in `middle_frame_spec`, one line per frame:

```
position, strength[, description]
```

```
0.33, 0.7, boat halfway out of frame
0.66, 0.5, boat near the horizon
```

- **position** — a fraction of the clip (`0.5` = halfway), or an absolute frame
  number if above `1.0`. Clamped to the interior; frame 0 and the final frame
  belong to `first_frame` / `last_frame`.
- **strength** — same scale as the other dials. **Keep waypoints low** (0.5–0.7).
  A waypoint at `1.0` tries to freeze a moment mid-motion, which is exactly the
  stiffness you're trying to avoid.
- **description** — optional. Appended to the prompt as a labelled note, e.g.
  `<Picture 2> at 1.7 seconds: boat halfway out of frame.`

Lines are paired with slots in order, then sorted along the timeline for you —
you can write them in any order and the `<Picture N>` labels renumber to match.
Lines starting with `#` are ignored.

### Why descriptions work this way

The tokenizer already emits `"<Picture N>: "` before each image and Qwen3-VL binds
those labels, so referring back to them in the prompt is enough to tie text to a
frame. There's no slot for text interleaved *between* image blocks in the keyframe
presentation, so the notes are appended after the prompt rather than inlined.

You can of course write the references yourself — `<Picture 2> shows the midpoint`
in your own prompt does the same job. The spec field just automates it and adds the
timing.

### How the anchor is placed

Core hard-codes two cases:

```python
if   pixel_index == 0:                  cond_t = text_len
elif pixel_index == frame_count - 1:    cond_t = text_len + sum(spans) - FRAME_RESCALE
else:                                   raise ValueError(...)
```

Both are the same formula written twice:

```
cond_t = text_len + FRAME_RESCALE × pixel_index
```

because `sum(spans)` always equals `FRAME_RESCALE × frame_count` — the `(1,4,4,4,4)`
token pattern covers 17 pixel frames per 5 latent tokens, which *is* the `17k+5` grid
the model snaps to. Verified identical to core's values for every length from 5 to
702 frames. A middle keyframe was always a well-defined position; core just never
implemented the general case.

The patch doesn't reimplement the layout. `cond_t` is written to exactly one place —
`position_ids[:, 0]` — and nothing else in the constructor depends on the frame
index, so we let core build the whole layout and rewrite that one column. Workflows
using only first/last anchors never enter the patched path and are byte-identical to
stock.

## How it works

H3 feeds keyframes to the DiT as **condition rows** that ride through every
sampling step and are never denoised. For a strength `s < 1` the node blends the
keyframe's condition latent toward noise with the **linear flow-forward** form —
the same maths as core's global noise aug:

```
z' = a·z + (1 − a)·noise,     a = s · 0.999
```

— and, via a per-row patch of the model's timestep table, **relabels that row's
modulation timestep to `a`**. That relabeling is the whole trick: the model was
trained on references at every noise level, so a 0.65 keyframe now reads as "a
reference at noise level 0.65" (in-distribution, soft guidance) instead of "a
clean image that happens to be full of static" (which it dutifully painted into
the clip as distortion — the earlier behavior). Each keyframe and reference
carries its own label, which core's single global knob cannot do.

Works with the turbo LoRA too — its rebuilt modulation grid interpolates at
arbitrary timesteps, and the compat shim feeds it the per-row values. When the
label patch can't install (core drift upstream), the node falls back to the
linear blend with the global label — better than the old variance-preserving
blend, but softer strengths may drift; the log says when this happens.

Each frame gets its **own noise stream**. Core reuses a single stream for every
condition latent, which is harmless when only one is diluted — but weaken both
and they'd receive the identical noise field at each end of the clip, a static
texture the model can lock onto. The seeds are pinned to the *role*, not to list
position, so a frame's noise never shifts depending on whether the other frame
happens to be wired up.

The **text encoder still sees the full-strength images**. Qwen's description of a
keyframe is a far softer constraint than the condition latent, and blunting it
too would just cost you prompt understanding. Only the latents are diluted.

## Reference images

Connect up to four `ref_image_` inputs. Unlike keyframes, references are **not tied
to a moment** — they condition the whole clip. A keyframe says "the frame at 2.5s
looks like this." A reference says "this is what the subject looks like," full stop.

This is the strongest combination in the pack: **references lock identity while
weakened keyframes free the motion.** Better than framing one photo three ways,
because the reference can be a completely different shot of the same person.

Refer to them in your prompt by number — `<Picture 4>`. Keyframes are numbered first,
then references, and the node **prints the numbers to the log** when you run so you
don't have to count.

| Input | Notes |
|---|---|
| `ref_spec` | One strength per image, in slot order. Empty = `1.0` for all. `0.7` softens a reference into a likeness hint rather than a lock. (Or just drag the sliders on the timeline.) |
| `ref_image_size` | `match` scales each reference to the generation's pixel area. `max` uses the 2048px short edge for best identity fidelity. |
| `ref_megapixels` | Optional area cap, e.g. `0.4`. Overrides `ref_image_size` when above 0 — each reference is scaled down (never up) to at most this many megapixels, so no external resize nodes are needed. Keyframe inputs never need capping; they're stretched to the generation canvas regardless. Sizes round to the model's 32px grid, so the result can land a hair over the cap. `0` = off. |

**`max` is expensive.** Reference rows ride through *every* sampling step. On a
3000×2000 source: `match` costs 1014 sequence rows, `max` costs 5828 — against a
37296-row target. That's a permanent tax on the whole sample, not a one-off.

### Stock ComfyUI cannot do this

Two separate blockers, both fixed here:

**The tokenizer's paths are mutually exclusive.** Pass `minimax_ref_items` and the
`images=` list is ignored outright:

```python
if minimax_ref_items:  ...        # references
else:                  ...        # keyframes — never reached
```

But the reference branch emits exactly the same `"<Picture N>: " + vision` block the
keyframe branch does, so when references are present this node routes *everything*
through `ref_items` — keyframes first, then references. Identical token stream, no
tokenizer patch needed. With no references it stays on the stock path, byte-for-byte.

**`extra_conds` clobbers its own work.** It assigns the DiT's condition-latent list
twice:

```python
payload["cond_video_latents"] = [kf["latent"] for kf in keyframes]   # then...
payload["cond_video_latents"] = [r["latent"] for r in refs ...]      # ...clobbered
```

So the packed layout reserves rows for both while the model supplies only the
references — a shape mismatch. The layout was never the problem; it packs both
correctly. The patch just concatenates in the order the layout allocates. Verified:
2 keyframes + 2 references need 3280 rows and the merged list supplies exactly 3280.

It also means this **has** to live in one node — you can't chain this to
`MiniMax H3 Reference to Video`, because whichever runs last owns the single
tokenize call and the other's presentation is lost.

### Reference masks

Each `ref_image_N` can take a matching `ref_mask_N` — **paired by slot number**, so
`ref_mask_2` masks `ref_image_2` even if slots 0 and 1 are empty. White keeps the
reference at full strength, black dilutes it to noise. Use it to take just a face or
a subject out of a busy photograph.

H3 has no native masked-reference mechanism — a reference block carries only
`{kind, latent_h, latent_w, latent}` and attention runs with `mask=None`. What this
does instead is make the strength dial **spatial**: the same linear blend, with
a per-latent-position value rather than one number.

```
z' = s·z + (1−s)·noise        # s is now a map, not a scalar
```

Mask and `ref_spec` strength **compose** — a white mask at strength `0.6` gives 0.6
everywhere; a half mask at `0.6` gives 0.6 over the subject and 0 elsewhere. A fully
white mask is bit-identical to no mask at all.

`mask_ref_pixels` controls whether the mask also applies to the image handed to the
text encoder. Off (default), only the latent is masked and the encoder still sees the
whole photo. On, the excluded area is greyed out so it describes only the kept region
— more consistent, but it sees a hard-edged cutout, which it may read oddly.

**It saves no compute.** Masked-out regions still occupy their sequence rows and still
ride through every sampling step. You are reducing influence, not cost — dropping
rows would need `PackedLayout` surgery, since row counts derive from the latent grid.

If you wire a Load Image `MASK` output from a file with no alpha you get an all-black
mask, which nulls the reference completely. The node warns when that happens rather
than leaving you wondering why the reference stopped working.

### Reference audio

Up to three `ref_audio_` inputs, plus `audio_vae` (the MiniMax H3 audio VAE — only
needed when audio is connected). A voice, a room tone, a music bed: like image
references it conditions the whole clip rather than a moment. Cite it in the prompt
as `<Audio N>`; the numbers are logged when you run.

Input audio is resampled to the VAE's rate automatically — a 48 kHz and a 32 kHz clip
of the same length produce identical latents.

`ref_audio_strength` applies to all connected audio. Worth knowing: **H3 leaves audio
conditioning completely clean by default** — its `audio_cond_noise_aug` is `1.0`,
meaning no noise at all, unlike the video side's `0.999`. So this dial is the only
way to soften an audio reference.

The timeline editor has an **audio section**: every connected or picked audio shows
as a card with its `<Audio N>` number and an inline player, plus the shared strength
dial with a live caption, and a red warning if `audio_vae` isn't connected. Audio no
longer needs a socket at all:

- **+ audio…** picks a wav/mp3/m4a/flac from the input folder (with upload), written
  to `ref_audio_files` — decoded server-side by the same PyAV path as LoadAudio.
- **● record mic** records straight from your microphone into the node: the take is
  re-encoded to 16-bit WAV in the browser (no codec guessing server-side), uploaded
  to the input folder as `h3-mic-<timestamp>.wav`, and added as a reference. Press
  again to stop; the browser will ask for mic permission the first time.

Sockets and files combine (sockets first) up to H3's limit of 3 audios total.

### Turbo LoRA compatibility

If you use **ComfyUI-MiniMax-H3-Turbo**, reference audio crashes it:

```
RuntimeError: The size of tensor a (3) must match the size of tensor b (2)
              at non-singleton dimension 0
```

The turbo LoRA re-injects its adaln update at run time, so it has to rebuild the
DiT's per-timestep row table itself — and its copy of that logic has no audio branch
at all:

```python
s = {t_v, t_a}
if has_vis_cond:
    s.add(max(t_v, 0.999))
return sorted(s)                  # core also adds max(t_a, 1.0) for ref_audio
```

So core builds 4 rows and turbo builds 3 (or 3 vs 2 at the ends of the schedule,
where `max(t_v, 0.999)` collapses onto `t_v` — that's the exact error above).

**This is not caused by this pack.** Core's own MiniMax H3 Reference to Video hits it
identically with any audio reference.

This pack ships a small shim (`turbo_compat.py`) that replaces the turbo pack's
injection with a corrected copy. It installs at import, no-ops when that pack isn't
present, and steps aside if the upstream signature changes (taken as a sign it's been
fixed). Two further latent bugs are corrected while we're in there:

- `has_vis_cond` was `bool(keyframes or refs)`, so an audio-**only** reference counted
  as a visual condition. Doesn't crash — the counts happen to coincide — but silently
  feeds the wrong adaln rows.
- The sigma shifts were read only from the model, ignoring what
  `MiniMax H3 Sigma Shift` puts in `transformer_options`, so a custom shift gave a
  wrong `t_a`.

A fork of the turbo pack would have been worse: duplicate node IDs
(`MiniMaxH3TurboSampler`, `MiniMaxH3TurboLoRA`) would collide with the original.

### Reference video

Up to three reference videos — the real **motion + identity** conditioning, and the
key to seamless clip chaining. Cite them as `<Video N>` in the prompt.

Sources, combining sockets-then-files like everything else:

- **`ref_video_` sockets** (frames at 24fps from any video loader) with optional
  index-paired `ref_video_audio_` soundtracks — exactly core's contract.
- **`ref_video_files`** — mp4/webm/mov straight from the input *or output* folder
  (the editor's **+ video…** picker has both tabs; the output tab is how you chain
  motion from a previous render). Files are resampled to 24fps by frame index,
  capped at the trained 15s, and an **embedded soundtrack is used automatically**.

`ref_video_spec` gives one strength per video (empty = 1.0), applied to the video
latent *and* its soundtrack together. Soundtracks take `<Audio N>` numbers **before**
standalone audio, matching the presentation order; the editor's chips mark standalone
numbers with `~` when a file video's embedded audio can't be confirmed client-side
(the server log always has the exact numbering).

**`ref_video_megapixels`** caps frame area (e.g. `0.4`) — aspect-preserving,
down-only, on the 32px grid, never a squish. `0` uses the model's own
768-short-edge canvas rule. This is the single biggest speed dial in the pack:
video reference rows ride through every sampling step *multiplied by duration* —
the cost meter includes them (always as an estimate).

The editor's **VIDEO REFERENCES** section shows each video as a playable card with
its `V` chip, soundtrack badge, per-video strength slider and ✕; drag a video file
from your OS straight onto the section.

**Framing videos:** every video card has the same ⛶ framing tool as images —
essential when continuing footage of a different aspect. The window keeps the
source aspect (pure zoom/pan), applies *before* sizing (so the MP cap operates on
your chosen region), and the framer gains a **scrub bar** so you can check the
window against several moments, not just the first frame. Stored in
`ref_video_crops`, captured by setups, carried through rewires.

Honest limit: a reference video conditions *style, motion and identity* for the
whole clip — it is not a guaranteed frame-exact continuation. For chaining, pair it
with the output-tab **first frame** (exact pixel continuity) and let the video ref
carry the momentum.

## 🎭 Cast — people that persist across clips

The model has no memory between runs; identity lives in whatever references you
attach. The **cast library** makes that persistent at the workflow level: the
🎭 button on the references header saves your current file-sourced references
(with their strengths *and framings*) plus reference audio as a named cast
member — `h3cast-<name>.json` in the input folder — and loads any saved member
into any clip, in any workflow, with one click.

- Save: pick a name, tick which refs/audio belong to the person. Socket-fed refs
  can't travel in a file (the modal says so) — pick them via **+ reference** first.
- Load: every member listed with thumbnails; **add** appends their references
  with strengths and face-crops intact, correctly `<Picture N>`-numbered.
- Remove: delete the `h3cast-….json` from the input folder (no server delete API).
- Moving machines: cast files reference images by name — bring the input folder.

## Video-to-video (experimental)

**MiniMax H3 Video To Latent** encodes existing footage (+ its soundtrack) into
the H3 AV latent. Sample that latent with an ordinary KSampler at **denoise
0.3–0.7** and the clip is restyled while keeping its motion — img2img for video.
Output shapes exactly match the empty-latent node, so it drops into any H3
sampling workflow.

Honest framing: v2v is **not a trained H3 task**. Video restyling should behave
the way img2img behaves everywhere; the audio stream runs a shifted sigma
schedule internally, and partial denoise across that dual schedule is untested —
the node zero-fills audio if you leave it disconnected.

### Section restyle — bake it back in

Restyle just the seconds you want and export the whole clip untouched elsewhere:

```
Load Video → Get Video Components ─┬─→ H3 Frame Range (2.0s–5.0s)
                                   │        │
                                   │   H3 Video To Latent → KSampler (denoise ≈0.5)
                                   │        → decode as in your H3 workflow
                                   │        │
                                   └─→ H3 Splice (original + restyled section)
                                            → Create Video → Save Video
```

- **H3 Frame Range** cuts frames + sample-accurate audio by seconds.
- **H3 Splice** replaces exactly those frames in the untouched original, with a
  linear audio crossfade at both joins (`audio_crossfade_ms`, 0 = hard cut). A
  section spliced back unmodified reproduces the original — bit-for-bit at
  `audio_crossfade_ms = 0`, within float epsilon (~1e-7) with a crossfade.
- Continuity tip: pin the restyled section's **first/last frames from the
  untouched footage** (output-tab picker + framer on the Guide node) so the
  joins can't drift.

### Soft denoise zones (differential diffusion for H3)

**H3 Soft Denoise Zone (v2v)** gives v2v a spatially-varying denoise: a
feathered circle (dials on the node) or any MASK input, where white areas get
`inner_denoise`, black get `outer_denoise`, and greys blend — as *fractions of
the sampler's own denoise*, which stays the master dial. The classic ask —
"reinvent the subject in the centre, barely touch the environment" — is
`inner 1.0 / outer 0.3` with a wide feather.

The mask input accepts a **per-frame batch** as well as a single mask: feed
SAM2 video segmentation (or any per-frame matte) and the zone follows the
subject through time — each latent step pools its group of pixel frames
(H3's (1,4,4,4,4) time grid), which also softens motion temporally.
`mask_feather` grows-then-blurs hard mattes so the falloff extends outward
while the subject's interior keeps full strength; mask frame counts that
don't match the clip are resampled by index.

Mechanically this is Differential Diffusion (the same idea core ComfyUI ships
for image models): the soft map is thresholded against sampling progress every
step, and not-yet-participating pixels are re-injected from the clean footage
latent — re-noised to the current sigma on the way in, pinned to the footage
in the denoised prediction on the way out. All blending happens in noise
space, per step, which is why soft maps give seamless transitions instead of
matte lines. Core's node can't run on H3 (the AV latent is a packed
video/audio pair the mask plumbing was never wired for), so this node
implements the loop in a model wrapper: video stream only, audio untouched,
composes with the wired-denoise scheduler or any other. Wire the Guide's v2v
LATENT into both the node and the sampler.

**EXPERIMENTAL** — to our knowledge the first spatially-varying denoise on
H3. Verified headlessly (threshold maths, flow re-noising, run detection,
mask path — 18 checks); the perceptual result on a video model with global
attention is exactly the thing only a render can prove. If the feather band
shows content disagreement, widen the feather.

### Regional prompting (H3 Regional Prompt)

The zone node controls where change is *allowed*; **H3 Regional Prompt**
supplies what belongs there. A fragment that appears verbatim in your prompt
("a woman in a red coat") is located in the tokenised text, and the DiT's
attention is biased so the masked region's video rows attend harder to those
tokens (`strength`, additive on the attention logits) while the rest of the
frame attends less to them (`containment`) — the description is pulled into
the zone and kept out of everywhere else. Same mask input as the zone node
(single or per-frame SAM2 batch, same feather); share the wire and you have
the full region-swap stack: *only this region may change, and this is what
belongs in it*. It also works with no v2v at all — spatial subject placement
in plain generation.

This is possible on H3 because the model runs one global attention over a
single packed sequence (text, conditions, audio and video rows side by side),
so "these pixels should listen to those words" is an additive bias on
specific attention entries. No core edits: it rides comfy's own
`optimized_attention_override` hook plus a forward wrapper that reads the
packed layout each run; the negative/uncond pass is detected by text length
and left untouched.

**EXPERIMENTAL** — to our knowledge the first regional prompting on H3, and
attention bias is a dial the model never trained with: expect a usable range
(~1.0–2.5) and a too-high range that degrades composition. Costs a dense
S×S bias (~200 MB) and usually moves attention off the flash kernel while
armed. One regional node per graph for now. Verified headlessly against real
packed layouts (bias lands exactly on zone-rows × fragment-tokens, uncond
untouched, 16 checks).

### v2v from the Guide node itself

You don't need the standalone nodes for the common case — the Guide node takes
v2v source footage directly, "piped in at the side":

- **`v2v_images`** / **`v2v_audio`** sockets: feed frames (e.g. from
  `Get Video Components`) and optionally their soundtrack.
- **`v2v_video_file`**: or just name a video from the input folder — its embedded
  soundtrack comes along automatically (an explicit `v2v_audio` socket wins).
- **`v2v_start_seconds`** / **`v2v_end_seconds`**: restyle only a slice
  (`0` end = to the end); audio is cut sample-accurately with the frames.

When a v2v source is present the Guide node's **latent output is the encoded
footage instead of an empty latent** — sample it at denoise 0.3–0.7 exactly as
with `H3 Video To Latent`, keeping every other Guide feature (keyframes, refs,
beats, cast) in play. The fullscreen editor shows a **🎞 v2v bar** whenever a
source is connected or named, with the file picker and the seconds range inline.

## Motion context (⏭▶) — joins with real continuity

Classic chaining hands the next clip a still frame: pixel continuity, but the
model re-decides instantaneous motion from a standing start, and reference
audio is only ever *imitated* — a cover band, not the same recording. With a
clip in the reel, **▶ queue opens a chooser**: continue **with motion**,
continue **the classic way** (final frame → first frame *and* the clip into
the video-reference slot), or just render — from any reel clip, **newest by
default**, a trimmed card continuing from its OUT point. The motion option
fixes both problems at once:

- The previous clip's **tail frames** (default 22) are encoded in **one VAE
  call** and pinned at the new clip's head **on its own timeline** — one
  never-denoised condition block per latent step, at its exact frame offset.
  The motion between those frames lives inside the pinned latents, so the new
  clip picks up the same velocity and direction, not just the same pixels.
- The **tail audio** rides the reference machinery for construction, then its
  time coordinates are rewritten to end exactly at the join. That single
  coordinate change is what turns "similar music" into **the same waveform,
  continued** — the upstream pack this technique comes from measured join
  correlation going from ~0.45 (incoherent timing) to 0.95+ with a stable
  offset.

Practicalities:

- The chaining loop is *queue → choose → render → 🎞 add to reel → queue
  again*. **🎲 re-roll** retakes with the same choice and source. Choosing
  motion removes a classic-continuation video ref the chooser added earlier
  (they'd double-condition); refs you added yourself are never touched.
  **pick clip…** in the MOTION bar offers a file outside the reel as the
  chooser's "picked" source.
- The render **opens by repeating the pinned tail** (~0.92 s at 22 frames).
  **🎞 add to reel** sets the new card's in-trim automatically, so an export
  never duplicates the join — non-destructive, adjust it on the card if you
  want.
- The model's first generated frames after the pinned block **zigzag in
  brightness** (measured ~8% bright for a frame, then ~10% dark, converging
  within half a second) — a tiny flash on a hard cut, most visible in dark
  scenes. Export **luma-matches** the join automatically for motion clips:
  a decaying gain correction toward the previous clip's closing level,
  export-only, never baked. The ✂ popup's "✨ luma-match join" checkbox
  controls it per clip.
- Regeneration can also run a few percent **darker per link**, which
  compounds invisibly down a chain once the joins are smoothed. The same
  checkbox therefore also applies a **whole-clip level match**: one constant
  gain (clamped ±20–25%) anchoring each clip's settled brightness to its
  corrected predecessor, so a chain stays level by construction — verified
  flat over five links at 4%/link drift. Motion joins are same-scene by
  definition, so a large level shift there is drift, not intent; on very
  long chains the clamp saturates gracefully and a chain restart at a
  natural transition remains the honest fix.
- The **⏭▶ MOTION bar** holds the dials: frames (snapped down to the VAE's
  5/22/39 run grid) and audio frames (end-aligned with the video window; 0 =
  picture only, needs `audio_vae` otherwise). The pinned span shows hatched
  at the head of the timeline.
- Under a context, `first_frame` is ignored (the context IS the opening),
  waypoints must sit past the pinned span, and the context rows are ground
  truth — full strength, untouched by the strength dials.
- **Cost**: every pinned row rides through all sampling steps. 22 frames ≈ 7
  extra cond blocks; 5 frames is the budget option. Spend ⏭▶ only on joins
  where continuous motion crosses the cut, and use plain ⏭ elsewhere.
- **Sound dulls down a chain.** Each clip's audio regenerates the previous
  clip's *output*, so losses compound like photocopies — timing stays locked,
  but after several motion-joins the top end goes first. Restart the chain at
  a natural transition when you hear it.

**Credit:** the technique — including the audio-timeline discovery and its
seam-probe verification — is from the **ComfyUI-H3-Motion-Context** pack,
absorbed here natively so it composes with this pack's layout patches, refs
and reel. **Don't run both packs in one ComfyUI session**: both patch the same
H3 layout machinery, and with this pack's class replacement active the other
pack's constructor wrapper goes dead, silently anchoring its context frames at
frame 0. Use one or the other.

## Timed text (experimental)

Beats of description pinned to moments in the clip — **no image required**. One line
per beat in `timed_text`:

```
0.2, the boat pulls away from the dock
0.8, only wake remains on the water
```

Position is a fraction of the clip, or an absolute frame number above `1.0`.

`timed_text_mode` picks how the timing actually reaches the model, and the three
options are a deliberate A/B rig:

| Mode | What happens | Patches core? |
|---|---|---|
| **`text only`** *(default)* | Appended as prose: `At 1.0 seconds: the boat pulls away.` Pure prompting. | No |
| **`rope + text`** | Same prose, **and** those tokens are moved onto the video timeline. Degrades to `text only` if the positional trick does nothing. | Yes |
| **`rope only`** | Positions moved, no time words at all. The purest test — and the most likely to fail. | Yes |

**Try `text only` first.** The model has seen `<N.N seconds>` labels in its
reference-video training, so that vocabulary is already familiar. If plain timed
prose steers the clip, you're done and nothing gets patched.

### What the rope modes actually do

Text tokens normally sit at `t = 0 … text_len-1`, and the video timeline starts at
`t = text_len`. Those are **the same axis** — laid end to end, not separate — which
is exactly why a keyframe anchor is `text_len + FRAME_RESCALE × frame_index`.

So a span of text can be given a video-time coordinate. Rather than the *words* "at
2.5 seconds", RoPE places those tokens **at** 2.5 seconds — the same mechanism that
anchors a keyframe.

Two details that matter:

- The span keeps **unit spacing** (`base+0, base+1, …`) instead of collapsing to a
  single `t` like a keyframe's condition rows. Those rows can share one `t` because
  they're spread across h/w; text sits at `h = w = 0`, so identical `t` would make
  every token in the beat indistinguishable and destroy reading order.
- Spans are recorded as offsets **from the end** of the sequence. The prompt is always
  emitted last, after the vision blocks, so end-relative offsets survive whatever the
  images expand to.

Untouched text keeps its original positions; only the beat spans move.

## Load Image (Zoom & Pan)

A second node in this pack, under `image`. A normal Load Image with a virtual camera
bolted on — and a **live preview you frame by hand**:

- **drag** the image to pan
- **scroll** to zoom, anchored on the cursor so the point under the pointer stays put
- **double-click** to reset to the full frame
- rule-of-thirds guides appear while dragging
- the HUD shows current zoom, crop size and output size, and turns orange the moment
  you zoom past the point where real detail runs out

The canvas renders **exactly** what the node outputs — same crop maths as the Python
side, verified identical across 400 combinations of source size, output mode, zoom
and centre. What you frame is what you get.

The `zoom` / `center_x` / `center_y` widgets stay live and in sync, so you can drag
to get close and then type an exact value, or copy numbers between nodes to match
framings precisely.

**Why it's here.** Point two or three of these at the *same* source image, vary only
the zoom and centre, and wire them into first / middle / last. The keyframes are now
different framings of one photograph — a push-in, a pan, a reveal — and the scene
physically cannot drift between them. No second subject, no changed lighting, no
wandering background.

That matters specifically because you're running below 1.0 strength. A weakened
anchor lets the model reinterpret content, and you want it reinterpreting *motion*,
not *what the scene is*. Same pixels at every anchor removes that whole failure mode.

**No quality cost.** The crop is taken from the full-resolution original and resized
exactly once. Zooming into a 4000px source for a 1344×768 output is free up to
zoom 2.98 — the crop is still larger than the output. Past that the node logs the
zoom at which detail runs out. Compare with zooming a pre-resized image, which
discards detail and then upscales what's left.

| Input | Notes |
|---|---|
| `zoom` | `1.0` = whole image. `2.0` = half the width and height. Crop keeps the output aspect and is clamped to the source edges. |
| `center_x` / `center_y` | `0.0`–`1.0`. No effect at zoom 1.0, where the crop already fills the frame. |
| `output_width` / `output_height` | Default `1344×768` to match the H3 node. Set either to `0` to take it from the source, or to derive it from the other axis. Fully editable — the buttons below are shortcuts, not a mode. |
| `resample` | `lanczos` default — sharpest for downscaling, which is the normal case here. |

Two shortcut buttons sit directly under the size fields:

- **⤢ snap to image size** — sets output to the loaded image's exact pixel
  dimensions. Turns the node into a plain full-resolution loader that you can still
  zoom and pan.
- **⇔ match image aspect** — keeps your current width and sets the height to the
  source's aspect ratio. This is usually the one you want for H3: you keep a
  sensible pixel budget and just stop the framing being letterboxed or cropped.

Both confirm on the canvas, and both respect the fields' limits. Heights are rounded
to the widgets' step of 8, so the aspect can drift under half a percent at awkward
ratios (16:9 at width 1344 wants 756, which falls between two multiples of 8) —
invisible in practice, but it's a rounding, not an exact match.

Set these to the **same width/height as the H3 node** and its internal resize becomes
a no-op, so your pixels are resampled exactly once from source to latent.

### A push-in in three nodes

Three loaders on one image, all `1344×768`. Frame each by dragging on its preview —
these numbers are just where you'd end up:

| | zoom | center |
|---|---|---|
| first_frame | `1.0` | `0.5, 0.5` |
| middle (`0.5, 0.6`) | `1.4` | `0.5, 0.45` |
| last_frame | `1.9` | `0.5, 0.4` |

Set `first_frame_strength` to `1.0` and `last_frame_strength` to `0.7`, and you get a
push-in that's anchored to a real photograph but free to animate within it.

## Honest limits

- **Not a temporal fade.** Strength is constant across sampling; the end frame
  doesn't "fade in" over steps. A per-step schedule would need sampler patching.
- **The noise patterns are deterministic** (fixed internal seeds), mirroring how
  core restarts the same RNG stream per condition. Same strength always gives the
  same guide — reproducible, but you can't reroll the dilution for variety.
- **The model is told the row is clean.** At low strengths you're feeding it a
  noisy latent labelled as near-clean. This is exactly what weakens adherence,
  but it is a mismatch — below about `0.3` expect that end of the clip to get
  unpredictable rather than gracefully vague.
- **Low `first_frame_strength` is the riskier dial.** The start frame anchors the
  shot's geometry, so diluting it can destabilise composition for the whole clip,
  not just the opening. Treat sub-`0.5` values here as experimental.
- **Middle frames are out of distribution.** H3's training tasks are t2va, i2va
  and fl2va — first and last anchors only. The RoPE positions a waypoint gets are
  mathematically correct, and the model will attend to it, but whether it reads as
  "be here at 2.5s" or smears across the clip is genuinely unknown. Treat waypoints
  as experimental, and keep their strength low. This is the one feature here that
  might simply not work well; the strength dials are on much firmer ground.
- **The `rope` timed-text modes are the biggest gamble here.** A middle keyframe at
  least reuses a row type the model knows (`cond`) at a coordinate type it knows
  (video time). Moving text into video time changes what the text span's coordinates
  *mean*, and text has lived in `[0, text_len)` for every sample the model ever saw.
  It may do nothing; it may scramble the prompt. That's why `text only` is the
  default and why the mode switch exists — so you can measure it rather than trust it.
- **Middle frames monkey-patch a core class.** `PackedLayout` is replaced at
  import. It's surgical and self-checks on load, but a future ComfyUI change to
  the H3 layout could break it — it raises a clear error rather than silently
  producing garbage, and stock first/last workflows bypass it entirely.
- **Untested against `MiniMax H3 Reference to Video`.** This only touches the
  keyframe path. Reference conditioning is a separate mechanism.
- **Motion context inherits its source pack's test breadth**: the mechanism was
  verified upstream on one machine, one resolution, one sampler (plus our
  headless maths cross-check against that pack's own formulas). Audio quality
  degrades cumulatively down a motion-chained sequence; a small constant
  ~10 ms audio offset per context clip was measured upstream and is unfixed.
  And it conflicts with running ComfyUI-H3-Motion-Context itself in the same
  session — pick one pack.
- **Above `1.0` is unclamped and off-distribution.** It's there because
  sometimes you want it. It can also blow out the ending.
- Inherits every stock H3 constraint: batch size 1, frame counts snap to the
  17k+5 grid at 24fps, trained range roughly 124–362 frames.
- **Zoom & Pan is a still-image framer**, not a motion path. It gives you one crop
  per node — the animation between crops is the model's job, not the node's. If you
  want an actual interpolated camera move you want a Ken Burns node, not this.
- Zoom & Pan reads via Pillow, so it handles the usual still formats. Multi-frame
  files (animated WebP/GIF) get every frame cropped identically; frames whose size
  differs from the first are skipped, matching stock Load Image.
- **Timeline thumbnails are best-effort.** They come from the upstream node's own
  preview (LoadImage, Zoom & Pan). A source with no browser-side preview — VAE
  Decode output, most image-processing nodes — shows a labelled placeholder
  instead. The marker still works; only the picture is missing.
- The timeline widget hides the three spec text fields by default. If a future
  ComfyUI frontend changes widget-hiding behaviour, the `✎ raw text specs` toggle
  and the fields underneath are the fallback — the node is fully operable without
  the timeline, which also covers browsers where the extension fails to load.

## Support

If this tool saves you time or fits into your workflow, consider [buying me a coffee](https://buymeacoffee.com/lorasandlenses). Members get early access to new builds before public release.

[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20me%20a%20coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black)](https://buymeacoffee.com/lorasandlenses)

---

Peter Neill — [ShootTheSound.com](https://shootthesound.com) / [UltrawideWallpapers.net](https://ultrawidewallpapers.net)

Background in music industry photography and video, which is where most of these
tools come from — they get built because a real shoot needed them.

Feedback is welcome — open an issue or reach out.

## License

MIT
