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
"""

import random
import time


def fetch_inventory_from_erp(component_value: str) -> dict:
    """
    Simulates an ERP API call to fetch stock availability.

    Replace this stub with your real ERP integration:

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
    time.sleep(0.1)  # simulate network latency
    stock = random.choice([0, 1, 2, 5, 10, 50])
    return {"value": component_value, "stock": stock}