import { useState } from "react";
import type { FormEvent, JSX } from "react";
import { needsReindex } from "./repos";
import type { Models, Repo, RepoKind } from "./repos";

interface RepoPanelProps {
  repos: Repo[];
  models: Models | null;
  loaded: boolean;
  allowLocal: boolean;
  readonly: boolean;     // hosted demo: no add / re-index / remove
  loadError: string | null;
  activeRepoId: string | null;
  onSelect: (id: string) => void;
  onAdd: (kind: RepoKind, location: string, ref: string) => Promise<void>;
  onReindex: (id: string) => Promise<void>;
  onRemove: (id: string) => Promise<void>;
}

const KIND_LABEL: Record<Repo["kind"], string> = { github: "GitHub", local: "Local", cli: "CLI" };

const provider = (signature?: string) => (signature ?? "").split(":")[0];

function StatusLine({ repo, models }: { repo: Repo; models: Models | null }): JSX.Element {
  const kind = KIND_LABEL[repo.kind];
  if (repo.status === "ready" && needsReindex(repo, models)) {
    return (
      <span className="truncate text-amber-300/90"
            title={`Built with ${repo.embedding}; the server now uses ${models?.embedding}`}>
        {kind} · built with {provider(repo.embedding)} — re-index (↻)
      </span>
    );
  }
  if (repo.status === "indexing") {
    return (
      <span className="flex items-center gap-1.5 text-amber-300/90">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-300 animate-pulse" />
        <span className="truncate" title={repo.message}>{kind} · {repo.message || "Indexing…"}</span>
      </span>
    );
  }
  if (repo.status === "error") {
    return <span className="truncate text-red-400" title={repo.message}>{repo.message || "Indexing failed"}</span>;
  }
  if (repo.status === "ready") {
    const branch = repo.kind === "github" && repo.branch ? ` · ${repo.branch}` : "";
    return <span className="truncate text-zinc-500">{kind} · {repo.documents} documents{branch}</span>;
  }
  return <span className="text-zinc-500">{kind} · not indexed yet</span>;
}

export function RepoPanel({
  repos, models, loaded, allowLocal, readonly, loadError, activeRepoId, onSelect, onAdd, onReindex, onRemove,
}: RepoPanelProps): JSX.Element {
  const [formOpen, setFormOpen] = useState(false);
  // With no repositories yet, the form is the only useful thing to show.
  const firstRun = loaded && repos.length === 0 && !loadError;
  const showForm = !readonly && (formOpen || firstRun);
  const [kind, setKind] = useState<RepoKind>("github");
  const [location, setLocation] = useState("");
  const [ref, setRef] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<string | null>(null);

  const busy = repos.some((r) => r.status === "indexing");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!location.trim() || submitting) return;
    setSubmitting(true);
    setFormError(null);
    try {
      await onAdd(kind, location.trim(), kind === "github" ? ref.trim() : "");
      setLocation("");
      setRef("");
      setFormOpen(false);
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const act = async (fn: () => Promise<void>) => {
    setRowError(null);
    try {
      await fn();
    } catch (err) {
      setRowError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between px-2 mt-2">
        <p className="text-xs font-semibold text-zinc-500 tracking-wide">Repositories</p>
        {!firstRun && !readonly && (
          <button
            onClick={() => { setFormOpen((v) => !v); setFormError(null); }}
            className="text-xs font-medium text-zinc-400 hover:text-zinc-100 px-1.5 py-0.5 rounded hover:bg-white/10"
          >
            {formOpen ? "Cancel" : "+ Add"}
          </button>
        )}
      </div>

      {loadError && (
        <p className="px-2 text-xs text-red-400">Chat API unreachable: {loadError}</p>
      )}

      {showForm && (
        <form onSubmit={(e) => void submit(e)} className="mx-1 flex flex-col gap-2 rounded-lg bg-white/5 p-2.5">
          <div className="grid grid-cols-2 gap-1 rounded-md bg-black/30 p-0.5 text-xs font-medium">
            {(["github", "local"] as const).map((k) => {
              const disabled = k === "local" && !allowLocal;
              return (
                <button
                  key={k}
                  type="button"
                  disabled={disabled}
                  title={disabled ? "Local folders are disabled on this server" : undefined}
                  onClick={() => setKind(k)}
                  className={`rounded px-2 py-1.5 transition-colors ${
                    kind === k ? "bg-zinc-700 text-zinc-100" : "text-zinc-400 hover:text-zinc-200"
                  } ${disabled ? "opacity-40 cursor-not-allowed" : ""}`}
                >
                  {k === "github" ? "GitHub" : "Local folder"}
                </button>
              );
            })}
          </div>
          <input
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            placeholder={kind === "github" ? "owner/repo or GitHub URL" : "D:\\path\\to\\kicad-repo"}
            className="w-full rounded-md bg-black/30 px-2.5 py-2 text-xs text-zinc-100 placeholder:text-zinc-500 focus:outline-none focus:ring-1 focus:ring-blue-500/60"
            autoFocus
          />
          {kind === "github" && (
            <input
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder="Branch or tag (optional)"
              className="w-full rounded-md bg-black/30 px-2.5 py-2 text-xs text-zinc-100 placeholder:text-zinc-500 focus:outline-none focus:ring-1 focus:ring-blue-500/60"
            />
          )}
          <p className="text-[11px] leading-snug text-zinc-500">
            {kind === "github"
              ? "Reads the board history through the GitHub API — nothing is cloned."
              : "A git clone on this computer; also indexes its text files."}
          </p>
          {formError && <p className="text-xs text-red-400">{formError}</p>}
          <button
            type="submit"
            disabled={!location.trim() || submitting || busy}
            className="rounded-md bg-blue-600 px-3 py-2 text-xs font-semibold text-white hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {submitting ? "Starting…" : busy ? "Wait for the current indexing" : "Index repository"}
          </button>
        </form>
      )}

      {firstRun && (
        <p className="px-2 text-xs leading-snug text-zinc-500">
          {readonly
            ? "No repositories are indexed on this server."
            : "No repositories yet. Add a GitHub repository or a local clone to chat about its board history."}
        </p>
      )}

      <div className="space-y-0.5">
        {repos.map((repo) => {
          const active = repo.id === activeRepoId;
          return (
            <div
              key={repo.id}
              className={`group relative rounded-lg ${active ? "bg-zinc-800" : "hover:bg-white/5"}`}
            >
              <button
                onClick={() => onSelect(repo.id)}
                className={`w-full text-left px-3 py-2 flex flex-col gap-0.5 ${
                  repo.kind !== "cli" && !readonly ? "group-hover:pr-14" : ""
                }`}
                title={repo.location}
              >
                <span className={`truncate text-sm ${active ? "font-medium text-zinc-100" : "text-zinc-300"}`}>
                  {repo.label}
                </span>
                <span className="text-[11px] min-w-0 flex">
                  <StatusLine repo={repo} models={models} />
                </span>
              </button>
              {repo.kind !== "cli" && !readonly && (
                <div className="absolute right-1.5 top-2 hidden gap-0.5 group-hover:flex">
                  <button
                    disabled={busy}
                    onClick={() => void act(() => onReindex(repo.id))}
                    title="Re-index (pick up new commits)"
                    className="rounded px-1.5 py-0.5 text-xs text-zinc-400 hover:bg-white/10 hover:text-zinc-100 disabled:opacity-30"
                  >
                    ↻
                  </button>
                  <button
                    disabled={repo.status === "indexing"}
                    onClick={() => {
                      if (window.confirm(`Remove ${repo.label} and its index?`)) {
                        void act(() => onRemove(repo.id));
                      }
                    }}
                    title="Remove"
                    className="rounded px-1.5 py-0.5 text-xs text-zinc-400 hover:bg-white/10 hover:text-red-300 disabled:opacity-30"
                  >
                    ×
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
      {rowError && <p className="px-2 text-xs text-red-400">{rowError}</p>}
    </div>
  );
}
