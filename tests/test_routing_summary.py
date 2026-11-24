import pytest

from fluxdiff.rag.ingest.routing_summary import summarize_routing


def test_segments_and_vias_are_summarised_per_net():
    lines = [
        "Trace added: net +3V3, layer F.Cu, from (0.0, 0.0) to (3.0, 4.0), width 0.254 mm",
        "Trace added: net +3V3, layer B.Cu, from (3.0, 4.0) to (3.0, 6.0), width 0.152 mm",
        "Trace removed: net +3V3, layer F.Cu, from (0.0, 0.0) to (0.0, 1.0), width 0.254 mm",
        "Via added: net GND, at (1.0, 1.0)",
        "Via added: net GND, at (2.0, 1.0)",
        "Zone added: net GND, layer B.Cu",
    ]
    assert summarize_routing(lines) == [
        "5 routing change(s) across 2 net(s):",
        "Net +3V3: 2 segments added (7.0 mm on B.Cu, F.Cu; width 0.152, 0.254 mm), "
        "1 segment removed (1.0 mm)",
        "Net GND: 2 vias added",
        "Zone added: net GND, layer B.Cu",
    ]


def test_arc_length_is_counted():
    lines = ["Arc added: net CLK, layer F.Cu, start (1.0, 0.0), mid (0.0, 1.0), end (-1.0, 0.0), width 0.2 mm"]
    assert summarize_routing(lines)[1] == "Net CLK: 1 segment added (3.1 mm on F.Cu; width 0.2 mm)"


def test_removed_only_net_keeps_layer_detail():
    lines = ["Trace removed: net SDA, layer B.Cu, from (0, 0) to (0, 2), width 0.2 mm"]
    assert summarize_routing(lines)[1] == "Net SDA: 1 segment removed (2.0 mm on B.Cu; width 0.2 mm)"


def test_net_names_with_commas_and_parentheses():
    lines = ["Trace added: net Net-(J1-PadA5), layer F.Cu, from (0, 0) to (1, 0), width 0.15 mm"]
    assert summarize_routing(lines)[1].startswith("Net Net-(J1-PadA5): 1 segment added (1.0 mm")
