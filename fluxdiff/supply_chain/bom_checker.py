"""
fluxdiff/supply_chain/bom_checker.py

Extracts a Bill-of-Materials from a PCBData object, queries the ERP for each
unique component value, and returns findings in the standard FluxDiff format:

    "CRITICAL: Out of stock: C100nF (need 12, have 0)"
    "WARNING:  Low stock: R10k (need 8, have 3)"
    "INFO:     In stock: U STM32F4 (need 2, have 50)"

These strings are consumed by diff_engine._tag() just like every other
analysis module, so they appear as NEW: / EXISTING: / FIXED: in the final
diff report.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, List

from fluxdiff.supply_chain.erp_service import fetch_inventory_from_erp

if TYPE_CHECKING:
    from fluxdiff.models.pcb_models import PCBData


def _build_bom(pcb: "PCBData") -> list[dict]:
    """
    Aggregate components by (value, footprint) and count occurrences.

    Power symbols (#PWR / #FLG) are excluded — they have no physical part.

    Returns a list of dicts:
        {"value": str, "footprint": str, "count": int,
         "display_name": str, "refs": list[str]}
    """
    counts: dict[tuple, dict] = defaultdict(
        lambda: {"count": 0, "refs": []}
    )

    for comp in pcb.components:
        if comp.is_power_symbol:
            continue
        key = (comp.value or "?", comp.footprint or "")
        counts[key]["count"] += 1
        counts[key]["refs"].append(comp.ref)

    bom = []
    for (value, footprint), data in sorted(counts.items()):
        display_name = f"{value}" + (f" [{footprint.split(':')[-1]}]" if footprint else "")
        bom.append(
            {
                "value": value,
                "footprint": footprint,
                "count": data["count"],
                "display_name": display_name,
                "refs": sorted(data["refs"]),
            }
        )
    return bom


def analyse_supply_chain(pcb: "PCBData") -> List[str]:
    """
    Main entry point — mirrors the signature of every other FluxDiff analysis
    function: takes PCBData, returns List[str] sorted CRITICAL→WARNING→INFO.

    Calls the ERP once per unique (value, footprint) pair.
    """
    bom = _build_bom(pcb)
    findings: list[str] = []
    seen: set[str] = set()

    for item in bom:
        erp_data = fetch_inventory_from_erp(item["value"])
        stock: int = erp_data.get("stock", 0)
        required: int = item["count"]
        refs_str = ", ".join(item["refs"][:5])
        if len(item["refs"]) > 5:
            refs_str += f" … (+{len(item['refs']) - 5} more)"

        if stock == 0:
            severity = "CRITICAL"
            msg = (
                f"CRITICAL: Out of stock: {item['display_name']} "
                f"(need {required}, have 0) — refs: {refs_str}"
            )
        elif stock < required:
            severity = "WARNING"
            msg = (
                f"WARNING: Low stock: {item['display_name']} "
                f"(need {required}, have {stock}) — refs: {refs_str}"
            )
        else:
            msg = (
                f"INFO: In stock: {item['display_name']} "
                f"(need {required}, have {stock}) — refs: {refs_str}"
            )

        if msg not in seen:
            seen.add(msg)
            findings.append(msg)

    findings.sort(
        key=lambda m: 0 if m.startswith("CRITICAL") else 1 if m.startswith("WARNING") else 2
    )
    return findings