import pytest

from fluxdiff.analysis.impedance import (
    _build_net_targets, _match_net_target, _stripline_impedance, analyse_impedance,
)
from fluxdiff.models.pcb_models import Component, Pad, PCBData, Trace
from fluxdiff.supply_chain.bom_checker import analyse_supply_chain


def _board():
    pads = lambda: [Pad("1", "GND"), Pad("2", "VCC")]
    return PCBData(components=[
        Component("R1", "10k", "R_0805", 0, 0, 0, "F.Cu", pads=pads()),
        Component("C1", "100nF", "C_0805", 0, 0, 0, "F.Cu", pads=pads()),
        # A padless footprint is board artwork, not a BOM line.
        Component("kibuzzard-61B9CE86", "LOGO", "", 0, 0, 0, "F.Cu"),
    ])


def test_bom_without_erp_reports_unchecked_not_fake_stock(monkeypatch):
    monkeypatch.delenv("FLUXLINK_ERP_DEMO", raising=False)
    findings = analyse_supply_chain(_board())
    assert [(f.severity, f.key) for f in findings] == [("INFO", "bom:no_erp")]


def test_bom_demo_data_is_labelled(monkeypatch):
    monkeypatch.setenv("FLUXLINK_ERP_DEMO", "1")
    findings = analyse_supply_chain(_board())
    assert findings and all(f.message.startswith("[DEMO DATA]") for f in findings)


@pytest.mark.parametrize("net,expected", [
    ("/USB_DP", (45, 5)),
    ("/LVDS_CLK_P", (50, 5)),     # 100 Ω diff -> 50 Ω single-ended
    ("/ETH0_TX_P", (50, 5)),
    ("/PCIE_TX0_P", (42.5, 4)),   # 85 Ω diff
    ("/RF_IN", (50, 5)),
    ("/METHANE_SENSE", None),
    ("/VREF_RFU", None),
])
def test_default_net_targets(net, expected):
    assert _match_net_target(net, {}) == expected


def test_differential_config_target_is_halved():
    cfg = {"critical_nets": {"/CLK_P": {"target_ohms": 100, "tolerance_ohms": 10,
                                         "differential": True}}}
    assert _build_net_targets(cfg) == {"/CLK_P": (50.0, 5.0)}


def test_stripline_uses_ipc2141_term():
    # 60/sqrt(4.3) * ln(4*0.4 / (0.67*pi*(0.8*0.15 + 0.035)))
    assert _stripline_impedance(0.15, 0.4, 4.3, 0.035) == 46.0


def test_lvds_trace_near_50_ohm_is_not_flagged():
    pcb = PCBData(traces=[Trace("F.Cu", (0, 0), (10, 0), "/LVDS_CLK_P", 0.32)])
    [finding] = analyse_impedance(pcb)
    assert finding.severity == "INFO"


def test_graphic_footprints_are_not_bom_lines(monkeypatch):
    monkeypatch.setenv("FLUXLINK_ERP_DEMO", "1")
    refs = {r for f in analyse_supply_chain(_board()) for r in f.related_refs}
    assert refs == {"R1", "C1"}
