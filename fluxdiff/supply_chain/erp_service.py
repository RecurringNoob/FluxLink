"""
fluxdiff/supply_chain/erp_service.py

ERP inventory adapter.  Replace the body of fetch_inventory_from_erp() with
your real ERP client (SAP, NetSuite, custom REST, etc.) without touching any
other FluxDiff module.

Contract
--------
fetch_inventory_from_erp(component_value: str, footprint: str = "") -> dict | None
    Returns {"value": str, "stock": int, "source": str}, or None when no ERP
    is configured.  Raises on unrecoverable network/auth errors so callers
    can surface them.

By default no ERP is configured and every lookup returns None, so the BOM
check reports that stock was not checked instead of inventing numbers.

Set FLUXLINK_ERP_DEMO=1 to use a deterministic demo stub (the same part value
always gets the same stock).  Its findings are labelled as demo data.
"""

import os
import zlib
from typing import Optional

_STOCK_LEVELS = (0, 1, 2, 5, 10, 50)


def erp_demo_enabled() -> bool:
    return os.getenv("FLUXLINK_ERP_DEMO", "").strip().lower() in ("1", "true", "yes")


def fetch_inventory_from_erp(component_value: str, footprint: str = "") -> Optional[dict]:
    """
    Look up stock for one BOM line.  Replace with your real integration, e.g.:

        resp = requests.get(
            f"{ERP_BASE_URL}/inventory",
            params={"part": component_value, "footprint": footprint},
            headers={"Authorization": f"Bearer {ERP_TOKEN}"},
            timeout=5,
        )
        resp.raise_for_status()
        return {"value": component_value,
                "stock": int(resp.json()["qty_on_hand"]),
                "source": "erp"}
    """
    if not erp_demo_enabled():
        return None
    index = zlib.crc32(component_value.encode("utf-8")) % len(_STOCK_LEVELS)
    return {"value": component_value, "stock": _STOCK_LEVELS[index], "source": "demo"}
