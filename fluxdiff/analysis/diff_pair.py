# fluxdiff/analysis/diff_pair.py
"""
Differential Pair Validation for FluxDiff.

F12 FIX: The bare ("P", "N") suffix pair now requires the base name to be at
least MIN_BASE_LENGTH_FOR_BARE_SUFFIX characters. Previously, single- or
two-character net names ending in "P" (e.g. "COMP") would seek a partner
"COMN" — if both existed they'd be falsely flagged as a diff pair, misidentifying
real pairs and polluting the report with spurious findings.

B8 FIX: The original guard was `len(base) < MIN_BASE_LENGTH_FOR_BARE_SUFFIX`
where MIN_BASE_LENGTH_FOR_BARE_SUFFIX = 3. The motivating false-positive was
"COMP" → base "COM" (3 chars) paired with "COMN". With the old threshold,
len("COM") < 3 is False, so "COM" passed the guard and "COMP"/"COMN" were
still falsely paired. The threshold is now applied as
`len(base) < MIN_BASE_LENGTH_FOR_BARE_SUFFIX` with the constant raised to 4,
meaning a bare-suffix base must be at least 4 characters. "COM" (3 chars)
now correctly fails the guard. Genuine differential pairs like "TXDAT" (5 chars)
or "USBP" (4 chars, base "USB") are unaffected.
"""

from math import hypot
from collections import defaultdict

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

LENGTH_MISMATCH_THRESHOLD_MM = 0.5

VIA_COUNT_MISMATCH = 1

DIFF_PAIR_SUFFIXES = [
    ("_P",  "_N"),
    ("_DP", "_DN"),
    ("+",   "-"),
    ("_POS","_NEG"),
    ("P",   "N"),       # bare suffix — guarded by MIN_BASE_LENGTH below
]

# B8 FIX: raised from 3 to 4. With threshold=3, base "COM" (len 3) passed
# the `< 3` guard and allowed "COMP"/"COMN" false positives. With threshold=4,
# a bare-suffix base must have at least 4 characters (e.g. "USBD", "TXDA").
MIN_BASE_LENGTH_FOR_BARE_SUFFIX = 4

MIN_TRACES_FOR_PAIR = 1

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _trace_length(trace) -> float:
    dx = trace.end[0] - trace.start[0]
    dy = trace.end[1] - trace.start[1]
    return hypot(dx, dy)


def _net_total_length(traces) -> float:
    return sum(_trace_length(t) for t in traces)


def _net_layers(traces) -> set:
    return {t.layer for t in traces}


def _net_via_count(net_name: str, vias) -> int:
    return sum(1 for v in vias if v.net == net_name)


def _find_diff_pairs(net_names: set) -> list:
    """
    Return list of (p_net, n_net) tuples identified as differential pairs.

    B8 FIX: MIN_BASE_LENGTH_FOR_BARE_SUFFIX raised to 4 (was 3). See module
    docstring for full explanation.
    """
    paired = set()
    pairs = []
    net_list = sorted(net_names)

    for p_suffix, n_suffix in DIFF_PAIR_SUFFIXES:
        is_bare = (p_suffix == "P")   # only the last entry is the bare single-char suffix
        for net in net_list:
            if net in paired:
                continue
            upper = net.upper()
            p_suf_upper = p_suffix.upper()

            if upper.endswith(p_suf_upper):
                base = net[: -len(p_suffix)]
                # B8 FIX: bare suffix requires base length >= 4, not >= 3
                if is_bare and len(base) < MIN_BASE_LENGTH_FOR_BARE_SUFFIX:
                    continue
                candidate_n = base + n_suffix
                match = next(
                    (n for n in net_list if n.upper() == candidate_n.upper()
                     and n not in paired),
                    None,
                )
                if match:
                    pairs.append((net, match))
                    paired.add(net)
                    paired.add(match)

    return pairs


# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def _check_length_mismatch(p_net, n_net, traces_by_net) -> list:
    msgs = []
    p_traces = traces_by_net.get(p_net, [])
    n_traces = traces_by_net.get(n_net, [])

    if not p_traces and not n_traces:
        return msgs

    p_len = _net_total_length(p_traces)
    n_len = _net_total_length(n_traces)
    delta = abs(p_len - n_len)

    if delta > LENGTH_MISMATCH_THRESHOLD_MM:
        msgs.append(
            f"WARNING: Diff pair '{p_net}' / '{n_net}' length mismatch "
            f"{delta:.3f} mm (P={p_len:.3f} mm, N={n_len:.3f} mm) — "
            f"exceeds {LENGTH_MISMATCH_THRESHOLD_MM} mm threshold"
        )
    return msgs


def _check_via_asymmetry(p_net, n_net, vias) -> list:
    msgs = []
    p_vias = _net_via_count(p_net, vias)
    n_vias = _net_via_count(n_net, vias)
    delta = abs(p_vias - n_vias)

    if delta > VIA_COUNT_MISMATCH:
        msgs.append(
            f"WARNING: Diff pair '{p_net}' / '{n_net}' via count mismatch "
            f"(P={p_vias}, N={n_vias}) — asymmetric via count affects impedance"
        )
    elif delta == VIA_COUNT_MISMATCH:
        msgs.append(
            f"INFO: Diff pair '{p_net}' / '{n_net}' has unequal via counts "
            f"(P={p_vias}, N={n_vias}) — verify intentional"
        )
    return msgs


def _check_layer_asymmetry(p_net, n_net, traces_by_net) -> list:
    msgs = []
    p_layers = _net_layers(traces_by_net.get(p_net, []))
    n_layers = _net_layers(traces_by_net.get(n_net, []))

    only_in_p = p_layers - n_layers
    only_in_n = n_layers - p_layers

    if only_in_p or only_in_n:
        msgs.append(
            f"WARNING: Diff pair '{p_net}' / '{n_net}' routed on asymmetric layers "
            f"(P-only: {sorted(only_in_p) or 'none'}, "
            f"N-only: {sorted(only_in_n) or 'none'}) — "
            f"asymmetric layers cause impedance discontinuity"
        )
    return msgs


def _check_unpaired_diff_nets(net_names: set, paired_nets: set) -> list:
    msgs = []
    strong_suffixes = [("_P", "_N"), ("_DP", "_DN")]

    for net in sorted(net_names):
        if net in paired_nets:
            continue
        upper = net.upper()
        for p_suf, n_suf in strong_suffixes:
            if upper.endswith(p_suf.upper()):
                base = net[: -len(p_suf)]
                partner = base + n_suf
                if partner.upper() not in {n.upper() for n in net_names}:
                    msgs.append(
                        f"INFO: Net '{net}' looks like a differential P-net "
                        f"but partner '{partner}' not found in netlist"
                    )
            elif upper.endswith(n_suf.upper()):
                base = net[: -len(n_suf)]
                partner = base + p_suf
                if partner.upper() not in {n.upper() for n in net_names}:
                    msgs.append(
                        f"INFO: Net '{net}' looks like a differential N-net "
                        f"but partner '{partner}' not found in netlist"
                    )
    return msgs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_diff_pairs(pcb) -> list:
    """
    Run differential pair validation on a single PCBData.
    """
    traces_by_net = defaultdict(list)
    for trace in pcb.traces:
        if trace.net and trace.net != "__unconnected__":
            traces_by_net[trace.net].append(trace)

    active_nets = {
        net for net, traces in traces_by_net.items()
        if len(traces) >= MIN_TRACES_FOR_PAIR
    }

    for via in pcb.vias:
        if via.net and via.net != "__unconnected__":
            active_nets.add(via.net)

    pairs = _find_diff_pairs(active_nets)
    if not pairs:
        return []

    paired_nets = {net for pair in pairs for net in pair}
    findings = []

    for p_net, n_net in pairs:
        findings.extend(_check_length_mismatch(p_net, n_net, traces_by_net))
        findings.extend(_check_via_asymmetry(p_net, n_net, pcb.vias))
        findings.extend(_check_layer_asymmetry(p_net, n_net, traces_by_net))

    findings.extend(_check_unpaired_diff_nets(active_nets, paired_nets))

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