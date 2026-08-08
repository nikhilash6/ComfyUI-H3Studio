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

### I want to describe what happens at a moment (no image)
Click the **beats lane** under the track at the right moment, type the text — that's a **beat**. Leave `timed_text_mode` on `text only` first; try the rope modes only if plain timing doesn't steer. (A beat can be upgraded to a waypoint later — right-click it.)

### I want the same person in every clip
Add their photos with **+ reference** (strength `1.0` locks identity, `0.7` = likeness hint). Then **🎭 cast** → **💾 save cast member** once — from any other clip or workflow, **🎭 cast** → add, and their refs, strengths and face-crops come back in one click.

### I want a reference voice or sound
**+ audio…** to load a file, or **● record mic** straight into the node. Say `<Audio 1>` in the prompt to invoke it. Max 3.

### I want motion/style from existing footage
**+ video…** in VIDEO REFERENCES. Its soundtrack rides along automatically. Cap cost with `ref_video_megapixels` (e.g. `0.4`). Max 3.

### I want to continue my previous render
Set the first frame from the picker's **output folder** tab (newest first) — grab the last frame of the previous clip. Add the previous clip as a video reference if you also want its momentum.

### I want to continue an external video file (from my drive, someone else's render…)
**+ video…** → **upload…** (any file on disk), or drag it from Explorer onto the VIDEO REFERENCES section — that carries its look, motion and sound. Then click **⏭** on the video's card: its **final frame becomes the clip's first frame**, one click, done. Different aspect? The toast tells you — **⛶** the first-frame card. (Graph alternative: `Load Video → Get Video Components → Image From Batch` → `first_frame` socket.)

### I want to fix a wrong crop / aspect mismatch
Every image and video card has **⛶** — drag/zoom the window over what the model should see. Keyframes are locked to the output aspect, refs/videos keep their own. Corner-drag for exact size, wheel to zoom (Shift = fine), scrub bar on videos.

### I want to restyle existing footage (v2v)
Pick footage in the editor's **🎞 v2v bar** (or feed the `v2v_images`/`v2v_audio` sockets); the latent output becomes the encoded footage. Sample at **denoise 0.3–0.7** — the bar reminds you: that dial lives on your KSampler, not this node. **✂ section…** scrubs the clip and sets the in/out points visually; **⛶** reframes the footage to your width×height canvas (landscape → vertical, etc.) — unframed, the canvas follows the footage instead. The selected section shows as a **ghosted filmstrip behind the timeline** so your keyframes and beats sit over the actual footage; the "ghost" dial sets its opacity (0 hides it).

### I want to restyle only part of a clip and keep the rest untouched
Use **H3 Frame Range → H3 Video To Latent → KSampler → H3 Splice** — recipe diagram in the [README](README.md#section-restyle--bake-it-back-in). Pin the section's first/last frames from the untouched footage so the joins can't drift.

### I want a LoRA to only affect part of the clip
**MiniMax H3 Temporal LoRA Blend** node, before the sampler: `model_base` from the checkpoint loader, `model_a` = base → your LoRA loader(s) (used *before* `boundary_seconds`), `model_b` for after (empty = the LoRA just drops out). Feather softens the handover; `audio_from` decides whether the soundtrack follows the ramp. Costs ~2× sampling time. Never put the turbo LoRA on one side only.

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
