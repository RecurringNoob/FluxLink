"""
fluxdiff/supply_chain/erp_service.py

ERP inventory adapter.  Swap the body of fetch_inventory_from_erp() for your
real ERP client (SAP, NetSuite, custom REST, etc.) without touching any other
FluxDiff module.

Contract
--------
fetch_inventory_from_erp(component_value: str) -> dict
    Always returns {"value": str, "stock": int}.
    Raises on unrecoverable network/auth errors so callers can surface them.

The default implementation is a DETERMINISTIC stub: the same part value always
returns the same stock, so repeated runs and old-vs-new board comparisons do
not produce phantom NEW / FIXED BOM findings.
"""

import zlib

_STOCK_LEVELS = (0, 1, 2, 5, 10, 50)


def fetch_inventory_from_erp(component_value: str) -> dict:
    """
    Stub ERP lookup.  Replace with your real integration, for example:

        import requests
        resp = requests.get(
            f"{ERP_BASE_URL}/inventory",
            params={"part": component_value},
            headers={"Authorization": f"Bearer {ERP_TOKEN}"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"value": component_value, "stock": int(data["qty_on_hand"])}
    """
    index = zlib.crc32(component_value.encode("utf-8")) % len(_STOCK_LEVELS)
    return {"value": component_value, "stock": _STOCK_LEVELS[index]}
