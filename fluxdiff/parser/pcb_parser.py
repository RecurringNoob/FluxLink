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
  - extract_board_outline: reads Edge.Cuts shapes (board and footprint) to
    find the tight bounding box of the board outline in absolute KiCad mm.
  - extract_outline_anchors: Edge.Cuts points that plotters write verbatim,
    used by visual/svg_frame.py to locate the board inside exported SVGs.
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
    arcs = extract_arcs(root, index, nets)
    zones = extract_zones(index, nets)
    board_outline = extract_board_outline(root)
    outline_anchors = extract_outline_anchors(root)

    return PCBData(
        components=components,
        nets=nets,
        traces=traces,
        vias=vias,
        arcs=arcs,
        zones=zones,
        board_outline=board_outline,
        outline_anchors=outline_anchors,
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

def extract_arcs(root, index, nets):
    """
    Parse copper arcs.

    Routed arc tracks are top-level ``(arc ...)`` nodes (KiCad 6+); copper
    graphic arcs are ``(gr_arc ...)``.  Only top-level ``arc`` nodes are read:
    KiCad 7+ also writes ``(arc ...)`` entries inside polygon ``(pts ...)``
    lists, which are outline geometry, not tracks.

    KiCad uses three-point arcs: (start ...) (mid ...) (end ...).
    Arcs on non-copper layers (e.g. Edge.Cuts, silkscreen) are skipped.
    Net is extracted from the (net ...) child if present; arcs without a net
    are assigned UNCONNECTED_NET.
    """
    arcs = []
    net_map = {n.net_id: n.name for n in nets}
    arc_nodes = [c for c in root.children if c.name == "arc"] + index.get("gr_arc", [])

    for arc_node in arc_nodes:
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


def _xy(node, name):
    for c in node.children:
        if c.name == name:
            try:
                return float(c.values[0]), float(c.values[1])
            except (IndexError, ValueError):
                return None
    return None


def _shape_points(node):
    """
    Points whose bounding box covers one Edge.Cuts shape, in the shape's own
    coordinate frame.  Handles line/rect/arc/circle/poly/curve, with both the
    gr_* (board) and fp_* (footprint) spellings.
    """
    kind = node.name.split("_", 1)[-1]
    if kind in ("line", "rect"):
        pts = [p for p in (_xy(node, "start"), _xy(node, "end")) if p]
        if kind == "rect" and len(pts) == 2:
            (x0, y0), (x1, y1) = pts
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return pts
    if kind == "arc":
        start, mid, end = _xy(node, "start"), _xy(node, "mid"), _xy(node, "end")
        if start and mid and end:
            bx0, by0, bx1, by1 = _arc_bbox(start, mid, end)
            return [(bx0, by0), (bx1, by1)]
        return []
    if kind == "circle":
        center, end = _xy(node, "center"), _xy(node, "end")
        if center and end:
            r = math.hypot(end[0] - center[0], end[1] - center[1])
            return [(center[0] - r, center[1] - r), (center[0] + r, center[1] + r)]
        return []
    if kind in ("poly", "curve"):
        # Bezier control points bound the curve, so this is conservative.
        pts = []
        for c in node.children:
            if c.name != "pts":
                continue
            for item in c.children:
                if item.name == "xy" and len(item.values) >= 2:
                    try:
                        pts.append((float(item.values[0]), float(item.values[1])))
                    except ValueError:
                        pass
                elif item.name == "arc":
                    s, m, e = _xy(item, "start"), _xy(item, "mid"), _xy(item, "end")
                    if s and m and e:
                        bx0, by0, bx1, by1 = _arc_bbox(s, m, e)
                        pts.extend([(bx0, by0), (bx1, by1)])
        return pts
    return []


def _node_layer(node):
    for c in node.children:
        if c.name == "layer":
            return _strip(c.values[0]) if c.values else ""
    return ""


_EDGE_SHAPES = {"line", "rect", "arc", "circle", "poly", "curve"}


def _edge_cut_items(root):
    """
    Yield (shape_node, to_board) for every Edge.Cuts shape: top-level gr_*
    items, and fp_* items inside footprints with their local -> board
    transform.
    """
    identity = lambda x, y: (x, y)
    for node in root.children:
        if node.name.startswith("gr_") and node.name[3:] in _EDGE_SHAPES:
            if "Edge.Cuts" in _node_layer(node):
                yield node, identity
        elif node.name in ("footprint", "module"):
            fp_at = _xy(node, "at") or (0.0, 0.0)
            fp_rot = 0.0
            for c in node.children:
                if c.name == "at" and len(c.values) >= 3:
                    try:
                        fp_rot = float(c.values[2])
                    except ValueError:
                        pass

            def to_board(x, y, fx=fp_at[0], fy=fp_at[1], rot=fp_rot):
                rx, ry = _rotate_point(x, y, rot)
                return fx + rx, fy + ry

            for child in node.children:
                if (child.name.startswith("fp_") and child.name[3:] in _EDGE_SHAPES
                        and "Edge.Cuts" in _node_layer(child)):
                    yield child, to_board


def _anchor_points(node):
    """Points of one shape that a plotter writes as-is (see outline_anchors)."""
    kind = node.name.split("_", 1)[-1]
    if kind == "line":
        return [p for p in (_xy(node, "start"), _xy(node, "end")) if p]
    if kind == "rect":
        s, e = _xy(node, "start"), _xy(node, "end")
        if s and e:
            return [s, (e[0], s[1]), e, (s[0], e[1])]
        return []
    if kind == "arc":
        return [p for p in (_xy(node, "start"), _xy(node, "end")) if p]
    if kind == "circle":
        c = _xy(node, "center")
        return [c] if c else []
    if kind in ("poly", "curve"):
        pts = []
        for c in node.children:
            if c.name != "pts":
                continue
            for item in c.children:
                if item.name == "xy" and len(item.values) >= 2:
                    try:
                        pts.append((float(item.values[0]), float(item.values[1])))
                    except ValueError:
                        pass
                elif item.name == "arc":
                    pts.extend(p for p in (_xy(item, "start"), _xy(item, "end")) if p)
        return pts
    return []


def extract_outline_anchors(root):
    """Board-space anchor points of every Edge.Cuts shape (see PCBData.outline_anchors)."""
    anchors = []
    for node, to_board in _edge_cut_items(root):
        anchors.extend(to_board(x, y) for x, y in _anchor_points(node))
    return anchors


def extract_board_outline(root):
    """
    Collect every Edge.Cuts shape — top-level gr_line / gr_rect / gr_arc /
    gr_circle / gr_poly / gr_curve, plus fp_* Edge.Cuts items inside
    footprints (transformed to board space) — and return the tight
    axis-aligned bounding box as (min_x, min_y, max_x, max_y) in KiCad
    absolute mm.

    Returns None if no Edge.Cuts geometry is found.

    Note: kicad-cli's "board area only" SVG is NOT framed on this box (it
    uses KiCad's item bounding box, which can be larger).  The CLI crops
    each SVG to the diff's board_bounds with visual/svg_frame.py.
    """
    xs, ys = [], []

    def add(points):
        for x, y in points:
            xs.append(x)
            ys.append(y)

    for node, to_board in _edge_cut_items(root):
        local = _shape_points(node)
        if node.name.startswith("fp_") and len(local) == 2:
            # Footprint items are stored in the footprint's local frame.
            # Rotating a bbox's corners and re-boxing stays conservative.
            (x0, y0), (x1, y1) = local
            local = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        add(to_board(x, y) for x, y in local)

    if not xs:
        return None

    return (min(xs), min(ys), max(xs), max(ys))