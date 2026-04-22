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

**System requirement:** KiCad must be installed and `kicad-cli` must be on your `PATH`.

```bash
pip install fluxlink
```

Or from source:

```bash
git clone https://github.com/your-org/fluxlink.git
cd fluxlink
pip install -e .
```

**Optional dependencies:**

| Package | Purpose |
|---|---|
| `pyyaml` | Stackup config in YAML format (falls back to JSON if absent) |
| `faiss-cpu` | Required for the chat / RAG sub-system |
| `openai` | Required for embeddings and chat completions |

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

The board viewer runs at `http://localhost:5000`. When the Vite dev server is running on port 5173, the viewer redirects there automatically.

### Ask questions about board history

```bash
# 1. Set credentials
export FLUXDIFF_REPO_PATH=/path/to/kicad/repo
export OPENAI_API_KEY=sk-...

# 2. Ingest git history into the vector store
python -m fluxlink.rag.ingest.run_ingest --max-commits 50

# 3. Start the chat API
uvicorn fluxlink.rag.api:app --host 0.0.0.0 --port 8000 --reload

# 4. Open the chat UI at /chat in the board viewer
```

---

## Output Files

All files are written to `output/` in the working directory.

| File | Description |
|---|---|
| `output/diff_report.txt` | Full text diff report |
| `output/before.svg` / `output/after.svg` | Board SVGs (viewer backgrounds) |
| `output/diff_overlay.png` | Pixel-level red/green diff image |
| `output/component_diff.png` | Annotated component change markers |

---

## Board Viewer

The viewer is a React/Vite frontend served by Flask. Three view modes:

- **Side by side** — before and after boards rendered simultaneously, findings markers on the after board
- **Toggle** — flip between before and after with a single key
- **Overlay** — pixel-level diff image

The **findings sidebar** lists all issues grouped by category (ERC, POWER, DIFF_PAIR, GROUND, IMPEDANCE, BOM, COMPONENT) with severity badges (CRITICAL / WARNING / INFO), free-text search, and severity filtering. Clicking any finding or component change pans and zooms the board to that location.

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

Every answer cites the specific commit ID and file it was sourced from. A collapsible sources panel appears below each assistant message.

### Chat API

```
POST /chat
Body:    { "query": "What changed in the last commit?" }
Returns: { "answer": "...", "sources": [{ "commit_id": "a1b2c3d", "file_name": "board/main.kicad_pcb" }] }
```

#### Filtered retrieval

Restrict retrieval to a specific commit, document type, or file:

```json
POST /chat/filtered
{
  "query": "Are there impedance issues?",
  "filters": {
    "commit": "a1b2c3d4e5f6",
    "type": "impedance",
    "file": "board/main.kicad_pcb"
  }
}
```

All filter keys are optional. Supported document types: `summary`, `component`, `net`, `routing`, `power_tree`, `diff_pair`, `grounding`, `impedance`, `bom`, `repo_file`.

#### Health check

```
GET /health
→ { "status": "ok", "documents_indexed": 142 }
```

---

## Ingestion

```bash
# Index the last 50 commits
python -m fluxlink.rag.ingest.run_ingest --max-commits 50

# Re-index from scratch
python -m fluxlink.rag.ingest.run_ingest --max-commits 50 --clear

# Include impedance analysis during ingestion
python -m fluxlink.rag.ingest.run_ingest --stackup stackup.yaml
```

Ingestion is idempotent — documents are deduplicated by SHA-256 content hash. Running it twice produces the same index.

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

Pass with `--stackup stackup.yaml`. Both YAML and JSON are accepted.

---

## Analysis Modules

### ERC
Power shorts, missing pull-ups on I2C/open-drain nets, missing bypass caps within 5 mm of IC power pins, under-connected power nets, and floating nets.

### Power Tree
Classifies regulators, connectors, batteries, and IC loads into a rail graph. Reports rail contention (CRITICAL), unused regulator outputs (WARNING), and sourceless rails (WARNING).

### Differential Pairs
Detects paired nets by suffix (`_P`/`_N`, `+`/`−`, `_DP`/`_DN`, etc.). Checks length mismatch > 0.5 mm, via count asymmetry, and layer asymmetry per pair.

### Grounding
GND island detection, analog/digital IC mix on a shared GND net, and ADC components without a GND reference within 10 mm.

### Impedance
Microstrip and stripline impedance from trace width and stackup config, compared against targets for critical net types (USB, RF, LVDS, HDMI, Ethernet, MIPI, PCIe).

### BOM / Supply Chain
Groups components by `(value, footprint)`, queries the ERP adapter, and reports out-of-stock (CRITICAL), low-stock (WARNING), or sufficient-stock (INFO). Swap `erp_service.fetch_inventory_from_erp()` with any real ERP client without touching other modules.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FLUXDIFF_REPO_PATH` | *(required)* | Path to the KiCad git repository |
| `OPENAI_API_KEY` | *(required)* | OpenAI API key |
| `FLUXDIFF_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model |
| `FLUXDIFF_LLM_MODEL` | `gpt-4o-mini` | Chat completion model |
| `FLUXDIFF_TOP_K` | `5` | Documents retrieved per query |
| `FLUXDIFF_MEMORY_WINDOW` | `5` | Conversation turns kept in context |
| `FLUXDIFF_VECTOR_DB_PATH` | `./rag_db` | FAISS index storage path |
| `FLUXDIFF_CORS_ORIGINS` | `localhost:5173,3000,5000` | Allowed CORS origins (comma-separated) |

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
fluxlink/
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
Replace `ChatMemory` in `rag/chat/memory.py` with a Redis- or DB-backed implementation. Keep the interface: `add(user, assistant)`, `get_context() -> str`, `clear()`.

---

## Dependencies

| Package | Purpose |
|---|---|
| `click` | CLI argument parsing |
| `flask` + `flask-cors` | Board viewer API |
| `fastapi` + `uvicorn` + `pydantic` | RAG chat API |
| `openai` | Embeddings and chat completions |
| `faiss-cpu` | Vector similarity search |
| `numpy` | Array operations |
| `opencv-python` | Image processing |
| `cairosvg` | SVG → PNG conversion |
| `python-dotenv` | `.env` file loading |
| `pyyaml` *(optional)* | YAML stackup config |
| `kicad-cli` *(system)* | PCB export to SVG/PNG |

---

## License

MIT