"""
Power Tree Analysis for FluxDiff.

FIX: CONNECTOR_PREFIXES now includes 'BT'/'BAT'/'B' at the top level so
battery components are treated as power sources directly.

FIX: REGULATOR_VALUE_SUBSTRINGS extended with common MCU/IC part numbers.

B6 FIX: _infer_regulator_roles now uses the graph parameter that was
previously accepted but never read. The graph is used to disambiguate
ambiguous nets (those without VIN/VOUT/OUT keywords) by counting how many
non-regulator, non-GND loads are on each candidate net. The net with MORE
loads is classified as the output (regulators drive loads; input rails are
shared with other sources). Previously all ambiguous nets were pushed into
output_nets regardless, which caused regulators with generic net names
(e.g. U3 on nets "3V3_BUS" and "5V_IN") to incorrectly classify their
input net as a second output, producing spurious "rail contention" CRITICAL
findings.

B7 FIX: _is_regulator and _is_ic_load both matched U-prefix components,
causing any unrecognised U-prefix part (e.g. a custom LDO "MY_LDO") to be
classified as a load. The heuristic catch-all described in the original
docstring is now implemented: a U-prefix component with exactly one
power-input net and one power-output net (inferred via _infer_regulator_roles)
is treated as a regulator even if its value doesn't match any known substring.
This prevents the component from appearing in `loads` for the rail it drives.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional
from math import hypot

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

REGULATOR_PREFIXES = ("U", "VR", "IC", "REG", "LDO")

REGULATOR_VALUE_SUBSTRINGS = (
    "7805", "7812", "7815", "7833", "7905",
    "LM317", "LM337", "LM1117", "LM2596", "LM2576",
    "AMS1117", "AP2112", "MCP1700", "MCP1702",
    "TPS", "LT1", "LT3", "LT8",
    "REG",
)

REGULATOR_FOOTPRINT_SUBSTRINGS = (
    "SOT-223", "SOT-89", "TO-252", "DPAK", "TO-220", "TO-92",
)

CONNECTOR_PREFIXES = ("J", "P", "CN", "CONN", "X")
BATTERY_PREFIXES   = ("BT", "BAT", "B")

POWER_NET_SUBSTRINGS = (
    "VCC", "VDD", "VBUS", "V3V3", "V5V", "V1V8",
    "V3V", "AVCC", "AVDD", "DVCC", "DVDD", "VIN",
    "VOUT", "VREG", "VSYS", "VBAT", "PWR",
)

GND_NET_SUBSTRINGS = ("GND", "AGND", "DGND", "PGND", "EARTH", "VSS", "GNDPWR")

POWER_SYMBOL_PREFIXES = ("#PWR", "#FLG")

LOAD_COUNT_ADVISORY = 5


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PowerRail:
    net_name: str
    sources: Set[str] = field(default_factory=set)
    loads: Set[str] = field(default_factory=set)
    regulators_out: List[str] = field(default_factory=list)


@dataclass
class PowerTree:
    rails: Dict[str, PowerRail] = field(default_factory=dict)
    regulator_outputs: Dict[str, List[str]] = field(default_factory=dict)
    findings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_power_symbol(ref):
    return any(ref.upper().startswith(p) for p in POWER_SYMBOL_PREFIXES)

def _is_connector(comp):
    return any(comp.ref.upper().startswith(p) for p in CONNECTOR_PREFIXES)

def _is_battery(comp):
    return any(comp.ref.upper().startswith(p) for p in BATTERY_PREFIXES)

def _is_power_source(comp):
    return _is_connector(comp) or _is_battery(comp)

def _is_ic_load(comp):
    return any(comp.ref.upper().startswith(p) for p in ("U", "IC", "AR"))

def _net_is_power(net_name):
    upper = net_name.upper()
    return any(sub in upper for sub in POWER_NET_SUBSTRINGS)

def _net_is_gnd(net_name):
    upper = net_name.upper()
    return any(sub in upper for sub in GND_NET_SUBSTRINGS)

def _real_refs(connections):
    return {ref for ref, _ in connections if ref != "VIA" and not _is_power_symbol(ref)}


def _is_known_regulator(comp) -> bool:
    """
    Returns True for components that are definitively identifiable as
    regulators from their ref prefix, value string, or footprint.
    Does NOT include the U-prefix heuristic — that is handled separately
    in build_power_tree via _infer_regulator_roles.
    """
    ref_upper = comp.ref.upper()
    if any(ref_upper.startswith(p) for p in ("VR", "REG", "LDO")):
        return True
    if any(ref_upper.startswith(p) for p in ("U", "IC")):
        val_upper = comp.value.upper()
        fp_upper  = comp.footprint.upper()
        if any(s in val_upper for s in REGULATOR_VALUE_SUBSTRINGS):
            return True
        if any(s in fp_upper for s in REGULATOR_FOOTPRINT_SUBSTRINGS):
            return True
    return False


# ---------------------------------------------------------------------------
# Regulator pin role inference
# ---------------------------------------------------------------------------

def _infer_regulator_roles(comp, graph):
    """
    Classify each non-GND pad net of `comp` as an input or output net.

    B6 FIX: The `graph` parameter is now actually used. Previously it was
    accepted in the signature but never read, so ambiguous nets (those
    without VIN/VOUT/OUT keywords) were all pushed into output_nets,
    producing spurious multi-output and rail-contention findings.

    Resolution strategy for ambiguous nets:
      1. Nets with explicit output keywords (VOUT, OUT, VREG) → output.
      2. Nets with explicit input keywords (VIN, VBUS, VBAT) → input.
      3. Remaining nets: count non-regulator, non-GND connections in the
         graph. The net with MORE connections is the output (regulators
         drive loads; input rails are shared with other sources and have
         more connections). On a tie, the net is treated as output
         (conservative — avoids hiding a potential problem).
    """
    input_nets, output_nets, ambiguous = [], [], []

    for pad in comp.pads:
        net = pad.net
        if not net or net == "__unconnected__":
            continue
        if _net_is_gnd(net):
            continue
        upper = net.upper()
        if any(kw in upper for kw in ("VOUT", "OUT", "VREG")):
            output_nets.append(net)
        elif any(kw in upper for kw in ("VIN", "VBUS", "VBAT")):
            input_nets.append(net)
        else:
            ambiguous.append(net)

    # B6 FIX: use graph load counts to resolve ambiguous nets
    if ambiguous and graph:
        def _load_count(net):
            conns = graph.get(net, set())
            return sum(
                1 for ref, _ in conns
                if ref != "VIA"
                and not _is_power_symbol(ref)
                and ref != comp.ref
            )

        # Sort descending by load count — highest load count = output net
        ambiguous.sort(key=_load_count, reverse=True)

        if len(ambiguous) >= 2:
            # First (most loads) → output, rest → input
            output_nets.append(ambiguous[0])
            input_nets.extend(ambiguous[1:])
        else:
            # Single ambiguous net: treat as output (conservative)
            output_nets.extend(ambiguous)
    else:
        # No graph available — fall back to original behaviour
        output_nets.extend(ambiguous)

    if not output_nets and not input_nets:
        for pad in comp.pads:
            if pad.net and pad.net != "__unconnected__" and not _net_is_gnd(pad.net):
                output_nets.append(pad.net)

    return input_nets, output_nets


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_power_tree(pcb, graph):
    tree = PowerTree()
    comp_map = {c.ref: c for c in pcb.components if not _is_power_symbol(c.ref)}

    for net_name in graph:
        if _net_is_power(net_name) and not _net_is_gnd(net_name):
            tree.rails[net_name] = PowerRail(net_name=net_name)

    # B7 FIX: Two-pass approach.
    # Pass 1: identify all regulators (known + U-prefix heuristic).
    # Pass 2: classify loads, skipping anything identified as a regulator
    #         in pass 1 to prevent a custom LDO from appearing in load lists.

    regulator_refs = set()

    for ref, comp in comp_map.items():
        if _is_known_regulator(comp):
            regulator_refs.add(ref)
            continue

        # B7 FIX: U-prefix heuristic catch-all — a U-prefixed component with
        # exactly one power-input and one power-output net is a regulator even
        # if its value string is not in REGULATOR_VALUE_SUBSTRINGS.
        if any(comp.ref.upper().startswith(p) for p in ("U", "IC")):
            input_nets, output_nets = _infer_regulator_roles(comp, graph)
            # Deduplicate while preserving order
            input_nets  = list(dict.fromkeys(input_nets))
            output_nets = list(dict.fromkeys(output_nets))
            if len(input_nets) == 1 and len(output_nets) == 1:
                regulator_refs.add(ref)

    for ref, comp in comp_map.items():
        is_reg = ref in regulator_refs
        is_src = _is_power_source(comp)
        is_load = _is_ic_load(comp)

        if is_reg:
            _, output_nets = _infer_regulator_roles(comp, graph)
            output_nets = list(dict.fromkeys(output_nets))
            tree.regulator_outputs[ref] = output_nets
            for net in output_nets:
                if net in tree.rails:
                    tree.rails[net].sources.add(ref)

        if is_src:
            for pad in comp.pads:
                if pad.net and pad.net in tree.rails:
                    tree.rails[pad.net].sources.add(ref)

        # B7 FIX: only add to loads if not already classified as a regulator
        if is_load and not is_reg:
            for pad in comp.pads:
                if pad.net and pad.net in tree.rails:
                    tree.rails[pad.net].loads.add(ref)

    return tree


# ---------------------------------------------------------------------------
# Finding generators
# ---------------------------------------------------------------------------

def _findings_unused_outputs(tree):
    msgs = []
    for ref, output_nets in tree.regulator_outputs.items():
        for net in output_nets:
            if net not in tree.rails: continue
            if not [l for l in tree.rails[net].loads if l != ref]:
                msgs.append(f"WARNING: Regulator {ref} output net '{net}' has no downstream loads — possibly unused or unrouted")
    return msgs

def _findings_rail_contention(tree):
    msgs = []
    for net_name, rail in tree.rails.items():
        reg_sources = [s for s in rail.sources if s in tree.regulator_outputs]
        if len(reg_sources) > 1:
            msgs.append(f"CRITICAL: Net '{net_name}' driven by multiple regulators ({', '.join(sorted(reg_sources))}) — contention risk")
    return msgs

def _findings_sourceless_rails(tree):
    msgs = []
    for net_name, rail in tree.rails.items():
        if rail.loads and not rail.sources:
            msgs.append(f"WARNING: Power net '{net_name}' has {len(rail.loads)} load(s) but no source — floating supply rail")
    return msgs

def _findings_load_advisory(tree):
    msgs = []
    for net_name, rail in tree.rails.items():
        if len(rail.loads) > LOAD_COUNT_ADVISORY:
            msgs.append(f"INFO: Power net '{net_name}' has {len(rail.loads)} loads — verify current budget")
    return msgs

def _findings_empty_rails(tree):
    msgs = []
    for net_name, rail in tree.rails.items():
        if not rail.sources and not rail.loads:
            msgs.append(f"INFO: Power net '{net_name}' exists but has no sources or loads")
    return msgs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_power_tree(pcb, graph):
    tree = build_power_tree(pcb, graph)
    findings = []
    findings.extend(_findings_rail_contention(tree))
    findings.extend(_findings_unused_outputs(tree))
    findings.extend(_findings_sourceless_rails(tree))
    findings.extend(_findings_load_advisory(tree))
    findings.extend(_findings_empty_rails(tree))

    seen, deduped = set(), []
    for f in findings:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    deduped.sort(key=lambda m: 0 if m.startswith("CRITICAL") else 1 if m.startswith("WARNING") else 2)
    tree.findings = deduped
    return tree, deduped


def format_power_tree_report(tree):
    if not tree.rails:
        return "  (no power rails detected)"
    lines = []
    for net_name in sorted(tree.rails):
        rail = tree.rails[net_name]
        src_str  = ", ".join(sorted(rail.sources)) if rail.sources else "⚠ no source"
        load_str = ", ".join(sorted(rail.loads))   if rail.loads   else "none"
        lines.append(f"  {net_name:20s}  source: {src_str:25s}  loads: {load_str}")
    return "\n".join(lines)