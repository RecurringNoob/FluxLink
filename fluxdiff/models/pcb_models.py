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
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    severity: str
    category: str
    message: str
    related_refs: Tuple[str, ...] = ()
    affected_nets: Tuple[str, ...] = ()
    coordinates: Optional[Tuple[float, float]] = None
    highlight_refs: Tuple[str, ...] = ()

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
    component_findings: List[Finding] = field(default_factory=list)
    summary: str = ""
    graph_old: Dict[str, Set] = field(default_factory=dict)
    graph_new: Dict[str, Set] = field(default_factory=dict)
    # Board spatial bounds in mm — populated by diff_engine from parsed geometry.
    # Used by the React viewer to map KiCad mm coordinates → overlay pixels.
    board_bounds: Optional[dict] = None  # {"min_x","min_y","max_x","max_y"}