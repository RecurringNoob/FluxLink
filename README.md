# FluxLink

**Semantic PCB diff review with a natural-language chat interface.**

FluxLink combines two tools into one workflow: **FluxDiff** — a structured diff engine for KiCad `.kicad_pcb` files — and a **RAG-powered chat layer** that lets engineers query board changes in plain English. Review diffs visually in the interactive board viewer, then ask questions about what changed and why.

---

## What's Inside

| Layer | What it does |
|---|---|
| **FluxDiff** | Compares two KiCad board files semantically — components, nets, routing, ERC, power tree, differential pairs, grounding, impedance, and BOM |
| **Board Viewer** | Flask + React/Vite frontend: side-by-side / toggle / overlay view with interactive findings markers |
| **FluxLink Chat** | RAG pipeline over git history — embeds diff summaries into a FAISS vector store, answers natural-language questions with cited sources, using OpenAI, Gemini or any OpenAI-compatible model |

---

## Installation

**System requirements:** Python 3.9+, KiCad with `kicad-cli` on your `PATH`
(for the SVG/PNG exports), and the Cairo library (for `cairosvg`). Parsing,
diffing and the text report work without KiCad or Cairo.

```bash
git clone https://github.com/RecurringNoob/FluxLink.git
cd FluxLink
pip install -e .            # diff engine, CLI and board viewer API
pip install -e ".[rag]"     # + chat / RAG (fastapi, uvicorn, openai, faiss-cpu)
pip install -e ".[dev]"     # + pytest; run the tests with `pytest`
```

The board viewer frontend is a separate npm project:

```bash
cd fluxdiff-ui/fluxlink
npm install
npm run dev      # dev server on :5173, or `npm run build` to have Flask serve it on :5000
```

---

## Quick Start

### Diff two board files

```bash
# Print report to stdout, write output/ directory
fluxlink before.kicad_pcb after.kicad_pcb

# With impedance analysis
fluxlink before.kicad_pcb after.kicad_pcb --stackup stackup.yaml

# Open the interactive board viewer after diffing
fluxlink before.kicad_pcb after.kicad_pcb --viewer
```

`--viewer` starts the Flask API on `http://127.0.0.1:5000` and keeps it running in that terminal. If you have run
`npm run build` in `fluxdiff-ui/fluxlink`, Flask serves the viewer itself on
:5000; otherwise run `npm run dev` there and open `http://localhost:5173`.
Set `FLUXLINK_FRONTEND_DIST` to serve a build from another location.
(On macOS, AirPlay Receiver also listens on port 5000; turn it off if the
viewer API cannot bind.)

### Ask questions about board history

```bash
# 1. Set credentials — one of these (see "Choosing the model" below)
export FLUXDIFF_REPO_PATH=/path/to/kicad/repo
export OPENAI_API_KEY=sk-...        # or: export GEMINI_API_KEY=...

# 2. Ingest git history into the vector store
python -m fluxdiff.rag.ingest.run_ingest --max-commits 50

# 3. Start the chat API
uvicorn fluxdiff.rag.api:app --host 127.0.0.1 --port 8000

# 4. Open the chat UI at /chat (http://localhost:5173/chat with the Vite dev server)
```

---

## Output Files

All files are written to `output/` in the working directory.

| File | Description |
|---|---|
| `output/diff_report.txt` | Full text diff report |
| `output/before.svg` / `output/after.svg` | Board SVGs (viewer backgrounds), both cropped to the same board frame so markers line up |
| `output/diff_overlay.png` | Pixel-level red/green diff image |
| `output/component_diff.png` | Annotated component change markers |

---

## Board Viewer

The viewer is a React/Vite frontend served by Flask. Three view modes:

- **Side by side** — before and after boards rendered simultaneously, findings markers on the after board
- **Toggle** — flip between before and after with a single key
- **Overlay** — pixel-level diff image

The **findings sidebar** lists all issues grouped by category (ERC, POWER, DIFF_PAIR, GROUND, IMPEDANCE, BOM) with severity badges (CRITICAL / WARNING / INFO), a NEW / EXISTING / FIXED status, free-text search, and severity filtering. The **components tab** lists added, removed, moved and modified parts. Clicking any finding or component change pans and zooms the board to that location.

A finding is EXISTING when the same issue (same check, severity and nets) is on both boards, even if a part moved or a measured value changed; only issues that appear or disappear are NEW or FIXED.

**Keyboard shortcuts:**

| Key | Action |
|---|---|
| `j` / `↓` | Next finding |
| `k` / `↑` | Previous finding |
| `Escape` | Clear selection |

---

## Chat Interface

The `/chat` route opens FluxLink Chat — a full-page conversational UI backed by the RAG API.

### Tools

Besides similarity search, the chat model can call tools that read the indexed
history exactly, so list, count, "latest" and history questions get complete answers:

| Tool | Answers |
|---|---|
| `list_commits` | latest / first commit, timelines, how many revisions |
| `get_commit` | everything recorded for one commit (hash or 4+ character prefix) |
| `component_history` | every change mentioning a reference such as `U6` or `R5` |
| `get_findings` | design-check findings (NEW / FIXED, by severity) and pad net changes |
| `search_changes` | exact text search over changes (net names, values, footprints) |
| `search_history` | similarity search with a new query |
| `repo_overview` | what the board is: the repository README plus index statistics |

The tools use existing indexes (no re-index needed). Each answer lists the tools
it used under "Looked up". If a model cannot use tools, the chat answers without
them; `FLUXDIFF_CHAT_TOOLS=0` turns tools off.

### Repositories: local or GitHub

The **Repositories** panel in the chat sidebar indexes board history in two modes:

| Mode | You enter | How it reads history |
|---|---|---|
| **GitHub** | `owner/repo` or a GitHub URL, optionally a branch/tag | GitHub API + raw file downloads — nothing is cloned |
| **Local folder** | the path of a git clone on the machine running the chat API | local `git`; also indexes the repo's text files |

Click **Index repository**; indexing runs in the background with live progress, and
the chat is enabled when it finishes. Each repository has its own index — switch
between them in the panel (switching starts a new conversation), **↻** picks up new
commits, **×** removes a repository and its index. An index built with the
command-line ingester appears as *Command-line index*.

When the chat API is reachable by other people, set `FLUXLINK_ALLOW_LOCAL_REPOS=0`
so the browser cannot make it read folders on your machine.

Ask questions like:

- *"What changed in the last commit?"*
- *"Are there any impedance issues on the USB traces?"*
- *"Which components were moved between rev A and rev B?"*
- *"Show me all CRITICAL findings across the last 10 commits."*

Every answer lists the commits and files it was sourced from in a collapsible sources panel below the message. Each browser conversation has its own server-side memory; **New Chat** clears it.

### Chat API

```
POST /chat
Body:    { "query": "What changed in the last commit?", "session_id": "optional" }
Returns: { "answer": "...",
           "session_id": "3f2a…",
           "sources": [{ "commit": "<40-char hash>", "file": "board/main.kicad_pcb",
                         "type": "net", "date": "...", "author": "..." }] }
```

Send the returned `session_id` with the next request to continue the conversation;
omit it to start a new one. Add `"repo_id"` (from `GET /repos`) to ask about a
specific repository; without it the command-line index is used.

#### Repositories

```
GET    /repos                  → { "repos": [...], "allow_local": true, "busy": false }
POST   /repos                  { "kind": "github", "location": "esp-rs/esp-rust-board", "ref": "" }
                               { "kind": "local",  "location": "D:\\boards\\my-board" }
                               → 202, the repo with "status": "indexing"
GET    /repos/{id}             → poll until "status" is "ready" (or "error", with "message")
POST   /repos/{id}/reindex     → pick up new commits
DELETE /repos/{id}             → remove the repository and its index
```

One repository is indexed at a time (`409` while busy). Indexes live under
`rag_db/repos/<id>/`, listed in `rag_db/repos.json`. `DELETE /chat/session/{session_id}` forgets a conversation.
If the LLM call fails the API returns HTTP 502 with the error in `detail`.
The server starts without an API key; until one is set, chat requests
and `/health` return HTTP 503 with the reason in `detail`.

#### Filtered retrieval

Restrict retrieval to a specific commit, document type, or file:

```json
POST /chat/filtered
{
  "query": "Are there impedance issues?",
  "filters": {
    "commit": "a1b2c3d",
    "type": "impedance",
    "file": "board/main.kicad_pcb"
  }
}
```

All filter keys are optional, and filters are applied before the top-k cut, so you get the best matches *within* the filter. `commit` accepts a full hash or a prefix of at least 4 characters; `file` is the repository-relative path. Supported document types: `summary`, `component`, `net`, `routing`, `power_tree`, `diff_pair`, `grounding`, `impedance`, `bom`, `repo_file`.

#### Health check

```
GET /health
→ { "status": "ok", "documents_indexed": 142 }
```

---

## Ingestion

```bash
# Index the last 50 commits that changed a .kicad_pcb file
python -m fluxdiff.rag.ingest.run_ingest --max-commits 50

# Re-index from scratch
python -m fluxdiff.rag.ingest.run_ingest --max-commits 50 --clear

# Include impedance analysis during ingestion
python -m fluxdiff.rag.ingest.run_ingest --stackup stackup.yaml

# Board history only — do not index the repository's text files
python -m fluxdiff.rag.ingest.run_ingest --no-repo-files

# Read a GitHub repository directly — no clone needed
python -m fluxdiff.rag.ingest.run_ingest --github esp-rs/esp-rust-board
python -m fluxdiff.rag.ingest.run_ingest --github https://github.com/owner/repo --ref dev
```

**GitHub mode** (`--github`, or `FLUXDIFF_GITHUB_REPO` in `.env`) reads the board
history through the GitHub API and downloads each board version from
raw.githubusercontent.com. A typical run makes 3–5 API calls, within GitHub's
60-per-hour limit without a token; set `GITHUB_TOKEN` for private repositories or
a higher limit. It indexes board history only (not the repository's text files),
and finds board files from the tree at `--ref`, so boards deleted earlier are skipped.

Each commit that changed a board file is diffed against its first parent, one
board file at a time. Routing changes are indexed as a per-net summary
(segments and length added/removed, layers, widths, vias) rather than one line
per segment, and padless footprints (logos, text labels) are not treated as parts. Long documents are split into chunks that fit the
embedding model's input limit, and requests are batched.

Ingestion is idempotent: documents are deduplicated by SHA-256 content hash and
only new documents are embedded, so re-running it costs nothing for unchanged
history. A running chat server picks up a new ingestion without a restart.

By default the repository's `.md`, `.txt`, `.py`, `.json` and `.yaml` files are
also indexed (lock files and files over 256 KB are skipped). **Their contents are
sent to the OpenAI embeddings API** — keep secrets out of tracked config files,
or use `--no-repo-files`.

---

## Stackup Configuration

Impedance analysis uses a per-layer stackup config. If omitted, a default microstrip layer is assumed (0.2 mm dielectric, εr = 4.5, 35 µm copper).

```yaml
# stackup.yaml
layers:
  F.Cu:
    type: microstrip
    dielectric_thickness: 0.2
    dielectric_er: 4.5
    copper_thickness: 0.035
  In1.Cu:
    type: stripline
    dielectric_thickness: 0.18
    dielectric_er: 4.3
    copper_thickness: 0.035
```

`dielectric_thickness` is the trace-to-plane height for microstrip and the
plane-to-plane spacing for stripline.

Optional `critical_nets` sets explicit targets for named nets (exact net names).
The calculator works on single traces, so targets are **single-ended**; mark a
differential target with `differential: true` and it is halved (Zdiff ≈ 2·Z0, a
loose-coupling approximation):

```yaml
critical_nets:
  /RF_OUT:
    target_ohms: 50
    tolerance_ohms: 3
  /HDMI_D0_P:
    target_ohms: 100
    tolerance_ohms: 10
    differential: true
```

Without `critical_nets`, built-in single-ended targets apply to nets whose name
contains a USB, RF/ANT, LVDS, HDMI, ETH, MIPI or PCIE segment.

Pass with `--stackup stackup.yaml`. Both YAML and JSON are accepted.

---

## Analysis Modules

### ERC
Power shorts, missing pull-ups on I2C/open-drain nets, missing bypass caps within 5 mm of IC power pins, under-connected power nets, and floating nets.

### Power Tree
Classifies regulators, connectors, batteries, and IC loads into a rail graph. Reports rail contention (CRITICAL), unused regulator outputs (WARNING), and sourceless rails (WARNING).

### Differential Pairs
Detects paired nets by suffix (`_P`/`_N`, `+`/`−`, `_DP`/`_DN`, etc.). Checks length mismatch > 0.5 mm (segments and arc tracks), via count asymmetry, and layer asymmetry per pair. A lone `_P`/`_N` net is reported only when its name looks like a differential interface (USB, LVDS, CLK, TX/RX, …), so active-low signals such as `RESET_N` are left alone.

### Grounding
GND island detection, analog/digital IC mix on a shared GND net, and ADC components without a GND reference within 10 mm.

### Impedance
Single-ended microstrip and stripline (IPC-2141) impedance from trace width and stackup config, compared against single-ended targets for critical net types (USB, RF, LVDS, HDMI, Ethernet, MIPI, PCIe). See *Stackup Configuration* for how differential targets are handled.

### BOM / Supply Chain
Groups components by `(value, footprint)`, queries the ERP adapter, and reports out-of-stock (CRITICAL), low-stock (WARNING), or sufficient-stock (INFO). **No ERP is connected by default**, so the check reports "Stock not checked" instead of inventing numbers. Implement `erp_service.fetch_inventory_from_erp()` for your ERP without touching other modules. `FLUXLINK_ERP_DEMO=1` enables deterministic demo stock levels, labelled `[DEMO DATA]` in every finding.

---

## Deploying the chat (hosted demo)

The hosted chat is **read-only and pre-indexed**: you index repositories on your
machine, ship the indexes with the app, and the server only answers questions.
(Free hosting wipes the disk on restart, and indexing takes minutes.)

**1. Index locally** with the provider you will deploy with (same `.env` key),
in the chat page or from the command line:

```bash
python -m fluxdiff.rag.repos add github esp-rs/esp-rust-board
python -m fluxdiff.rag.repos list
```

**2. Export the indexes into the repo** and commit them:

```bash
python -m fluxdiff.rag.repos export deploy/index
git add deploy/index && git commit -m "Demo indexes" && git push
```

Everything in `deploy/index` becomes public with your repository, so only export
repositories whose content may be public.

**3. Deploy the Docker image.** The `Dockerfile` builds the chat page and API into
one service that listens on `$PORT`. On [Render](https://render.com) (free tier):
*New → Web Service →* connect the GitHub repository (Render uses the
`Dockerfile`) *→ Instance type: Free →* add the environment variable
`GEMINI_API_KEY` (or `OPENAI_API_KEY`) *→ Deploy*. The site opens on the chat.

Free Render services sleep after 15 minutes without traffic and take about a
minute to wake: open the URL a couple of minutes before presenting.

**Rehearse locally** with the same setup:

```bash
docker build -t fluxlink .
docker run -p 8000:8000 -e GEMINI_API_KEY=... fluxlink        # http://localhost:8000
```

or, without Docker (Windows `cmd`):

```bat
cd fluxdiff-ui\fluxlink
set VITE_API_URL=/api&& set VITE_CHAT_ONLY=1&& npm run build
cd ..\..
set FLUXLINK_READONLY=1&& set FLUXDIFF_VECTOR_DB_PATH=deploy\index&& uvicorn fluxdiff.rag.server:app --port 8000
```

| Variable (set by the Dockerfile) | Value | Purpose |
|---|---|---|
| `FLUXLINK_READONLY` | `1` | Visitors can chat but not add, re-index or remove repositories |
| `FLUXLINK_ALLOW_LOCAL_REPOS` | `0` | Never read folders on the server |
| `FLUXDIFF_VECTOR_DB_PATH` | `/app/index` | The shipped `deploy/index` |
| `FLUXLINK_FRONTEND_DIST` | `/app/web` | The built chat page |

## Choosing the model (OpenAI, Gemini, local)

The chat is provider-agnostic. Set the key for the provider you want in `.env`:

```bash
OPENAI_API_KEY=sk-...          # OpenAI: gpt-4o-mini + text-embedding-3-small
# or
GEMINI_API_KEY=...             # Gemini: gemini-3.8-flash + gemini-embedding-2
```

With only one key set, that provider is used. With both, OpenAI is used unless
`FLUXDIFF_LLM_PROVIDER=gemini`. Answers and search can use different providers
(`FLUXDIFF_EMBEDDING_PROVIDER`), and either model can be changed with
`FLUXDIFF_LLM_MODEL` / `FLUXDIFF_EMBEDDING_MODEL`. The chat header shows the
model answering.

**OpenAI-compatible servers** (Ollama, LM Studio, vLLM, Groq, OpenRouter, Azure
OpenAI) use the OpenAI provider with a base URL, for example a fully local setup:

```bash
OPENAI_BASE_URL=http://localhost:11434/v1   # Ollama
OPENAI_API_KEY=ollama                        # any value
FLUXDIFF_LLM_MODEL=llama3.1
FLUXDIFF_EMBEDDING_MODEL=nomic-embed-text
```

**Busy models.** When a provider answers "busy" or "rate limited" (HTTP 503/429),
the chat retries once after a short pause, then tries the fallback models
(`FLUXDIFF_LLM_FALLBACK_MODELS`; for Gemini, `gemini-3.5-flash-lite` by default).
Other errors are reported immediately as one readable line.

**Changing the embedding model** puts existing indexes in a different vector
space. Each index records the model that built it: the chat asks you to re-index
(↻) such repositories, and re-indexing rebuilds them automatically.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FLUXDIFF_REPO_PATH` | current directory | Path to the KiCad git repository |
| `FLUXDIFF_GITHUB_REPO` | *(unset)* | `owner/repo` to ingest from GitHub without cloning (same as `--github`) |
| `GITHUB_TOKEN` | *(unset)* | GitHub token for private repositories or a higher API limit |
| `FLUXLINK_ALLOW_LOCAL_REPOS` | `1` | `0` stops the chat UI from indexing local folders (set it when the API is shared) |
| `FLUXLINK_READONLY` | `0` | `1` serves existing indexes only (hosted demos; set by the Dockerfile) |
| `OPENAI_API_KEY` | — | OpenAI (or OpenAI-compatible server) key |
| `GEMINI_API_KEY` | — | Gemini key (`GOOGLE_API_KEY` also works) |
| `FLUXDIFF_LLM_PROVIDER` | from the key present | `openai` or `gemini` |
| `FLUXDIFF_EMBEDDING_PROVIDER` | same as LLM provider | `openai` or `gemini` |
| `FLUXDIFF_LLM_MODEL` | `gpt-4o-mini` / `gemini-3.8-flash` | Chat completion model |
| `FLUXDIFF_EMBEDDING_MODEL` | `text-embedding-3-small` / `gemini-embedding-2` | Embedding model |
| `FLUXDIFF_CHAT_TOOLS` | `1` | `0` answers from similarity search only (no tool calls) |
| `FLUXDIFF_LLM_FALLBACK_MODELS` | — / `gemini-3.5-flash-lite` | Comma-separated models tried when the main one is busy (503/429); empty disables |
| `OPENAI_BASE_URL` | OpenAI | OpenAI-compatible server (Ollama, Groq, OpenRouter, ...) |
| `FLUXDIFF_TOP_K` | `5` | Documents retrieved per query |
| `FLUXDIFF_MEMORY_WINDOW` | `5` | Conversation turns kept in context |
| `FLUXDIFF_VECTOR_DB_PATH` | `rag_db/` at the repository root | FAISS index storage path |
| `FLUXDIFF_CORS_ORIGINS` | `localhost:5173,3000,5000` | Allowed CORS origins for the chat API (comma-separated) |
| `FLUXLINK_ERP_DEMO` | *(off)* | `1` to use demo stock levels in the BOM check |
| `FLUXLINK_FRONTEND_DIST` | `fluxdiff-ui/fluxlink/dist` | Frontend build served by the Flask viewer |
| `VITE_API_URL` | `http://localhost:8000` | Chat API URL used by the frontend (set at build time) |

---

## Ports

| Service | Port | Purpose |
|---|---|---|
| Flask board viewer | 5000 | `/api/diff`, `/api/board/*`, serves the React SPA |
| FastAPI RAG chat | 8000 | `/chat`, `/chat/filtered`, `/health` |
| Vite dev server | 5173 | Frontend HMR (proxies `/api` to Flask) |

---

## Architecture

```
fluxdiff/                        # Python package (installed as `fluxlink`)
├── cli/main.py                  # Entry point — orchestrates the full pipeline
├── parser/
│   ├── sexp_parser.py           # S-expression tokenizer and AST builder
│   └── pcb_parser.py            # AST → domain objects (PCBData)
├── models/pcb_models.py         # Dataclasses: Pad, Component, Net, Trace, Via, Finding, DiffResult
├── diff/diff_engine.py          # compare_pcbs() — master diff orchestrator
├── analysis/
│   ├── connectivity_graph.py
│   ├── trace_connectivity.py
│   ├── geometry.py
│   ├── erc_checker.py
│   ├── power_tree.py
│   ├── diff_pair.py
│   ├── ground_checker.py
│   └── impedance.py
├── supply_chain/
│   ├── bom_checker.py
│   └── erp_service.py           # Swappable ERP adapter stub
├── visual/
│   ├── kicad_export.py          # kicad-cli → SVG/PNG export
│   ├── image_diff.py            # Pixel-level before/after overlay
│   └── component_diff.py        # Component marker and arrow overlay
├── viewer/server.py             # Flask API and SPA server
└── rag/
    ├── config.py                # All RAG tunables (env-driven)
    ├── api.py                   # FastAPI app
    ├── ingest/                  # Git loader, diff generator, document builder
    ├── embedding/               # OpenAI embedder, FAISS vector store
    ├── retrieval/retriever.py   # Query embedding → similarity search → metadata filter
    ├── llm/                     # LLM client, prompt templates
    └── chat/                    # ChatEngine, ChatMemory (rolling window)
fluxdiff-ui/fluxlink/              # React + Vite frontend (board viewer, /chat)
tests/                             # pytest suite (`pytest`)
```

---

## Extending FluxLink

### Adding a new analysis module

1. Implement `analyse_X(pcb: PCBData, ...) -> list[Finding]`.
2. Add `X_changes: List[str]` and `X_findings: List[Finding]` to `DiffResult` in `pcb_models.py`.
3. Call it in `compare_pcbs()` in `diff_engine.py`.
4. Add a section to the report writer in `cli/main.py`.
5. Expose the new findings key in `/api/diff` in `viewer/server.py`.
6. Map it in `DiffSummary`, `DiffGenerator`, and `DocumentBuilder` under `rag/`.

### Swapping the vector store
Replace `VectorStore` in `rag/embedding/vector_store.py`. Keep the interface: `add_documents()`, `similarity_search()`, `clear()`.

### Adding an LLM or embedding provider
Add the provider name to `rag/providers.py` (defaults, key check), a branch in
`LLMClient` (`rag/llm/llm_client.py`) and a backend class in `rag/embedding/embedder.py`
with `embed(texts, is_query)` and `max_input_chars`. Indexes record
`Embedder.signature` (`provider:model`), so switching models is detected and
re-indexing rebuilds them.

### Persisting chat memory across restarts
`ChatEngine` keeps one `ChatMemory` per session id in process memory (the 500 most recent sessions). Replace `ChatMemory` in `rag/chat/memory.py` with a Redis- or DB-backed implementation. Keep the interface: `add(user, assistant)`, `get_context() -> str`, `clear()`.

---

## Dependencies

| Package | Purpose |
|---|---|
| `click` | CLI argument parsing |
| `flask` + `flask-cors` | Board viewer API |
| `fastapi` + `uvicorn` + `pydantic` *(`[rag]` extra)* | RAG chat API |
| `openai`, `google-genai`, `faiss-cpu` *(`[rag]` extra)* | Embeddings, chat completions (OpenAI / Gemini), vector search |
| `numpy` | Array operations |
| `opencv-python` | Image processing |
| `cairosvg` | SVG → PNG conversion |
| `python-dotenv` | `.env` file loading |
| `pyyaml` | YAML stackup config |
| `kicad-cli` *(system)* | PCB export to SVG/PNG |

---

## License

MIT