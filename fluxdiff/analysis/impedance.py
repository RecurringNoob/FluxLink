"""
Impedance & Stack-Up Awareness for FluxDiff.

Accepts an optional stack-up configuration (YAML or JSON) describing the
board layer structure. Without a config, checks are skipped gracefully.

F3 FIX: _microstrip_impedance narrow-trace branch now uses ε_eff instead of
raw er, matching the wide-trace branch and IPC-2141A.

FIX (config path): load_stackup_config now resolves relative paths to absolute
paths before checking os.path.isfile.

B9 FIX: The severity threshold condition used strict greater-than (`>`) to
decide between INFO and WARNING, meaning a delta exactly equal to tol_z was
silently classified as INFO ("within target"). A trace right on the tolerance
boundary is not within spec — it should be WARNING. The condition is now `>=`
so delta == tol_z produces a WARNING. The CRITICAL threshold (delta > tol_z*2)
is unchanged and still uses strict greater-than.
"""

import json
import math
import os
from collections import defaultdict

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DEFAULT_CRITICAL_NETS = {
    "USB_DP":   (45,  5),
    "USB_DN":   (45,  5),
    "USB_D+":   (45,  5),
    "USB_D-":   (45,  5),
    "RF":       (50,  5),
    "ANT":      (50,  5),
    "LVDS":     (100, 10),
    "HDMI":     (50,  5),
    "ETH":      (100, 10),
    "MIPI":     (100, 10),
    "PCIE":     (85,  5),
}

DEFAULT_LAYER_CONFIG = {
    "type": "microstrip",
    "dielectric_thickness": 0.2,
    "dielectric_er": 4.5,
    "copper_thickness": 0.035,
}

# ---------------------------------------------------------------------------
# Impedance formulas
# ---------------------------------------------------------------------------

def _microstrip_impedance(w_mm: float, h_mm: float, er: float, t_mm: float) -> "float | None":
    """
    IPC-2141A microstrip characteristic impedance (Ω).

    F3 FIX: Both narrow (w/h ≤ 1) and wide (w/h > 1) branches now compute and
    use ε_eff. The original code used bare `er` in the narrow-trace branch.
    """
    if w_mm <= 0 or h_mm <= 0 or er <= 0:
        return None

    if t_mm > 0:
        w_eff = w_mm + (t_mm / math.pi) * (1 + math.log(4 * math.e * h_mm / t_mm))
    else:
        w_eff = w_mm
    w_eff = max(w_eff, 1e-6)

    er_eff = (er + 1) / 2 + (er - 1) / 2 * (1 / math.sqrt(1 + 12 * h_mm / w_eff))

    ratio = w_eff / h_mm

    if ratio <= 1:
        z0 = (60 / math.sqrt(er_eff)) * math.log(8 * h_mm / w_eff + w_eff / (4 * h_mm))
    else:
        z0 = (
            (120 * math.pi)
            / (
                math.sqrt(er_eff)
                * (w_eff / h_mm + 1.393 + 0.667 * math.log(w_eff / h_mm + 1.444))
            )
        )

    return round(z0, 1)


def _stripline_impedance(w_mm: float, h_mm: float, er: float, t_mm: float) -> "float | None":
    if w_mm <= 0 or h_mm <= 0 or er <= 0:
        return None

    b = h_mm
    d = 0.8 * (w_mm + t_mm)
    if d <= 0 or b <= 0:
        return None

    z0 = (60 / math.sqrt(er)) * math.log(4 * b / (0.67 * math.pi * d))
    return round(max(z0, 0), 1)


def _calculate_impedance(w_mm: float, layer_cfg: dict) -> "float | None":
    ltype = layer_cfg.get("type", "microstrip").lower()
    h = layer_cfg.get("dielectric_thickness", 0.2)
    er = layer_cfg.get("dielectric_er", 4.5)
    t = layer_cfg.get("copper_thickness", 0.035)

    if ltype == "microstrip":
        return _microstrip_impedance(w_mm, h, er, t)
    elif ltype == "stripline":
        return _stripline_impedance(w_mm, h, er, t)
    return None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_stackup_config(config_path: str) -> "dict | None":
    """
    Load a YAML or JSON stack-up config file.

    FIX: config_path is now resolved to an absolute path before the isfile
    check. A relative path silently returned None when the process cwd differed
    from where the CLI was invoked.
    """
    if not config_path:
        return None

    config_path = os.path.abspath(config_path)

    if not os.path.isfile(config_path):
        print(f"[WARNING] Stack-up config not found: {config_path} — impedance check will use defaults")
        return None

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        try:
            import yaml
            return yaml.safe_load(content)
        except ImportError:
            pass

        return json.loads(content)

    except Exception as e:
        print(f"[WARNING] Could not load stack-up config '{config_path}': {e}")
        return None


# ---------------------------------------------------------------------------
# Net → target lookup
# ---------------------------------------------------------------------------

def _build_net_targets(config: "dict | None") -> dict:
    if config and "critical_nets" in config:
        return {
            net: (float(v["target_ohms"]), float(v.get("tolerance_ohms", 5)))
            for net, v in config["critical_nets"].items()
        }
    return {}


def _match_net_target(net_name: str, net_targets: dict) -> "tuple | None":
    if net_name in net_targets:
        return net_targets[net_name]

    upper = net_name.upper()
    for key, val in DEFAULT_CRITICAL_NETS.items():
        if key.upper() in upper:
            return val

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyse_impedance(pcb, config_path: "str | None" = None) -> list:
    """
    Run impedance analysis on all traces belonging to critical nets.
    """
    config = load_stackup_config(config_path)
    layer_configs = (config or {}).get("layers", {})
    net_targets = _build_net_targets(config)

    findings = []
    net_layer_widths = defaultdict(lambda: defaultdict(list))

    for trace in pcb.traces:
        if not trace.net or trace.net == "__unconnected__":
            continue
        if trace.width <= 0:
            continue
        if _match_net_target(trace.net, net_targets) is None:
            continue
        net_layer_widths[trace.net][trace.layer].append(trace.width)

    if not net_layer_widths:
        if config:
            findings.append(
                "INFO: No critical net traces found matching stack-up config — "
                "verify net names in critical_nets section"
            )
        return findings

    for net_name, layer_map in sorted(net_layer_widths.items()):
        target = _match_net_target(net_name, net_targets)
        if target is None:
            continue
        target_z, tol_z = target

        for layer, widths in sorted(layer_map.items()):
            avg_width = sum(widths) / len(widths)
            layer_cfg = layer_configs.get(layer, DEFAULT_LAYER_CONFIG)
            z0 = _calculate_impedance(avg_width, layer_cfg)

            if z0 is None:
                findings.append(
                    f"WARNING: Could not calculate impedance for net '{net_name}' "
                    f"on layer '{layer}' — check stack-up config values"
                )
                continue

            delta = abs(z0 - target_z)

            # B9 FIX: use >= instead of > so a delta exactly at the tolerance
            # boundary is reported as WARNING, not silently passed as INFO.
            if delta >= tol_z:
                target_width = _find_target_width(target_z, layer_cfg)
                hint = (
                    f" (target width ≈ {target_width:.3f} mm)"
                    if target_width else ""
                )
                # CRITICAL when delta exceeds double the tolerance (unchanged)
                severity = "CRITICAL" if delta > tol_z * 2 else "WARNING"
                findings.append(
                    f"{severity}: Net '{net_name}' on layer '{layer}' — "
                    f"calculated Z0={z0}Ω vs target {target_z}±{tol_z}Ω "
                    f"(avg trace width {avg_width:.3f} mm){hint}"
                )
            else:
                findings.append(
                    f"INFO: Net '{net_name}' on layer '{layer}' — "
                    f"Z0={z0}Ω ✓ (within {target_z}±{tol_z}Ω target)"
                )

    seen = set()
    deduped = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            deduped.append(f)

    deduped.sort(key=lambda m: (
        0 if m.startswith("CRITICAL") else
        1 if m.startswith("WARNING") else
        2
    ))

    return deduped


def _find_target_width(target_z: float, layer_cfg: dict) -> "float | None":
    lo, hi = 0.05, 5.0
    for _ in range(40):
        mid = (lo + hi) / 2
        z = _calculate_impedance(mid, layer_cfg)
        if z is None:
            return None
        if abs(z - target_z) < 0.5:
            return round(mid, 3)
        if z > target_z:
            lo = mid
        else:
            hi = mid
    return None