# FluxDiff

**Semantic diff tool for KiCad PCB files.**

FluxDiff compares two `.kicad_pcb` files and produces a textual diff report, visual PNG overlays, an optional local web viewer, and a RAG-powered chat interface for querying the git history of a KiCad repository in natural language.

---

## Features

- **Semantic diff** — components, nets, routing, ERC, power tree, differential pairs, grounding, impedance, and BOM
- **Visual overlays** — pixel-level before/after diff and annotated component change markers
- **Local web viewer** — Flask backend + React/Vite frontend with an interactive board view and sidebar findings
- **RAG chat layer** — ingests git history, embeds diff summaries into a FAISS vector store, and answers natural-language questions via OpenAI

---

## Installation

**System requirement:** KiCad must be installed and `kicad-cli` must be on your `PATH`.

```bash
pip install fluxdiff
```

Or from source:

```bash
git clone https://github.com/your-org/fluxdiff.git
cd fluxdiff
pip install -e .
```

**Optional dependencies:**

| Package | Purpose |
|---|---|
| `pyyaml` | Stackup config in YAML format (falls back to JSON if absent) |
| `faiss-cpu` | Required for the RAG sub-system |
| `openai` | Required for RAG embeddings and chat completions |

---

## Quick Start

```bash
# Basic diff — prints report to stdout and writes output/ directory
fluxdiff before.kicad_pcb after.kicad_pcb

# With impedance analysis using a stackup config
fluxdiff before.kicad_pcb after.kicad_pcb --stackup stackup.yaml

# Launch the interactive web viewer after diffing
fluxdiff before.kicad_pcb after.kicad_pcb --viewer
```

The web viewer runs on `http://localhost:5000`. When the Vite dev server is running on port 5173 the viewer redirects there automatically.

---

## Output

All files are written to `output/` relative to the working directory.

| File | Description |
|---|---|
| `output/diff_report.txt` | Full text report |
| `output/before.svg` / `output/after.svg` | Board SVGs (viewer backgrounds) |
| `output/diff_overlay.png` | Pixel-level red/green diff image |
| `output/component_diff.png` | Annotated component change markers |

---

## Stackup Configuration

Impedance analysis uses a per-layer stackup config. If no config is provided, a default microstrip layer (0.2 mm dielectric, εr = 4.5, 35 µm copper) is assumed.

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

Pass the file with `--stackup stackup.yaml`. Both YAML and JSON formats are accepted.

---

## RAG Chat Sub-System

The RAG layer ingests a KiCad repository's git history and exposes a chat API for natural-language queries about board changes.

### Setup

```bash
# Required environment variables
export FLUXDIFF_REPO_PATH=/path/to/kicad/repo
export OPENAI_API_KEY=sk-...

# Optional overrides
export FLUXDIFF_EMBEDDING_MODEL=text-embedding-3-small
export FLUXDIFF_LLM_MODEL=gpt-4o-mini
export FLUXDIFF_TOP_K=5
export FLUXDIFF_MEMORY_WINDOW=5
export FLUXDIFF_VECTOR_DB_PATH=./rag_db
```

### Ingestion

```bash
# Ingest the last 50 commits
python -m fluxdiff.rag.ingest.run_ingest --max-commits 50

# Re-index from scratch
python -m fluxdiff.rag.ingest.run_ingest --max-commits 50 --clear

# Include impedance analysis during ingestion
python -m fluxdiff.rag.ingest.run_ingest --stackup stackup.yaml
```

Ingestion is idempotent — documents are deduplicated by SHA-256 content hash, so running it twice produces the same index.

### Chat API

```bash
uvicorn fluxdiff.rag.api:app --host 0.0.0.0 --port 8000 --reload
```

The API runs on port 8000. CORS origins are configurable via `FLUXDIFF_CORS_ORIGINS` (comma-separated; defaults to `localhost:5173,3000,5000`).

#### `POST /chat`

```json
{ "query": "What changed in the last commit?" }
```

```json
{
  "answer": "In the most recent commit...",
  "sources": [
    { "type": "summary", "commit": "a1b2c3d...", "file": "board/main.kicad_pcb" }
  ]
}
```

#### `POST /chat/filtered`

Restrict retrieval to a specific commit, document type, or file:

```json
{
  "query": "Are there any impedance issues?",
  "filters": {
    "commit": "a1b2c3d4e5f6...",
    "type": "impedance",
    "file": "board/main.kicad_pcb"
  }
}
```

All filter keys are optional. Document types: `summary`, `component`, `net`, `routing`, `power_tree`, `diff_pair`, `grounding`, `impedance`, `bom`, `repo_file`.

#### `GET /health`

```json
{ "status": "ok", "documents_indexed": 142 }
```

---

## Architecture

### Core Pipeline

```
Parse → Enrich → Analyse → Diff → Report / Visualise
```

1. **Parse** — S-expression tokenizer builds an AST; `pcb_parser` converts it to typed domain objects (`PCBData`, `Component`, `Net`, `Trace`, `Via`, `Pad`).
2. **Enrich** — Traces are snapped to their nearest pads by net within a 2 mm tolerance.
3. **Graph build** — A connectivity graph maps each net to the set of `(ref, pad_number)` tuples connected to it.
4. **Diff** — `compare_pcbs()` runs all analysis modules and collects `Finding` objects and plain-string change lists into a `DiffResult`.
5. **Report / Visualise** — Text report is printed and written; SVGs and PNGs are exported; the viewer is optionally launched.

### RAG Pipeline

```
Git history → FluxDiff core → Documents → Embeddings → FAISS → Retriever → LLM → FastAPI
```

### Ports

| Service | Port | Purpose |
|---|---|---|
| Flask board viewer | 5000 | `/api/diff`, `/api/board/*` |
| FastAPI RAG chat | 8000 | `/chat`, `/chat/filtered`, `/health` |
| React/Vite dev server | 5173 | Frontend (proxies `/api` to Flask) |

---

## Project Structure

```
fluxdiff/
├── cli/main.py                  # Entry point — orchestrates the full pipeline
├── parser/
│   ├── sexp_parser.py           # S-expression tokenizer and AST builder
│   └── pcb_parser.py            # AST → domain objects (PCBData)
├── models/pcb_models.py         # Dataclasses: Pad, Component, Net, Trace, Via, Finding, DiffResult
├── diff/diff_engine.py          # compare_pcbs() — master diff orchestrator
├── analysis/
│   ├── connectivity_graph.py    # Net connectivity graph build and comparison
│   ├── trace_connectivity.py    # Trace-to-pad snapping (enrich phase)
│   ├── geometry.py              # Pad index, nearest-pad lookup, distance helpers
│   ├── erc_checker.py           # ERC checks (pull-ups, bypass caps, floating nets, power shorts)
│   ├── power_tree.py            # Power rail analysis and tree report
│   ├── diff_pair.py             # Differential pair length, via, and layer asymmetry checks
│   ├── ground_checker.py        # GND island detection, analog/digital mix, ADC proximity
│   └── impedance.py             # Trace impedance vs. target; microstrip and stripline models
├── supply_chain/
│   ├── bom_checker.py           # BOM build and ERP stock lookup
│   └── erp_service.py           # Swappable ERP adapter stub
├── visual/
│   ├── constants.py             # EXPORT_SCALE, PIXELS_PER_MM
│   ├── kicad_export.py          # kicad-cli → SVG/PNG export
│   ├── image_diff.py            # Pixel-level before/after overlay
│   └── component_diff.py        # Component marker and arrow overlay
├── viewer/server.py             # Flask API and SPA server
└── rag/
    ├── config.py                # All RAG tunables (env-driven)
    ├── schemas.py               # RAG dataclasses
    ├── api.py                   # FastAPI app
    ├── ingest/                  # Git loader, diff generator, document builder, ingestion entry point
    ├── embedding/               # OpenAI embedder, FAISS vector store
    ├── retrieval/retriever.py   # Embed query → similarity search → optional metadata filter
    ├── llm/                     # LLM client, prompt templates
    └── chat/                    # ChatEngine, ChatMemory (rolling window)
```

---

## Analysis Modules

### ERC (`erc_checker.py`)

Checks power shorts, missing pull-ups on I2C/open-drain nets, missing bypass caps on IC power pins, under-connected power nets, and floating nets. Returns `list[Finding]` sorted CRITICAL → WARNING → INFO.

Key tunables:
- `BYPASS_CAP_RADIUS_MM = 5.0` — maximum distance from IC power pin to a bypass cap
- `I2C_NET_SUBSTRINGS = ("SDA", "SCL")`
- `OPEN_DRAIN_NET_SUBSTRINGS = ("OD", "INT", "ALERT", "NRST", "RESET", ...)`

### Power Tree (`power_tree.py`)

Classifies components as regulators, connectors, batteries, or IC loads, then builds a rail graph. Reports rail contention (CRITICAL), unused regulator outputs (WARNING), sourceless rails (WARNING), and high load counts (INFO).

### Differential Pairs (`diff_pair.py`)

Detects paired nets by suffix (`_P`/`_N`, `_DP`/`_DN`, `+`/`-`, `_POS`/`_NEG`, `P`/`N`). Checks per pair:
- Length mismatch > 0.5 mm → WARNING
- Via count asymmetry → WARNING / INFO
- Layer asymmetry → WARNING

### Grounding (`ground_checker.py`)

Three checks: GND island detection (CRITICAL if unbridged), analog/digital IC mix on the same GND net (WARNING), and ADC components without a GND reference within 10 mm (WARNING).

### Impedance (`impedance.py`)

Computes microstrip and stripline impedance from trace width and stackup config. Compares against target impedance for critical nets (USB, RF, LVDS, HDMI, Ethernet, MIPI, PCIe). Severity scales with deviation from target tolerance.

### BOM / Supply Chain (`bom_checker.py`)

Groups components by `(value, footprint)`, queries the ERP adapter, and reports out-of-stock (CRITICAL), low-stock (WARNING), or sufficient-stock (INFO) findings. Swap `erp_service.fetch_inventory_from_erp()` with a real ERP client (SAP, NetSuite, REST) without touching any other module.

---

## Extending FluxDiff

### Adding a new analysis module

1. Implement `analyse_X(pcb: PCBData, ...) -> list[Finding]` with deduplication and severity sort.
2. Add `X_changes: List[str]` and `X_findings: List[Finding]` to `DiffResult` in `pcb_models.py`.
3. Call it in `compare_pcbs()` via `_tag(set(X_old), set(X_new), result.X_changes, result.X_findings)`.
4. Add a section to `_print_report()` / `_write_report()` in `main.py`.
5. Add the findings key to `/api/diff` in `server.py`.
6. Add the new field to `DiffSummary` in `rag/schemas.py` and map it in `DiffGenerator` and `DocumentBuilder`.

### Swapping the vector store

Replace `VectorStore` in `rag/embedding/vector_store.py`. Keep the same interface: `add_documents(docs, embeddings)`, `similarity_search(vec, top_k)`, `clear()`. `Retriever` and `ChatEngine` need no changes.

### Swapping the LLM

Replace the body of `LLMClient.generate_response(prompt: str) -> str` in `rag/llm/llm_client.py`. Update `RAG_CONFIG["llm_model"]`. `ChatEngine` needs no changes.

### Swapping the embedding model

Replace `Embedder.embed_documents()` and `embed_query()` in `rag/embedding/embedder.py`. Keep return types `List[List[float]]` and `List[float]`. If the embedding dimension changes, run `--clear` before re-ingesting — the FAISS index dimension is fixed at creation time.

### Persisting chat memory across restarts

Replace `ChatMemory` in `rag/chat/memory.py` with a Redis- or DB-backed implementation. Keep the interface: `add(user, assistant)`, `get_context() -> str`, `clear()`. `ChatEngine` needs no changes.

---

## Dependencies

| Package | Purpose |
|---|---|
| `click` | CLI argument parsing |
| `flask` + `flask-cors` | Web viewer API |
| `fastapi` + `uvicorn` + `pydantic` | RAG chat API |
| `openai` | Embeddings and chat completions |
| `faiss-cpu` | Vector similarity search |
| `numpy` | Array operations |
| `opencv-python` (`cv2`) | Image processing |
| `cairosvg` | SVG → PNG conversion |
| `python-dotenv` | `.env` file loading |
| `pyyaml` *(optional)* | YAML stackup config |
| `kicad-cli` *(system)* | PCB export to SVG/PNG |

---

## License

MIT