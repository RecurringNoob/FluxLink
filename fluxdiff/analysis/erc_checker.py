"""
Functional ERC checks on a connectivity graph + component list.

Return type change: all check functions now return list[Finding] instead of
list[str].  The Finding dataclass carries the same human-readable message as
before (str(finding) == old string) plus board coordinates and related refs
so the React viewer can pan to the relevant location.

Coordinate strategy per check:
  _check_bypass_caps   — IC's power-pin pad position (B4 FIX, already computed)
  _check_pullups       — centroid of the net's connected pad positions
  _check_power_nets    — centroid of the net's connected pad positions
  _check_floating_nets — position of the single connected pad, or None
  _check_power_shorts  — None (net-name issue, no board location)

All other logic, tunables, and fix annotations are unchanged.
"""

from math import hypot
from fluxdiff.models.pcb_models import Finding

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

CAT = "ERC"


# ---------------------------------------------------------------------------
# Helpers (unchanged from original)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Coordinate helpers (B4 FIX — pad positions, not centroids)
# ---------------------------------------------------------------------------

def _power_pin_position(comp, net_name):
    for pad in comp.pads:
        if pad.net == net_name and pad.has_explicit_position:
            return pad.x, pad.y
    return comp.x, comp.y


def _nearest_pad_position(comp):
    for pad in comp.pads:
        if pad.has_explicit_position:
            return pad.x, pad.y
    return comp.x, comp.y


def _net_centroid(net_name, connections, comp_lookup):
    """
    Return the centroid (x, y) of all pad positions on this net, or None
    if no positioned components are found.  Used for pullup and floating-net
    findings where there is no single canonical component.
    """
    xs, ys = [], []
    for ref, _ in connections:
        if ref == "VIA" or _is_power_symbol(ref):
            continue
        comp = comp_lookup.get(ref)
        if comp:
            x, y = _nearest_pad_position(comp)
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return sum(xs) / len(xs), sum(ys) / len(ys)


# ---------------------------------------------------------------------------
# Check functions — now return list[Finding]
# ---------------------------------------------------------------------------

def _check_pullups(graph, comp_lookup) -> list:
    findings = []
    for net_name, connections in graph.items():
        if not _net_needs_pullup(net_name):
            continue
        real_conns = _real_connections(connections)
        if not real_conns:
            continue
        if not any(_is_resistor(ref) for ref, _ in real_conns):
            coords = _net_centroid(net_name, real_conns, comp_lookup)
            refs = tuple(sorted(
                ref for ref, _ in real_conns
                if not _is_power_symbol(ref) and ref != "VIA"
            ))
            findings.append(Finding(
                severity="WARNING",
                category=CAT,
                message=(
                    f"Net '{net_name}' looks like an open-drain/I2C signal "
                    f"but has no pull-up resistor connected"
                ),
                related_refs=refs,
                affected_nets=(net_name,),
                coordinates=coords,
                highlight_refs=refs,
            ))
    return findings


def _check_bypass_caps(graph, comp_lookup) -> list:
    """
    B4 FIX: Distance measured from IC power-pin pad to cap nearest pad.
    Coordinate stored on Finding is the IC's power-pin pad position.
    """
    findings = []
    for net_name, connections in graph.items():
        if not _net_is_power(net_name):
            continue
        real_conns = _real_connections(connections)
        if len(real_conns) < BYPASS_CAP_MIN_CONNECTIONS:
            continue
        ics_on_net  = [ref for ref, _ in real_conns if _is_ic(ref) and ref in comp_lookup]
        caps_on_net = [ref for ref, _ in real_conns if _is_capacitor(ref) and ref in comp_lookup]
        for ic_ref in ics_on_net:
            ic = comp_lookup[ic_ref]
            ic_px, ic_py = _power_pin_position(ic, net_name)
            nearby = any(
                _distance(ic_px, ic_py, *_nearest_pad_position(comp_lookup[cap_ref]))
                <= BYPASS_CAP_RADIUS_MM
                for cap_ref in caps_on_net
                if cap_ref in comp_lookup
            )
            if not nearby:
                findings.append(Finding(
                    severity="WARNING",
                    category=CAT,
                    message=(
                        f"{ic_ref} has no bypass capacitor within "
                        f"{BYPASS_CAP_RADIUS_MM}mm on power net '{net_name}'"
                    ),
                    related_refs=(ic_ref,),
                    affected_nets=(net_name,),
                    coordinates=(ic_px, ic_py),
                    highlight_refs=(ic_ref,),
                ))
    return findings


def _check_power_nets(graph, comp_lookup) -> list:
    findings = []
    for net_name, connections in graph.items():
        if not _net_is_power(net_name):
            continue
        real_conns = _real_connections(connections)
        coords = _net_centroid(net_name, real_conns, comp_lookup)
        if len(real_conns) == 0:
            findings.append(Finding(
                severity="WARNING",
                category=CAT,
                message=f"Power net '{net_name}' has no real component connections",
                affected_nets=(net_name,),
                coordinates=coords,
            ))
        elif len(real_conns) == 1:
            only_ref = next(iter(real_conns))[0]
            findings.append(Finding(
                severity="WARNING",
                category=CAT,
                message=(
                    f"Power net '{net_name}' has only one connection "
                    f"({only_ref}) — likely a dangling rail"
                ),
                related_refs=(only_ref,),
                affected_nets=(net_name,),
                coordinates=coords,
                highlight_refs=(only_ref,),
            ))
    return findings


def _check_floating_nets(graph, comp_lookup) -> list:
    findings = []
    for net_name, connections in graph.items():
        if _net_is_power(net_name):
            continue
        if net_name in ("__unconnected__", "", "Net-(0)"):
            continue
        real_conns = _real_connections(connections)
        if len(real_conns) == 0:
            findings.append(Finding(
                severity="INFO",
                category=CAT,
                message=f"Net '{net_name}' has no connections",
                affected_nets=(net_name,),
            ))
        elif len(real_conns) == 1:
            only_ref = next(iter(real_conns))[0]
            comp = comp_lookup.get(only_ref)
            coords = _nearest_pad_position(comp) if comp else None
            findings.append(Finding(
                severity="WARNING",
                category=CAT,
                message=(
                    f"Net '{net_name}' is floating "
                    f"(only one real connection: {only_ref})"
                ),
                related_refs=(only_ref,),
                affected_nets=(net_name,),
                coordinates=coords,
                highlight_refs=(only_ref,),
            ))
    return findings


def _check_power_shorts(graph) -> list:
    findings = []
    for net_name in graph:
        upper = net_name.upper()
        has_gnd = any(kw in upper for kw in _GND_KEYWORDS)
        has_vcc = any(kw in upper for kw in _VCC_KEYWORDS)
        if has_gnd and has_vcc:
            findings.append(Finding(
                severity="CRITICAL",
                category=CAT,
                message=(
                    f"Net '{net_name}' name contains both a ground marker "
                    f"and a power-rail marker; possible schematic short"
                ),
                affected_nets=(net_name,),
                # No board coordinate — this is a net-naming issue
                coordinates=None,
            ))
    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_erc_checks(graph, components=None) -> list:
    """
    Run all ERC checks.

    Returns list[Finding] sorted CRITICAL → WARNING → INFO.
    str(finding) produces the same output as the previous list[str] format,
    so existing code that prints or logs findings is unaffected.
    """
    comp_lookup = _build_component_lookup(components) if components else {}
    findings = []

    findings.extend(_check_power_shorts(graph))
    findings.extend(_check_pullups(graph, comp_lookup))

    if components:
        findings.extend(_check_bypass_caps(graph, comp_lookup))
    else:
        findings.append(Finding(
            severity="INFO",
            category=CAT,
            message="Bypass capacitor check skipped — component list not provided",
        ))

    findings.extend(_check_power_nets(graph, comp_lookup))
    findings.extend(_check_floating_nets(graph, comp_lookup))

    seen, deduped = set(), []
    for f in findings:
        if f not in seen:
            seen.add(f)
            deduped.append(f)

    deduped.sort(key=lambda f: (
        0 if f.severity == "CRITICAL" else
        1 if f.severity == "WARNING" else 2
    ))
    return deduped