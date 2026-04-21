import os
import click

from fluxdiff.parser.pcb_parser import parse_pcb
from fluxdiff.diff.diff_engine import compare_pcbs
from fluxdiff.visual.kicad_export import export_pcb_png
from fluxdiff.visual.image_diff import generate_visual_diff
from fluxdiff.visual.component_diff import generate_component_visual_diff
from fluxdiff.analysis.power_tree import analyse_power_tree, format_power_tree_report


@click.command()
@click.argument("before_file", type=click.Path(exists=True))
@click.argument("after_file", type=click.Path(exists=True))
@click.option("--viewer", is_flag=True, default=False, help="Open PCB diff viewer after diffing")
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

    # 1. Parse both PCBs
    before_pcb = parse_pcb(before_file)
    after_pcb = parse_pcb(after_file)

    print(f"Before: {len(before_pcb.components)} components, "
          f"{len(before_pcb.traces)} traces, {len(before_pcb.vias)} vias")
    print(f"After:  {len(after_pcb.components)} components, "
          f"{len(after_pcb.traces)} traces, {len(after_pcb.vias)} vias")

    # B12 FIX: resolve to abspath before the isfile check so the result is
    # consistent with what diff_engine.compare_pcbs (which also calls
    # os.path.abspath) will see. Previously, main.py checked the raw relative
    # path while diff_engine resolved it — they could disagree if the process
    # cwd differed from the invocation directory, producing a spurious
    # "[WARNING] Stack-up config not found" even when the file loaded fine.
    if stackup:
        stackup_abs = os.path.abspath(stackup)
        if os.path.isfile(stackup_abs):
            print(f"[INFO] Stack-up config loaded: {stackup_abs}")
        else:
            print(f"[WARNING] Stack-up config not found: {stackup_abs} — impedance check will use heuristics")

    if len(before_pcb.components) == 0 and len(after_pcb.components) == 0:
        print(
            "\n[WARNING] No named components found in either file.\n"
            "  This usually means all footprints still have 'REF**' as their\n"
            "  Reference designator (i.e. they haven't been annotated yet).\n"
            "  Component and net diffs will be empty. Trace diff will still run.\n"
        )

    # 2. Compute differences (ERC, power tree, diff pair, ground, impedance,
    #    and supply chain / BOM all run inside compare_pcbs)
    diff_report = compare_pcbs(before_pcb, after_pcb, stackup_config=stackup)

    # 3. Build power tree hierarchy string for after board.
    #    B10 FIX: reuse graph_new from diff_report — compare_pcbs already built
    #    and stored it. No need to re-enrich traces or rebuild the graph here.
    after_power_tree, _ = analyse_power_tree(after_pcb, diff_report.graph_new)
    power_tree_report = format_power_tree_report(after_power_tree)

    # 4. Create output directory
    output_dir = "output"
    os.makedirs(output_dir, exist_ok=True)

    before_png = os.path.join(output_dir, "before.png")
    after_png  = os.path.join(output_dir, "after.png")
    diff_png   = os.path.join(output_dir, "diff_overlay.png")
    component_diff_png = os.path.join(output_dir, "component_diff.png")

    # 5. Export PCB images
    images_ok = False
    try:
        export_pcb_png(before_file, before_png)
        export_pcb_png(after_file, after_png)
        images_ok = True
    except Exception as e:
        print(f"\n[WARNING] PCB image export failed: {e}")

    # 6. Visual diffs
    if images_ok:
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

    # 7. Print semantic diff report
    _print_report(diff_report, power_tree_report)

    # 8. Write report to file
    diff_report_path = os.path.join(output_dir, "diff_report.txt")
    _write_report(diff_report_path, diff_report, power_tree_report)
    print(f"\nReport written to: {diff_report_path}")

    # 9. Launch viewer
    if viewer:
        try:
            from fluxdiff.viewer.server import run_viewer_server
            print("\nOpening PCB diff viewer at http://localhost:5000")
            run_viewer_server(diff_report, power_tree_report)
        except Exception as e:
            print(f"[ERROR] Could not launch viewer: {e}")


# ---------------------------------------------------------------------------
# Report helpers — separated so _write_report and _print_report stay in sync
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
    for line in _section("POWER TREE", diff_report.power_tree_changes, "  No new power tree issues."):
        print(line)
    print("\n  Power hierarchy (after board):")
    print(power_tree_report)
    for line in _section("DIFFERENTIAL PAIRS", diff_report.diff_pair_changes, "  No diff pair issues."):
        print(line)
    for line in _section("GROUNDING", diff_report.ground_changes, "  No grounding issues."):
        print(line)
    for line in _section("IMPEDANCE", diff_report.impedance_changes, "  No impedance issues."):
        print(line)
    for line in _section("SUPPLY CHAIN / BOM", diff_report.bom_changes, "  No supply chain issues."):
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

        f.write(f"=== DIFFERENTIAL PAIRS ({len(diff_report.diff_pair_changes)}) ===\n")
        f.write("\n".join(f"- {i}" for i in diff_report.diff_pair_changes)
                if diff_report.diff_pair_changes else "  No diff pair issues.")
        f.write("\n\n")

        f.write(f"=== GROUNDING ({len(diff_report.ground_changes)}) ===\n")
        f.write("\n".join(f"- {i}" for i in diff_report.ground_changes)
                if diff_report.ground_changes else "  No grounding issues.")
        f.write("\n\n")

        f.write(f"=== IMPEDANCE ({len(diff_report.impedance_changes)}) ===\n")
        f.write("\n".join(f"- {i}" for i in diff_report.impedance_changes)
                if diff_report.impedance_changes else "  No impedance issues.")
        f.write("\n\n")

        f.write(f"=== SUPPLY CHAIN / BOM ({len(diff_report.bom_changes)}) ===\n")
        f.write("\n".join(f"- {i}" for i in diff_report.bom_changes)
                if diff_report.bom_changes else "  No supply chain issues.")
        f.write("\n\n")

        f.write("=== SUMMARY ===\n")
        f.write(diff_report.summary + "\n")


if __name__ == "__main__":
    main()