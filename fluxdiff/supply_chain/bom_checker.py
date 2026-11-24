from __future__ import annotations
from collections import defaultdict
from typing import TYPE_CHECKING, List
from fluxdiff.supply_chain.erp_service import fetch_inventory_from_erp
from fluxdiff.models.pcb_models import Finding

if TYPE_CHECKING:
    from fluxdiff.models.pcb_models import PCBData

CAT = "BOM"
_SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


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
    if not bom:
        return []

    findings: list[Finding] = []
    unchecked = 0

    for item in bom:
        erp_data = fetch_inventory_from_erp(item["value"], item["footprint"])
        if erp_data is None:
            unchecked += 1
            continue

        stock: int    = int(erp_data.get("stock", 0))
        required: int = item["count"]
        demo          = erp_data.get("source") == "demo"
        tag           = "[DEMO DATA] " if demo else ""
        refs_str      = ", ".join(item["refs"][:5])
        if len(item["refs"]) > 5:
            refs_str += f" … (+{len(item['refs']) - 5} more)"

        if stock == 0:
            severity, status = "CRITICAL", "out_of_stock"
            message = (f"{tag}Out of stock: {item['display_name']} "
                       f"(need {required}, have 0) — refs: {refs_str}")
        elif stock < required:
            severity, status = "WARNING", "low_stock"
            message = (f"{tag}Low stock: {item['display_name']} "
                       f"(need {required}, have {stock}) — refs: {refs_str}")
        else:
            severity, status = "INFO", "in_stock"
            message = (f"{tag}In stock: {item['display_name']} "
                       f"(need {required}) — refs: {refs_str}")

        findings.append(Finding(
            severity     = severity,
            category     = CAT,
            message      = message,
            related_refs = tuple(item["refs"]),
            highlight_refs = tuple(item["refs"]),
            # Quantities and ref lists change without the BOM line changing.
            key          = f"bom:{status}:{item['value']}:{item['footprint']}",
        ))

    if unchecked == len(bom):
        findings.append(Finding(
            severity = "INFO",
            category = CAT,
            message  = (
                "Stock not checked — no ERP adapter configured "
                "(see supply_chain/erp_service.py; FLUXLINK_ERP_DEMO=1 enables demo data)"
            ),
            key      = "bom:no_erp",
        ))

    findings.sort(key=lambda f: _SEVERITY_ORDER[f.severity])
    return findings
