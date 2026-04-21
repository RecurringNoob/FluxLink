from fluxdiff.models.pcb_models import PCBData, DiffResult, Finding
import math
import os
from fluxdiff.analysis.connectivity_graph import build_connectivity_graph, compare_connectivity
from fluxdiff.analysis.erc_checker import run_erc_checks
from fluxdiff.analysis.trace_connectivity import enrich_traces_with_connectivity
from fluxdiff.analysis.power_tree import analyse_power_tree
from fluxdiff.analysis.diff_pair import analyse_diff_pairs
from fluxdiff.analysis.ground_checker import analyse_grounding
from fluxdiff.analysis.impedance import analyse_impedance
from fluxdiff.supply_chain.bom_checker import analyse_supply_chain

MOVE_THRESHOLD = 0.05
ROT_THRESHOLD  = 1.0
TRACE_ROUND    = 5


# ---------------------------------------------------------------------------
# Board bounds — needed by the React viewer for mm → pixel coordinate mapping
# ---------------------------------------------------------------------------

def _compute_board_bounds(pcb: PCBData) -> dict:
    """
    Compute the bounding box of all positioned geometry in mm.
    Includes component centroids, trace endpoints, and via positions.
    Returns {"min_x", "min_y", "max_x", "max_y"} with a small margin.
    """
    xs, ys = [], []

    for comp in pcb.components:
        if not comp.is_power_symbol:
            xs.append(comp.x)
            ys.append(comp.y)

    for trace in pcb.traces:
        xs.extend([trace.start[0], trace.end[0]])
        ys.extend([trace.start[1], trace.end[1]])

    for via in pcb.vias:
        xs.append(via.x)
        ys.append(via.y)

    if not xs:
        return {"min_x": 0, "min_y": 0, "max_x": 100, "max_y": 100}

    margin = 2.0  # mm padding around the board
    return {
        "min_x": min(xs) - margin,
        "min_y": min(ys) - margin,
        "max_x": max(xs) + margin,
        "max_y": max(ys) + margin,
    }

def _extract_svg_bounds(svg_path: str) -> dict | None:
    """
    Parse the viewBox from the exported SVG to get exact board bounds in mm.
    KiCad SVG exports use mm units in the viewBox.
    Falls back to None if parsing fails.
    """
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(svg_path)
        root = tree.getroot()
        # Handle namespaced SVG
        vb = root.get("viewBox") or root.get("viewbox")
        if not vb:
            return None
        parts = vb.replace(",", " ").split()
        if len(parts) != 4:
            return None
        min_x, min_y, width, height = map(float, parts)
        return {
            "min_x": min_x,
            "min_y": min_y,
            "max_x": min_x + width,
            "max_y": min_y + height,
        }
    except Exception as e:
        print(f"[WARNING] Could not parse SVG viewBox: {e}")
        return None
# ---------------------------------------------------------------------------
# _tag — works on set[Finding] (frozen dataclass → hashable)
#
# B11 FIX: within each group (NEW/EXISTING/FIXED) findings are sorted by
# severity first (CRITICAL=0, WARNING=1, INFO=2), then lexicographically
# by message within the same severity.
#
# Dual output: human-readable strings go into dest_str (for text report /
# stdout); Finding objects go into dest_findings (for the React viewer).
# The label prefix (NEW:/EXISTING:/FIXED:) is stored on the Finding's
# message when written to dest_str but NOT mutated on the Finding object
# itself — the viewer uses severity + category directly.
# ---------------------------------------------------------------------------

def _severity_key(f) -> int:
    if isinstance(f, Finding):
        sev = f.severity
    else:
        sev = f
    if "CRITICAL" in sev: return 0
    if "WARNING"  in sev: return 1
    return 2


def _tag(old_set: set, new_set: set, dest_str: list, dest_findings: list,
         prefix: str = ""):
    pre = f"{prefix} " if prefix else ""

    new_only = sorted(new_set - old_set,  key=lambda f: (_severity_key(f), f.message))
    both     = sorted(new_set & old_set,  key=lambda f: (_severity_key(f), f.message))
    old_only = sorted(old_set - new_set,  key=lambda f: (_severity_key(f), f.message))

    for f in new_only:
        dest_str.append(f"{pre}NEW: {f}")
        dest_findings.append(f)
    for f in both:
        dest_str.append(f"{pre}EXISTING: {f}")
        dest_findings.append(f)
    for f in old_only:
        dest_str.append(f"{pre}FIXED: {f}")
        # Fixed findings still go into dest_findings so the viewer can show
        # them with a "FIXED" badge — useful for confirming regressions cleared.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compare_pcbs(old_pcb: PCBData, new_pcb: PCBData,
                 stackup_config: str = None) -> DiffResult:
    result = DiffResult()

    if stackup_config:
        stackup_config = os.path.abspath(stackup_config)

    component_stats = component_diff(old_pcb, new_pcb, result)
    net_diff(old_pcb, new_pcb, result)
    routing_diff(old_pcb, new_pcb, result)

    enrich_traces_with_connectivity(old_pcb)
    enrich_traces_with_connectivity(new_pcb)
    graph_old = build_connectivity_graph(old_pcb)
    graph_new = build_connectivity_graph(new_pcb)
    result.graph_old = graph_old
    result.graph_new = graph_new

    # Board bounds from the after board (used by viewer for coordinate mapping)
    result.board_bounds = _compute_board_bounds(new_pcb)

    connectivity_changes = compare_connectivity(graph_old, graph_new)
    result.net_changes.extend(
        [f"CONNECTIVITY: {msg}" for msg in connectivity_changes]
    )

    # ERC
    erc_old = set(run_erc_checks(graph_old, components=old_pcb.components))
    erc_new = set(run_erc_checks(graph_new, components=new_pcb.components))
    _tag(erc_old, erc_new, result.net_changes, result.erc_findings, prefix="ERC")

    # Power tree
    _, pt_old = analyse_power_tree(old_pcb, graph_old)
    _, pt_new = analyse_power_tree(new_pcb, graph_new)
    _tag(set(pt_old), set(pt_new),
         result.power_tree_changes, result.power_tree_findings)

    # Diff pairs
    dp_old = set(analyse_diff_pairs(old_pcb))
    dp_new = set(analyse_diff_pairs(new_pcb))
    _tag(dp_old, dp_new,
         result.diff_pair_changes, result.diff_pair_findings)

    # Grounding
    gnd_old = set(analyse_grounding(old_pcb, graph_old))
    gnd_new = set(analyse_grounding(new_pcb, graph_new))
    _tag(gnd_old, gnd_new,
         result.ground_changes, result.ground_findings)

    # Impedance
    imp_old = set(analyse_impedance(old_pcb, stackup_config))
    imp_new = set(analyse_impedance(new_pcb, stackup_config))
    _tag(imp_old, imp_new,
         result.impedance_changes, result.impedance_findings)

    # BOM
    bom_old = set(analyse_supply_chain(old_pcb))
    bom_new = set(analyse_supply_chain(new_pcb))
    _tag(bom_old, bom_new,
         result.bom_changes, result.bom_findings, prefix="BOM")

    result.net_changes.sort()
    result.routing_changes.sort()

    def _n(lst, tag): return sum(1 for x in lst if x.startswith(tag))

    summary_lines = [
        "SUMMARY", "-------",
        f"Components added:           {component_stats['added']}",
        f"Components removed:         {component_stats['removed']}",
        f"Components modified:        {component_stats['modified']}",
        f"Net / ERC changes:          {len(result.net_changes)}",
        f"Routing changes:            {len(result.routing_changes)}",
        f"Power tree — new:           {_n(result.power_tree_changes,'NEW:')}",
        f"Power tree — existing:      {_n(result.power_tree_changes,'EXISTING:')}",
        f"Power tree — fixed:         {_n(result.power_tree_changes,'FIXED:')}",
        f"Diff pair issues — new:     {_n(result.diff_pair_changes,'NEW:')}",
        f"Ground issues — new:        {_n(result.ground_changes,'NEW:')}",
        f"Ground issues — existing:   {_n(result.ground_changes,'EXISTING:')}",
        f"Impedance issues — new:     {_n(result.impedance_changes,'NEW:')}",
        f"Impedance issues — existing:{_n(result.impedance_changes,'EXISTING:')}",
        f"BOM out-of-stock — new:     {_n(result.bom_changes,'BOM NEW: CRITICAL')}",
        f"BOM low-stock — new:        {_n(result.bom_changes,'BOM NEW: WARNING')}",
    ]
    result.summary = "\n".join(summary_lines)
    return result


# ---------------------------------------------------------------------------
# Component / net / routing diff functions — unchanged
# ---------------------------------------------------------------------------

def _build_component_map(components):
    by_uuid, by_ref, ref_counts = {}, {}, {}
    for c in components:
        if not c.ref or c.ref == "REF**" or c.is_power_symbol:
            continue
        ref_counts[c.ref] = ref_counts.get(c.ref, 0) + 1
        if c.uuid:
            by_uuid[c.uuid] = c
        elif c.ref not in by_ref:
            by_ref[c.ref] = c
    ambiguous = {r for r, n in ref_counts.items() if n > 1}
    if by_uuid:
        return by_uuid, ambiguous, "uuid"
    return by_ref, ambiguous, "ref"


def _label(comp, ambiguous):
    if comp.ref in ambiguous and comp.uuid:
        return f"{comp.ref} [uuid:{comp.uuid[:8]}]"
    return comp.ref if comp.ref else f"[uuid:{comp.uuid[:8]}]"


def _pos_equal(p1, p2, tol):
    return math.hypot(p1[0]-p2[0], p1[1]-p2[1]) <= tol


def component_diff(old_pcb, new_pcb, result):
    old_map, old_amb, old_key = _build_component_map(old_pcb.components)
    new_map, new_amb, new_key = _build_component_map(new_pcb.components)

    if old_key != new_key:
        print("[WARNING] UUID/ref mismatch — falling back to ref matching.")
        old_map = {c.ref: c for c in old_pcb.components
                   if c.ref and c.ref != "REF**" and not c.is_power_symbol}
        new_map = {c.ref: c for c in new_pcb.components
                   if c.ref and c.ref != "REF**" and not c.is_power_symbol}
        old_amb = new_amb = set()

    ambiguous = old_amb | new_amb
    shared = sorted(old_map.keys() & new_map.keys())
    added, removed, modified, modified_keys = [], [], [], set()

    for key in sorted(new_map.keys() - old_map.keys()):
        added.append(f"Component added: {_label(new_map[key], ambiguous)}")
    for key in sorted(old_map.keys() - new_map.keys()):
        removed.append(f"Component removed: {_label(old_map[key], ambiguous)}")

    old_pos = {k: (old_map[k].x, old_map[k].y) for k in shared}
    new_pos = {k: (new_map[k].x, new_map[k].y) for k in shared}
    swapped, swap_msgs = set(), []

    for i, ka in enumerate(shared):
        if ka in swapped: continue
        if not _pos_equal(old_pos[ka], new_pos[ka], MOVE_THRESHOLD):
            for kb in shared[i+1:]:
                if kb in swapped: continue
                if (not _pos_equal(old_pos[kb], new_pos[kb], MOVE_THRESHOLD)
                        and _pos_equal(new_pos[ka], old_pos[kb], MOVE_THRESHOLD)
                        and _pos_equal(new_pos[kb], old_pos[ka], MOVE_THRESHOLD)):
                    la, lb = sorted([_label(old_map[ka], ambiguous),
                                     _label(old_map[kb], ambiguous)])
                    swap_msgs.append(f"Components swapped positions: {la} <-> {lb}")
                    swapped.update([ka, kb])
                    break

    for key in shared:
        oc, nc = old_map[key], new_map[key]
        lbl = _label(nc, ambiguous)
        changed = False
        if old_key == "uuid" and oc.ref != nc.ref:
            modified.append(f"Component re-annotated: {oc.ref} -> {nc.ref} [uuid:{nc.uuid[:8]}]")
            changed = True
        if math.hypot(oc.x-nc.x, oc.y-nc.y) > MOVE_THRESHOLD and key not in swapped:
            modified.append(
                f"Component moved: {lbl} ({oc.x:.3f},{oc.y:.3f}) -> ({nc.x:.3f},{nc.y:.3f})"
            )
            changed = True
        if oc.value != nc.value:
            modified.append(f"Component value changed: {lbl} [{oc.value}] -> [{nc.value}]")
            changed = True
        if oc.footprint != nc.footprint:
            modified.append(
                f"Component footprint changed: {lbl} {oc.footprint} -> {nc.footprint}"
            )
            changed = True
        if oc.layer != nc.layer:
            modified.append(f"Component layer changed: {lbl} {oc.layer} -> {nc.layer}")
            changed = True
        if abs(oc.rotation - nc.rotation) > ROT_THRESHOLD:
            modified.append(
                f"Component rotation changed: {lbl} {oc.rotation}° -> {nc.rotation}°"
            )
            changed = True
        if changed:
            modified_keys.add(key)

    result.component_changes.extend(added + removed + swap_msgs + modified)
    n_swap_pairs = len(swapped) // 2
    return {
        "added":     len(added),
        "removed":   len(removed),
        "modified":  len(modified_keys - swapped) + n_swap_pairs,
        "match_key": old_key,
    }


def net_diff(old_pcb, new_pcb, result):
    def build_pad_map(components):
        pad_map = {}
        for comp in components:
            if not comp.ref or comp.ref == "REF**" or comp.is_power_symbol:
                continue
            id_key = comp.uuid if comp.uuid else comp.ref
            for pad in comp.pads:
                if pad.number:
                    pad_map[(id_key, pad.number)] = (pad.net, comp.ref)
        return pad_map

    old_map = build_pad_map(old_pcb.components)
    new_map = build_pad_map(new_pcb.components)
    new_comp_pads = {
        (c.uuid if c.uuid else c.ref, pad.number)
        for c in new_pcb.components
        for pad in c.pads
        if not c.is_power_symbol
    }
    for pad_key in sorted(set(old_map) | set(new_map)):
        oe, ne   = old_map.get(pad_key), new_map.get(pad_key)
        old_net  = oe[0] if oe else None
        new_net  = ne[0] if ne else None
        if old_net == new_net:
            continue
        ref = (ne or oe)[1]
        _, pad_num = pad_key
        if not ref:
            continue
        if old_net is not None and new_net is not None:
            result.net_changes.append(
                f"CRITICAL: {ref} pad {pad_num} changed from {old_net} -> {new_net}"
            )
        elif old_net is None:
            result.net_changes.append(
                f"INFO: {ref} pad {pad_num} connected to {new_net}"
            )
        elif new_net is None and pad_key in new_comp_pads:
            result.net_changes.append(
                f"WARNING: {ref} pad {pad_num} disconnected from {old_net}"
            )


def routing_diff(old_pcb, new_pcb, result):
    def trace_key(t):
        s = tuple(round(v, TRACE_ROUND) for v in t.start)
        e = tuple(round(v, TRACE_ROUND) for v in t.end)
        if s > e: s, e = e, s
        return (t.layer, s, e, t.net, round(t.width, 3))

    old_t = {trace_key(t) for t in old_pcb.traces}
    new_t = {trace_key(t) for t in new_pcb.traces}
    for t in new_t - old_t:
        result.routing_changes.append(
            f"Trace added: net {t[3]}, layer {t[0]}, "
            f"from {t[1]} to {t[2]}, width {t[4]} mm"
        )
    for t in old_t - new_t:
        result.routing_changes.append(
            f"Trace removed: net {t[3]}, layer {t[0]}, "
            f"from {t[1]} to {t[2]}, width {t[4]} mm"
        )

    def via_key(v):
        return (round(v.x, TRACE_ROUND), round(v.y, TRACE_ROUND), v.net)

    old_v = {via_key(v) for v in old_pcb.vias}
    new_v = {via_key(v) for v in new_pcb.vias}
    for v in new_v - old_v:
        result.routing_changes.append(f"Via added: net {v[2]}, at ({v[0]}, {v[1]})")
    for v in old_v - new_v:
        result.routing_changes.append(f"Via removed: net {v[2]}, at ({v[0]}, {v[1]})")