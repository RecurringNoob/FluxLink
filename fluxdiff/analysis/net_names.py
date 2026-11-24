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


# ---------------------------------------------------------------------------
# Reference-designator prefixes
# ---------------------------------------------------------------------------

_REF_PREFIX = re.compile(r"^#?[A-Za-z]+")


def ref_prefix(ref: str) -> str:
    """
    Letter prefix of a reference designator, upper-cased: ``C12`` -> ``C``,
    ``CN3`` -> ``CN``, ``BTN1`` -> ``BTN``.

    Checks compare this prefix exactly instead of using ``startswith``, which
    treated connectors (``CN1``) as capacitors, buzzers (``BZ1``) and buttons
    (``BTN1``) as batteries, and so on.
    """
    m = _REF_PREFIX.match(ref or "")
    return m.group(0).upper() if m else ""


def has_ref_prefix(ref: str, prefixes) -> bool:
    return ref_prefix(ref) in prefixes


def net_tokens(net_name: str):
    """Alphanumeric tokens of a net's last path segment: ``/I2C/SDA0`` -> [``SDA0``]."""
    return [t for t in re.split(r"[^A-Z0-9]+", _leaf(net_name)) if t]
