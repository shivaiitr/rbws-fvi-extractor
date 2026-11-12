"""Process-piping P&ID names stay the value written for that line tag."""

from rbws_extractor.ocr.edop_piping import _assemble_pid, _pid_for_line


def test_slash_suffixes_stay_on_the_same_drawing():
    assert _assemble_pid(["NGQP-EP-", "PDPID-", "0017591/92"]) == "NGQP-EP-PDPID-0017591/92"
    assert _assemble_pid(["NGQP-EP-", "PDPID-", "0017591", "/", "92"]) == "NGQP-EP-PDPID-0017591/92"
    assert _assemble_pid(["NGQP-EP-PDPID-0017595/96/97/99"]) == "NGQP-EP-PDPID-0017595/96/97/99"
    assert _assemble_pid(["PDPID-0017599/603"]) == "NGQP-EP-PDPID-0017599/603"
    assert _assemble_pid(["NGQP-EP-", "PDPID-", "0017602"]) == "NGQP-EP-PDPID-0017602"


def test_mp_discipline_is_not_renamed_to_ep():
    assert _assemble_pid(["NGQP-MP-", "PDPID-", "0017594"]) == "NGQP-MP-PDPID-0017594"
    assert _assemble_pid(["NGQP-MP-PDPID0017587/92"]) == "NGQP-MP-PDPID-0017587/92"


def test_other_pid_families_are_left_as_written():
    assert _assemble_pid(["QP-PR-026"]) == "QP-PR-026"
    assert _assemble_pid(["NGGN-TCE-GN-PD-520-B2-0001"]) == "NGGN-TCE-GN-PD-520-B2-0001"
    assert _assemble_pid(["NGQP-EP-PDIPD-0017602"]) == "NGQP-EP-PDIPD-0017602"


def test_checklist_pid_is_kept_for_its_line_tag():
    checklist = "NGQP-MP-PDPID-0017594"
    line_pdf = "NGQP-EP-PDPID-0017594/599"
    assert _pid_for_line(checklist, line_pdf) == "NGQP-MP-PDPID-0017594"
    assert _pid_for_line(None, "NGQP-MP-PDPID-0017579") == "NGQP-MP-PDPID-0017579"
    assert _pid_for_line("NGQP-EP-PDPID-0017585/99", "NGQP-EP-PDPID-0017585/599") == (
        "NGQP-EP-PDPID-0017585/99"
    )
