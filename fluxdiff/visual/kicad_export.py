# fluxdiff/visual/kicad_export.py
import subprocess
import os
import cairosvg

from fluxdiff.visual.constants import EXPORT_SCALE

# Default layer set for exports.
# F.Cu + B.Cu: front and back copper (segments, pads, zones).
# F.SilkS + B.SilkS: front and back silkscreen (component outlines, refs).
# F.Mask + B.Mask: solder mask openings (shows pad sizes accurately).
# Edge.Cuts: board outline — required for --page-size-mode 2 to work correctly.
#
# The viewer SVG uses --mode-single (all layers merged into one file) so the
# existing single-SVG viewer pipeline is unchanged.  Callers can pass a
# custom `layers` string to override for single-side or reduced exports.
DEFAULT_LAYERS = "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"


def export_pcb_svg(pcb_file: str, output_svg: str, layers: str = DEFAULT_LAYERS):
    """
    Export a KiCad PCB file as an SVG using KiCad CLI.

    This is the primary export for the React viewer — SVG scales cleanly
    at any zoom level without pixelation.  The viewer uses this as the
    board background layer.

    layers — comma-separated KiCad layer names.  Defaults to DEFAULT_LAYERS
             which includes both copper layers, silkscreen, mask and Edge.Cuts.
             Pass a custom string to restrict to a subset (e.g. "F.Cu,Edge.Cuts").

    Raises RuntimeError if kicad-cli fails or produces no output.
    """
    output_dir = os.path.dirname(output_svg)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    try:
        subprocess.run(
            [
                "kicad-cli", "pcb", "export", "svg",
                pcb_file,
                "--layers", layers,
                "--page-size-mode", "2",
                "--theme", "kicad_default",
                "--output", output_svg,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"kicad-cli failed for {pcb_file}:\n{e.stderr}"
        ) from e

    if not os.path.isfile(output_svg):
        raise RuntimeError(
            f"kicad-cli did not produce an SVG at {output_svg}"
        )

    print(f"✅ SVG exported: {output_svg}")


def export_pcb_png(pcb_file: str, output_png: str, layers: str = DEFAULT_LAYERS):
    """
    Export a KiCad PCB file as a PNG (SVG → cairosvg → PNG).

    Used as input for image_diff.py (copper pixel diff).
    The PNG is an intermediate — it is not served directly to the viewer,
    which uses the SVG from export_pcb_svg instead.

    layers — same as export_pcb_svg.  Keeping both exports on the same layer
             set ensures the pixel diff and the SVG overlay are comparing
             the same visual content.

    Raises RuntimeError if kicad-cli or cairosvg fails.
    """
    output_dir = os.path.dirname(output_png)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    svg_file = output_png.replace(".png", "_tmp.svg")

    try:
        subprocess.run(
            [
                "kicad-cli", "pcb", "export", "svg",
                pcb_file,
                "--layers", layers,
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

    try:
        cairosvg.svg2png(url=svg_file, write_to=output_png, scale=EXPORT_SCALE)
    except Exception as e:
        raise RuntimeError(
            f"CairoSVG conversion failed for {svg_file}: {e}"
        ) from e

    if not os.path.isfile(output_png) or os.path.getsize(output_png) == 0:
        raise RuntimeError(
            f"PNG was not generated or is empty: {output_png}."
        )

    # Clean up temp SVG (the viewer-facing SVG is produced by export_pcb_svg)
    try:
        os.remove(svg_file)
    except OSError:
        pass

    print(f"✅ PNG exported: {output_png}")