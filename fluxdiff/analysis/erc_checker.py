"""
Functional ERC checks on a connectivity graph + component list.

F7 FIX: _check_power_shorts checks net names, not component refs.

FIX: POWER_NET_SUBSTRINGS and _GND_KEYWORDS now include GNDPWR and slash-
prefixed variants are handled by substring matching (e.g. '/VCC' contains 'VCC').

B4 FIX: _check_bypass_caps previously measured Euclidean distance between
component *centroids* (comp.x, comp.y). For large ICs (QFP, BGA, SO-16) the
centroid can be 3–5 mm from the power-pin pads, so a decoupling cap that is
physically adjacent to the power pins appears distant and triggers a false
WARNING. The fix measures from the IC's nearest power-pin pad position to the
cap's nearest pad position, giving a physically meaningful distance. When a
component has no parsed pad positions (has_explicit_position False for all
pads) it falls back to the centroid so behaviour is unchanged for simple
single-pad or through-hole components.
"""

from math import hypot

I2C_NET_SUBSTRINGS = ("SDA", "SCL")
OPEN_DRAIN_NET_SUBSTRINGS = ("OD", "OPEN_DRAIN", "OPENDRAIN", "INT", "ALERT", "NRST", "RESET")
BYPASS_CAP_RADIUS_MM = 5.0
BYPASS_CAP_MIN_CONNECTIONS = 2

POWER_NET_SUBSTRINGS = ("VCC", "VDD", "VBUS", "V3V3", "V5V", "V1V8", "V3V", "AVCC", "AVDD", "DVCC", "DVDD")
POWER_SYMBOL_PREFIXES = ("#PWR", "#FLG")
RESISTOR_PREFIXES = ("R",)
CAPACITOR_PREFIXES = ("C",)
IC_PREFIXES = ("U", "IC", "AR")

_VCC_KEYWORDS = ("VCC", "VDD", "VBUS", "V3V3", "V5V", "V1V8", "VSYS", "VBAT")
_GND_KEYWORDS = ("GND", "AGND", "DGND", "PGND", "VSS", "GNDPWR")


def _is_power_symbol(ref):
    return any(ref.upper().startswith(p) for p in POWER_SYMBOL_PREFIXES)

def _is_resistor(ref):
    return any(ref.upper().startswith(p) for p in RESISTOR_PREFIXES)

def _is_capacitor(ref):
    return any(ref.upper().startswith(p) for p in CAPACITOR_PREFIXES)

def _is_ic(ref):
    return any(ref.upper().startswith(p) for p in IC_PREFIXES)

def _real_connections(connections):
    return {(ref, pad) for ref, pad in connections
            if ref != "VIA" and not _is_power_symbol(ref)}

def _net_is_power(net_name):
    upper = net_name.upper()
    return any(sub in upper for sub in POWER_NET_SUBSTRINGS)

def _net_needs_pullup(net_name):
    upper = net_name.upper()
    return (any(sub in upper for sub in I2C_NET_SUBSTRINGS)
            or any(sub in upper for sub in OPEN_DRAIN_NET_SUBSTRINGS))

def _distance(ax, ay, bx, by):
    return hypot(ax - bx, ay - by)

def _build_component_lookup(components):
    return {c.ref: c for c in components if not _is_power_symbol(c.ref)}


# B4 FIX: helpers to find the best representative position for proximity checks.

def _power_pin_position(comp, net_name):
    """
    Return (x, y) of the pad on `comp` connected to `net_name`.
    Falls back to the component centroid if no pad has an explicit position
    or none is connected to the given net.
    """
    for pad in comp.pads:
        if pad.net == net_name and pad.has_explicit_position:
            return pad.x, pad.y
    # Fallback: centroid (correct for single-pad / THT components)
    return comp.x, comp.y


def _nearest_pad_position(comp):
    """
    Return (x, y) of the first pad with an explicit position, or the
    component centroid if no pad has been positioned by the parser.
    """
    for pad in comp.pads:
        if pad.has_explicit_position:
            return pad.x, pad.y
    return comp.x, comp.y


def _check_pullups(graph, comp_lookup):
    messages = []
    for net_name, connections in graph.items():
        if not _net_needs_pullup(net_name): continue
        real_conns = _real_connections(connections)
        if not real_conns: continue
        if not any(_is_resistor(ref) for ref, _ in real_conns):
            messages.append(
                f"WARNING: Net '{net_name}' looks like an open-drain/I2C signal "
                f"but has no pull-up resistor connected")
    return messages


def _check_bypass_caps(graph, comp_lookup):
    """
    B4 FIX: Distance is now measured from the IC's power-pin pad position
    to the cap's nearest pad position, not between component centroids.
    This eliminates false WARNINGs for large-footprint ICs where the centroid
    is several mm from the actual power pins.
    """
    messages = []
    for net_name, connections in graph.items():
        if not _net_is_power(net_name): continue
        real_conns = _real_connections(connections)
        if len(real_conns) < BYPASS_CAP_MIN_CONNECTIONS: continue
        ics_on_net  = [ref for ref, _ in real_conns if _is_ic(ref) and ref in comp_lookup]
        caps_on_net = [ref for ref, _ in real_conns if _is_capacitor(ref) and ref in comp_lookup]
        for ic_ref in ics_on_net:
            ic = comp_lookup[ic_ref]
            # B4 FIX: use power-pin position for the IC, nearest pad for caps
            ic_px, ic_py = _power_pin_position(ic, net_name)
            nearby = any(
                _distance(
                    ic_px, ic_py,
                    *_nearest_pad_position(comp_lookup[cap_ref])
                ) <= BYPASS_CAP_RADIUS_MM
                for cap_ref in caps_on_net if cap_ref in comp_lookup
            )
            if not nearby:
                messages.append(
                    f"WARNING: {ic_ref} has no bypass capacitor within "
                    f"{BYPASS_CAP_RADIUS_MM}mm on power net '{net_name}'")
    return messages


def _check_power_nets(graph):
    messages = []
    for net_name, connections in graph.items():
        if not _net_is_power(net_name): continue
        real_conns = _real_connections(connections)
        if len(real_conns) == 0:
            messages.append(f"WARNING: Power net '{net_name}' has no real component connections")
        elif len(real_conns) == 1:
            only_ref = next(iter(real_conns))[0]
            messages.append(
                f"WARNING: Power net '{net_name}' has only one connection "
                f"({only_ref}) — likely a dangling rail")
    return messages


def _check_floating_nets(graph):
    messages = []
    for net_name, connections in graph.items():
        if _net_is_power(net_name): continue
        if net_name in ("__unconnected__", "", "Net-(0)"): continue
        real_conns = _real_connections(connections)
        if len(real_conns) == 0:
            messages.append(f"INFO: Net '{net_name}' has no connections")
        elif len(real_conns) == 1:
            only_ref = next(iter(real_conns))[0]
            messages.append(f"WARNING: Net '{net_name}' is floating (only one real connection: {only_ref})")
    return messages


def _check_power_shorts(graph):
    messages = []
    for net_name in graph:
        upper = net_name.upper()
        has_gnd = any(kw in upper for kw in _GND_KEYWORDS)
        has_vcc = any(kw in upper for kw in _VCC_KEYWORDS)
        if has_gnd and has_vcc:
            messages.append(
                f"CRITICAL: Net '{net_name}' name contains both a ground marker "
                f"and a power-rail marker; possible schematic short")
    return messages


def run_erc_checks(graph, components=None):
    messages = []
    comp_lookup = _build_component_lookup(components) if components else {}

    messages.extend(_check_power_shorts(graph))
    messages.extend(_check_pullups(graph, comp_lookup))

    if components:
        messages.extend(_check_bypass_caps(graph, comp_lookup))
    else:
        messages.append("INFO: Bypass capacitor check skipped — component list not provided")

    messages.extend(_check_power_nets(graph))
    messages.extend(_check_floating_nets(graph))

    seen, deduped = set(), []
    for msg in messages:
        if msg not in seen:
            seen.add(msg)
            deduped.append(msg)

    deduped.sort(key=lambda m: 0 if m.startswith("CRITICAL") else 1 if m.startswith("WARNING") else 2)
    return deduped