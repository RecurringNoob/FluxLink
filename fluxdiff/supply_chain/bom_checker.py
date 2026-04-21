from __future__ import annotations
from collections import defaultdict
from typing import TYPE_CHECKING, List
from fluxdiff.supply_chain.erp_service import fetch_inventory_from_erp
from fluxdiff.models.pcb_models import Finding

if TYPE_CHECKING:
    from fluxdiff.models.pcb_models import PCBData


def _build_bom(pcb: "PCBData") -> list[dict]:
    counts: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "refs": []})
    for comp in pcb.components:
        if comp.is_power_symbol:
            continue
        key = (comp.value or "?", comp.footprint or "")
        counts[key]["count"] += 1
        counts[key]["refs"].append(comp.ref)
    bom = []
    for (value, footprint), data in sorted(counts.items()):
        display_name = f"{value}" + (f" [{footprint.split(':')[-1]}]" if footprint else "")
        bom.append({
            "value":        value,
            "footprint":    footprint,
            "count":        data["count"],
            "display_name": display_name,
            "refs":         sorted(data["refs"]),
        })
    return bom


def analyse_supply_chain(pcb: "PCBData") -> List[Finding]:
    bom = _build_bom(pcb)
    findings: list[Finding] = []
    seen: set[str] = set()

    for item in bom:
        erp_data      = fetch_inventory_from_erp(item["value"])
        stock: int    = erp_data.get("stock", 0)
        required: int = item["count"]
        refs_str      = ", ".join(item["refs"][:5])
        if len(item["refs"]) > 5:
            refs_str += f" … (+{len(item['refs']) - 5} more)"

        if stock == 0:
            severity = "CRITICAL"
            message  = (
                f"Out of stock: {item['display_name']} "
                f"(need {required}, have 0) — refs: {refs_str}"
            )
        elif stock < required:
            severity = "WARNING"
            message  = (
                f"Low stock: {item['display_name']} "
                f"(need {required}, have {stock}) — refs: {refs_str}"
            )
        else:
            severity = "INFO"
            message  = (
                f"In stock: {item['display_name']} "
                f"(need {required}, have {stock}) — refs: {refs_str}"
            )

        dedup_key = f"{severity}:{message}"
        if dedup_key not in seen:
            seen.add(dedup_key)
            findings.append(Finding(
                severity     = severity,
                category     = "BOM",
                message      = message,
                related_refs = tuple(item["refs"]),
                coordinates  = None,
            ))

    findings.sort(key=lambda f: {"CRITICAL": 0, "WARNING": 1, "INFO": 2}[f.severity])
    return findings