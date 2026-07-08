# Generative Asset Fallbacks Are Deterministic and Degraded

Paper2Poster enables teaser and background Generative Assets in the Default Standard Variant, but external image generation failures must not produce silent placeholder successes. Failed or unusable generated assets will use deterministic procedural fallbacks when possible, record degraded provenance in artifact metadata, and be disabled if the fallback cannot meet visual quality constraints.

