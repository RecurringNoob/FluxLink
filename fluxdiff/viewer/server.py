"""
Flask viewer server for FluxDiff.

New endpoints added (existing endpoints unchanged):
  GET /api/board/before   — serves before.svg for the React board viewer
  GET /api/board/after    — serves after.svg for the React board viewer
  GET /api/board/bounds   — serves board bounding box in mm + pixel constants

/api/diff now includes structured findings (list[dict]) alongside the
existing plain-string lists, so the React viewer can access coordinates
and related_refs without changing the text report format.

CORS headers are added for the Vite dev server (localhost:5173).
"""

from flask import Flask, send_from_directory, jsonify, send_file
from flask_cors import CORS
import os
import webbrowser
import threading

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": [
    "http://localhost:5173",   # Vite dev server
    "http://localhost:3000",   # CRA fallback
]}})

OUTPUT_DIR = os.path.abspath("output")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _findings_to_json(findings):
    """Convert list[Finding] to list[dict] for JSON serialisation."""
    return [f.to_dict() for f in findings]


# ---------------------------------------------------------------------------
# Existing /api/diff — extended with structured findings
# ---------------------------------------------------------------------------

@app.route("/api/diff")
def get_diff():
    diff_result = app.config.get("DIFF_RESULT")
    if diff_result is None:
        return jsonify({"error": "No diff available"}), 404

    return jsonify({
        # ---- existing plain-string lists (text report, unchanged) ----
        "components":        diff_result.component_changes,
        "nets":              diff_result.net_changes,
        "routing":           diff_result.routing_changes,
        "power_tree":        diff_result.power_tree_changes,
        "power_tree_report": app.config.get("POWER_TREE_REPORT", ""),
        "diff_pairs":        diff_result.diff_pair_changes,
        "grounding":         diff_result.ground_changes,
        "impedance":         diff_result.impedance_changes,
        "bom":               diff_result.bom_changes,
        "summary":           diff_result.summary,

        # ---- NEW: structured findings for the React viewer ----
        "findings": {
            "erc":       _findings_to_json(diff_result.erc_findings),
            "power":     _findings_to_json(diff_result.power_tree_findings),
            "diff_pair": _findings_to_json(diff_result.diff_pair_findings),
            "ground":    _findings_to_json(diff_result.ground_findings),
            "impedance": _findings_to_json(diff_result.impedance_findings),
            "bom":       _findings_to_json(diff_result.bom_findings),
        },

        # ---- NEW: board bounds for coordinate mapping ----
        "board_bounds": diff_result.board_bounds,
    })


# ---------------------------------------------------------------------------
# NEW: board SVG endpoints
# ---------------------------------------------------------------------------

@app.route("/api/board/before")
def board_before():
    """Serve the before-board SVG for the React viewer background layer."""
    svg_path = os.path.join(OUTPUT_DIR, "before.svg")
    if not os.path.isfile(svg_path):
        return jsonify({"error": "before.svg not found — run pipeline first"}), 404
    return send_file(svg_path, mimetype="image/svg+xml")


@app.route("/api/board/after")
def board_after():
    """Serve the after-board SVG for the React viewer background layer."""
    svg_path = os.path.join(OUTPUT_DIR, "after.svg")
    if not os.path.isfile(svg_path):
        return jsonify({"error": "after.svg not found — run pipeline first"}), 404
    return send_file(svg_path, mimetype="image/svg+xml")


@app.route("/api/board/diff-overlay")
def board_diff_overlay():
    """Serve the pixel diff PNG for the overlay view mode."""
    png_path = os.path.join(OUTPUT_DIR, "diff_overlay.png")
    if not os.path.isfile(png_path):
        return jsonify({"error": "diff_overlay.png not found"}), 404
    return send_file(png_path, mimetype="image/png")


@app.route("/api/board/bounds")
def board_bounds():
    """
    Serve the board bounding box in mm and the pixel-per-mm constants.

    The React viewer uses this to map KiCad mm coordinates to overlay pixels:
      pixel_x = (kicad_x - min_x) * px_per_mm
      pixel_y = (kicad_y - min_y) * px_per_mm

    px_per_mm here refers to the SVG's internal coordinate space, not screen
    pixels — the SVG scales with the zoom container so the overlay div must
    scale identically.  The viewer reads the SVG's viewBox at runtime and
    derives the final scale factor from (viewBox_width / board_width_mm).
    """
    diff_result = app.config.get("DIFF_RESULT")
    bounds = diff_result.board_bounds if diff_result else None
    if not bounds:
        return jsonify({"error": "Board bounds not available"}), 404
    return jsonify(bounds)


# ---------------------------------------------------------------------------
# Existing image route (unchanged — still serves diff_overlay.png etc.)
# ---------------------------------------------------------------------------

@app.route("/images/<path:filename>")
def images(filename):
    return send_from_directory(OUTPUT_DIR, filename)


# ---------------------------------------------------------------------------
# Root — in production, serve the Vite build; in dev, redirect to Vite
# ---------------------------------------------------------------------------

FRONTEND_BUILD = os.path.join(os.path.dirname(__file__), "frontend", "dist")


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    """
    Production: serve the Vite build from viewer/frontend/dist/.
    Dev: the Vite dev server runs on :5173 and proxies /api → :5000,
         so this route is only hit for non-API paths in production.
    """
    if os.path.isdir(FRONTEND_BUILD):
        target = os.path.join(FRONTEND_BUILD, path)
        if path and os.path.isfile(target):
            return send_from_directory(FRONTEND_BUILD, path)
        return send_from_directory(FRONTEND_BUILD, "index.html")
    # Dev fallback — tell the user to open Vite instead
    return (
        "<h2>FluxDiff API running on :5000</h2>"
        "<p>Open <a href='http://localhost:5173'>http://localhost:5173</a> "
        "for the React viewer (Vite dev server).</p>",
        200,
    )


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_viewer_server(diff_result=None, power_tree_report=""):
    app.config["DIFF_RESULT"]        = diff_result
    app.config["POWER_TREE_REPORT"]  = power_tree_report
    threading.Timer(
        1, lambda: webbrowser.open("http://localhost:5173")
    ).start()
    app.run(host="localhost", port=5000, use_reloader=False)


if __name__ == "__main__":
    run_viewer_server()