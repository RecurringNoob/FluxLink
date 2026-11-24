# fluxdiff/rag/chat/tools.py
"""
Tools the chat model can call to look things up precisely, instead of
relying only on similarity search.

They read the documents already stored in a repository's index, so they
work on existing (and deployed, read-only) indexes without re-indexing:

  list_commits        commits with date, author, message and change counts
  get_commit          everything recorded for one commit
  component_history   every change that mentions a reference (U6, R5, J1 ...)
  get_findings        ERC / power / diff-pair / grounding / impedance / BOM findings
  search_changes      exact text search over the recorded changes
  search_history      similarity search (the same retrieval the chat starts with)
  repo_overview       what the repository is: index statistics plus its README

Results are plain text, capped in size, and every commit mentioned is
recorded so the chat can cite it as a source.
"""

import json
import re
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from fluxdiff.rag.schemas import RAGDocument

MAX_RESULT_CHARS = 6_000
SECTION_TYPES = ("component", "net", "routing", "power_tree", "diff_pair",
                 "grounding", "impedance", "bom")
FINDING_TYPES = ("net", "power_tree", "diff_pair", "grounding", "impedance", "bom")
_FINDING = re.compile(r"^(?:[A-Z]+ )?(NEW|FIXED): (CRITICAL|WARNING|INFO): (.+)$")
_PAD_CHANGE = re.compile(r"^(CRITICAL|WARNING|INFO): (\S+ pad .+)$")

SPECS = [
    {
        "name": "list_commits",
        "description": "List the indexed commits that changed the board, with date, author, "
                       "message and how much changed. Use for 'latest/first commit', 'how many "
                       "revisions', 'what happened recently', timelines.",
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Maximum commits to return (default 20)."},
            "oldest_first": {"type": "boolean", "description": "Chronological order instead of newest first."},
        }},
    },
    {
        "name": "get_commit",
        "description": "Everything recorded for one commit: message, component, net/ERC, routing "
                       "and analysis changes. Accepts a full hash or a prefix of 4+ characters.",
        "parameters": {"type": "object", "properties": {
            "commit": {"type": "string", "description": "Commit hash or prefix."},
        }, "required": ["commit"]},
    },
    {
        "name": "component_history",
        "description": "Every recorded change mentioning a component reference (e.g. U6, R5, J1, "
                       "C3), oldest first, with commit, date and message.",
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "Reference designator, e.g. U6."},
        }, "required": ["ref"]},
    },
    {
        "name": "get_findings",
        "description": "Design-check findings recorded per commit (ERC, power tree, diff pairs, "
                       "grounding, impedance, BOM) and pad net changes. NEW = introduced by that "
                       "commit, FIXED = resolved by it.",
        "parameters": {"type": "object", "properties": {
            "severity": {"type": "string", "enum": ["", "CRITICAL", "WARNING", "INFO"],
                         "description": "Only this severity (empty = all)."},
            "status": {"type": "string", "enum": ["NEW", "FIXED", "ANY"],
                       "description": "NEW (default), FIXED or ANY."},
        }},
    },
    {
        "name": "search_changes",
        "description": "Exact, case-insensitive text search over all recorded changes (net names "
                       "like USB_D+, footprints, values like 10k, layers). Returns matching lines "
                       "grouped by commit.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Text to find."},
            "change_type": {"type": "string", "enum": [""] + list(SECTION_TYPES),
                            "description": "Only this kind of change (empty = all)."},
        }, "required": ["text"]},
    },
    {
        "name": "search_history",
        "description": "Similarity search over the indexed history for a natural-language query; "
                       "returns the most relevant recorded documents.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for."},
        }, "required": ["query"]},
    },
    {
        "name": "repo_overview",
        "description": "What this repository/board is: its README (purpose, features) plus "
                       "statistics of the indexed history (commits, date range, authors, files).",
        "parameters": {"type": "object", "properties": {}},
    },
]


def _parse_date(value: str) -> datetime:
    value = (value or "").strip()
    for parse in (lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
                  lambda v: datetime.strptime(v, "%a %b %d %H:%M:%S %Y %z")):
        try:
            d = parse(value)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _field(content: str, name: str) -> str:
    m = re.search(rf"^{name}: (.*)$", content, re.M)
    return m.group(1).strip() if m else ""


def _changes(doc: RAGDocument) -> List[str]:
    """The change lines of a section document (after the 'Changes:' header)."""
    _, sep, body = doc.content.partition("\nChanges:\n")
    return [l for l in body.split("\n") if l.strip()] if sep else []


def _cap(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n… (truncated; narrow the request for more)"


def _fetch_github_readme(location: str, branch: str) -> Optional[str]:
    owner_repo = location.strip("/")
    for name in ("README.md", "readme.md", "README.rst", "README"):
        url = (f"https://raw.githubusercontent.com/{owner_repo}/"
               f"{urllib.parse.quote(branch or 'HEAD')}/{name}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fluxlink-chat"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception:
            continue
    return None


_README_CACHE: Dict[str, Optional[str]] = {}


class HistoryTools:
    def __init__(self, documents: List[RAGDocument], repo: Optional[dict] = None,
                 search: Optional[Callable[[str], List[RAGDocument]]] = None,
                 fetch_readme: Callable[[str, str], Optional[str]] = _fetch_github_readme):
        self.documents = documents
        self.repo = repo or {}
        self._search = search
        self._fetch_readme = fetch_readme
        self.used: List[str] = []           # "tool(arg)" for the UI
        self.touched: List[str] = []        # commit hashes cited by tool results

        self._summaries = sorted(
            (d for d in documents if d.metadata.get("type") == "summary"),
            key=lambda d: _parse_date(d.metadata.get("date", "")))
        self._info = {}                     # commit -> (date, message, author)
        for d in self._summaries:
            c = d.metadata.get("commit", "")
            self._info[c] = (d.metadata.get("date", "")[:10], _field(d.content, "Message"),
                             d.metadata.get("author", ""))
        self._order = {d.metadata.get("commit"): i for i, d in enumerate(self._summaries)}

    specs = SPECS

    # ------------------------------------------------------------------

    def call(self, name: str, args: Optional[dict] = None) -> str:
        args = dict(args or {})
        label = name + (f"({', '.join(str(v) for v in args.values() if v not in ('', None))})"
                        if any(v not in ("", None) for v in args.values()) else "")
        self.used.append(label)
        fn = getattr(self, f"_tool_{name}", None)
        if fn is None or name not in {s["name"] for s in SPECS}:
            return f"Unknown tool {name!r}."
        try:
            return _cap(fn(**args))
        except TypeError as e:
            return f"Bad arguments for {name}: {e}"

    def _cite(self, commit: str):
        if commit and commit not in self.touched:
            self.touched.append(commit)

    def _head(self, commit: str) -> str:
        date, message, _ = self._info.get(commit, ("", "", ""))
        return f"{commit[:7]} ({date}) {message}".strip()

    def _match_commits(self, prefix: str) -> List[str]:
        p = (prefix or "").strip().lower()
        commits = {d.metadata.get("commit", "") for d in self.documents if d.metadata.get("commit")}
        if len(p) < 4:
            return []
        return sorted(c for c in commits if c.lower().startswith(p))

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _tool_list_commits(self, limit: int = 20, oldest_first: bool = False) -> str:
        if not self._summaries:
            return "No commits are indexed for this repository."
        rows = self._summaries if oldest_first else list(reversed(self._summaries))
        limit = max(1, min(int(limit or 20), 100))
        lines = [f"{len(self._summaries)} indexed commit(s) changed a board file; "
                 f"showing {min(limit, len(rows))}, {'oldest' if oldest_first else 'newest'} first:"]
        for d in rows[:limit]:
            c = d.metadata.get("commit", "")
            self._cite(c)
            counts = re.findall(r"^\s+([a-zA-Z /]+?)\s*:\s*(\d+)$", d.content, re.M)
            changed = ", ".join(f"{k.strip()} {v}" for k, v in counts if v != "0") or "no recorded changes"
            date, message, author = self._info.get(c, ("", "", ""))
            lines.append(f"- {c[:7]} | {date} | {author} | {message} | {changed} | "
                         f"{d.metadata.get('file', '')}")
        return "\n".join(lines)

    def _tool_get_commit(self, commit: str) -> str:
        matches = self._match_commits(commit)
        if not matches:
            return f"No indexed commit starts with {commit!r} (use 4+ characters; see list_commits)."
        if len(matches) > 1:
            return "Ambiguous prefix; matches: " + ", ".join(self._head(c) for c in matches)
        c = matches[0]
        self._cite(c)
        docs = sorted((d for d in self.documents if d.metadata.get("commit") == c),
                      key=lambda d: (d.metadata.get("type") != "summary", d.metadata.get("type", ""),
                                     d.metadata.get("chunk", 0)))
        return "\n\n".join(d.content for d in docs)

    def _tool_component_history(self, ref: str) -> str:
        ref = (ref or "").strip()
        if not ref:
            return "Give a reference designator such as U6."
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(ref)}(?![A-Za-z0-9_])", re.I)
        by_commit = defaultdict(list)
        for d in self.documents:
            if d.metadata.get("type") in SECTION_TYPES:
                for line in _changes(d):
                    if pattern.search(line):
                        by_commit[d.metadata.get("commit", "")].append(line)
        if not by_commit:
            return f"No recorded change mentions {ref}."
        out = [f"Changes mentioning {ref} ({len(by_commit)} commit(s), oldest first):"]
        for c in sorted(by_commit, key=lambda c: self._order.get(c, -1)):
            self._cite(c)
            out.append(f"\n{self._head(c)}")
            out.extend(f"  {l}" for l in dict.fromkeys(by_commit[c]))
        return "\n".join(out)

    def _tool_get_findings(self, severity: str = "", status: str = "NEW") -> str:
        severity = (severity or "").upper()
        status = (status or "NEW").upper()
        by_commit = defaultdict(list)
        for d in self.documents:
            if d.metadata.get("type") not in FINDING_TYPES:
                continue
            for line in _changes(d):
                m = _FINDING.match(line)
                if m:
                    st, sev, text = m.groups()
                    kind = d.metadata.get("type")
                elif d.metadata.get("type") == "net" and _PAD_CHANGE.match(line):
                    sev, text = _PAD_CHANGE.match(line).groups()
                    st, kind = "NEW", "pad net change"
                    # Routine "pad connected to" lines only when INFO is asked for.
                    if sev == "INFO" and severity != "INFO":
                        continue
                else:
                    continue
                if severity and sev != severity:
                    continue
                if status != "ANY" and st != status:
                    continue
                by_commit[d.metadata.get("commit", "")].append(f"[{st} {sev}] ({kind}) {text}")
        if not by_commit:
            what = " ".join(x for x in (status if status != "ANY" else "", severity) if x)
            return f"No {what + ' ' if what else ''}findings recorded in the indexed commits."
        out = []
        for c in sorted(by_commit, key=lambda c: self._order.get(c, -1), reverse=True):
            self._cite(c)
            out.append(self._head(c))
            out.extend(f"  {l}" for l in dict.fromkeys(by_commit[c]))
        total = sum(len(set(v)) for v in by_commit.values())
        return f"{total} finding(s) in {len(by_commit)} commit(s), newest first:\n" + "\n".join(out)

    def _tool_search_changes(self, text: str, change_type: str = "") -> str:
        needle = (text or "").strip().lower()
        if not needle:
            return "Give some text to search for."
        by_commit = defaultdict(list)
        for d in self.documents:
            kind = d.metadata.get("type")
            if kind not in SECTION_TYPES or (change_type and kind != change_type):
                continue
            for line in _changes(d):
                if needle in line.lower():
                    by_commit[d.metadata.get("commit", "")].append(f"({kind}) {line}")
        if not by_commit:
            return f"No recorded change contains {text!r}."
        out, shown = [], 0
        for c in sorted(by_commit, key=lambda c: self._order.get(c, -1), reverse=True):
            self._cite(c)
            out.append(self._head(c))
            for l in dict.fromkeys(by_commit[c]):
                if shown < 60:
                    out.append(f"  {l}")
                shown += 1
        more = f"\n… {shown - 60} more matching line(s) not shown" if shown > 60 else ""
        return f"{shown} matching line(s) in {len(by_commit)} commit(s), newest first:\n" + "\n".join(out) + more

    def _tool_search_history(self, query: str) -> str:
        if not self._search:
            return "Similarity search is not available here."
        docs = self._search(query)
        if not docs:
            return "Nothing relevant found."
        for d in docs:
            self._cite(d.metadata.get("commit", ""))
        return "\n\n".join(f"[{d.metadata.get('type')} | {d.metadata.get('commit', '')[:7]}]\n{d.content}"
                           for d in docs)

    def _tool_repo_overview(self) -> str:
        repo = self.repo
        out = []
        if repo:
            out.append(f"Repository: {repo.get('label')} ({repo.get('kind')}: {repo.get('location')}"
                       + (f", branch {repo.get('branch')}" if repo.get("branch") else "") + ")")
        if self._summaries:
            first, last = self._summaries[0], self._summaries[-1]
            authors = Counter(d.metadata.get("author", "") for d in self._summaries)
            files = sorted({d.metadata.get("file", "") for d in self._summaries})
            out.append(f"Indexed history: {len(self._summaries)} board commit(s) from "
                       f"{first.metadata.get('date', '')[:10]} to {last.metadata.get('date', '')[:10]}")
            out.append("Authors: " + ", ".join(f"{a} ({n})" for a, n in authors.most_common(5)))
            out.append("Board files: " + ", ".join(files))
        kinds = Counter(d.metadata.get("type") for d in self.documents)
        out.append("Documents: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))

        readme = None
        local = [d for d in self.documents if d.metadata.get("type") == "repo_file"
                 and d.metadata.get("file", "").split("/")[-1].lower().startswith("readme")]
        if local:
            readme = local[0].content
        elif repo.get("kind") == "github":
            key = f"{repo.get('location')}@{repo.get('branch')}"
            if key not in _README_CACHE:
                _README_CACHE[key] = self._fetch_readme(repo.get("location", ""), repo.get("branch", ""))
            readme = _README_CACHE[key]
        out.append("\nREADME:\n" + (readme[:3500] if readme else "(not available)"))
        return "\n".join(out)


def tools_enabled() -> bool:
    import os
    return os.getenv("FLUXDIFF_CHAT_TOOLS", "1").strip().lower() not in ("0", "false", "no")


def as_json_args(raw) -> dict:
    """Tool-call arguments arrive as a JSON string (OpenAI) or a mapping (Gemini)."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}
