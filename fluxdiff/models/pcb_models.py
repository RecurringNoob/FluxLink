from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Set


@dataclass
class Pad:
    number: str
    net: str
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    has_explicit_position: bool = False


@dataclass
class Component:
    ref: str
    value: str
    footprint: str
    x: float
    y: float
    rotation: float
    layer: str
    pads: List[Pad] = field(default_factory=list)
    uuid: str = ""
    is_power_symbol: bool = False


@dataclass
class Net:
    net_id: int
    name: str


@dataclass
class Trace:
    layer: str
    start: Tuple[float, float]
    end: Tuple[float, float]
    net: str
    width: float = 0.0
    start_ref: Optional[str] = field(default=None)
    start_pad: Optional[str] = field(default=None)
    end_ref: Optional[str] = field(default=None)
    end_pad: Optional[str] = field(default=None)


@dataclass
class Arc:
    """
    A copper arc routing segment (gr_arc on a copper layer, or fp_arc).

    start / mid / end are three points on the arc (KiCad uses three-point arcs).
    width is in mm; net is the net name string.
    """
    layer: str
    start: Tuple[float, float]
    mid: Tuple[float, float]
    end: Tuple[float, float]
    net: str
    width: float = 0.0


@dataclass
class Zone:
    """
    A copper pour / zone fill.

    outline_pts is the list of (x, y) vertices of the zone boundary polygon
    in KiCad absolute mm coordinates.
    uuid uniquely identifies the zone so the diff engine can track renames.
    """
    layer: str
    net: str
    outline_pts: List[Tuple[float, float]] = field(default_factory=list)
    uuid: str = ""


@dataclass
class Via:
    x: float
    y: float
    net: str


@dataclass
class PCBData:
    components: List[Component] = field(default_factory=list)
    nets: List[Net] = field(default_factory=list)
    traces: List[Trace] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    arcs: List[Arc] = field(default_factory=list)
    zones: List[Zone] = field(default_factory=list)
    # Edge.Cuts bounding box in KiCad absolute mm coordinates.
    # Populated by extract_board_outline(); the diff engine derives
    # DiffResult.board_bounds from it.
    board_outline: Optional[Tuple[float, float, float, float]] = None  # (min_x, min_y, max_x, max_y)
    # Points KiCad writes verbatim when plotting Edge.Cuts (line/arc end
    # points, rect corners, polygon vertices, circle centres), in board mm.
    # visual/svg_frame.py uses them to locate the board inside an exported SVG.
    outline_anchors: List[Tuple[float, float]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Finding — structured analysis result with board coordinates
#
# frozen=True makes Finding hashable so _tag() in diff_engine.py can use
# set operations (new_set - old_set, etc.) exactly as it does today with
# plain strings.  __str__ returns the same human-readable message that
# analysis modules previously returned directly, preserving backward
# compatibility with anything that prints or logs findings.
#
# Fields:
#   severity      — "CRITICAL" | "WARNING" | "INFO"
#   category      — "ERC" | "POWER" | "DIFF_PAIR" | "GROUND" |
#                   "IMPEDANCE" | "BOM" | "COMPONENT"
#   message       — the human-readable string (same as the old List[str] entry)
#   related_refs  — component refs directly involved in this finding
#   affected_nets — net names involved (for sidebar net highlighting)
#   coordinates   — (x, y) in KiCad mm board-space, or None for net-level
#                   findings that have no single meaningful board location
#   highlight_refs — all refs whose markers should pulse when this finding
#                    is selected (may include refs not in related_refs, e.g.
#                    both legs of a diff pair)
#   key           — stable identity used to match a finding between the old
#                   and new board.  Defaults to the message.  Checks whose
#                   message contains measured values (lengths, Z0, counts,
#                   ref lists) pass an explicit key without those values.
#
# Identity: two findings are "the same" when severity, category, key and
# affected_nets match.  message, related_refs, coordinates and
# highlight_refs are display data and are excluded from __eq__/__hash__ —
# otherwise moving a part 0.1 mm, or re-routing a diff pair, made an
# unchanged issue show up as both FIXED and NEW.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    message: str = field(compare=False)
    related_refs: Tuple[str, ...] = field(default=(), compare=False)
    affected_nets: Tuple[str, ...] = ()
    coordinates: Optional[Tuple[float, float]] = field(default=None, compare=False)
    highlight_refs: Tuple[str, ...] = field(default=(), compare=False)
    # Diff status relative to the old board: "NEW", "EXISTING" or "FIXED"
    # (empty for findings produced outside a diff).
    status: str = field(default="", compare=False)
    key: str = ""

    def __post_init__(self):
        if not self.key:
            object.__setattr__(self, "key", self.message)

    def __str__(self) -> str:
        """Backward-compatible string representation matches the old list[str] format."""
        return f"{self.severity}: {self.message}"

    def to_dict(self) -> dict:
        """Serialise to a JSON-safe dict for the /api/diff Flask endpoint."""
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "related_refs": list(self.related_refs),
            "affected_nets": list(self.affected_nets),
            "coordinates": {"x": self.coordinates[0], "y": self.coordinates[1]}
                           if self.coordinates else None,
            "highlight_refs": list(self.highlight_refs),
            "status": self.status,
            # Full label kept for sidebar display — matches old _tag() output
            "label": self.message,
        }


@dataclass
class DiffResult:
    component_changes: List[str] = field(default_factory=list)
    net_changes: List[str] = field(default_factory=list)
    routing_changes: List[str] = field(default_factory=list)
    # Structured findings — parallel to the *_changes string lists.
    # diff_engine._tag() populates both: strings go into *_changes for the
    # text report; Finding objects go into *_findings for the React viewer.
    power_tree_changes: List[str] = field(default_factory=list)
    power_tree_findings: List[Finding] = field(default_factory=list)
    diff_pair_changes: List[str] = field(default_factory=list)
    diff_pair_findings: List[Finding] = field(default_factory=list)
    ground_changes: List[str] = field(default_factory=list)
    ground_findings: List[Finding] = field(default_factory=list)
    impedance_changes: List[str] = field(default_factory=list)
    impedance_findings: List[Finding] = field(default_factory=list)
    bom_changes: List[str] = field(default_factory=list)
    bom_findings: List[Finding] = field(default_factory=list)
    erc_findings: List[Finding] = field(default_factory=list)
    # One dict per component change, parallel to component_changes:
    #   {"type", "ref", "message", "from": {"x","y"}|None, "to": {"x","y"}|None}
    # Positions are KiCad mm on the old ("from") and new ("to") board.
    component_change_details: List[dict] = field(default_factory=list)
    # Arc routing changes (gr_arc on copper layers)
    arc_changes: List[str] = field(default_factory=list)
    # Zone / copper-pour changes
    zone_changes: List[str] = field(default_factory=list)
    summary: str = ""
    graph_old: Dict[str, Set] = field(default_factory=dict)
    graph_new: Dict[str, Set] = field(default_factory=dict)
    # Shared board frame in mm: union of both boards' Edge.Cuts outlines plus
    # a margin.  The CLI crops before.svg/after.svg (and so the PNGs) to
    # exactly this rectangle, so a board point maps to the image fraction
    #   ((x - min_x) / (max_x - min_x), (y - min_y) / (max_y - min_y)).
    # Format: {"min_x", "min_y", "max_x", "max_y", "offset_x", "offset_y"};
    # offset_x/offset_y equal min_x/min_y.
    board_bounds: Optional[dict] = None