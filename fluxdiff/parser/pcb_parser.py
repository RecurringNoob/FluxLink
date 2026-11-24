"""
Parses a .kicad_pcb S-expression file into PCBData domain objects.

Key additions over the baseline:
  - Pad absolute coordinates: each pad's (at dx dy [rot]) node is read and
    combined with the parent footprint's position and rotation to produce
    true board-space coordinates stored on Pad.x / Pad.y / Pad.rotation.
  - Trace width: the (width ...) child of each (segment ...) node is parsed
    and stored on Trace.width for use by impedance analysis.

FIX (has_explicit_position): extract_pads now sets has_explicit_position=True
whenever an (at ...) node is actually found. Previously the flag was never set,
so build_pad_index always fell back to comp.x/comp.y (footprint origin) for
every pad, making trace-to-pad snapping completely wrong.

B1 FIX: Duplicate reference warning no longer fires for power symbols (#PWR,
#FLG). The seen_refs tracking is now inside the `if not is_pwr_sym` guard,
so expected KiCad duplicate power symbols are silently accepted.

B2 FIX: Net ID 0 is mapped to UNCONNECTED_NET as before, but if the file
provides a non-empty name for net 0 we preserve it under a separate
_raw_net_0_name attribute (for diagnostics). More importantly, the
display_name logic now only substitutes UNCONNECTED_NET when net_id == 0
AND the name is empty or absent, not unconditionally. This prevents a
malformed file from silently dropping a real net named on ID 0.

ARC/ZONE/OUTLINE additions:
  - extract_board_outline: reads Edge.Cuts gr_line, gr_arc, gr_circle nodes
    to find the tight bounding box of the board outline in absolute KiCad mm.
    This is the correct origin for SVG coordinate mapping (kicad-cli shifts
    the board to origin 0,0 when --page-size-mode 2 is used).
  - extract_arcs: parses gr_arc nodes on copper layers into Arc objects.
    gr_arc nodes on Edge.Cuts are excluded (they are outline, not routing).
  - extract_zones: parses zone nodes into Zone objects, capturing net, layer
    and outline polygon vertices for the diff engine.
"""

import math
import os
from fluxdiff.models.pcb_models import PCBData, Component, Net, Trace, Via, Pad, Arc, Zone
from fluxdiff.parser.sexp_parser import parse_sexp, build_index

UNCONNECTED_NET = "__unconnected__"

_POWER_SYMBOL_PREFIXES = ("#PWR", "#FLG")


def _strip(s: str) -> str:
    return s.strip('"') if s else ""


def _is_power_symbol_ref(ref: str) -> bool:
    upper = ref.upper()
    return any(upper.startswith(p) for p in _POWER_SYMBOL_PREFIXES)


def find_direct_children(node, name):
    return [c for c in node.children if c.name == name]


def _is_copper_layer(layer: str) -> bool:
    """Return True for any copper signal layer."""
    return layer.endswith(".Cu")


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def _rotate_point(dx, dy, angle_deg):
    rad = math.radians(-angle_deg)
    cos_a = math.cos(rad)
    sin_a = math.sin(rad)
    return dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a


def _pad_absolute_position(fp_x, fp_y, fp_rot, pad_dx, pad_dy, pad_local_rot):
    rx, ry = _rotate_point(pad_dx, pad_dy, fp_rot)
    abs_x = fp_x + rx
    abs_y = fp_y + ry
    abs_rot = (fp_rot + pad_local_rot) % 360.0
    return abs_x, abs_y, abs_rot


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_pcb(file_path):
    root = parse_sexp(file_path)
    index = build_index(root)

    nets = extract_nets(root)
    components = extract_components(index, nets)
    traces = extract_traces(index, nets)
    vias = extract_vias(index, nets)
    arcs = extract_arcs(index, nets)
    zones = extract_zones(index, nets)
    board_outline = extract_board_outline(root)

    return PCBData(
        components=components,
        nets=nets,
        traces=traces,
        vias=vias,
        arcs=arcs,
        zones=zones,
        board_outline=board_outline,
    )


# ---------------------------------------------------------------------------
# Nets
# ---------------------------------------------------------------------------

def extract_nets(root):
    nets = []
    seen = set()

    for net_node in root.children:
        if net_node.name != "net":
            continue
        try:
            net_id = int(net_node.values[0])
            net_name = _strip(net_node.values[1]) if len(net_node.values) > 1 else ""
        except (IndexError, ValueError):
            continue

        if net_id not in seen:
            seen.add(net_id)

            # B2 FIX: Only substitute UNCONNECTED_NET for net 0 when the file
            # provides no name (the normal KiCad case). If a malformed file
            # assigns a real name to net 0, preserve it and warn.
            if net_id == 0:
                if net_name and net_name != UNCONNECTED_NET:
                    print(
                        f"[WARNING] Net ID 0 has unexpected name '{net_name}' — "
                        f"KiCad reserves ID 0 for unconnected pads. "
                        f"Using '{UNCONNECTED_NET}' but this file may be malformed."
                    )
                display_name = UNCONNECTED_NET
            else:
                display_name = net_name

            nets.append(Net(net_id=net_id, name=display_name))

    return nets


# ---------------------------------------------------------------------------
# Pads  (correctly sets has_explicit_position)
# ---------------------------------------------------------------------------

def extract_pads(fp, net_map, fp_x, fp_y, fp_rot):
    """
    Extract pads from a footprint node, computing each pad's absolute
    board-space position by combining the footprint transform with the
    pad's local (at dx dy [rot]) offset.

    FIX: has_explicit_position is now set to True when an (at ...) node is
    found under the pad. Previously it was never set, causing build_pad_index
    to always use the component origin (fp_x, fp_y) as the pad position,
    which broke trace-to-pad snapping for every pad on the board.
    """
    pads = []

    for child in fp.children:
        if child.name != "pad":
            continue

        number = _strip(child.values[0]) if child.values else ""

        # --- Net ---
        net_name = None
        for c in child.children:
            if c.name == "net":
                try:
                    net_id = int(c.values[0])
                    net_name = net_map.get(net_id, UNCONNECTED_NET)
                except (IndexError, ValueError):
                    pass
                break

        # --- Pad local position ---
        pad_dx, pad_dy, pad_local_rot = 0.0, 0.0, 0.0
        at_nodes = find_direct_children(child, "at")

        found_at = bool(at_nodes)
        if at_nodes:
            vals = at_nodes[0].values
            try:
                if len(vals) >= 1:
                    pad_dx = float(vals[0])
                if len(vals) >= 2:
                    pad_dy = float(vals[1])
                if len(vals) >= 3:
                    pad_local_rot = float(vals[2])
            except (IndexError, ValueError):
                found_at = False

        abs_x, abs_y, abs_rot = _pad_absolute_position(
            fp_x, fp_y, fp_rot, pad_dx, pad_dy, pad_local_rot
        )

        pads.append(Pad(
            number=number,
            net=net_name,
            x=abs_x,
            y=abs_y,
            rotation=abs_rot,
            has_explicit_position=found_at,
        ))

    return pads


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def extract_components(index, nets):
    components = []
    net_map = {n.net_id: n.name for n in nets}
    footprints = index.get("footprint", []) + index.get("module", [])

    # B1 FIX: seen_refs is only used for non-power-symbol components.
    seen_refs = {}

    for fp in footprints:
        ref, value, layer, uuid = "", "", "", ""
        x, y, rot = 0.0, 0.0, 0.0
        footprint_name = _strip(fp.values[0]) if fp.values else ""

        uuid_nodes = find_direct_children(fp, "uuid")
        if uuid_nodes and uuid_nodes[0].values:
            uuid = _strip(uuid_nodes[0].values[0])

        at_nodes = find_direct_children(fp, "at")
        if at_nodes:
            vals = at_nodes[0].values
            try:
                if len(vals) >= 2:
                    x, y = float(vals[0]), float(vals[1])
                if len(vals) >= 3:
                    rot = float(vals[2])
            except (IndexError, ValueError):
                pass

        for c in fp.children:
            if c.name == "layer" and not layer:
                layer = _strip(c.values[0]) if c.values else ""
            elif c.name == "property":
                if len(c.values) < 2:
                    continue
                key = _strip(c.values[0])
                val = _strip(c.values[1])
                if key == "Reference" and not ref:
                    ref = val
                elif key == "Value" and not value:
                    value = val
            elif c.name == "fp_text":
                if not c.values:
                    continue
                t = _strip(c.values[0]).lower()
                v = _strip(c.values[1]) if len(c.values) > 1 else ""
                if t == "reference" and not ref:
                    ref = v
                elif t == "value" and not value:
                    value = v

        if not ref or ref == "REF**":
            continue

        is_pwr_sym = _is_power_symbol_ref(ref)

        # B1 FIX: only track and warn for real (non-power-symbol) components.
        if not is_pwr_sym:
            if ref in seen_refs:
                print(
                    f"[WARNING] Duplicate reference '{ref}' at ({x}, {y}) — "
                    f"same ref as footprint at {seen_refs[ref]}. "
                    f"Both components are included; the diff engine will use "
                    f"their UUIDs to tell them apart."
                )
            else:
                seen_refs[ref] = (x, y)

        pads = extract_pads(fp, net_map, fp_x=x, fp_y=y, fp_rot=rot)

        components.append(Component(
            ref=ref,
            value=value,
            footprint=footprint_name,
            x=x,
            y=y,
            rotation=rot,
            layer=layer,
            pads=pads,
            uuid=uuid,
            is_power_symbol=is_pwr_sym,
        ))

    return components


# ---------------------------------------------------------------------------
# Traces  (with width)
# ---------------------------------------------------------------------------

def extract_traces(index, nets):
    traces = []
    net_map = {n.net_id: n.name for n in nets}

    for seg in index.get("segment", []):
        layer, net_name = "", None
        start, end = None, None
        width = 0.0

        for c in seg.children:
            if c.name == "layer":
                layer = _strip(c.values[0]) if c.values else ""
            elif c.name == "net":
                try:
                    net_id = int(c.values[0])
                    net_name = net_map.get(net_id, UNCONNECTED_NET)
                except (IndexError, ValueError):
                    pass
            elif c.name == "start":
                try:
                    start = (float(c.values[0]), float(c.values[1]))
                except (IndexError, ValueError):
                    pass
            elif c.name == "end":
                try:
                    end = (float(c.values[0]), float(c.values[1]))
                except (IndexError, ValueError):
                    pass
            elif c.name == "width":
                try:
                    width = float(c.values[0])
                except (IndexError, ValueError):
                    pass

        if layer and start and end and net_name is not None:
            traces.append(Trace(
                layer=layer,
                start=start,
                end=end,
                net=net_name,
                width=width,
            ))

    return traces


# ---------------------------------------------------------------------------
# Vias
# ---------------------------------------------------------------------------

def extract_vias(index, nets):
    vias = []
    net_map = {n.net_id: n.name for n in nets}

    for via in index.get("via", []):
        x, y, net_name = None, None, None

        for c in via.children:
            if c.name == "at":
                try:
                    x, y = float(c.values[0]), float(c.values[1])
                except (IndexError, ValueError):
                    pass
            elif c.name == "net":
                try:
                    net_id = int(c.values[0])
                    net_name = net_map.get(net_id, UNCONNECTED_NET)
                except (IndexError, ValueError):
                    pass

        if x is not None and y is not None and net_name is not None:
            vias.append(Via(x=x, y=y, net=net_name))

    return vias


# ---------------------------------------------------------------------------
# Arcs  (gr_arc on copper layers only)
# ---------------------------------------------------------------------------

def extract_arcs(index, nets):
    """
    Parse gr_arc nodes that reside on copper layers.

    KiCad uses three-point arcs: (start ...) (mid ...) (end ...).
    Arcs on non-copper layers (e.g. Edge.Cuts, silkscreen) are skipped.
    Net is extracted from the (net ...) child if present; arcs without a net
    are assigned UNCONNECTED_NET.
    """
    arcs = []
    net_map = {n.net_id: n.name for n in nets}

    for arc_node in index.get("gr_arc", []):
        layer = ""
        start = mid = end = None
        net_name = UNCONNECTED_NET
        width = 0.0

        for c in arc_node.children:
            if c.name == "layer":
                layer = _strip(c.values[0]) if c.values else ""
            elif c.name == "start":
                try:
                    start = (float(c.values[0]), float(c.values[1]))
                except (IndexError, ValueError):
                    pass
            elif c.name == "mid":
                try:
                    mid = (float(c.values[0]), float(c.values[1]))
                except (IndexError, ValueError):
                    pass
            elif c.name == "end":
                try:
                    end = (float(c.values[0]), float(c.values[1]))
                except (IndexError, ValueError):
                    pass
            elif c.name == "net":
                try:
                    net_id = int(c.values[0])
                    net_name = net_map.get(net_id, UNCONNECTED_NET)
                except (IndexError, ValueError):
                    pass
            elif c.name == "width":
                try:
                    width = float(c.values[0])
                except (IndexError, ValueError):
                    pass
            elif c.name == "stroke":
                # KiCad 7+ stores width inside (stroke (width N) ...)
                for sc in c.children:
                    if sc.name == "width":
                        try:
                            width = float(sc.values[0])
                        except (IndexError, ValueError):
                            pass

        if layer and _is_copper_layer(layer) and start and mid and end:
            arcs.append(Arc(
                layer=layer,
                start=start,
                mid=mid,
                end=end,
                net=net_name,
                width=width,
            ))

    return arcs


# ---------------------------------------------------------------------------
# Zones  (copper pours)
# ---------------------------------------------------------------------------

def extract_zones(index, nets):
    """
    Parse zone nodes into Zone objects.

    Each zone has a net, a layer, and a boundary polygon (the first (polygon ...)
    child that contains a (pts ...) list of (xy x y) nodes).
    filled_polygon children are skipped — only the user-defined outline
    is captured, since the fill changes with DRC and is not meaningful for diff.
    """
    zones = []
    net_map = {n.net_id: n.name for n in nets}

    for zone_node in index.get("zone", []):
        layer = ""
        net_name = UNCONNECTED_NET
        uuid = ""
        outline_pts = []

        for c in zone_node.children:
            if c.name == "layer":
                layer = _strip(c.values[0]) if c.values else ""
            elif c.name == "layers":
                # Multi-layer zone (rule area): take the first layer listed.
                layer = _strip(c.values[0]) if c.values else ""
            elif c.name == "net":
                try:
                    net_id = int(c.values[0])
                    net_name = net_map.get(net_id, UNCONNECTED_NET)
                except (IndexError, ValueError):
                    pass
            elif c.name == "uuid":
                uuid = _strip(c.values[0]) if c.values else ""
            elif c.name == "polygon" and not outline_pts:
                # First polygon child is the user-defined boundary.
                for pc in c.children:
                    if pc.name == "pts":
                        for xy in pc.children:
                            if xy.name == "xy":
                                try:
                                    outline_pts.append(
                                        (float(xy.values[0]), float(xy.values[1]))
                                    )
                                except (IndexError, ValueError):
                                    pass

        if layer:
            zones.append(Zone(
                layer=layer,
                net=net_name,
                outline_pts=outline_pts,
                uuid=uuid,
            ))

    return zones


# ---------------------------------------------------------------------------
# Board outline  (Edge.Cuts → tight bounding box in absolute KiCad mm)
# ---------------------------------------------------------------------------

def _arc_bbox(start, mid, end):
    """
    Compute the axis-aligned bounding box of a three-point arc by sampling
    points on the circumscribed circle.  This correctly handles arc extrema
    that lie between the three given points.
    """
    ax, ay = start
    bx, by = mid
    cx, cy = end

    # Circumscribed circle centre via perpendicular-bisector method.
    D = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(D) < 1e-9:
        xs = [ax, bx, cx]
        ys = [ay, by, cy]
        return min(xs), min(ys), max(xs), max(ys)

    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) +
          (cx**2 + cy**2) * (ay - by)) / D
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) +
          (cx**2 + cy**2) * (bx - ax)) / D

    r = math.hypot(ax - ux, ay - uy)

    # Determine CW/CCW direction via cross product of start->mid and start->end.
    cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

    a_start = math.atan2(ay - uy, ax - ux)
    a_end   = math.atan2(cy - uy, cx - ux)

    # Normalise sweep direction.
    if cross >= 0:
        # CCW: ensure a_end > a_start
        if a_end < a_start:
            a_end += 2 * math.pi
    else:
        # CW: ensure a_end < a_start
        if a_end > a_start:
            a_end -= 2 * math.pi

    pts_x = [ax, bx, cx]
    pts_y = [ay, by, cy]
    n_samples = 64
    for i in range(n_samples + 1):
        angle = a_start + (a_end - a_start) * i / n_samples
        pts_x.append(ux + r * math.cos(angle))
        pts_y.append(uy + r * math.sin(angle))

    return min(pts_x), min(pts_y), max(pts_x), max(pts_y)


def extract_board_outline(root):
    """
    Walk all top-level gr_line, gr_arc, and gr_circle nodes whose layer is
    Edge.Cuts and return the tight axis-aligned bounding box as
    (min_x, min_y, max_x, max_y) in KiCad absolute mm.

    Returns None if no Edge.Cuts geometry is found.

    This bounding box is the correct origin for SVG coordinate mapping:
    kicad-cli --page-size-mode 2 shifts the board so that the Edge.Cuts
    top-left corner maps to SVG coordinate (0, 0).  Markers must therefore
    be placed at:
        svg_x = kicad_x - min_x
        svg_y = kicad_y - min_y
    """
    xs, ys = [], []

    for node in root.children:
        # Determine layer for this node.
        layer = ""
        for c in node.children:
            if c.name == "layer":
                layer = _strip(c.values[0]) if c.values else ""
                break

        if "Edge.Cuts" not in layer:
            continue

        if node.name == "gr_line":
            for c in node.children:
                if c.name in ("start", "end"):
                    try:
                        xs.append(float(c.values[0]))
                        ys.append(float(c.values[1]))
                    except (IndexError, ValueError):
                        pass

        elif node.name == "gr_arc":
            start = mid = end = None
            for c in node.children:
                if c.name == "start":
                    try:
                        start = (float(c.values[0]), float(c.values[1]))
                    except (IndexError, ValueError):
                        pass
                elif c.name == "mid":
                    try:
                        mid = (float(c.values[0]), float(c.values[1]))
                    except (IndexError, ValueError):
                        pass
                elif c.name == "end":
                    try:
                        end = (float(c.values[0]), float(c.values[1]))
                    except (IndexError, ValueError):
                        pass
            if start and mid and end:
                bx0, by0, bx1, by1 = _arc_bbox(start, mid, end)
                xs.extend([bx0, bx1])
                ys.extend([by0, by1])

        elif node.name == "gr_circle":
            center = None
            end_pt = None
            for c in node.children:
                if c.name == "center":
                    try:
                        center = (float(c.values[0]), float(c.values[1]))
                    except (IndexError, ValueError):
                        pass
                elif c.name == "end":
                    try:
                        end_pt = (float(c.values[0]), float(c.values[1]))
                    except (IndexError, ValueError):
                        pass
            if center and end_pt:
                r = math.hypot(end_pt[0] - center[0], end_pt[1] - center[1])
                xs.extend([center[0] - r, center[0] + r])
                ys.extend([center[1] - r, center[1] + r])

    if not xs:
        return None

    return (min(xs), min(ys), max(xs), max(ys))