import type { DiffResponse, BoardBounds } from "../types/finding";

const START_HINT =
  "Start it with: fluxlink before.kicad_pcb after.kicad_pcb --viewer " +
  "(it keeps running in that terminal).";

export async function fetchDiff(): Promise<DiffResponse> {
  let res: Response;
  try {
    res = await fetch("/api/diff");
  } catch {
    throw new Error(`Could not reach the FluxDiff API. ${START_HINT}`);
  }
  if (res.status === 404) {
    throw new Error(
      "The FluxDiff API is running but has no diff loaded. " + START_HINT,
    );
  }
  if (res.status >= 500) {
    // The Vite dev proxy answers 500/502 when nothing is listening on :5000.
    throw new Error(
      `The FluxDiff API on port 5000 is not reachable (HTTP ${res.status}). ${START_HINT}`,
    );
  }
  if (!res.ok) throw new Error(`Failed to fetch diff: HTTP ${res.status}`);
  return res.json();
}

export async function fetchBoardBounds(): Promise<BoardBounds> {
  const res = await fetch("/api/board/bounds");
  if (!res.ok) throw new Error(`Failed to fetch board bounds: ${res.status}`);
  return res.json();
}

export const BOARD_URLS = {
  before: "/api/board/before",
  after: "/api/board/after",
  overlay: "/api/board/diff-overlay",
} as const;