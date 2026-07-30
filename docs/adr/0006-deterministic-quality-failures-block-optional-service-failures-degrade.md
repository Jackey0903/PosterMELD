# Deterministic Quality Failures Block, Optional Service Failures Degrade

PosterMELD will reject posters with deterministic quality failures such as missing PPTX or PNG artifacts, layout overflow or overlap, render-measurement mismatch, missing visual assets, invalid schemas, placeholder generated assets, or unrelated-domain content leakage. When an external image or VLM service is explicitly enabled, its unavailability blocks final acceptance by default. An explicitly configured fallback may complete only when it produces a valid artifact and records its degraded provenance.
