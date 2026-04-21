from math import hypot
from collections import defaultdict
from fluxdiff.models.pcb_models import Finding

GND_NET_SUBSTRINGS     = ("GND", "AGND", "DGND", "PGND", "EARTH", "VSS")
ANALOG_GND_SUBSTRINGS  = ("AGND",)
DIGITAL_GND_SUBSTRINGS = ("DGND", "PGND")

ANALOG_VALUE_SUBSTRINGS = (
    "OPA", "LM358", "LM741", "TL07", "TL08", "MCP60",
    "AD822", "INA", "ADA", "OP07", "OP27",
)
ADC_VALUE_SUBSTRINGS = (
    "ADC", "ADS", "MCP330", "MCP320", "MCP300",
    "AD7", "AD9", "ADS1", "ADS8", "MAX11",
)
DIGITAL_VALUE_SUBSTRINGS = (
    "MCU", "STM32", "ESP", "ATMEGA", "PIC",
    "FPGA", "CPLD", "FIFO", "74HC", "74LS",
)
FERRITE_VALUE_SUBSTRINGS = ("FERRITE", "FB", "CMC", "BEAD")
FERRITE_REF_PREFIXES     = ("FB", "L", "Z")
POWER_SYMBOL_PREFIXES    = ("#PWR", "#FLG")

ADC_GND_PROXIMITY_MM = 10.0
CAT = "GROUND"


def _is_power_symbol(ref):
    return any(ref.upper().startswith(p) for p in POWER_SYMBOL_PREFIXES)

def _net_is_gnd(net_name):
    upper = net_name.upper()
    return any(sub in upper for sub in GND_NET_SUBSTRINGS)

def _net_is_analog_gnd(net_name):
    return any(sub in net_name.upper() for sub in ANALOG_GND_SUBSTRINGS)

def _net_is_digital_gnd(net_name):
    return any(sub in net_name.upper() for sub in DIGITAL_GND_SUBSTRINGS)

def _is_analog_ic(comp):
    return any(s in comp.value.upper() for s in ANALOG_VALUE_SUBSTRINGS)

def _is_adc(comp):
    return (
        any(s in comp.value.upper() for s in ADC_VALUE_SUBSTRINGS) or
        "ADC" in comp.ref.upper()
    )

def _is_digital_ic(comp):
    return any(s in comp.value.upper() for s in DIGITAL_VALUE_SUBSTRINGS)

def _is_ferrite(comp):
    return (
        any(s in comp.value.upper() for s in FERRITE_VALUE_SUBSTRINGS) or
        any(comp.ref.upper().startswith(p) for p in FERRITE_REF_PREFIXES)
    )

def _distance(ax, ay, bx, by):
    return hypot(ax - bx, ay - by)

def _gnd_pad_position(comp):
    for pad in comp.pads:
        if pad.net and _net_is_gnd(pad.net) and pad.has_explicit_position:
            return pad.x, pad.y
    return comp.x, comp.y


def _check_gnd_islands(graph, pcb) -> list:
    findings = []
    gnd_nets = sorted(net for net in graph if _net_is_gnd(net))
    if len(gnd_nets) <= 1:
        return findings

    comp_map = {c.ref: c for c in pcb.components if not _is_power_symbol(c.ref)}
    bridges  = []

    for ref, comp in comp_map.items():
        pad_nets = {pad.net for pad in comp.pads if pad.net and _net_is_gnd(pad.net)}
        if len(pad_nets) >= 2:
            net_list = sorted(pad_nets)
            bridges.append((ref, net_list[0], net_list[1]))

    if not bridges:
        nets_str = ", ".join(f"'{n}'" for n in gnd_nets)
        findings.append(Finding(
            severity = "CRITICAL",
            category = CAT,
            message  = (
                f"Multiple ground nets detected ({nets_str}) with no "
                f"bridging component — unintentional GND islands may cause "
                f"ground loops or floating references"
            ),
        ))
    else:
        for ref, net_a, net_b in bridges:
            comp = comp_map.get(ref)
            if comp and _is_ferrite(comp):
                findings.append(Finding(
                    severity     = "INFO",
                    category     = CAT,
                    message      = (
                        f"Ground nets '{net_a}' and '{net_b}' are bridged by "
                        f"{ref} (ferrite/bead) — verify star-point is intentional"
                    ),
                    related_refs = (ref,),
                ))
            else:
                comp_type = comp.value if comp else "unknown"
                findings.append(Finding(
                    severity     = "WARNING",
                    category     = CAT,
                    message      = (
                        f"Ground nets '{net_a}' and '{net_b}' are bridged by "
                        f"{ref} ({comp_type}) — non-ferrite bridge creates DC path "
                        f"between grounds; verify this is intentional"
                    ),
                    related_refs = (ref,),
                ))
    return findings


def _check_analog_digital_mix(graph, pcb) -> list:
    findings = []
    comp_map = {c.ref: c for c in pcb.components if not _is_power_symbol(c.ref)}
    gnd_net_occupants = defaultdict(list)

    for ref, comp in comp_map.items():
        gnd_nets_for_comp = {
            pad.net for pad in comp.pads
            if pad.net and _net_is_gnd(pad.net)
        }
        for net in gnd_nets_for_comp:
            if _is_ferrite(comp):
                gnd_net_occupants[net].append((ref, "bridge"))
            elif _is_analog_ic(comp):
                gnd_net_occupants[net].append((ref, "analog"))
            elif _is_digital_ic(comp):
                gnd_net_occupants[net].append((ref, "digital"))

    for gnd_net, occupants in gnd_net_occupants.items():
        analog_refs  = sorted(r for r, t in occupants if t == "analog")
        digital_refs = sorted(r for r, t in occupants if t == "digital")
        if analog_refs and digital_refs:
            findings.append(Finding(
                severity     = "WARNING",
                category     = CAT,
                message      = (
                    f"Ground net '{gnd_net}' mixes analog ICs "
                    f"({', '.join(analog_refs)}) and digital ICs "
                    f"({', '.join(digital_refs)}) without isolation — "
                    f"route analog components to a separate AGND net joined "
                    f"by a ferrite bead to reduce noise coupling"
                ),
                related_refs  = tuple(sorted(analog_refs + digital_refs)),
                affected_nets = (gnd_net,),
            ))
    return findings


def _check_adc_ground_proximity(graph, pcb) -> list:
    findings = []
    comp_map = {c.ref: c for c in pcb.components if not _is_power_symbol(c.ref)}

    gnd_positions = []
    for ref, comp in comp_map.items():
        for pad in comp.pads:
            if pad.net and _net_is_gnd(pad.net):
                if pad.has_explicit_position:
                    gnd_positions.append((pad.x, pad.y))
                else:
                    gnd_positions.append((comp.x, comp.y))
                break

    for via in pcb.vias:
        if via.net and _net_is_gnd(via.net):
            gnd_positions.append((via.x, via.y))

    for ref, comp in comp_map.items():
        if not _is_adc(comp):
            continue
        has_gnd_pin = any(pad.net and _net_is_gnd(pad.net) for pad in comp.pads)
        if not has_gnd_pin:
            continue

        adc_gnd_x, adc_gnd_y = _gnd_pad_position(comp)
        nearby_gnd = any(
            _distance(adc_gnd_x, adc_gnd_y, gx, gy) <= ADC_GND_PROXIMITY_MM
            for gx, gy in gnd_positions
            if not (gx == adc_gnd_x and gy == adc_gnd_y)
        )
        if not nearby_gnd:
            findings.append(Finding(
                severity     = "WARNING",
                category     = CAT,
                message      = (
                    f"ADC {ref} has no ground reference within "
                    f"{ADC_GND_PROXIMITY_MM} mm — poor ground return path "
                    f"will degrade conversion accuracy"
                ),
                related_refs = (ref,),
                coordinates  = (adc_gnd_x, adc_gnd_y),
                highlight_refs = (ref,),
            ))
    return findings


def analyse_grounding(pcb, graph) -> list:
    findings = []
    findings.extend(_check_gnd_islands(graph, pcb))
    findings.extend(_check_analog_digital_mix(graph, pcb))
    findings.extend(_check_adc_ground_proximity(graph, pcb))

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