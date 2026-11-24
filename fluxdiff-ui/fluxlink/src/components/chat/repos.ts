import { useCallback, useEffect, useState } from "react";

// Mirrors fluxdiff/rag/repos.py (GET /repos).
export type RepoKind = "local" | "github";

export interface Repo {
  id: string;
  kind: RepoKind | "cli";
  location: string;
  label: string;
  ref: string;
  branch: string;
  status: "new" | "indexing" | "ready" | "error";
  message: string;
  documents: number;
  max_commits: number;
  updated: string;
}

interface RepoList {
  repos: Repo[];
  allow_local: boolean;
  busy: boolean;
}

async function call<T>(apiUrl: string, path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${apiUrl}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const data: unknown = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    throw new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
  }
  return data as T;
}

const POLL_MS = 1500;

/** Repositories known to the chat API, refreshed while one is indexing. */
export function useRepos(apiUrl: string) {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [allowLocal, setAllowLocal] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await call<RepoList>(apiUrl, "/repos");
      setRepos(data.repos);
      setAllowLocal(data.allow_local);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoaded(true);
    }
  }, [apiUrl]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const indexing = repos.some((r) => r.status === "indexing");
  useEffect(() => {
    if (!indexing) return;
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [indexing, refresh]);

  const add = useCallback(async (kind: RepoKind, location: string, ref: string) => {
    const repo = await call<Repo>(apiUrl, "/repos", {
      method: "POST",
      body: JSON.stringify({ kind, location, ref }),
    });
    await refresh();
    return repo;
  }, [apiUrl, refresh]);

  const reindex = useCallback(async (id: string) => {
    await call<Repo>(apiUrl, `/repos/${encodeURIComponent(id)}/reindex`, { method: "POST" });
    await refresh();
  }, [apiUrl, refresh]);

  const remove = useCallback(async (id: string) => {
    await call<{ status: string }>(apiUrl, `/repos/${encodeURIComponent(id)}`, { method: "DELETE" });
    await refresh();
  }, [apiUrl, refresh]);

  return { repos, allowLocal, loadError, loaded, refresh, add, reindex, remove };
}
