"""
Shared net-name helpers.

KiCad's stock power symbols produce names like ``+3V3``, ``+5V``, ``+3.3V``,
``+12V`` and ``-12V``, none of which contain the substrings the checkers look
for (``VCC``, ``V3V3`` ...).  ``is_voltage_rail_name`` recognises that style so
bypass-cap, floating-power and power-tree checks run on real boards.

Hierarchical sheet paths (``/Power/+3V3``) are reduced to the last segment.
"""

import re

# +3V3, +5V, +3.3V, +12V, -12V, 3V3, +1V8, +3V3_ANA, +5VA, +5V_USB
_VOLTAGE_RAIL = re.compile(r"^[+\-]?\d+(?:\.\d+)?V\d*(?:[_\-]?[A-Z0-9]+)*$")


def _leaf(net_name: str) -> str:
    return net_name.rsplit("/", 1)[-1].strip().upper()


def is_voltage_rail_name(net_name: str) -> bool:
    """True for voltage-style names such as ``+3V3`` or ``+12V``."""
    return bool(_VOLTAGE_RAIL.match(_leaf(net_name)))
