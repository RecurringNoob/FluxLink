import os
import click

from fluxdiff.parser.pcb_parser import parse_pcb
from fluxdiff.diff.diff_engine import compare_pcbs
from fluxdiff.visual.kicad_export import export_pcb_svg, export_pcb_png
from fluxdiff.visual.image_diff import generate_visual_diff
from fluxdiff.visual.component_diff import generate_component_visual_diff
from fluxdiff.analysis.power_tree import analyse_power_tree, format_power_tree_report


@click.command()
@click.argument("before_file", type=click.Path(exists=True))
@click.argument("after_file",  type=click.Path(exists=True))
@click.option("--viewer", is_flag=True, default=False,
              help="Open PCB diff viewer after diffing")
@click.option(
    "--stackup",
    default=None,
    type=click.Path(),
    help="Path to YAML/JSON stack-up config for impedance analysis (optional)",
)
def main(before_file, after_file, viewer, stackup):
    """
    Compare two KiCad PCB files and generate a semantic + visual diff.

    Example:
        python -m fluxdiff.cli.main before.kicad_pcb after.kicad_pcb
        python -m fluxdiff.cli.main before.kicad_pcb after.kicad_pcb --stackup stackup.yaml
    """

    # 1. Parse
    before_pcb = parse_pcb(before_file)
    after_pcb  = parse_pcb(after_file)

    print(f"Before: {len(before_pcb.components)} components, "
          f"{len(before_pcb.traces)} traces, {len(before_pcb.vias)} vias")
    print(f"After:  {len(after_pcb.components)} components, "
          f"{len(after_pcb.traces)} traces, {len(after_pcb.vias)} vias")

    if stackup:
        stackup_abs = os.path.abspath(stackup)
        if os.path.isfile(stackup_abs):
            print(f"[INFO] Stack-up config loaded: {stackup_abs}")
        else:
            print(f"[WARNING] Stack-up config not found: {stackup_abs} "
                  f"— impedance check will use heuristics")

    if len(before_pcb.components) == 0 and len(after_pcb.components) == 0:
        print(
            "\n[WARNING] No named components found in either file.\n"
            "  This usually means all footprints still have 'REF**' as their\n"
            "  Reference designator (i.e. they haven't been annotated yet).\n"
            "  Component and net diffs will be empty. Trace diff will still run.\n"
        )

    # 2. Diff (enrich, graph build, all analysis inside compare_pcbs)
    diff_report = compare_pcbs(before_pcb, after_pcb, stackup_config=stackup)

    # 3. Power tree report — reuse graph_new stored on DiffResult (B10 FIX)
    after_power_tree, _ = analyse_power_tree(after_pcb, diff_report.graph_new)
    power_tree_report   = format_power_tree_report(after_power_tree)

    # 4. Output directory
    output_dir = "output"
    os.makedirs(output_dir, exist_ok=True)

    before_svg = os.path.join(output_dir, "before.svg")
    after_svg  = os.path.join(output_dir, "after.svg")
    before_png = os.path.join(output_dir, "before.png")
    after_png  = os.path.join(output_dir, "after.png")
    diff_png   = os.path.join(output_dir, "diff_overlay.png")
    component_diff_png = os.path.join(output_dir, "component_diff.png")

    # 5. Export SVGs (primary: viewer board backgrounds)
    #    Export PNGs (secondary: inputs for image_diff pixel comparison)
    svgs_ok = False
    pngs_ok = False

    try:
        export_pcb_svg(before_file, before_svg)
        export_pcb_svg(after_file,  after_svg)
        svgs_ok = True
    except Exception as e:
        print(f"\n[WARNING] PCB SVG export failed: {e}")

    try:
        export_pcb_png(before_file, before_png)
        export_pcb_png(after_file,  after_png)
        pngs_ok = True
    except Exception as e:
        print(f"\n[WARNING] PCB PNG export failed: {e}")

    # 6. Pixel diff overlay (unchanged — still uses PNGs)
    if pngs_ok:
        try:
            generate_visual_diff(before_png, after_png, diff_png)
            print(f"Visual diff generated: {diff_png}")
        except Exception as e:
            print(f"[WARNING] Pixel diff skipped: {e}")

        all_components = before_pcb.components + after_pcb.components
        if all_components:
            try:
                generate_component_visual_diff(
                    before_png, after_png,
                    before_pcb.components, after_pcb.components,
                    component_diff_png,
                )
                print(f"Component diff generated: {component_diff_png}")
            except Exception as e:
                print(f"[WARNING] Component visual diff skipped: {e}")
        else:
            print("[INFO] Component visual diff skipped: no annotated components found.")

    # 7. Text report
    _print_report(diff_report, power_tree_report)

    diff_report_path = os.path.join(output_dir, "diff_report.txt")
    _write_report(diff_report_path, diff_report, power_tree_report)
    print(f"\nReport written to: {diff_report_path}")

    # 8. Viewer
    if viewer:
        try:
            from fluxdiff.viewer.server import run_viewer_server
            print("\nStarting Flask API on http://localhost:5000")
            print("Opening React viewer at http://localhost:5173")
            print("(Run 'npm run dev' in fluxdiff/viewer/frontend/ if not already running)")
            run_viewer_server(diff_report, power_tree_report)
        except Exception as e:
            print(f"[ERROR] Could not launch viewer: {e}")


# ---------------------------------------------------------------------------
# Report helpers (unchanged)
# ---------------------------------------------------------------------------

def _section(title, items, empty_msg="  No changes."):
    lines = [f"\n=== {title} ({len(items)}) ==="]
    if items:
        lines.extend(f" - {item}" for item in items)
    else:
        lines.append(empty_msg)
    return lines


def _print_report(diff_report, power_tree_report):
    print("\n" + "=" * 50)
    print("PCB DIFF REPORT")
    print("=" * 50)
    for line in _section("COMPONENT CHANGES", diff_report.component_changes):
        print(line)
    for line in _section("NET CHANGES", diff_report.net_changes):
        print(line)
    for line in _section("ROUTING CHANGES", diff_report.routing_changes):
        print(line)
    for line in _section("POWER TREE", diff_report.power_tree_changes,
                          "  No new power tree issues."):
        print(line)
    print("\n  Power hierarchy (after board):")
    print(power_tree_report)
    for line in _section("DIFFERENTIAL PAIRS", diff_report.diff_pair_changes,
                          "  No diff pair issues."):
        print(line)
    for line in _section("GROUNDING", diff_report.ground_changes,
                          "  No grounding issues."):
        print(line)
    for line in _section("IMPEDANCE", diff_report.impedance_changes,
                          "  No impedance issues."):
        print(line)
    for line in _section("SUPPLY CHAIN / BOM", diff_report.bom_changes,
                          "  No supply chain issues."):
        print(line)
    print(f"\n=== SUMMARY ===")
    print(diff_report.summary)


def _write_report(path, diff_report, power_tree_report):
    with open(path, "w", encoding="utf-8") as f:
        f.write("PCB DIFF REPORT\n\n")
        sections = [
            ("COMPONENT CHANGES", diff_report.component_changes, "  No changes."),
            ("NET CHANGES",       diff_report.net_changes,       "  No changes."),
            ("ROUTING CHANGES",   diff_report.routing_changes,   "  No changes."),
        ]
        for title, items, empty in sections:
            f.write(f"=== {title} ({len(items)}) ===\n")
            f.write("\n".join(f"- {i}" for i in items) if items else empty)
            f.write("\n\n")

        f.write(f"=== POWER TREE ({len(diff_report.power_tree_changes)} new issues) ===\n")
        f.write("\n".join(f"- {i}" for i in diff_report.power_tree_changes)
                if diff_report.power_tree_changes else "  No new power tree issues.")
        f.write("\n\n  Power hierarchy (after board):\n")
        f.write(power_tree_report + "\n\n")

        for title, items, empty in [
            ("DIFFERENTIAL PAIRS", diff_report.diff_pair_changes, "  No diff pair issues."),
            ("GROUNDING",          diff_report.ground_changes,    "  No grounding issues."),
            ("IMPEDANCE",          diff_report.impedance_changes,  "  No impedance issues."),
            ("SUPPLY CHAIN / BOM", diff_report.bom_changes,       "  No supply chain issues."),
        ]:
            f.write(f"=== {title} ({len(items)}) ===\n")
            f.write("\n".join(f"- {i}" for i in items) if items else empty)
            f.write("\n\n")

        f.write("=== SUMMARY ===\n")
        f.write(diff_report.summary + "\n")


if __name__ == "__main__":
    main()