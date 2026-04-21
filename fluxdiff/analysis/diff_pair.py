from math import hypot
from collections import defaultdict
from fluxdiff.models.pcb_models import Finding

LENGTH_MISMATCH_THRESHOLD_MM   = 0.5
VIA_COUNT_MISMATCH             = 1
MIN_BASE_LENGTH_FOR_BARE_SUFFIX = 4
MIN_TRACES_FOR_PAIR            = 1
CAT = "DIFF_PAIR"

DIFF_PAIR_SUFFIXES = [
    ("_P",   "_N"),
    ("_DP",  "_DN"),
    ("+",    "-"),
    ("_POS", "_NEG"),
    ("P",    "N"),
]


def _trace_length(trace):
    return hypot(trace.end[0] - trace.start[0], trace.end[1] - trace.start[1])

def _net_total_length(traces):
    return sum(_trace_length(t) for t in traces)

def _net_layers(traces):
    return {t.layer for t in traces}

def _net_via_count(net_name, vias):
    return sum(1 for v in vias if v.net == net_name)


def _find_diff_pairs(net_names):
    paired, pairs = set(), []
    net_list = sorted(net_names)
    for p_suffix, n_suffix in DIFF_PAIR_SUFFIXES:
        is_bare = (p_suffix == "P")
        for net in net_list:
            if net in paired:
                continue
            upper       = net.upper()
            p_suf_upper = p_suffix.upper()
            if upper.endswith(p_suf_upper):
                base = net[: -len(p_suffix)]
                if is_bare and len(base) < MIN_BASE_LENGTH_FOR_BARE_SUFFIX:
                    continue
                candidate_n = base + n_suffix
                match = next(
                    (n for n in net_list
                     if n.upper() == candidate_n.upper() and n not in paired),
                    None,
                )
                if match:
                    pairs.append((net, match))
                    paired.add(net)
                    paired.add(match)
    return pairs


def _check_length_mismatch(p_net, n_net, traces_by_net) -> list:
    findings = []
    p_traces = traces_by_net.get(p_net, [])
    n_traces = traces_by_net.get(n_net, [])
    if not p_traces and not n_traces:
        return findings
    p_len = _net_total_length(p_traces)
    n_len = _net_total_length(n_traces)
    delta = abs(p_len - n_len)
    if delta > LENGTH_MISMATCH_THRESHOLD_MM:
        findings.append(Finding(
            severity      = "WARNING",
            category      = CAT,
            message       = (
                f"Diff pair '{p_net}' / '{n_net}' length mismatch "
                f"{delta:.3f} mm (P={p_len:.3f} mm, N={n_len:.3f} mm) — "
                f"exceeds {LENGTH_MISMATCH_THRESHOLD_MM} mm threshold"
            ),
            affected_nets = (p_net, n_net),
        ))
    return findings


def _check_via_asymmetry(p_net, n_net, vias) -> list:
    findings = []
    p_vias = _net_via_count(p_net, vias)
    n_vias = _net_via_count(n_net, vias)
    delta  = abs(p_vias - n_vias)
    if delta > VIA_COUNT_MISMATCH:
        findings.append(Finding(
            severity      = "WARNING",
            category      = CAT,
            message       = (
                f"Diff pair '{p_net}' / '{n_net}' via count mismatch "
                f"(P={p_vias}, N={n_vias}) — asymmetric via count affects impedance"
            ),
            affected_nets = (p_net, n_net),
        ))
    elif delta == VIA_COUNT_MISMATCH:
        findings.append(Finding(
            severity      = "INFO",
            category      = CAT,
            message       = (
                f"Diff pair '{p_net}' / '{n_net}' has unequal via counts "
                f"(P={p_vias}, N={n_vias}) — verify intentional"
            ),
            affected_nets = (p_net, n_net),
        ))
    return findings


def _check_layer_asymmetry(p_net, n_net, traces_by_net) -> list:
    findings  = []
    p_layers  = _net_layers(traces_by_net.get(p_net, []))
    n_layers  = _net_layers(traces_by_net.get(n_net, []))
    only_in_p = p_layers - n_layers
    only_in_n = n_layers - p_layers
    if only_in_p or only_in_n:
        findings.append(Finding(
            severity      = "WARNING",
            category      = CAT,
            message       = (
                f"Diff pair '{p_net}' / '{n_net}' routed on asymmetric layers "
                f"(P-only: {sorted(only_in_p) or 'none'}, "
                f"N-only: {sorted(only_in_n) or 'none'}) — "
                f"asymmetric layers cause impedance discontinuity"
            ),
            affected_nets = (p_net, n_net),
        ))
    return findings


def _check_unpaired_diff_nets(net_names, paired_nets) -> list:
    findings       = []
    strong_suffixes = [("_P", "_N"), ("_DP", "_DN")]
    for net in sorted(net_names):
        if net in paired_nets:
            continue
        upper = net.upper()
        for p_suf, n_suf in strong_suffixes:
            if upper.endswith(p_suf.upper()):
                base    = net[: -len(p_suf)]
                partner = base + n_suf
                if partner.upper() not in {n.upper() for n in net_names}:
                    findings.append(Finding(
                        severity      = "INFO",
                        category      = CAT,
                        message       = (
                            f"Net '{net}' looks like a differential P-net "
                            f"but partner '{partner}' not found in netlist"
                        ),
                        affected_nets = (net,),
                    ))
            elif upper.endswith(n_suf.upper()):
                base    = net[: -len(n_suf)]
                partner = base + p_suf
                if partner.upper() not in {n.upper() for n in net_names}:
                    findings.append(Finding(
                        severity      = "INFO",
                        category      = CAT,
                        message       = (
                            f"Net '{net}' looks like a differential N-net "
                            f"but partner '{partner}' not found in netlist"
                        ),
                        affected_nets = (net,),
                    ))
    return findings


def analyse_diff_pairs(pcb) -> list:
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
    findings    = []

    for p_net, n_net in pairs:
        findings.extend(_check_length_mismatch(p_net, n_net, traces_by_net))
        findings.extend(_check_via_asymmetry(p_net, n_net, pcb.vias))
        findings.extend(_check_layer_asymmetry(p_net, n_net, traces_by_net))

    findings.extend(_check_unpaired_diff_nets(active_nets, paired_nets))

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