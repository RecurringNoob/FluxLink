"""
Shared visual constants for FluxDiff image export and coordinate mapping.

F4 FIX: EXPORT_SCALE and PIXELS_PER_MM were previously hardcoded separately
in kicad_export.py (scale=4.0) and component_diff.py (PIXELS_PER_MM inline
constant). If one was changed without the other, component markers would
silently appear at wrong board positions.

Both modules now import from here so the values are guaranteed to stay in sync.
"""

# Scale factor passed to cairosvg.svg2png — must match kicad_export.py
EXPORT_SCALE: float = 4.0

# Pixels per millimetre at EXPORT_SCALE
# 96 DPI (CSS standard) ÷ 25.4 mm/inch × EXPORT_SCALE
PIXELS_PER_MM: float = (96 / 25.4) * EXPORT_SCALE   # ≈ 15.118 px/mm