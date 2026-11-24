"""
Geometry utilities for trace-to-pad snapping and proximity checks.

F5 FIX: build_pad_index uses Pad.has_explicit_position.

FIX (tolerance): TOLERANCE raised from 0.8mm to 2.0mm. On real boards,
trace endpoints frequently sit 1-2mm from pad centres due to via fanouts,
teardrops, and rounded pad corners. 0.8mm was causing 201/305 traces to go
unenriched on a test board, leaving the connectivity graph severely incomplete.
2.0mm is still tight enough to avoid cross-net false matches (pads on different
nets within 2mm would be caught by the net= filter anyway).
"""

from math import hypot, asin, pi

TOLERANCE = 2.0  # mm — raised from 0.8 to capture real board routing


def distance(p1, p2):
    return hypot(p1[0] - p2[0], p1[1] - p2[1])


def build_pad_index(components):
    """
    Build list of all pad positions: [(x, y, ref, pad_number, net)]
    Uses pad.has_explicit_position to choose between computed board coords
    and component-origin fallback.
    """
    pads = []
    for comp in components:
        for pad in comp.pads:
            if pad.has_explicit_position:
                px, py = pad.x, pad.y
            else:
                px, py = comp.x, comp.y
            pads.append((px, py, comp.ref, pad.number, pad.net))
    return pads


def find_nearest_pad(point, pad_index, net=None):
    """
    Find closest pad within TOLERANCE.
    Always pass net= to prevent cross-net phantom matches.
    """
    best = None
    best_dist = float("inf")

    for px, py, ref, pad_num, pad_net in pad_index:
        if net is not None and pad_net != net:
            continue
        d = distance(point, (px, py))
        if d < best_dist:
            best_dist = d
            best = (ref, pad_num)

    return best if best_dist <= TOLERANCE else None

def arc_length(start, mid, end):
    """
    Length of the three-point arc start -> mid -> end (KiCad arc tracks).
    Falls back to the chord path when the points are collinear.
    """
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return distance(start, mid) + distance(mid, end)
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay)
          + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx)
          + (cx**2 + cy**2) * (bx - ax)) / d
    r = hypot(ax - ux, ay - uy)
    chord = distance(start, end)
    # Central angle from the chord; the arc is the major one when mid lies on
    # the far side of the chord from the centre.
    theta = 2 * asin(min(1.0, chord / (2 * r)))
    cross_mid = (cx - ax) * (by - ay) - (cy - ay) * (bx - ax)
    cross_ctr = (cx - ax) * (uy - ay) - (cy - ay) * (ux - ax)
    if cross_mid * cross_ctr > 0:
        theta = 2 * pi - theta
    return r * theta
