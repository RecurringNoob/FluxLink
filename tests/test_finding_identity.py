from pathlib import Path

from fluxdiff.diff.diff_engine import _tag, compare_pcbs
from fluxdiff.models.pcb_models import Finding
from fluxdiff.parser.pcb_parser import parse_pcb

BOARDS = Path(__file__).resolve().parent.parent / "test_boards"


def test_display_fields_do_not_affect_identity():
    a = Finding("WARNING", "ERC", "msg", related_refs=("R1",), coordinates=(1.0, 2.0))
    b = Finding("WARNING", "ERC", "msg", related_refs=("R1", "R2"), coordinates=(5.0, 6.0))
    assert a == b and hash(a) == hash(b)


def test_explicit_key_ignores_measured_values():
    a = Finding("WARNING", "DIFF_PAIR", "mismatch 0.620 mm", key="length_mismatch:A_P/A_N")
    b = Finding("WARNING", "DIFF_PAIR", "mismatch 0.910 mm", key="length_mismatch:A_P/A_N")
    assert a == b


def test_severity_change_is_a_different_finding():
    assert Finding("WARNING", "IMPEDANCE", "x", key="k") != Finding("CRITICAL", "IMPEDANCE", "x", key="k")


def test_existing_findings_carry_new_board_data():
    old = Finding("WARNING", "ERC", "old text", coordinates=(0.0, 0.0), key="k")
    new = Finding("WARNING", "ERC", "new text", coordinates=(9.0, 9.0), key="k")
    out_str, out = [], []
    # Make old_set the smaller set: intersection would otherwise return `old`.
    _tag({old}, {new, Finding("INFO", "ERC", "other")}, out_str, out)
    existing = [f for f in out if f.status == "EXISTING"]
    assert existing[0].coordinates == (9.0, 9.0)
    assert existing[0].message == "new text"


def test_test_boards_have_no_finding_both_new_and_fixed():
    result = compare_pcbs(parse_pcb(BOARDS / "before3.kicad_pcb"),
                          parse_pcb(BOARDS / "after3.kicad_pcb"))
    for findings in (result.erc_findings, result.power_tree_findings,
                     result.diff_pair_findings, result.ground_findings,
                     result.impedance_findings, result.bom_findings):
        new = {f.message for f in findings if f.status == "NEW"}
        fixed = {f.message for f in findings if f.status == "FIXED"}
        assert not (new & fixed), new & fixed

    erc_new = [f.message for f in result.erc_findings if f.status == "NEW"]
    assert erc_new == ["Net 'Net-(C3-Pad1)' is floating (only one real connection: C3)"]
