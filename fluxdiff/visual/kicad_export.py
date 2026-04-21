# fluxdiff/visual/kicad_export.py
import subprocess
import os
import cairosvg

# F4 FIX: import EXPORT_SCALE from the shared constants module instead of
# hardcoding 4.0 here. This guarantees component_diff.py's PIXELS_PER_MM
# stays in sync — previously they were separate magic numbers.
from fluxdiff.visual.constants import EXPORT_SCALE


def export_pcb_png(pcb_file: str, output_png: str):
    """
    Export a KiCad PCB file as a PNG using KiCad CLI + CairoSVG.

    Raises immediately if CairoSVG fails regardless of whether a
    (possibly corrupt/zero-byte) file was written, so the pipeline never
    continues silently with bad image data.
    """
    output_dir = os.path.dirname(output_png)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    svg_file = output_png.replace(".png", ".svg")

    # 1. Export SVG via KiCad CLI
    try:
        subprocess.run(
            [
                "kicad-cli", "pcb", "export", "svg",
                pcb_file,
                "--layers", "F.Cu,F.SilkS,Edge.Cuts",
                "--page-size-mode", "2",
                "--theme", "kicad_default",
                "--output", svg_file,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"kicad-cli failed for {pcb_file}:\n{e.stderr}"
        ) from e

    if not os.path.isfile(svg_file):
        raise RuntimeError(f"kicad-cli did not produce an SVG at {svg_file}")

    # 2. Convert SVG → PNG via CairoSVG
    # F4 FIX: use EXPORT_SCALE from constants instead of hardcoded 4.0
    try:
        cairosvg.svg2png(url=svg_file, write_to=output_png, scale=EXPORT_SCALE)
    except Exception as e:
        raise RuntimeError(f"CairoSVG conversion failed for {svg_file}: {e}") from e

    # Verify the output is a non-empty file
    if not os.path.isfile(output_png) or os.path.getsize(output_png) == 0:
        raise RuntimeError(
            f"PNG was not generated or is empty: {output_png}. "
            "Check CairoSVG installation and SVG validity."
        )

    print(f"✅ Exported: {output_png}")