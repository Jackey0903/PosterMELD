# Generative Asset Fallbacks Are Deterministic and Degraded

PosterMELD enables teaser and background Generative Assets in the Default Standard Variant, but external image generation failures must not produce silent placeholder successes. The default policy rejects failed or unusable generated assets. A deterministic procedural fallback may be used only when explicitly enabled; it records degraded provenance and must pass the same asset checks before final acceptance.
