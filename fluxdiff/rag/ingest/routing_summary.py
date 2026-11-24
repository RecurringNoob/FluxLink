# fluxdiff/rag/ingest/routing_summary.py
"""
Condense routing-diff lines into a per-net summary for the RAG index.

The diff engine reports routing one segment at a time ("Trace added: net
+3V3, layer F.Cu, from (..) to (..), width 0.254 mm").  A re-route easily
produces hundreds of such lines: tens of kilobytes of coordinates that crowd
the LLM's context and match no question an engineer would ask.  The index
stores this summary instead, e.g.

    Net +3V3: 14 segments added (21.4 mm on F.Cu; width 0.254 mm), 3 removed (4.1 mm); 2 vias added
    Zone added: net GND, layer B.Cu

Lines that are not segment / arc / via changes (zone changes) are kept as-is.
"""

import math
import re
from collections import defaultdict
from typing import Dict, List

_PAIR = r"\(\s*([-\d.]+),\s*([-\d.]+)\s*\)"
_TRACE = re.compile(
    r"^Trace (added|removed): net (.*), layer (\S+), from " + _PAIR + r" to " + _PAIR
    + r", width ([\d.]+) mm$")
_ARC = re.compile(
    r"^Arc (added|removed): net (.*), layer (\S+), start " + _PAIR + r", mid " + _PAIR
    + r", end " + _PAIR + r", width ([\d.]+) mm$")
_VIA = re.compile(r"^Via (added|removed): net (.*), at " + _PAIR + r"$")


def _new_stats():
    return {"seg": 0, "len": 0.0, "layers": set(), "widths": set(), "via": 0}


def _fmt_mm(v: float) -> str:
    return f"{v:.1f} mm" if v >= 0.1 else f"{v:.2f} mm"


def _side(stats: Dict, verb: str, with_detail: bool) -> List[str]:
    parts = []
    if stats["seg"]:
        detail = ""
        if with_detail:
            layers = ", ".join(sorted(stats["layers"]))
            widths = ", ".join(f"{w:g}" for w in sorted(stats["widths"]))
            detail = f" on {layers}; width {widths} mm"
        noun = "segment" if stats["seg"] == 1 else "segments"
        parts.append(f"{stats['seg']} {noun} {verb} ({_fmt_mm(stats['len'])}{detail})")
    if stats["via"]:
        noun = "via" if stats["via"] == 1 else "vias"
        parts.append(f"{stats['via']} {noun} {verb}")
    return parts


def summarize_routing(lines: List[str]) -> List[str]:
    nets: Dict[str, Dict[str, Dict]] = defaultdict(lambda: {"added": _new_stats(),
                                                            "removed": _new_stats()})
    passthrough = []
    for line in lines:
        m = _TRACE.match(line)
        if m:
            op, net, layer = m.group(1), m.group(2), m.group(3)
            x0, y0, x1, y1, w = (float(m.group(i)) for i in range(4, 9))
            s = nets[net][op]
            s["seg"] += 1
            s["len"] += math.hypot(x1 - x0, y1 - y0)
            s["layers"].add(layer)
            s["widths"].add(w)
            continue
        m = _ARC.match(line)
        if m:
            op, net, layer = m.group(1), m.group(2), m.group(3)
            from fluxdiff.analysis.geometry import arc_length
            pts = [float(m.group(i)) for i in range(4, 10)]
            s = nets[net][op]
            s["seg"] += 1
            s["len"] += arc_length((pts[0], pts[1]), (pts[2], pts[3]), (pts[4], pts[5]))
            s["layers"].add(layer)
            s["widths"].add(float(m.group(10)))
            continue
        m = _VIA.match(line)
        if m:
            nets[m.group(2)][m.group(1)]["via"] += 1
            continue
        passthrough.append(line)

    def magnitude(item):
        a, r = item[1]["added"], item[1]["removed"]
        return -(a["len"] + r["len"] + a["via"] + r["via"]), item[0]

    out = []
    for net, sides in sorted(nets.items(), key=magnitude):
        parts = (_side(sides["added"], "added", True)
                 + _side(sides["removed"], "removed", not sides["added"]["seg"]))
        out.append(f"Net {net}: " + ", ".join(parts))
    if nets:
        total = sum(s["seg"] + s["via"] for sides in nets.values() for s in sides.values())
        out.insert(0, f"{total} routing change(s) across {len(nets)} net(s):")
    return out + passthrough
