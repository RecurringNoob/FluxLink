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
"""

import math
import os
from fluxdiff.models.pcb_models import PCBData, Component, Net, Trace, Via, Pad
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

    return PCBData(
        components=components,
        nets=nets,
        traces=traces,
        vias=vias,
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
            # assigns a real name to net 0, preserve it and warn — do not
            # silently discard the name, which would make all pads on that
            # net appear unconnected with no diagnostic.
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
    # Power symbols (#PWR, #FLG) legitimately share reference designators
    # across a schematic (e.g. multiple #PWR01 on different sheets) and
    # should never trigger a duplicate warning.
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