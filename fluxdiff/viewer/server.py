from flask import Flask, send_from_directory, render_template_string, jsonify
import os
import webbrowser
import threading

app = Flask(__name__)

OUTPUT_DIR = os.path.abspath("output")

ALL_IMAGE_FILENAMES = [
    "before.png",
    "after.png",
    "diff_overlay.png",
    "component_diff.png",
]


# ---------- API ----------
@app.route("/api/diff")
def get_diff():
    diff_result = app.config.get("DIFF_RESULT")
    if diff_result is None:
        return jsonify({"error": "No diff available"}), 404

    return jsonify({
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
    })


# ---------- UI ----------
@app.route("/")
def index():
    available_images = [
        f for f in ALL_IMAGE_FILENAMES
        if os.path.isfile(os.path.join(OUTPUT_DIR, f))
    ]

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>FluxDiff Viewer</title>
        <style>
            * { box-sizing: border-box; }
            body { font-family: Arial, sans-serif; margin: 0; background: #f4f6f8; }
            .container { display: flex; height: 100vh; }

            .images { width: 60%; padding: 20px; overflow-y: scroll; background: #f9f9f9; }
            .img-block { margin-bottom: 20px; text-align: center; }
            img { max-width: 100%; border: 1px solid #ccc; border-radius: 6px; }

            .panel { width: 40%; padding: 20px; border-left: 2px solid #ddd; overflow-y: scroll; background: white; }
            h2 { margin-top: 0; }

            .summary-box {
                background: #eef1f4; padding: 12px; border-radius: 8px;
                margin-bottom: 16px; font-size: 13px; white-space: pre-wrap;
                font-family: monospace;
            }
            .counts {
                display: flex; flex-wrap: wrap; gap: 8px;
                margin-bottom: 16px;
            }
            .count-chip {
                background: #f0f0f0; border-radius: 12px;
                padding: 4px 10px; font-size: 12px; font-weight: bold;
            }
            .count-chip.has-issues { background: #fff3cd; }
            .count-chip.has-critical { background: #fde8e8; }

            .section { margin-bottom: 20px; border: 1px solid #e8e8e8; border-radius: 8px; overflow: hidden; }
            .section-header {
                cursor: pointer; padding: 10px 14px;
                background: #f8f9fa; font-weight: bold; font-size: 14px;
                display: flex; justify-content: space-between; align-items: center;
                user-select: none;
            }
            .section-header:hover { background: #e9ecef; }
            .section-body { padding: 10px; display: none; }
            .section-body.open { display: block; }

            .card { padding: 9px 12px; border-radius: 6px; margin-bottom: 6px; font-size: 13px; }
            .card:hover { opacity: 0.85; }
            .critical { background: #fde8e8; color: #c0392b; font-weight: bold; }
            .warning  { background: #fff3cd; color: #856404; }
            .info     { background: #e8f4e8; color: #276227; }
            .neutral  { background: #f8f9fa; color: #333; }

            .badge {
                font-size: 11px; padding: 2px 7px; border-radius: 10px;
                background: #6c63ff; color: white; margin-left: 8px;
            }
            .badge.warn  { background: #e67e22; }
            .badge.crit  { background: #c0392b; }
            .badge.ok    { background: #27ae60; }

            .mono-block {
                background: #f0f4f8; border-radius: 6px; padding: 10px;
                font-family: monospace; font-size: 12px;
                white-space: pre-wrap; border-left: 3px solid #6c63ff;
                margin-top: 8px;
            }
        </style>
    </head>
    <body>
    <div class="container">

        <div class="images">
            {% for filename in image_filenames %}
            <div class="img-block">
                <h3>{{ filename }}</h3>
                <img src="/images/{{ filename }}" alt="{{ filename }}">
            </div>
            {% endfor %}
        </div>

        <div class="panel">
            <h2>🔍 FluxDiff Report</h2>
            <div class="summary-box" id="summaryBox">Loading…</div>
            <div class="counts" id="counts"></div>

            <!-- Sections rendered by JS -->
            <div id="sections"></div>
        </div>
    </div>

    <script>
    function getClass(text) {
        if (!text) return "neutral";
        if (text.includes("CRITICAL")) return "critical";
        if (text.includes("WARNING"))  return "warning";
        if (text.includes("INFO"))     return "info";
        return "neutral";
    }

    function badgeClass(items) {
        if (!items || items.length === 0) return "ok";
        if (items.some(i => i.includes("CRITICAL"))) return "crit";
        if (items.some(i => i.includes("WARNING")))  return "warn";
        return "";
    }

    function formatItem(text) {
        if (text.includes("CRITICAL")) return "🔴 " + text;
        if (text.includes("WARNING"))  return "🟡 " + text;
        if (text.includes("INFO"))     return "🔵 " + text;
        if (text.includes("Component moved"))         return "📍 " + text;
        if (text.includes("Component value changed")) return "🔧 " + text;
        if (text.includes("Trace added"))             return "➕ " + text;
        if (text.includes("Trace removed"))           return "➖ " + text;
        if (text.includes("Out of stock"))            return "🚫 " + text;
        if (text.includes("Low stock"))               return "⚠️ " + text;
        if (text.includes("In stock"))                return "✅ " + text;
        return text;
    }

    function makeSection(emoji, title, items, extraHtml) {
        const bc = badgeClass(items);
        const count = items ? items.length : 0;
        const id = "sec_" + title.replace(/\s+/g, "_");

        let cards = "";
        if (!items || items.length === 0) {
            cards = `<div class="card info">No issues</div>`;
        } else {
            cards = items.map(i =>
                `<div class="card ${getClass(i)}">${formatItem(i)}</div>`
            ).join("");
        }

        return `
        <div class="section">
            <div class="section-header" onclick="toggleSection('${id}')">
                <span>${emoji} ${title}</span>
                <span class="badge ${bc}">${count}</span>
            </div>
            <div class="section-body" id="${id}">
                ${cards}
                ${extraHtml || ""}
            </div>
        </div>`;
    }

    function toggleSection(id) {
        const el = document.getElementById(id);
        el.classList.toggle("open");
    }

    function chipClass(items) {
        if (!items || items.length === 0) return "";
        if (items.some(i => i.includes("CRITICAL"))) return "has-critical";
        return "has-issues";
    }

    fetch("/api/diff")
        .then(r => { if (!r.ok) throw new Error("Diff not available"); return r.json(); })
        .then(data => {
            document.getElementById("summaryBox").innerText = data.summary || "No summary.";

            // Count chips
            const chips = [
                ["🔧", "Components", data.components],
                ["⚡", "Nets/ERC",   data.nets],
                ["🛣",  "Routing",    data.routing],
                ["🔋", "Power",      data.power_tree],
                ["〰", "Diff Pairs", data.diff_pairs],
                ["⏚",  "Grounding",  data.grounding],
                ["Ω",  "Impedance",  data.impedance],
                ["📦", "BOM",        data.bom],
            ];
            document.getElementById("counts").innerHTML = chips.map(([e, label, items]) =>
                `<div class="count-chip ${chipClass(items)}">${e} ${label}: ${(items||[]).length}</div>`
            ).join("");

            // Power tree extra block
            const ptExtra = data.power_tree_report
                ? `<div style="margin-top:10px;"><strong style="font-size:12px;">Hierarchy (after board)</strong>
                   <div class="mono-block">${data.power_tree_report}</div></div>`
                : "";

            // Build all sections
            const sections = document.getElementById("sections");
            sections.innerHTML = [
                makeSection("🔧", "Component Changes", data.components),
                makeSection("⚡", "Nets / ERC",        data.nets),
                makeSection("🛣",  "Routing",           data.routing),
                makeSection("🔋", "Power Tree",        data.power_tree, ptExtra),
                makeSection("〰", "Differential Pairs",data.diff_pairs),
                makeSection("⏚",  "Grounding",         data.grounding),
                makeSection("Ω",  "Impedance",         data.impedance),
                makeSection("📦", "Supply Chain / BOM",data.bom),
            ].join("");

            // Auto-open sections with issues
            ["Component_Changes","Nets_/_ERC","Power_Tree","Differential_Pairs","Grounding","Impedance","Supply_Chain_/_BOM"]
                .forEach(id => {
                    const el = document.getElementById("sec_" + id);
                    if (el) {
                        const cards = el.querySelectorAll(".critical, .warning");
                        if (cards.length > 0) el.classList.add("open");
                    }
                });
        })
        .catch(err => {
            document.getElementById("summaryBox").innerText = "Error: " + err.message;
        });
    </script>
    </body>
    </html>
    """
    return render_template_string(html, image_filenames=available_images)


# ---------- IMAGE ROUTE ----------
@app.route("/images/<path:filename>")
def images(filename):
    return send_from_directory(OUTPUT_DIR, filename)


# ---------- SERVER ----------
def run_viewer_server(diff_result=None, power_tree_report=""):
    app.config["DIFF_RESULT"] = diff_result
    app.config["POWER_TREE_REPORT"] = power_tree_report
    threading.Timer(1, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="localhost", port=5000)


if __name__ == "__main__":
    run_viewer_server()