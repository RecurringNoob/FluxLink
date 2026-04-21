from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Set


@dataclass
class Pad:
    number: str
    net: str
    # Absolute board coordinates (mm) computed from footprint position +
    # footprint rotation + pad-local offset. Populated by pcb_parser.py.
    x: float = 0.0
    y: float = 0.0
    # Pad rotation relative to the board (footprint rotation + pad-local rotation).
    rotation: float = 0.0
    # F5 FIX: explicit flag set by the parser when an (at …) node is found.
    # Replaces the fragile `pad.x == 0.0 and pad.y == 0.0` fallback heuristic
    # which incorrectly fell back to component origin for thermal pads legitimately
    # placed at (0, 0) in footprint-local coordinates.
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
    width: float = 0.0          # mm — parsed from (width ...) node; 0.0 if absent
    # Enrichment fields populated by enrich_traces_with_connectivity
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


@dataclass
class DiffResult:
    component_changes: List[str] = field(default_factory=list)
    net_changes: List[str] = field(default_factory=list)
    routing_changes: List[str] = field(default_factory=list)
    power_tree_changes: List[str] = field(default_factory=list)
    diff_pair_changes: List[str] = field(default_factory=list)
    ground_changes: List[str] = field(default_factory=list)
    impedance_changes: List[str] = field(default_factory=list)
    bom_changes: List[str] = field(default_factory=list)
    summary: str = ""
    # B10 FIX: graphs are stored on DiffResult so main.py can reuse them for
    # the power tree report without calling build_connectivity_graph a second
    # time (which would re-enrich already-enriched traces).
    graph_old: Dict[str, Set] = field(default_factory=dict)
    graph_new: Dict[str, Set] = field(default_factory=dict)