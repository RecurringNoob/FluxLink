# FluxLink

**Semantic PCB diff review with a natural-language chat interface.**

FluxLink combines two tools into one workflow: **FluxDiff** — a structured diff engine for KiCad `.kicad_pcb` files — and a **RAG-powered chat layer** that lets engineers query board changes in plain English. Review diffs visually in the interactive board viewer, then ask questions about what changed and why.

---

## What's Inside

| Layer | What it does |
|---|---|
| **FluxDiff** | Compares two KiCad board files semantically — components, nets, routing, ERC, power tree, differential pairs, grounding, impedance, and BOM |
| **Board Viewer** | Flask + React/Vite frontend: side-by-side / toggle / overlay view with interactive findings markers |
| **FluxLink Chat** | RAG pipeline over git history — embeds diff summaries into a FAISS vector store, answers natural-language questions via OpenAI with cited sources |

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
# 1. Set credentials
export FLUXDIFF_REPO_PATH=/path/to/kicad/repo
export OPENAI_API_KEY=sk-...

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
omit it to start a new one. `DELETE /chat/session/{session_id}` forgets a conversation.
If the LLM call fails the API returns HTTP 502 with the error in `detail`.
The server starts without `OPENAI_API_KEY`; until the key is set, chat requests
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

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FLUXDIFF_REPO_PATH` | current directory | Path to the KiCad git repository |
| `FLUXDIFF_GITHUB_REPO` | *(unset)* | `owner/repo` to ingest from GitHub without cloning (same as `--github`) |
| `GITHUB_TOKEN` | *(unset)* | GitHub token for private repositories or a higher API limit |
| `OPENAI_API_KEY` | *(required)* | OpenAI API key |
| `FLUXDIFF_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `FLUXDIFF_LLM_MODEL` | `gpt-4o-mini` | Chat completion model |
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

### Swapping the LLM
Replace `LLMClient.generate_response(prompt: str) -> str` in `rag/llm/llm_client.py`. Update `FLUXDIFF_LLM_MODEL`.

### Swapping the embedding model
Replace `Embedder.embed_documents()` and `embed_query()` in `rag/embedding/embedder.py`. If the embedding dimension changes, run `--clear` before re-ingesting — the FAISS index dimension is fixed at creation time.

### Persisting chat memory across restarts
`ChatEngine` keeps one `ChatMemory` per session id in process memory (the 500 most recent sessions). Replace `ChatMemory` in `rag/chat/memory.py` with a Redis- or DB-backed implementation. Keep the interface: `add(user, assistant)`, `get_context() -> str`, `clear()`.

---

## Dependencies

| Package | Purpose |
|---|---|
| `click` | CLI argument parsing |
| `flask` + `flask-cors` | Board viewer API |
| `fastapi` + `uvicorn` + `pydantic` *(`[rag]` extra)* | RAG chat API |
| `openai`, `faiss-cpu` *(`[rag]` extra)* | Embeddings, chat completions, vector search |
| `numpy` | Array operations |
| `opencv-python` | Image processing |
| `cairosvg` | SVG → PNG conversion |
| `python-dotenv` | `.env` file loading |
| `pyyaml` | YAML stackup config |
| `kicad-cli` *(system)* | PCB export to SVG/PNG |

---

## License

MIT