export type Severity = "CRITICAL" | "WARNING" | "INFO";
export type Category =
  | "ERC"
  | "POWER"
  | "DIFF_PAIR"
  | "GROUND"
  | "IMPEDANCE"
  | "BOM"
  | "COMPONENT";

// Status relative to the old board. "" only for findings produced outside a diff.
export type FindingStatus = "NEW" | "EXISTING" | "FIXED";

export interface Coordinates {
  x: number;
  y: number;
}

export interface Finding {
  severity: Severity;
  category: Category;
  message: string;
  related_refs: string[];
  affected_nets: string[];
  coordinates: Coordinates | null;
  highlight_refs: string[];
  status: FindingStatus;
  label: string;
}

export interface BoardBounds {
  min_x: number;
  min_y: number;
  max_x: number;
  max_y: number;
}

export interface FindingsMap {
  erc: Finding[];
  power: Finding[];
  diff_pair: Finding[];
  ground: Finding[];
  impedance: Finding[];
  bom: Finding[];
}

// One entry per component change, built by diff_engine.component_diff.
// Positions are KiCad mm on the old ("from") and new ("to") board.
export interface ComponentChangeDetail {
  type: "added" | "removed" | "moved" | "swapped" | "value" | "footprint" | "layer" | "rotation" | "annotated";
  ref: string;
  message: string;
  from: Coordinates | null;
  to: Coordinates | null;
}

export interface DiffResponse {
  // Plain string lists (text report — unchanged)
  components: string[];
  // Structured component changes (absent from older servers)
  component_details?: ComponentChangeDetail[];
  nets: string[];
  routing: string[];
  power_tree: string[];
  power_tree_report: string;
  diff_pairs: string[];
  grounding: string[];
  impedance: string[];
  bom: string[];
  summary: string;
  // Structured findings for the viewer
  findings: FindingsMap;
  board_bounds: BoardBounds;
}

export type ViewMode = "sidebyside" | "toggle" | "overlay";