"""
Align kicad-cli SVG exports with board coordinates.

`kicad-cli pcb export svg --page-size-mode 2` ("board area only") does not
frame the SVG on the Edge.Cuts outline.  It uses KiCad's bounding box of the
board's items, which can extend past the outline: on the bundled test board
the frame starts 0.12 mm above the outline and ends 1.83 mm below it.  The
viewer maps the outline onto the whole image, so markers drifted by up to
~2 mm toward the bottom of the board.

This module finds where the board sits inside the SVG and rewrites the SVG's
viewBox so the image covers exactly the given board-space bounds.  After
cropping, a board point (x, y) in mm is at
    ((x - min_x) / (max_x - min_x), (y - min_y) / (max_y - min_y))
of the image's width and height, which is the mapping the viewer, the
component overlay and the pixel diff all assume.

Locating the board: the SVG's drawing coordinates are board coordinates
scaled by s (SVG units per mm, read from the width/viewBox header) and
shifted by an unknown origin.  Edge.Cuts end points, rectangle corners,
polygon vertices and circle centres appear verbatim in the SVG, so every
pairing of a board anchor with an SVG point "votes" for an origin, and the
origin that lines up the most anchors wins.  This does not depend on the
colour theme, and tolerates extra points (e.g. arcs plotted as segments).
"""

import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]

_UNIT_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "px": 25.4 / 96, "": 25.4 / 96}
_ROOT_TAG = re.compile(r"<svg\b[^>]*>", re.S)
_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_PATH_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUMBER)
# Coordinates match when they agree to within this many SVG units per mm.
_MATCH_TOL_MM = 0.005


def _attr(tag: str, name: str) -> Optional[str]:
    m = re.search(r'\s' + name + r'\s*=\s*"([^"]*)"', tag)
    return m.group(1) if m else None


def _length_mm(value: str) -> Optional[float]:
    m = re.fullmatch(r"\s*(" + _NUMBER + r")\s*([a-z]*)\s*", value or "")
    if not m or m.group(2) not in _UNIT_MM:
        return None
    return float(m.group(1)) * _UNIT_MM[m.group(2)]


def read_frame(svg_text: str):
    """
    Return (root_tag, viewBox (x, y, w, h), units_per_mm) or None when the
    header has no usable width/height/viewBox.
    """
    m = _ROOT_TAG.search(svg_text)
    if not m:
        return None
    tag = m.group(0)
    vb, w, h = _attr(tag, "viewBox"), _attr(tag, "width"), _attr(tag, "height")
    w_mm, h_mm = _length_mm(w), _length_mm(h)
    if not vb or not w_mm or not h_mm:
        return None
    try:
        vx, vy, vw, vh = (float(v) for v in vb.replace(",", " ").split())
    except ValueError:
        return None
    sx, sy = vw / w_mm, vh / h_mm
    if abs(sx - sy) > 1e-3 * max(sx, sy):
        return None          # non-uniform scaling: not a KiCad export we understand
    return tag, (vx, vy, vw, vh), sx


def _path_points(d: str) -> Iterable[Point]:
    """Absolute end points of an SVG path (KiCad writes absolute commands)."""
    tokens = _PATH_TOKEN.findall(d)
    cmd, nums = None, []

    def flush():
        if cmd is None:
            return
        step = {"M": 2, "L": 2, "T": 2, "A": 7, "C": 6, "S": 4, "Q": 4}.get(cmd)
        if step:
            for i in range(0, len(nums) - step + 1, step):
                yield nums[i + step - 2], nums[i + step - 1]

    for tok in tokens:
        if tok.isalpha():
            yield from flush()
            cmd, nums = tok.upper() if tok.isupper() else None, []
        else:
            nums.append(float(tok))
    yield from flush()


def svg_points(svg_text: str) -> List[Point]:
    pts: List[Point] = []
    for d in re.findall(r'\sd="([^"]*)"', svg_text):
        pts.extend(_path_points(d))
    for tag in re.findall(r"<circle\b[^>]*>", svg_text):
        cx, cy = _attr(tag, "cx"), _attr(tag, "cy")
        if cx is not None and cy is not None:
            pts.append((float(cx), float(cy)))
    return pts


def find_origin(svg_text: str, anchors: Sequence[Point]) -> Optional[Point]:
    """
    SVG-unit origin O such that  svg = s * board - O  for this export, or
    None if the outline cannot be found with confidence.
    """
    frame = read_frame(svg_text)
    if frame is None or len(anchors) < 2:
        return None
    _, _, s = frame
    tol = _MATCH_TOL_MM * s
    scaled = [(s * x, s * y) for x, y in anchors]
    points = svg_points(svg_text)
    if not points:
        return None

    # Bucket every svg point so support counts are cheap.
    grid = tol * 4
    buckets: Dict[Tuple[int, int], List[Point]] = {}
    for p in points:
        buckets.setdefault((round(p[0] / grid), round(p[1] / grid)), []).append(p)

    def support(ox, oy):
        n = 0
        for bx, by in scaled:
            tx, ty = bx - ox, by - oy
            key = (round(tx / grid), round(ty / grid))
            if any(abs(px - tx) <= tol and abs(py - ty) <= tol
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                   for px, py in buckets.get((key[0] + dx, key[1] + dy), ())):
                n += 1
        return n

    # Each pairing of the first few anchors with every svg point proposes an
    # origin; the most common proposals are then scored against all anchors.
    votes = Counter()
    for bx, by in scaled[:8]:
        for px, py in points:
            votes[(round((bx - px) / tol), round((by - py) / tol))] += 1
    best, best_support = None, 0
    for (kx, ky), _ in votes.most_common(25):
        ox, oy = kx * tol, ky * tol
        n = support(ox, oy)
        if n > best_support:
            best, best_support = (ox, oy), n

    needed = max(2, int(0.6 * len(scaled) + 0.5))
    if best is None or best_support < needed:
        return None
    # Refine: average the exact offsets of the matched anchors.
    ox, oy = best
    dxs, dys = [], []
    for bx, by in scaled:
        tx, ty = bx - ox, by - oy
        near = [p for p in points if abs(p[0] - tx) <= tol and abs(p[1] - ty) <= tol]
        if near:
            px, py = min(near, key=lambda p: (p[0] - tx) ** 2 + (p[1] - ty) ** 2)
            dxs.append(bx - px)
            dys.append(by - py)
    return sum(dxs) / len(dxs), sum(dys) / len(dys)


def crop_svg_text(svg_text: str, anchors: Sequence[Point], bounds: Dict) -> Optional[str]:
    """
    Return svg_text with its root width/height/viewBox rewritten to cover
    exactly bounds (board mm: min_x, min_y, max_x, max_y), or None if the
    board could not be located in the SVG.
    """
    frame = read_frame(svg_text)
    origin = find_origin(svg_text, anchors)
    if frame is None or origin is None:
        return None
    tag, _, s = frame
    ox, oy = origin
    w_mm = bounds["max_x"] - bounds["min_x"]
    h_mm = bounds["max_y"] - bounds["min_y"]
    if w_mm <= 0 or h_mm <= 0:
        return None
    vb = (s * bounds["min_x"] - ox, s * bounds["min_y"] - oy, s * w_mm, s * h_mm)

    new_tag = tag
    for name, value in (("width", f"{w_mm:.4f}mm"), ("height", f"{h_mm:.4f}mm"),
                        ("viewBox", " ".join(f"{v:.4f}" for v in vb))):
        new_tag = re.sub(r'(\s' + name + r'\s*=\s*")[^"]*(")', lambda m: m.group(1) + value + m.group(2),
                         new_tag, count=1)
    # Content outside the new viewBox (e.g. the drawing sheet) stays clipped.
    return svg_text.replace(tag, new_tag, 1)


def crop_svg_file(path: str, anchors: Sequence[Point], bounds: Dict) -> bool:
    """Crop the SVG at path in place.  Returns False (file untouched) on failure."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    cropped = crop_svg_text(text, anchors, bounds)
    if cropped is None:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(cropped)
    return True
