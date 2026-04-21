# fluxdiff/analysis/connectivity_graph.py

_POWER_SYMBOL_PREFIXES = ("#PWR", "#FLG")


def build_connectivity_graph(pcb) -> dict:
    """
    Build net → set of (ref, pad) connections from components, traces, and vias.

    F6 FIX: Virtual power symbols (#PWR…, #FLG…) are excluded at build time
    to prevent spurious "gained/lost connection" messages when KiCad annotation
    adds or removes a #PWR flag.

    NOTE: VIA pseudo-connections are still added (they contribute to power-tree
    and ERC checks) but compare_connectivity strips them before diffing so that
    via moves are not double-reported — routing_diff already covers via moves.

    B3 FIX: Enriched trace endpoints (start_ref/end_ref) are no longer added
    to the graph. Section 1 (component pads) already adds every pad→net
    connection; section 2 was re-adding the exact same (ref, pad_number) tuples
    for every trace endpoint that snapped to a pad. Because graph[net] is a set
    the data was not corrupted, but when enrichment succeeded on one board and
    failed on the other (different trace counts, different snap results) the set
    difference produced spurious "gained/lost connection" messages in
    compare_connectivity. The trace section is now removed entirely — pad
    connections are owned exclusively by the component-pad loop, and trace
    routing is covered by routing_diff.
    """
    graph = {}

    def add(net, ref, pad):
        if not net or not ref:
            return
        ref_upper = ref.upper()
        if any(ref_upper.startswith(p) for p in _POWER_SYMBOL_PREFIXES):
            return
        graph.setdefault(net, set()).add((ref, pad))

    # 1. Pads — sole source of component-level connectivity.
    #    Trace endpoints are intentionally excluded (see B3 FIX above).
    for comp in pcb.components:
        for pad in comp.pads:
            add(pad.net, comp.ref, pad.number)

    # 2. Vias
    for via in pcb.vias:
        if via.net:
            graph.setdefault(via.net, set()).add(("VIA", f"{via.x:.2f},{via.y:.2f}"))

    return graph


def compare_connectivity(old_graph, new_graph):
    """
    Return human-readable messages for connectivity changes between two graphs.
    Note: messages do NOT include a 'CONNECTIVITY:' prefix — the caller adds it.

    FIX (double-reporting): VIA entries are stripped before diffing. Via moves
    are already reported by routing_diff as "Via removed / Via added". Without
    this strip, every via move produced four output lines:
      - routing_diff: "Via removed … / Via added …"
      - connectivity:  "Net X lost ('VIA','10.00,20.00') / gained ('VIA','10.50,20.00')"
    """
    changes = []

    all_nets = set(old_graph) | set(new_graph)

    for net in sorted(all_nets):
        # Strip VIA pseudo-connections — routing_diff owns via-move reporting
        old = {c for c in old_graph.get(net, set()) if c[0] != "VIA"}
        new = {c for c in new_graph.get(net, set()) if c[0] != "VIA"}

        lost = old - new
        gained = new - old

        for conn in sorted(lost):
            changes.append(f"Net {net} lost connection {conn}")

        for conn in sorted(gained):
            changes.append(f"Net {net} gained connection {conn}")

    return changes