"""
Grounding Strategy Checks for FluxDiff.

Analyses the PCB connectivity graph and component placement to detect:

  1. GND islands — multiple disconnected ground nets that should be unified.
  2. Analog/digital ground mixing — analog components sharing a ground net
     with high-frequency digital ICs without a star-point or ferrite bead
     separation.
  3. ADC without local ground reference.

All findings are deterministic strings for set-diffing in diff_engine.py.

B5 FIX: _check_adc_ground_proximity previously compared comp.x/comp.y
(component centroid) for the ADC against pad.x/pad.y for GND pads, mixing
two different coordinate frames. For large ADC packages (TQFP-48, QFN-32)
the centroid is several mm from the AGND/DGND pins, so nearby GND
connections were missed and false WARNINGs were emitted. The fix uses the
ADC's own GND pad position (or centroid fallback) as the measurement origin,
consistent with how GND pad positions are collected.
"""

from math import hypot
from collections import defaultdict

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

GND_NET_SUBSTRINGS    = ("GND", "AGND", "DGND", "PGND", "EARTH", "VSS")
ANALOG_GND_SUBSTRINGS = ("AGND",)
DIGITAL_GND_SUBSTRINGS = ("DGND", "PGND")

ANALOG_REF_PREFIXES = ("U",)
ANALOG_VALUE_SUBSTRINGS = (
    "OPA", "LM358", "LM741", "TL07", "TL08", "MCP60",
    "AD822", "INA", "ADA", "OP07", "OP27",
)

ADC_VALUE_SUBSTRINGS = (
    "ADC", "ADS", "MCP330", "MCP320", "MCP300",
    "AD7", "AD9", "ADS1", "ADS8", "MAX11",
)

DIGITAL_REF_PREFIXES = ("U", "IC")
DIGITAL_VALUE_SUBSTRINGS = (
    "MCU", "STM32", "ESP", "ATMEGA", "PIC",
    "FPGA", "CPLD", "FIFO", "74HC", "74LS",
)

FERRITE_VALUE_SUBSTRINGS = ("FERRITE", "FB", "CMC", "BEAD")
FERRITE_REF_PREFIXES = ("FB", "L", "Z")

ADC_GND_PROXIMITY_MM = 10.0

POWER_SYMBOL_PREFIXES = ("#PWR", "#FLG")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_power_symbol(ref: str) -> bool:
    return any(ref.upper().startswith(p) for p in POWER_SYMBOL_PREFIXES)


def _net_is_gnd(net_name: str) -> bool:
    upper = net_name.upper()
    return any(sub in upper for sub in GND_NET_SUBSTRINGS)


def _net_is_analog_gnd(net_name: str) -> bool:
    upper = net_name.upper()
    return any(sub in upper for sub in ANALOG_GND_SUBSTRINGS)


def _net_is_digital_gnd(net_name: str) -> bool:
    upper = net_name.upper()
    return any(sub in upper for sub in DIGITAL_GND_SUBSTRINGS)


def _is_analog_ic(comp) -> bool:
    val_upper = comp.value.upper()
    return any(s in val_upper for s in ANALOG_VALUE_SUBSTRINGS)


def _is_adc(comp) -> bool:
    val_upper = comp.value.upper()
    ref_upper = comp.ref.upper()
    return (
        any(s in val_upper for s in ADC_VALUE_SUBSTRINGS) or
        any(s in ref_upper for s in ("ADC",))
    )


def _is_digital_ic(comp) -> bool:
    val_upper = comp.value.upper()
    return any(s in val_upper for s in DIGITAL_VALUE_SUBSTRINGS)


def _is_ferrite(comp) -> bool:
    val_upper = comp.value.upper()
    ref_upper = comp.ref.upper()
    return (
        any(s in val_upper for s in FERRITE_VALUE_SUBSTRINGS) or
        any(ref_upper.startswith(p) for p in FERRITE_REF_PREFIXES)
    )


def _distance(ax, ay, bx, by) -> float:
    return hypot(ax - bx, ay - by)


def _gnd_pad_position(comp):
    """
    Return (x, y) of the first GND-connected pad with an explicit position,
    or the component centroid as fallback. Used as the reference point when
    measuring ground proximity.
    """
    for pad in comp.pads:
        if pad.net and _net_is_gnd(pad.net) and pad.has_explicit_position:
            return pad.x, pad.y
    return comp.x, comp.y


# ---------------------------------------------------------------------------
# Check 1 — GND islands
# ---------------------------------------------------------------------------

def _check_gnd_islands(graph: dict, pcb) -> list:
    """
    Detect multiple distinct ground nets with no bridging component between them.

    F2 FIX: Bridge type now determines severity:
      - ferrite/bead → INFO (intentional star-point, acceptable)
      - any other component → WARNING (unverified DC bridge between grounds)
    """
    msgs = []
    gnd_nets = sorted(net for net in graph if _net_is_gnd(net))

    if len(gnd_nets) <= 1:
        return msgs

    comp_map = {c.ref: c for c in pcb.components if not _is_power_symbol(c.ref)}
    bridges = []

    for ref, comp in comp_map.items():
        pad_nets = {pad.net for pad in comp.pads if pad.net and _net_is_gnd(pad.net)}
        if len(pad_nets) >= 2:
            net_list = sorted(pad_nets)
            bridges.append((ref, net_list[0], net_list[1]))

    if not bridges:
        nets_str = ", ".join(f"'{n}'" for n in gnd_nets)
        msgs.append(
            f"CRITICAL: Multiple ground nets detected ({nets_str}) with no "
            f"bridging component — unintentional GND islands may cause "
            f"ground loops or floating references"
        )
    else:
        for ref, net_a, net_b in bridges:
            comp = comp_map.get(ref)
            if comp and _is_ferrite(comp):
                msgs.append(
                    f"INFO: Ground nets '{net_a}' and '{net_b}' are bridged by "
                    f"{ref} (ferrite/bead) — verify star-point is intentional"
                )
            else:
                comp_type = comp.value if comp else "unknown"
                msgs.append(
                    f"WARNING: Ground nets '{net_a}' and '{net_b}' are bridged by "
                    f"{ref} ({comp_type}) — non-ferrite bridge creates DC path "
                    f"between grounds; verify this is intentional"
                )

    return msgs


# ---------------------------------------------------------------------------
# Check 2 — Analog/digital ground mixing
# ---------------------------------------------------------------------------

def _check_analog_digital_mix(graph: dict, pcb) -> list:
    """
    Flag the case where analog ICs and digital ICs share the same ground net.

    F10 FIX: Removed the `and not bridge_refs` suppression condition.
    A ferrite bead present on the same ground net does NOT electrically
    decouple the analog and digital ICs — it only helps when the ICs are on
    *different* nets joined by the ferrite.
    """
    msgs = []
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
            msgs.append(
                f"WARNING: Ground net '{gnd_net}' mixes analog ICs "
                f"({', '.join(analog_refs)}) and digital ICs "
                f"({', '.join(digital_refs)}) without isolation — "
                f"route analog components to a separate AGND net joined by a "
                f"ferrite bead to reduce noise coupling"
            )

    return msgs


# ---------------------------------------------------------------------------
# Check 3 — ADC without local ground reference
# ---------------------------------------------------------------------------

def _check_adc_ground_proximity(graph: dict, pcb) -> list:
    """
    For each ADC IC, verify that at least one ground-referenced component
    exists within ADC_GND_PROXIMITY_MM of the ADC's GND pin.

    B5 FIX: The measurement origin for the ADC is now its own GND pad
    position (via _gnd_pad_position), not comp.x/comp.y. Previously the
    code mixed component centroids (for the ADC) with pad positions (for
    everything in gnd_positions), producing inconsistent distances for any
    large-footprint ADC. Both sides of the comparison now use pad positions
    with a centroid fallback when no explicit pad position is available.
    """
    msgs = []
    comp_map = {c.ref: c for c in pcb.components if not _is_power_symbol(c.ref)}

    # Collect all GND-connected positions: pad positions preferred, via positions included
    gnd_positions = []

    for ref, comp in comp_map.items():
        for pad in comp.pads:
            if pad.net and _net_is_gnd(pad.net):
                if pad.has_explicit_position:
                    gnd_positions.append((pad.x, pad.y))
                else:
                    gnd_positions.append((comp.x, comp.y))
                break  # one position per component is sufficient

    for via in pcb.vias:
        if via.net and _net_is_gnd(via.net):
            gnd_positions.append((via.x, via.y))

    for ref, comp in comp_map.items():
        if not _is_adc(comp):
            continue

        has_gnd_pin = any(
            pad.net and _net_is_gnd(pad.net)
            for pad in comp.pads
        )
        if not has_gnd_pin:
            continue  # ERC already flags this

        # B5 FIX: use the ADC's own GND pad position as the measurement origin
        adc_gnd_x, adc_gnd_y = _gnd_pad_position(comp)

        nearby_gnd = any(
            _distance(adc_gnd_x, adc_gnd_y, gx, gy) <= ADC_GND_PROXIMITY_MM
            for gx, gy in gnd_positions
            if not (gx == adc_gnd_x and gy == adc_gnd_y)
        )

        if not nearby_gnd:
            msgs.append(
                f"WARNING: ADC {ref} has no ground reference within "
                f"{ADC_GND_PROXIMITY_MM} mm — poor ground return path "
                f"will degrade conversion accuracy"
            )

    return msgs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_grounding(pcb, graph: dict) -> list:
    """
    Run all grounding strategy checks.

    Returns:
        list[str] — deterministic finding strings, CRITICAL before WARNING before INFO.
    """
    findings = []
    findings.extend(_check_gnd_islands(graph, pcb))
    findings.extend(_check_analog_digital_mix(graph, pcb))
    findings.extend(_check_adc_ground_proximity(graph, pcb))

    seen = set()
    deduped = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            deduped.append(f)

    deduped.sort(key=lambda m: (
        0 if m.startswith("CRITICAL") else
        1 if m.startswith("WARNING") else
        2
    ))

    return deduped