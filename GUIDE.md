# H3 Guide — quick recipes

Add the **MiniMax H3 Image to Video (Guide)** node, click **⤢ open timeline editor**, work in fullscreen. Everything below assumes the editor unless it names a widget. Full detail: [README](README.md).

---

### I want a clip from one image
Click the **first frame** card → pick an image. Type a prompt. **▶ queue**.

### I want it to end on a specific image
Set the **last frame** card. Strength `1.0` = land exactly on it; `0.6–0.8` = treat it as a guide and keep the motion natural.

### I want it to pass through an image mid-clip
**+ waypoint** → pick the image → drag its marker along the track to the right moment (or double-click empty track space to drop one right there). Strength `0.6` is the sweet spot; add a short description on the card so the model knows what it's looking at.

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
Get it into ComfyUI first: **+ video…** → **upload…** (any file on disk), or just drag it from Explorer onto the VIDEO REFERENCES section — that carries its look, motion and sound. For pixel-exact continuation also pin the first frame to the video's final frame: `Load Video → Get Video Components → Image From Batch` (last index) → the node's `first_frame` socket. Different aspect? Fix it with **⛶** on the video card.

### I want to fix a wrong crop / aspect mismatch
Every image and video card has **⛶** — drag/zoom the window over what the model should see. Keyframes are locked to the output aspect, refs/videos keep their own. Corner-drag for exact size, wheel to zoom (Shift = fine), scrub bar on videos.

### I want to restyle existing footage (v2v)
Feed the Guide node's `v2v_video_file` (or the `v2v_images`/`v2v_audio` sockets); the latent output becomes the encoded footage. Sample at **denoise 0.3–0.7** — motion survives, style changes. `v2v_start_seconds`/`v2v_end_seconds` restyle just a slice.

### I want to restyle only part of a clip and keep the rest untouched
Use **H3 Frame Range → H3 Video To Latent → KSampler → H3 Splice** — recipe diagram in the [README](README.md#section-restyle--bake-it-back-in). Pin the section's first/last frames from the untouched footage so the joins can't drift.

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
