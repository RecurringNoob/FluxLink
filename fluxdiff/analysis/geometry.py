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

from math import hypot

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