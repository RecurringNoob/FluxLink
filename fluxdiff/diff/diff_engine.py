from fluxdiff.models.pcb_models import PCBData, DiffResult, Finding
import math
import os
from dataclasses import replace
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

# Space kept around the outline so the Edge.Cuts line itself is not clipped.
OUTLINE_MARGIN_MM = 0.25


def _pcb_extent(pcb: PCBData):
    """(min_x, min_y, max_x, max_y, from_outline) for one board, or None."""
    if pcb.board_outline is not None:
        return (*pcb.board_outline, True)
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
        return None
    return (min(xs), min(ys), max(xs), max(ys), False)


def _board_bounds(old_pcb: PCBData, new_pcb: PCBData) -> dict:
    """
    Board-space rectangle (KiCad mm) that every board image is cropped to
    (visual/svg_frame.py) and that the viewer maps onto the image.

    It is the union of both boards' Edge.Cuts outlines plus a small margin,
    so the before and after images share one frame even when the outline
    changed.  Without an outline, component/trace/via positions are used with
    a 2 mm margin.

    Returns min_x, min_y, max_x, max_y and offset_x/offset_y (= min_x/min_y,
    kept for callers that compute  image_x = kicad_x - offset_x).
    """
    extents = [e for e in (_pcb_extent(old_pcb), _pcb_extent(new_pcb)) if e]
    if not extents:
        return {"min_x": 0, "min_y": 0, "max_x": 100, "max_y": 100,
                "offset_x": 0, "offset_y": 0}
    margin = OUTLINE_MARGIN_MM if all(e[4] for e in extents) else 2.0
    min_x = min(e[0] for e in extents) - margin
    min_y = min(e[1] for e in extents) - margin
    max_x = max(e[2] for e in extents) + margin
    max_y = max(e[3] for e in extents) + margin
    return {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y,
            "offset_x": min_x, "offset_y": min_y}


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
    order = lambda f: (_severity_key(f), f.message)

    # Iterate new_set (not `new_set & old_set`) for EXISTING: set intersection
    # returns elements from whichever operand is smaller, which could hand back
    # the OLD board's finding with stale coordinates and message text.
    new_only = sorted((f for f in new_set if f not in old_set), key=order)
    both     = sorted((f for f in new_set if f in old_set),     key=order)
    old_only = sorted((f for f in old_set if f not in new_set), key=order)

    # Findings are copied with their status set (Finding is frozen).  FIXED
    # findings are included so the viewer can list them with a FIXED badge;
    # their coordinates refer to the OLD board, so the viewer must not draw
    # markers for them on the new board.
    for f in new_only:
        dest_str.append(f"{pre}NEW: {f}")
        dest_findings.append(replace(f, status="NEW"))
    for f in both:
        dest_str.append(f"{pre}EXISTING: {f}")
        dest_findings.append(replace(f, status="EXISTING"))
    for f in old_only:
        dest_str.append(f"{pre}FIXED: {f}")
        dest_findings.append(replace(f, status="FIXED"))


_GROUP_PREFIXES = ("CONNECTIVITY:", "ERC NEW:", "ERC EXISTING:", "ERC FIXED:")
_SEVERITY_RANK  = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


def _net_change_key(line: str):
    """
    Ordering for net_changes: pad net changes first, then connectivity, then
    ERC NEW / EXISTING / FIXED; within a group by severity, then text.
    (A plain alphabetical sort interleaved all of these.)
    """
    group = 0
    for i, p in enumerate(_GROUP_PREFIXES, start=1):
        if line.startswith(p):
            group = i
            break
    head = line.split(": ", 2)
    sev_word = head[1] if group >= 2 and len(head) > 2 else head[0]
    return (group, _SEVERITY_RANK.get(sev_word.strip(), 3), line)


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

    # Shared frame for both board images and the viewer's coordinate mapping.
    result.board_bounds = _board_bounds(old_pcb, new_pcb)

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

    # Arc and zone routing diffs
    arc_diff(old_pcb, new_pcb, result)
    zone_diff(old_pcb, new_pcb, result)

    result.net_changes.sort(key=_net_change_key)
    result.routing_changes.sort()
    result.arc_changes.sort()
    result.zone_changes.sort()

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


def _rotation_delta(a, b):
    """Smallest angle between two rotations: 359° vs 0° is 1°, not 359°."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _pos(comp):
    return {"x": comp.x, "y": comp.y}


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
    # Structured twin of the strings, with board positions, for the viewer.
    det_added, det_removed, det_swapped, det_modified = [], [], [], []

    def detail(kind, ref, message, old=None, new=None):
        return {"type": kind, "ref": ref, "message": message,
                "from": _pos(old) if old else None,
                "to":   _pos(new) if new else None}

    for key in sorted(new_map.keys() - old_map.keys()):
        msg = f"Component added: {_label(new_map[key], ambiguous)}"
        added.append(msg)
        det_added.append(detail("added", new_map[key].ref, msg, new=new_map[key]))
    for key in sorted(old_map.keys() - new_map.keys()):
        msg = f"Component removed: {_label(old_map[key], ambiguous)}"
        removed.append(msg)
        det_removed.append(detail("removed", old_map[key].ref, msg, old=old_map[key]))

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
                    msg = f"Components swapped positions: {la} <-> {lb}"
                    swap_msgs.append(msg)
                    det_swapped.append(detail(
                        "swapped", f"{old_map[ka].ref}, {old_map[kb].ref}", msg,
                        old=old_map[ka], new=new_map[ka]))
                    swapped.update([ka, kb])
                    break

    for key in shared:
        oc, nc = old_map[key], new_map[key]
        lbl = _label(nc, ambiguous)
        changes = []
        if old_key == "uuid" and oc.ref != nc.ref:
            changes.append(("annotated",
                f"Component re-annotated: {oc.ref} -> {nc.ref} [uuid:{nc.uuid[:8]}]"))
        if math.hypot(oc.x-nc.x, oc.y-nc.y) > MOVE_THRESHOLD and key not in swapped:
            changes.append(("moved",
                f"Component moved: {lbl} ({oc.x:.3f},{oc.y:.3f}) -> ({nc.x:.3f},{nc.y:.3f})"))
        if oc.value != nc.value:
            changes.append(("value",
                f"Component value changed: {lbl} [{oc.value}] -> [{nc.value}]"))
        if oc.footprint != nc.footprint:
            changes.append(("footprint",
                f"Component footprint changed: {lbl} {oc.footprint} -> {nc.footprint}"))
        if oc.layer != nc.layer:
            changes.append(("layer",
                f"Component layer changed: {lbl} {oc.layer} -> {nc.layer}"))
        if _rotation_delta(oc.rotation, nc.rotation) > ROT_THRESHOLD:
            changes.append(("rotation",
                f"Component rotation changed: {lbl} {oc.rotation}° -> {nc.rotation}°"))
        for kind, msg in changes:
            modified.append(msg)
            det_modified.append(detail(kind, nc.ref, msg, old=oc, new=nc))
        if changes:
            modified_keys.add(key)

    result.component_changes.extend(added + removed + swap_msgs + modified)
    result.component_change_details.extend(
        det_added + det_removed + det_swapped + det_modified)
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


def arc_diff(old_pcb, new_pcb, result):
    """
    Diff copper arc segments (gr_arc on copper layers).

    Key: (layer, start_rounded, mid_rounded, end_rounded, net, width_rounded)
    Two arcs are considered identical if all six fields match within rounding.
    The mid point uniquely identifies the arc even when start/end are reused.
    """
    def arc_key(a):
        s = tuple(round(v, TRACE_ROUND) for v in a.start)
        m = tuple(round(v, TRACE_ROUND) for v in a.mid)
        e = tuple(round(v, TRACE_ROUND) for v in a.end)
        return (a.layer, s, m, e, a.net, round(a.width, 3))

    old_a = {arc_key(a) for a in old_pcb.arcs}
    new_a = {arc_key(a) for a in new_pcb.arcs}

    for a in new_a - old_a:
        result.arc_changes.append(
            f"Arc added: net {a[4]}, layer {a[0]}, "
            f"start {a[1]}, mid {a[2]}, end {a[3]}, width {a[5]} mm"
        )
    for a in old_a - new_a:
        result.arc_changes.append(
            f"Arc removed: net {a[4]}, layer {a[0]}, "
            f"start {a[1]}, mid {a[2]}, end {a[3]}, width {a[5]} mm"
        )


def zone_diff(old_pcb, new_pcb, result):
    """
    Diff copper zone / pour definitions.

    Primary key: (layer, net, uuid) — detects added/removed zones.
    Secondary check: for zones that share the same (layer, net, uuid), compare
    the rounded outline polygon to detect boundary edits.
    """
    def zone_id(z):
        return (z.layer, z.net, z.uuid)

    def zone_pts_key(z):
        pts = tuple(
            (round(x, TRACE_ROUND), round(y, TRACE_ROUND))
            for x, y in z.outline_pts
        )
        return pts

    old_map = {zone_id(z): z for z in old_pcb.zones}
    new_map = {zone_id(z): z for z in new_pcb.zones}

    for key in sorted(new_map.keys() - old_map.keys()):
        z = new_map[key]
        result.zone_changes.append(
            f"Zone added: net {z.net}, layer {z.layer}"
            + (f" [{z.uuid[:8]}]" if z.uuid else "")
        )

    for key in sorted(old_map.keys() - new_map.keys()):
        z = old_map[key]
        result.zone_changes.append(
            f"Zone removed: net {z.net}, layer {z.layer}"
            + (f" [{z.uuid[:8]}]" if z.uuid else "")
        )

    for key in sorted(old_map.keys() & new_map.keys()):
        oz, nz = old_map[key], new_map[key]
        if zone_pts_key(oz) != zone_pts_key(nz):
            result.zone_changes.append(
                f"Zone boundary changed: net {nz.net}, layer {nz.layer}"
                + (f" [{nz.uuid[:8]}]" if nz.uuid else "")
            )