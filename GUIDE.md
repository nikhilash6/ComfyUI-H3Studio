# H3 Guide — quick recipes

Add the **MiniMax H3 Image to Video (Guide)** node, click **⤢ open timeline editor**, work in fullscreen. Everything below assumes the editor unless it names a widget. Full detail: [README](README.md).

---

### I want a clip from one image
Click the **first frame** card → pick an image. Type a prompt. **▶ queue**.

### I want it to end on a specific image
Set the **last frame** card. Strength `1.0` = land exactly on it; `0.6–0.8` = treat it as a guide and keep the motion natural.

### I want it to pass through an image mid-clip
**+ waypoint** → pick the image → drag its marker along the track to the right moment (or double-click empty track space to drop one right there). Frames drop in at full strength (`1.0` = hit exactly); lower to `0.6–0.8` to keep the composition but free the motion. Add a short description on the card so the model knows what it's looking at.

### I want a cinematic pan/zoom across one image (Ken Burns, but alive)
**✦ motion path…** on the KEYFRAMES header → pick the image → drag the **A** (start) and **B** (end) windows, wheel to zoom them. Choose waypoint count and a **speed curve** (ease-in-out etc.) — tween waypoints are placed on the timeline automatically, windows extrapolated along the curve. **▶ preview move** shows the exact camera move before you spend a single step. Keep tween strength ~0.5 so the model adds parallax and life; drag any placed waypoint to hand-tune the timing.

### I want to cut and keyframe to music
**♪ music** in the TIMELINE header: pick an audio file (upload, input folder, mic, or the free web search) and its **waveform + detected hits** draw on the lane. (Careful with words here: **"text beats"** are your timed prompt lines; **"hits"** are the green ticks the music detection finds. They share a lane, not a meaning.) **▶** auditions exactly the window under this clip, and the **use** dropdown says what the music is for — *timing only* (default), *soundtrack* (mixed into the export, level and fades in the REEL header), *model reference* (the model imitates its character), or both — place waypoints and beats on the music by eye, or turn on **snap ♪** and they click onto the nearest hit as you drag. **at N s / follow reel** controls which slice of the song sits under this clip — *follow reel* tracks the reel's running duration, so each new clip automatically sees its own part of the track. On *timing only* the music changes nothing about the render or the export — it's there to aim at. Each reel card has its **own volume** (🔊 slider under the thumbnail) for the clip's *generated* soundtrack — drop it, or hit 🔇 to mute that clip entirely, when the music bed and fx tracks are carrying the piece. **🔇 mute clips** in the header does all of them at once (click again to restore). Export-only; the render files are untouched, and ▶ play reel previews the levels you set. To put the song IN the finished piece, set **♪ music N %** in the reel header — the export mixes it under the clip audio (song starts at reel time 0 in follow mode, at the offset otherwise), with **♪fade in/out** seconds easing just the bed at either end (you're usually cutting into the middle of a song). **▶ play reel** previews the whole mix live — level slider in the player, fades included.

### I want to describe what happens at a moment (no image)
Click the **beats lane** under the track at the right moment, type the text — that's a **beat**. Leave `timed_text_mode` on `text only` first; try the rope modes only if plain timing doesn't steer. (A beat can be upgraded to a waypoint later — right-click it.)

### I want the same person in every clip
Add their photos with **+ reference** (strength `1.0` locks identity, `0.7` = likeness hint). Then **🎭 cast** → **💾 save cast member** once — from any other clip or workflow, **🎭 cast** → add, and their refs, strengths and face-crops come back in one click.

### I want a reference voice or sound
**+ audio…** to load a file, or **● record mic** straight into the node. Say `<Audio 1>` in the prompt to invoke it. Max 3.

### I want free images/sounds without leaving the editor
Any picker → **🌐 web…** → type a search, press Enter. Results come from Openverse (all Creative Commons / public-domain licenses) with license + creator shown on every card — check it if the clip is commercial (NC = non-commercial); clicking one downloads it into `input/web/` and uses it immediately. Every pull is logged to `input/web/credits.txt` for attribution. Works in the image pickers and the audio picker (with preview players).

### I want motion/style from existing footage
**+ video…** in VIDEO REFERENCES. Its soundtrack rides along automatically. Cap cost with `ref_video_megapixels` (e.g. `0.4`). Max 3.

### I want to continue my previous render
Set the first frame from the picker's **output folder** tab (newest first) — grab the last frame of the previous clip. Add the previous clip as a video reference if you also want its momentum.

### I want to continue an external video file (from my drive, someone else's render…)
**+ video…** → **upload…** (any file on disk), or drag it from Explorer onto the VIDEO REFERENCES section — that carries its look, motion and sound. Then click **⏭** on the video's card: its **final frame becomes the clip's first frame**, one click, done. Different aspect? The toast tells you — **⛶** the first-frame card. (Graph alternative: `Load Video → Get Video Components → Image From Batch` → `first_frame` socket.)

### I want the next clip to continue the last one
Hit **▶ queue** — with a clip in the reel, a chooser asks how this render should continue (defaulting to the **newest reel clip**; a trimmed card continues from its OUT point):
- **⏭▶ continue with motion** — the clip's tail frames **+ audio** are pinned at the new head on its own timeline: same motion, same direction, the **same waveform continued** (not imitated). Costs extra conditioning rows (dials in the MOTION bar — 22 frames is the sweet spot, 5 is cheap); the render opens by repeating the pinned tail, which **🎞 add to reel auto-trims** so an export never duplicates the join, and if a join flashes (the model's first free frames can swing ~10% in brightness) tick **✨ luma-match join** in that clip's ✂ popup — off by default, since it alters picture. The pinned span shows hatched at the head of the timeline. Sound dulls slightly at every motion-join down a long chain — restart the chain at a natural transition when you hear it. Technique credit: the **ComfyUI-H3-Motion-Context** pack, whose seam-probe work proved the audio-timeline trick.
- **⏭ continue the classic way** — the clip's final kept frame becomes the first frame **and the clip goes into the video-reference slot**: composition plus look/momentum carry over, cheap, but motion restarts at the join and audio is a sound-alike.
- **▶ just render** — no continuation.

The loop is *queue → choose → render → 🎞 add to reel → queue again*. **🎲 re-roll** retakes with the same choice and source. To continue from a file that isn't in the reel, use **pick clip…** in the MOTION bar — the chooser then offers it as the "picked" source.

### I want to fix a wrong crop / aspect mismatch
Every image and video card has **⛶** — drag/zoom the window over what the model should see. Keyframes are locked to the output aspect, refs/videos keep their own. Corner-drag for exact size, wheel to zoom (Shift = fine), scrub bar on videos.

### I want to restyle existing footage (v2v)
Pick footage in the editor's **🎞 v2v bar** (or feed the `v2v_images`/`v2v_audio` sockets); the latent output becomes the encoded footage. Sample at **denoise 0.3–0.7** — the bar has a **denoise slider** that flows out of the node's `v2v_denoise` output: wire it → **H3 Basic Scheduler (wired denoise)** → your sampler's sigmas and the editor drives the restyle amount (with a plain KSampler, match its denoise by hand). **✂ section…** scrubs the clip and sets the in/out points visually; **⛶** reframes the footage to your width×height canvas (landscape → vertical, etc.) — unframed, the canvas follows the footage instead. The selected section shows as a **ghosted filmstrip behind the timeline** so your keyframes and beats sit over the actual footage; the "ghost" dial sets its opacity (0 hides it).

### I want to restyle one REGION of the frame harder than the rest (soft zone)
**H3 Soft Denoise Zone (v2v)** — a feathered circle (or any MASK) where the denoise itself fades across the falloff, no matte lines. The mask input takes a single mask (whole clip) **or a per-frame batch** — run the v2v frames through SAM2 video segmentation and the zone *tracks the person* as they move (`mask_feather` grows + softens hard mattes outward, subject interior stays full strength; mismatched frame counts resample). Wire: Guide LATENT → the zone node's `v2v_latent` AND the sampler; model → zone node → sampler. The sampler's denoise stays the master dial; `inner_denoise`/`outer_denoise` are fractions of it — inner 1.0 / outer 0.3 at sampler denoise 0.6 reinvents the centre at 0.6 while the surroundings shimmer at ~0.18. `outer_denoise 0` = the rest of the frame is the untouched footage. Differential diffusion adapted to H3's paired AV latent — first of its kind on H3, EXPERIMENTAL: widen the feather if the transition band disagrees with itself.

### I want to tell the model WHAT goes in the masked region
**H3 Regional Prompt (mask)** — the missing half of the soft zone. Write the subject into your prompt as usual ("…a woman in a red coat walks left…"), copy that fragment into the node's `region_text`, and wire the **same mask** you gave the zone node: the region's pixels are attention-biased to *listen to those words*, and (via `containment`) the rest of the frame stops listening to them. Wiring: model → zone node → **regional node** → sampler; Guide's positive conditioning → the regional node's conditioning input (read-only — the sampler still gets it as before); clip → the node. `strength` ~1.0–2.5 is the expected range. Also works with **no v2v at all** — place a subject spatially in plain generation. EXPERIMENTAL, first regional prompting on H3; one regional node per graph for now.

### My v2v keeps giving me back the same material
Denoise alone often can't break it — and not because the dial is weak. The footage's structure is **correlated across frames** while the sampler's noise is independent per frame, so the model integrates the source back out of the noise; even 5% surviving latent carries the whole layout. The fix is to destroy the structure *before* sampling: the v2v bar's **scramble** slider blends the footage latent toward noise (0.3–0.6 keeps timing and rough motion while freeing content). Next to it, **declare** chooses how much of that scramble the sampler is told about through the `v2v_denoise` wire — at `1.0` the emitted denoise rises to where the latent actually sits (clean output, source genuinely gone rather than diluted); lower it and the model commits fewer steps over already-scrambled content, which is as far from the source as you can get, with grain as the price if you push it.

### My v2v looks unfinished / flickery at high denoise
That's the **denoise dial lying to you**, and it's core's maths, not H3's. Stock schedulers turn denoise into a schedule by building an `int(steps/denoise)`-step schedule and keeping its tail — which on H3's shifted flow schedule means **"0.9" actually starts at sigma 0.97**, nearly a full regeneration, and at low step counts the truncation makes anything above `steps/(steps+1)` *exactly* 1.0 (at 3 turbo steps: everything above **0.75**). So you get a from-scratch generation with too few steps: under-resolved, flickering. Set **H3 Basic Scheduler's `denoise_mode` to `rescale`** — same trajectory compressed into `[denoise, 0]`, so 0.9 means 0.9 at any step count, and distilled/turbo spacing is preserved. Leave it on `slice (core)` to match stock behaviour exactly.

### I want to restyle only part of a clip and keep the rest untouched
Use **H3 Frame Range → H3 Video To Latent → KSampler → H3 Splice** — recipe diagram in the [README](README.md#section-restyle--bake-it-back-in). Pin the section's first/last frames from the untouched footage so the joins can't drift.

### I want a LoRA to only affect part of the clip
**MiniMax H3 Temporal LoRA Blend** node, before the sampler: `model_base` from the checkpoint loader, `model_a` = base → your LoRA loader(s) (used *before* `boundary_seconds`), `model_b` for after (empty = the LoRA just drops out). Feather softens the handover; `audio_from` decides whether the soundtrack follows the ramp. Costs ~2× sampling time. Never put the turbo LoRA on one side only.

### I want to see the render without leaving the editor
Hit **▶ queue**: a progress strip appears in the header, the sampling preview streams into a corner dock, and when it finishes the clip **plays right there** — with **⏭ last frame → first**, **+ as video ref** and **🎞 add to reel** buttons to chain straight into the next shot.

### I want a whole sequence built for me (hands-free)
In the ▶ queue chooser (it appears once a clip is in the reel), pick **🔁 Auto Motion Mode** and a clip count. Each render then joins the reel, becomes the next clip's motion context, and queues again — same prompt, same settings, motion and sound continuing through every join, until the count is reached. **⏹ stop auto** appears in the header to end it early (the in-flight render still finishes and joins). It also stops itself on a render error, if you queue manually, or if you close the editor.

### I want to chain clips into one long video (and cut it)
Add renders to the **REEL** strip with the result dock's **🎞 add to reel**; **▶ queue** then asks how the next render continues the chain (see the recipe above). **▶ play reel** previews the whole chain in place — trims respected, joins as hard cuts (crossfades/fades/luma-match are export-only), looping until you close it. Click a card's **✂ readout** to trim it in a **big popup view** — full-size preview, draggable in/out handles, the kept range loops as it plays. The **⧉ dial between cards** sets a crossfade, and **fade in / out** on the header bookend the whole piece. Nothing is baked: every trim, crossfade and fade stays editable and only applies at **⇧ export as one video** — a single mp4 (equal-power audio blends, de-clicked cuts) in `output/h3reel/`, no external tools. **⏭** on a card continues from its OUT point without queueing; **🎲 re-roll last** replaces the newest take. Removing a card asks twice (✕ → ⚠), and **↩ undo** in the header brings the last removed clip back — trims, crossfade and setup intact — for about 12 seconds (covers re-roll's drop too).

### I want sound effects over the finished piece
**FX TRACKS** under the reel: three overlay lanes spanning the whole reel. **+** on a lane opens the audio picker (files, upload, mic, free web search); the sample lands as a **chip you drag into place** — band hit at 4s on fx1, car engine at 12s on fx2, they overlay freely. Click a chip to edit: **at** (position), **vol %** (per sample, up to 150), **clip in–out** (use just a slice of the file), **fade in/out** (the sample's own ease), **▶** auditions it, **✕** removes it. Everything is mixed at **⇧ export** on top of clip audio + music bed, and **▶ play reel** previews the lot live. The arrangement saves with the workflow and with **💾 save setup** files (a reel card's ⚙ deliberately leaves it alone).

### I want to fix an earlier clip in the chain
Every render added to the reel **remembers the full setup that made it**. Click **⚙** on the card (twice — it replaces your current editor setup), tweak whatever was wrong, and **▶ queue**: the continuation chooser pre-selects the clip *before* it as the source (a retake continues from the same place the original did), and when the render lands the dock offers **🎞 replace clip N** — position, crossfade kept, trims reset to fit the new take. If later clips were motion-continued from the old take, the toast reminds you to re-render them in order.

### I want to save my whole setup
**💾 save setup** → JSON with everything (prompt, frames, refs, framings, strengths). **📂 load setup** restores it on any fresh node — it tells you if a socket-fed input can't travel.

### I want faster renders
References ride through *every* sampling step — watch the **cost meter** in the header. Cut cost with `ref_megapixels` / `ref_video_megapixels`, or `ref_image_size: match`.

### I want to swap an image but keep its placement and settings
Right-click the card → **⇄ replace image…**. Placement, strength, description survive; the framer reopens if it was framed.

### I want to type specs by hand / drive it from the API
**✎ raw text specs** reveals the underlying text widgets — they're the source of truth and fully documented in the [README](README.md). Old saves, API workflows and headless use all work without the editor.

### Something's wrong
Errors show in the editor's banner (never silently). Mic blocked? Click the 🔒 icon in the address bar → allow microphone → record again. Turbo LoRA audio errors are auto-patched — restart once if you added the pack mid-session.
