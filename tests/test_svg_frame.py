"""
visual/svg_frame.py: locate the board in a kicad-cli SVG and crop to it.

The synthetic SVGs mirror what kicad-cli 8/9 writes for --page-size-mode 2:
a frame (width/height/viewBox) larger than the Edge.Cuts outline, drawing
coordinates equal to board mm minus an unknown origin, the drawing sheet at
far-off negative coordinates, and many unrelated points from other layers.
"""
import re
from pathlib import Path

import pytest

from fluxdiff.diff.diff_engine import compare_pcbs
from fluxdiff.parser.pcb_parser import parse_pcb
from fluxdiff.visual.svg_frame import crop_svg_file, crop_svg_text, find_origin, read_frame

BOARDS = Path(__file__).resolve().parent.parent / "test_boards"

# A 50 x 30 mm board with one rounded corner and a mounting-hole circle.
OUTLINE = [((100.0, 50.0), (148.0, 50.0)), ((150.0, 52.0), (150.0, 80.0)),
           ((150.0, 80.0), (100.0, 80.0)), ((100.0, 80.0), (100.0, 50.0))]
ARC = ((148.0, 50.0), (150.0, 52.0))
HOLE = (145.0, 75.0)
ANCHORS = [p for seg in OUTLINE for p in seg] + list(ARC) + [HOLE]


def kicad_like_svg(origin=(99.9, 49.85), frame=(50.2, 32.1), units_per_mm=1.0):
    s, (ox, oy) = units_per_mm, origin
    to = lambda x, y: f"{(x - ox) * s:.4f} {(y - oy) * s:.4f}"
    paths = "\n".join(f'<path d="M{to(*a)}\nL{to(*b)}\n" />' for a, b in OUTLINE)
    paths += f'\n<path d="M{to(*ARC[0])} A{2*s:.4f} {2*s:.4f} 0.0 0 1 {to(*ARC[1])}" />'
    hx, hy = (HOLE[0] - ox) * s, (HOLE[1] - oy) * s
    noise = "\n".join(f'<circle cx="{(101 + i * 1.37) * s - ox * s:.4f}" cy="{(55 + i * 0.91) * s - oy * s:.4f}" r="0.3" />'
                      for i in range(30))
    w, h = frame
    return f'''<?xml version="1.0" standalone="no"?>
<svg
  xmlns="http://www.w3.org/2000/svg"
  version="1.1"
  width="{w:.4f}mm" height="{h:.4f}mm" viewBox="0.0000 0.0000 {w * s:.4f} {h * s:.4f}">
<g style="fill:none; stroke:#C872AB; stroke-width:0.15;">
<path d="M-156.9955 -128.9170
L-48.9955 -128.9170
" />
</g>
<g style="fill:#C83434; stroke:none;">
{noise}
</g>
<g style="fill:none; stroke:#D0D2CD; stroke-width:0.05;">
{paths}
<circle cx="{hx:.4f}" cy="{hy:.4f}" r="{1.2 * s:.4f}" />
</g>
</svg>
'''


BOUNDS = {"min_x": 99.75, "min_y": 49.75, "max_x": 150.25, "max_y": 80.25}


@pytest.mark.parametrize("units", [1.0, 10.0])
def test_origin_is_found(units):
    ox, oy = find_origin(kicad_like_svg(units_per_mm=units), ANCHORS)
    assert ox == pytest.approx(99.9 * units, abs=1e-3)
    assert oy == pytest.approx(49.85 * units, abs=1e-3)


def test_crop_maps_board_points_to_bounds_fractions():
    cropped = crop_svg_text(kicad_like_svg(), ANCHORS, BOUNDS)
    tag, (vx, vy, vw, vh), s = read_frame(cropped)
    assert 'width="50.5000mm"' in tag and 'height="30.5000mm"' in tag
    # The mounting hole is drawn at board (145, 75); after cropping it must sit
    # at the same fraction of the image as the viewer computes from BOUNDS.
    hx, hy = (145.0 - 99.9), (75.0 - 49.85)
    assert (hx - vx) / vw == pytest.approx((145.0 - 99.75) / 50.5, abs=1e-5)
    assert (hy - vy) / vh == pytest.approx((75.0 - 49.75) / 30.5, abs=1e-5)


def test_crop_refuses_when_outline_is_missing():
    svg = kicad_like_svg()
    no_outline = re.sub(r'<g style="fill:none; stroke:#D0D2CD.*?</g>', "", svg, flags=re.S)
    assert crop_svg_text(no_outline, ANCHORS, BOUNDS) is None


def test_crop_file_leaves_file_untouched_on_failure(tmp_path):
    path = tmp_path / "board.svg"
    path.write_text("<svg><g/></svg>")
    assert crop_svg_file(str(path), ANCHORS, BOUNDS) is False
    assert path.read_text() == "<svg><g/></svg>"


def test_bounds_cover_both_boards_with_margin():
    old = parse_pcb(BOARDS / "before3.kicad_pcb")
    new = parse_pcb(BOARDS / "after3.kicad_pcb")
    b = compare_pcbs(old, new).board_bounds
    x0, y0, x1, y1 = new.board_outline
    assert (b["min_x"], b["min_y"]) == pytest.approx((x0 - 0.25, y0 - 0.25))
    assert (b["max_x"], b["max_y"]) == pytest.approx((x1 + 0.25, y1 + 0.25))
    assert (b["offset_x"], b["offset_y"]) == (b["min_x"], b["min_y"])


def test_outline_anchors_from_test_board():
    pcb = parse_pcb(BOARDS / "after3.kicad_pcb")
    # 4 lines x 2 + 4 arcs x 2 end points + 2 hole circle centres
    assert len(pcb.outline_anchors) == 18
