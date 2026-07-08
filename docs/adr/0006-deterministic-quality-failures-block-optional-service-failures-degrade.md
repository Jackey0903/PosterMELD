# Deterministic Quality Failures Block, Optional Service Failures Degrade

Paper2Poster will reject posters with deterministic quality failures such as missing PPTX or PNG artifacts, layout overflow or overlap, render-measurement mismatch, missing visual assets, invalid schemas, placeholder generated assets, or unrelated-domain content leakage. Optional external service failures, including image API or VLM unavailability, may complete through explicit fallbacks but must be recorded as Degraded Quality State rather than silent success.

