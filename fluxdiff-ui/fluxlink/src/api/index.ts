import type { DiffResponse, BoardBounds } from "../types/finding";

export async function fetchDiff(): Promise<DiffResponse> {
  const res = await fetch("/api/diff");
  if (!res.ok) throw new Error(`Failed to fetch diff: ${res.status}`);
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