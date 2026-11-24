import math
from pathlib import Path

import pytest

from fluxdiff.analysis.diff_pair import analyse_diff_pairs
from fluxdiff.analysis.erc_checker import _is_capacitor, _net_needs_pullup
from fluxdiff.analysis.ground_checker import _is_ferrite
from fluxdiff.analysis.power_tree import _is_battery, _is_connector
from fluxdiff.diff.diff_engine import _rotation_delta, compare_pcbs
from fluxdiff.models.pcb_models import Arc, Component, PCBData, Trace
from fluxdiff.parser.pcb_parser import parse_pcb

BOARDS = Path(__file__).resolve().parent.parent / "test_boards"

HEADER = '(kicad_pcb (version 20240108) (net 0 "") (net 1 "/USB_DP") (net 2 "/USB_DN")\n'


def _write(tmp_path, body):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(HEADER + body + ")\n")
    return parse_pcb(path)


def test_routed_arc_tracks_are_parsed(tmp_path):
    pcb = _write(tmp_path, '''
      (arc (start 100 130) (mid 101 131) (end 102 130) (width 0.25) (layer "F.Cu") (net 1) (uuid "a"))
      (gr_poly (pts (xy 0 0) (arc (start 1 0) (mid 2 1) (end 3 0)) (xy 0 5)) (layer "F.SilkS"))
    ''')
    assert len(pcb.arcs) == 1          # the polygon's (arc ...) is not a track
    assert pcb.arcs[0].net == "/USB_DP" and pcb.arcs[0].width == 0.25


def test_outline_from_gr_rect(tmp_path):
    pcb = _write(tmp_path, '(gr_rect (start 10 20) (end 60 50) (layer "Edge.Cuts"))')
    assert pcb.board_outline == (10, 20, 60, 50)


def test_outline_from_gr_poly(tmp_path):
    pcb = _write(tmp_path, '(gr_poly (pts (xy 5 5) (xy 40 5) (xy 40 30)) (layer "Edge.Cuts"))')
    assert pcb.board_outline == (5, 5, 40, 30)


def test_outline_from_rotated_footprint_edge(tmp_path):
    pcb = _write(tmp_path, '''
      (footprint "Outline" (layer "F.Cu") (at 100 100 90)
        (fp_line (start 0 0) (end 10 0) (layer "Edge.Cuts")))
    ''')
    x0, y0, x1, y1 = pcb.board_outline
    # 90° in KiCad (Y down) maps local +X to board -Y.
    assert (round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)) == (100, 90, 100, 100)


def test_test_board_outline_unchanged():
    assert parse_pcb(BOARDS / "after3.kicad_pcb").board_outline == pytest.approx(
        (94.16494542084894, 120.933608, 151.259327, 154.873709))


def test_arc_length_counts_toward_diff_pair_length():
    # P: one straight 3.1416 mm segment; N: a semicircle of radius 1 (π mm).
    pcb = PCBData(
        traces=[Trace("F.Cu", (0, 0), (math.pi, 0), "/USB_DP", 0.2)],
        arcs=[Arc("F.Cu", (1, 0), (0, 1), (-1, 0), "/USB_DN", 0.2)],
    )
    assert not [f for f in analyse_diff_pairs(pcb) if "length mismatch" in f.message]


def test_active_low_nets_are_not_reported_as_diff_halves():
    nets = ["/RESET_N", "/CS_N", "/USB_CLK_N"]
    pcb = PCBData(traces=[Trace("F.Cu", (0, 0), (1, 0), n, 0.2) for n in nets])
    flagged = [f.affected_nets[0] for f in analyse_diff_pairs(pcb)]
    assert flagged == ["/USB_CLK_N"]


@pytest.mark.parametrize("ref,cap", [("C1", True), ("C12", True), ("CN1", False), ("CONN2", False)])
def test_capacitor_prefix(ref, cap):
    assert _is_capacitor(ref) is cap


@pytest.mark.parametrize("ref,battery", [("BT1", True), ("B1", True), ("BZ1", False), ("BTN1", False)])
def test_battery_prefix(ref, battery):
    assert _is_battery(Component(ref, "", "", 0, 0, 0, "")) is battery


def test_connector_and_ferrite_prefixes():
    mk = lambda ref: Component(ref, "", "", 0, 0, 0, "")
    assert _is_connector(mk("J1")) and _is_connector(mk("X1"))
    assert not _is_connector(mk("XTAL1"))
    assert _is_ferrite(mk("FB1")) and not _is_ferrite(mk("Z1"))


@pytest.mark.parametrize("net,needs", [
    ("/SDA", True), ("/I2C/SCL0", True), ("/IMU_INT1", True), ("/nRESET", True),
    ("/SENSOR_ALERT", True), ("Net-(U1-SDA)", True), ("/LED_OPEN_DRAIN", True),
    ("/MODE", False), ("/NODE_SEL", False), ("/POINT", False), ("/PRINT_EN", False),
])
def test_pullup_net_matching(net, needs):
    assert _net_needs_pullup(net) is needs


def test_rotation_wraps():
    assert _rotation_delta(359.5, 0.0) == pytest.approx(0.5)
    assert _rotation_delta(-90.0, 270.0) == 0
    assert _rotation_delta(0.0, 90.0) == 90


def test_component_details_have_positions():
    old = PCBData(components=[Component("R1", "1k", "R", 1, 2, 0, "F.Cu", uuid="u1"),
                              Component("R2", "1k", "R", 5, 5, 0, "F.Cu", uuid="u2")])
    new = PCBData(components=[Component("R1", "1k", "R", 3, 4, 0, "F.Cu", uuid="u1"),
                              Component("R3", "1k", "R", 9, 9, 0, "F.Cu", uuid="u3")])
    details = {d["type"]: d for d in compare_pcbs(old, new).component_change_details}
    assert details["added"]["to"] == {"x": 9, "y": 9}
    assert details["removed"]["from"] == {"x": 5, "y": 5}
    assert details["moved"]["from"] == {"x": 1, "y": 2} and details["moved"]["to"] == {"x": 3, "y": 4}


def test_floating_net_marker_is_on_the_floating_pad():
    from fluxdiff.parser.pcb_parser import parse_pcb as _parse
    result = compare_pcbs(_parse(BOARDS / "before3.kicad_pcb"), _parse(BOARDS / "after3.kicad_pcb"))
    coords = {f.message: f.coordinates for f in result.erc_findings}
    # U2 pad 14 is at (117.09, 143.75); pad 1 is at (118.36, 133.11).
    x, y = coords["Net 'Net-(U2-Pad14)' is floating (only one real connection: U2)"]
    assert (round(x, 2), round(y, 2)) == (117.09, 143.75)
    # All six floating U2 nets get distinct markers.
    u2 = {c for m, c in coords.items() if m.startswith("Net 'Net-(U2-Pad")}
    assert len(u2) == 6
