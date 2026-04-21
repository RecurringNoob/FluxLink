from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional
from math import hypot
from fluxdiff.models.pcb_models import Finding

REGULATOR_PREFIXES = ("U", "VR", "IC", "REG", "LDO")
REGULATOR_VALUE_SUBSTRINGS = (
    "7805", "7812", "7815", "7833", "7905",
    "LM317", "LM337", "LM1117", "LM2596", "LM2576",
    "AMS1117", "AP2112", "MCP1700", "MCP1702",
    "TPS", "LT1", "LT3", "LT8", "REG",
)
REGULATOR_FOOTPRINT_SUBSTRINGS = (
    "SOT-223", "SOT-89", "TO-252", "DPAK", "TO-220", "TO-92",
)
CONNECTOR_PREFIXES   = ("J", "P", "CN", "CONN", "X")
BATTERY_PREFIXES     = ("BT", "BAT", "B")
POWER_NET_SUBSTRINGS = (
    "VCC", "VDD", "VBUS", "V3V3", "V5V", "V1V8",
    "V3V", "AVCC", "AVDD", "DVCC", "DVDD", "VIN",
    "VOUT", "VREG", "VSYS", "VBAT", "PWR",
)
GND_NET_SUBSTRINGS   = ("GND", "AGND", "DGND", "PGND", "EARTH", "VSS", "GNDPWR")
POWER_SYMBOL_PREFIXES = ("#PWR", "#FLG")
LOAD_COUNT_ADVISORY  = 5
CAT = "POWER"


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
    findings: List[Finding] = field(default_factory=list)


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
    return any(sub in net_name.upper() for sub in POWER_NET_SUBSTRINGS)

def _net_is_gnd(net_name):
    return any(sub in net_name.upper() for sub in GND_NET_SUBSTRINGS)

def _real_refs(connections):
    return {ref for ref, _ in connections if ref != "VIA" and not _is_power_symbol(ref)}


def _is_known_regulator(comp) -> bool:
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


def _infer_regulator_roles(comp, graph):
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

    if ambiguous and graph:
        def _load_count(net):
            conns = graph.get(net, set())
            return sum(
                1 for ref, _ in conns
                if ref != "VIA"
                and not _is_power_symbol(ref)
                and ref != comp.ref
            )
        ambiguous.sort(key=_load_count, reverse=True)
        if len(ambiguous) >= 2:
            output_nets.append(ambiguous[0])
            input_nets.extend(ambiguous[1:])
        else:
            output_nets.extend(ambiguous)
    else:
        output_nets.extend(ambiguous)

    if not output_nets and not input_nets:
        for pad in comp.pads:
            if pad.net and pad.net != "__unconnected__" and not _net_is_gnd(pad.net):
                output_nets.append(pad.net)

    return input_nets, output_nets


def build_power_tree(pcb, graph):
    tree     = PowerTree()
    comp_map = {c.ref: c for c in pcb.components if not _is_power_symbol(c.ref)}

    for net_name in graph:
        if _net_is_power(net_name) and not _net_is_gnd(net_name):
            tree.rails[net_name] = PowerRail(net_name=net_name)

    regulator_refs = set()

    for ref, comp in comp_map.items():
        if _is_known_regulator(comp):
            regulator_refs.add(ref)
            continue
        if any(comp.ref.upper().startswith(p) for p in ("U", "IC")):
            input_nets, output_nets = _infer_regulator_roles(comp, graph)
            input_nets  = list(dict.fromkeys(input_nets))
            output_nets = list(dict.fromkeys(output_nets))
            if len(input_nets) == 1 and len(output_nets) == 1:
                regulator_refs.add(ref)

    for ref, comp in comp_map.items():
        is_reg  = ref in regulator_refs
        is_src  = _is_power_source(comp)
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

        if is_load and not is_reg:
            for pad in comp.pads:
                if pad.net and pad.net in tree.rails:
                    tree.rails[pad.net].loads.add(ref)

    return tree


def _findings_unused_outputs(tree) -> list:
    findings = []
    for ref, output_nets in tree.regulator_outputs.items():
        for net in output_nets:
            if net not in tree.rails:
                continue
            if not [l for l in tree.rails[net].loads if l != ref]:
                findings.append(Finding(
                    severity      = "WARNING",
                    category      = CAT,
                    message       = (
                        f"Regulator {ref} output net '{net}' has no downstream "
                        f"loads — possibly unused or unrouted"
                    ),
                    related_refs  = (ref,),
                    affected_nets = (net,),
                ))
    return findings


def _findings_rail_contention(tree) -> list:
    findings = []
    for net_name, rail in tree.rails.items():
        reg_sources = [s for s in rail.sources if s in tree.regulator_outputs]
        if len(reg_sources) > 1:
            findings.append(Finding(
                severity      = "CRITICAL",
                category      = CAT,
                message       = (
                    f"Net '{net_name}' driven by multiple regulators "
                    f"({', '.join(sorted(reg_sources))}) — contention risk"
                ),
                related_refs  = tuple(sorted(reg_sources)),
                affected_nets = (net_name,),
            ))
    return findings


def _findings_sourceless_rails(tree) -> list:
    findings = []
    for net_name, rail in tree.rails.items():
        if rail.loads and not rail.sources:
            findings.append(Finding(
                severity      = "WARNING",
                category      = CAT,
                message       = (
                    f"Power net '{net_name}' has {len(rail.loads)} load(s) "
                    f"but no source — floating supply rail"
                ),
                affected_nets = (net_name,),
            ))
    return findings


def _findings_load_advisory(tree) -> list:
    findings = []
    for net_name, rail in tree.rails.items():
        if len(rail.loads) > LOAD_COUNT_ADVISORY:
            findings.append(Finding(
                severity      = "INFO",
                category      = CAT,
                message       = (
                    f"Power net '{net_name}' has {len(rail.loads)} loads — "
                    f"verify current budget"
                ),
                affected_nets = (net_name,),
            ))
    return findings


def _findings_empty_rails(tree) -> list:
    findings = []
    for net_name, rail in tree.rails.items():
        if not rail.sources and not rail.loads:
            findings.append(Finding(
                severity      = "INFO",
                category      = CAT,
                message       = (
                    f"Power net '{net_name}' exists but has no sources or loads"
                ),
                affected_nets = (net_name,),
            ))
    return findings


def analyse_power_tree(pcb, graph):
    tree     = build_power_tree(pcb, graph)
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

    deduped.sort(key=lambda f: (
        0 if f.severity == "CRITICAL" else
        1 if f.severity == "WARNING" else 2
    ))
    tree.findings = deduped
    return tree, deduped


def format_power_tree_report(tree):
    if not tree.rails:
        return "  (no power rails detected)"
    lines = []
    for net_name in sorted(tree.rails):
        rail     = tree.rails[net_name]
        src_str  = ", ".join(sorted(rail.sources)) if rail.sources else "⚠ no source"
        load_str = ", ".join(sorted(rail.loads))   if rail.loads   else "none"
        lines.append(f"  {net_name:20s}  source: {src_str:25s}  loads: {load_str}")
    return "\n".join(lines)