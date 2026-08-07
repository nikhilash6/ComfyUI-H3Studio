import logging

from .minimax_h3_guide import comfy_entrypoint

# The turbo LoRA rebuilds the DiT's adaln row table itself and omits audio
# conditioning, so any reference audio crashes it. Patch at import, before any
# workflow runs -- the buggy call site is a closure built when the LoRA is applied,
# so this has to be in place first. No-op when that pack is not installed.
try:
    from . import turbo_compat

    turbo_compat.install(at_import=True)
except Exception as exc:  # never let a compat shim break the pack's own load
    logging.warning("MiniMaxH3Guide: turbo compatibility shim skipped (%s: %s)",
                    type(exc).__name__, exc)

WEB_DIRECTORY = "./web"

__all__ = ["comfy_entrypoint", "WEB_DIRECTORY"]
