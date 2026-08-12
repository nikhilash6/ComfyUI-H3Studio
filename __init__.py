import logging

from .h3_studio import comfy_entrypoint

# The turbo LoRA rebuilds the DiT's adaln row table itself and omits audio
# conditioning, so any reference audio crashes it. Patch at import, before any
# workflow runs -- the buggy call site is a closure built when the LoRA is applied,
# so this has to be in place first. No-op when that pack is not installed.
try:
    from . import turbo_compat

    turbo_compat.install(at_import=True)
except Exception as exc:  # never let a compat shim break the pack's own load
    logging.warning("H3Studio: turbo compatibility shim skipped (%s: %s)",
                    type(exc).__name__, exc)

# orientation-correct thumbnail endpoint (no-op headless)
try:
    from . import h3_preview  # noqa: F401  (registers its route at import)
except Exception as exc:
    logging.warning("H3Studio: preview endpoint skipped (%s: %s)",
                    type(exc).__name__, exc)

# reel export endpoint (no-op headless)
try:
    from . import h3_reel  # noqa: F401  (registers its route at import)
except Exception as exc:
    logging.warning("H3Studio: reel export skipped (%s: %s)",
                    type(exc).__name__, exc)

# Openverse free image/audio search proxy (no-op headless)
try:
    from . import h3_web_media  # noqa: F401  (registers its routes at import)
except Exception as exc:
    logging.warning("H3Studio: web media search skipped (%s: %s)",
                    type(exc).__name__, exc)

WEB_DIRECTORY = "./web"

__all__ = ["comfy_entrypoint", "WEB_DIRECTORY"]
