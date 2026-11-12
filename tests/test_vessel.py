"""Pressure vessel workflow rules. Existing Fire Water and piping tests stay separate."""

import io
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import fitz
from openpyxl import load_workbook
from PIL import Image

from rbws_extractor.excel.writer import HEADERS, RbwsExcelWriter
from rbws_extractor.models.findings import (
    AssetClass,
    ExtractionResult,
    Finding,
    ReportMetadata,
)
from rbws_extractor.ocr.vessel import (
    _anomaly_text,
    _field_photos,
    _format_pit,
    _match_section,
    _parse_summary,
    _pit_lines,
    _write_photo_with_caption,
    build_vessel_result,
    is_pressure_vessel_report,
)
from rbws_extractor.ocr.vessel import PhotoSection


def _photo(num: int, caption: str):
    return SimpleNamespace(photo_num=num, caption=caption)


def test_title_detects_pressure_vessel_only():
    assert is_pressure_vessel_report("FULL VISUAL INSPECTION OF PRESSURE VESSELS")
    assert is_pressure_vessel_report("PRESSURE VESSEL & TANK\nFULL VISUAL INSPECTION REPORT")
    assert not is_pressure_vessel_report("Fire Water System Visual Inspection")


def test_anomaly_copies_the_words_under_each_photo():
    text = _anomaly_text(
        [
            _photo(1, "General view of NGFR-MR-3395 Photo # 01"),
            _photo(2, "Close view of tag name Photo # 02"),
            _photo(3, "Moderate corrosion on barrel door and hinge Photo #03"),
            _photo(6, "Close view Photo #06"),
        ]
    )
    assert text.splitlines() == [
        "- General view of NGFR-MR-3395 Photo # 01",
        "- Close view of tag name Photo # 02",
        "- Moderate corrosion on barrel door and hinge Photo #03",
        "- Close view Photo #06",
    ]


def test_summary_tag_matches_longer_photo_tag():
    sections = [
        PhotoSection("OSO HP Gas Pig Receiver", "NGFR-MR-3395", 2, 120.0),
        PhotoSection("Flare Scrubber", "FR-D-3140", 28, 100.0),
        PhotoSection("Cooler fan", "NGGN-DU-EM100-1", 40, 80.0),
    ]
    assert _match_section("NGFR-M-3395", sections).tag == "NGFR-MR-3395"
    assert _match_section("NGFR-D-3140", sections).tag == "FR-D-3140"
    assert _match_section("NGGN-DU-EM100-2", sections) is None


def test_decommissioned_summary_row_is_flagged():
    class _Page:
        def get_text(self, _kind):
            return """
1.0 SUMMARY OF FINDINGS
S/N
Equipment ID
Equipment Description
Anomaly Description
Page #
1
NGFR-M-3395
OSO-HP Gas Pig Receiver
Moderate corrosion on the barrel
Photos # 01-14
5-7
2
NGFR-G-3230
Pour Point Depressant Injection Pump
Decommissioned and out of site
--
"""

    rows = _parse_summary([_Page()])
    assert [row.tag for row in rows] == ["NGFR-M-3395", "NGFR-G-3230"]
    assert rows[0].out_of_site is False
    assert rows[1].out_of_site is True
    assert rows[0].description.startswith("OSO-HP")


def test_summary_accepts_equipment_tags_from_any_facility():
    class _Page:
        def get_text(self, _kind):
            return """
SUMMARY OF FINDINGS
S/N
Equipment ID
1
EAP-V-1001
Test Separator
Light corrosion on the shell
Photos # 01-02
3
2. YO-D-12
Drain Vessel
In good condition
--
"""

    rows = _parse_summary([_Page()])
    assert [row.tag for row in rows] == ["EAP-V-1001", "YO-D-12"]
    assert rows[0].description.startswith("Test Separator")
    assert rows[1].description.startswith("Drain Vessel")


def test_serial_stays_with_its_equipment_and_summary_description_is_kept(tmp_path: Path):
    doc = fitz.open()
    summary = doc.new_page()
    summary.insert_textbox(
        fitz.Rect(36, 36, 560, 780),
        "\n".join(
            [
                "Location EAP-GN",
                "SUMMARY OF FINDINGS",
                "S/N",
                "Equipment ID",
                "4",
                "NGFR-XLR-302",
                "ABANG Pig Receiver",
                "Generally observed in good condition",
                "--",
                "15",
                "NGFR-G-3230",
                "Pour Point Depressant Injection Pump",
                "Decommissioned and out of site",
                "--",
            ]
        ),
        fontsize=11,
    )
    photos = doc.new_page()
    photos.insert_text((48, 140), "Equipment ID FB Pig Receiver (NGFR-XLR-302)", fontsize=12)
    pdf_path = tmp_path / "vessel.pdf"
    doc.save(pdf_path)
    doc.close()
    result = build_vessel_result(pdf_path, tmp_path / "work")
    by_tag = {finding.line_number: finding for finding in result.findings}
    assert by_tag["NGFR-XLR-302"].sn == 4
    assert by_tag["NGFR-XLR-302"].line_description == "ABANG Pig Receiver"
    assert by_tag["NGFR-G-3230"].sn == 15
    assert by_tag["NGFR-G-3230"].line_description.startswith("Pour Point Depressant")


def test_shared_photo_heading_stays_one_row(tmp_path: Path):
    doc = fitz.open()
    summary = doc.new_page()
    summary.insert_textbox(
        fitz.Rect(36, 36, 560, 780),
        "\n".join(
            [
                "SUMMARY OF FINDINGS",
                "S/N",
                "Equipment ID",
                "51",
                "NGGN-DM-D048-A",
                "Filter",
                "Light corrosion",
                "--",
                "52",
                "NGGN-DM-D048-B",
                "Filter",
                "Light corrosion",
                "--",
                "53",
                "NGGN-DM-D099",
                "Cooler",
                "In good condition",
                "--",
            ]
        ),
        fontsize=11,
    )
    photos = doc.new_page()
    photos.insert_text((48, 80), "Equipment ID 51 & 52 Filter (NGGN-DM-D048-A & B)", fontsize=11)
    photos.insert_text((48, 200), "Equipment ID 53 Cooler (NGGN-DM-D099)", fontsize=11)
    pdf_path = tmp_path / "paired.pdf"
    doc.save(pdf_path)
    doc.close()
    result = build_vessel_result(pdf_path, tmp_path / "work")
    assert [finding.line_number for finding in result.findings] == [
        "NGGN-DM-D048-A & NGGN-DM-D048-B",
        "NGGN-DM-D099",
    ]
    assert result.findings[0].source.notes == "sn_label=51 & 52"
    assert result.findings[1].sn == 53
    out = tmp_path / "paired.xlsx"
    RbwsExcelWriter().write(result, out, embed_images=False, include_pof_note=False)
    ws = load_workbook(out).active
    assert ws.cell(3, 2).value == "51 & 52"
    assert ws.cell(4, 2).value == 53


def test_report_without_a_summary_uses_the_heading_description(tmp_path: Path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((48, 80), "Equipment ID 51 & 52 Filter (NGGN-DM-D048-A & B)", fontsize=11)
    page.insert_text((48, 220), "Equipment ID OSO HP Gas Pig Receiver (NGFR-MR-3395)", fontsize=11)
    pdf_path = tmp_path / "headings_only.pdf"
    doc.save(pdf_path)
    doc.close()
    result = build_vessel_result(pdf_path, tmp_path / "work")
    assert [finding.line_number for finding in result.findings] == [
        "NGGN-DM-D048-A & NGGN-DM-D048-B",
        "NGFR-MR-3395",
    ]
    assert result.findings[0].line_description == "Filter"
    assert result.findings[0].source.notes == "sn_label=51 & 52"
    assert result.findings[1].line_description == "OSO HP Gas Pig Receiver"
    assert result.findings[1].sn == 53


def test_pit_row_keeps_size_depth_and_remaining_wall():
    assert _format_pit("PP1", "25.5 26.2 26.7 26.3 21.5 PP1 30 x 20 4.0") == (
        "- PP1, size 30 x 20, max depth 4.0, UTG 25.5, 26.2, 26.7, 26.3, remaining wall 21.5"
    )


def test_pit_lines_read_a_measurement_page():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((40, 120), "PIT Measurement Table For NGFR-D-3140", fontsize=11)
    page.insert_text((400, 200), "25.5 26.2 26.7 26.3 21.5", fontsize=11)
    page.insert_text((40, 214), "PP1 30 x 20 4.0", fontsize=11)
    lines = _pit_lines(page)
    doc.close()
    assert lines
    assert lines[0].startswith("- PP1, size 30 x 20")
    assert "remaining wall 21.5" in lines[0]


def test_later_summary_pages_keep_each_equipment_and_its_description(tmp_path: Path):
    doc = fitz.open()
    first = doc.new_page()
    first.insert_text((72, 80), "1.0 SUMMARY OF FINDINGS", fontsize=11)
    first.insert_text((70, 110), "S/N", fontsize=10)
    first.insert_text((120, 110), "Equipment ID", fontsize=10)
    first.insert_text((250, 110), "Equipment Description", fontsize=10)
    first.insert_text((470, 110), "Anomaly Description", fontsize=10)
    first.insert_text((560, 110), "Page #", fontsize=10)
    first.insert_text((76, 150), "1", fontsize=10)
    first.insert_text((120, 150), "NGGN-DM-D263", fontsize=10)
    first.insert_text((250, 150), "Regen gas mercury removal bed", fontsize=10)
    first.insert_text((470, 150), "Coating breakdown on top dome", fontsize=10)
    second = doc.new_page()
    second.insert_text((76, 90), "2", fontsize=10)
    second.insert_text((120, 90), "NGGN-DU- EM100-2", fontsize=10)
    second.insert_text((250, 90), "V080 STAGE 3 DISCH GAS COOLER FAN 2", fontsize=10)
    second.insert_text((470, 90), "Wrap on electric fan motor", fontsize=10)
    pdf_path = tmp_path / "wide_vessel.pdf"
    doc.save(pdf_path)
    doc.close()
    result = build_vessel_result(pdf_path, tmp_path / "work")
    by_sn = {finding.sn: finding for finding in result.findings}
    assert set(by_sn) == {1, 2}
    assert by_sn[1].line_number == "NGGN-DM-D263"
    assert by_sn[1].line_description == "Regen gas mercury removal bed"
    assert by_sn[2].line_number == "NGGN-DU-EM100-2"
    assert by_sn[2].line_description.startswith("V080 STAGE 3 DISCH")


def test_summary_uses_the_columns_of_whatever_report_was_uploaded(tmp_path: Path):
    doc = fitz.open()
    first = doc.new_page()
    first.insert_text((36, 70), "SUMMARY OF FINDINGS", fontsize=11)
    first.insert_text((40, 100), "S/N", fontsize=10)
    first.insert_text((90, 100), "Equipment ID", fontsize=10)
    first.insert_text((260, 100), "Equipment Description", fontsize=10)
    first.insert_text((430, 100), "Anomaly Description", fontsize=10)
    first.insert_text((540, 100), "Page #", fontsize=10)
    first.insert_text((46, 140), "1", fontsize=10)
    first.insert_text((90, 140), "QB-T-12", fontsize=10)
    first.insert_text((260, 140), "Inlet scrubber", fontsize=10)
    first.insert_text((430, 140), "Light corrosion on the shell", fontsize=10)
    second = doc.new_page()
    second.insert_text((46, 80), "2", fontsize=10)
    second.insert_text((90, 80), "QB-P 4A", fontsize=10)
    second.insert_text((260, 80), "Discharge cooler", fontsize=10)
    second.insert_text((430, 80), "Coating breakdown on the flange", fontsize=10)
    photos = doc.new_page()
    photos.insert_text((40, 80), "2.0 Field photographs", fontsize=12)
    photos.insert_text((46, 140), "40", fontsize=10)
    photos.insert_text((90, 140), "QB-T-12", fontsize=10)
    pdf_path = tmp_path / "qb_vessel.pdf"
    doc.save(pdf_path)
    doc.close()
    result = build_vessel_result(pdf_path, tmp_path / "work")
    by_sn = {finding.sn: finding for finding in result.findings}
    assert set(by_sn) == {1, 2}
    assert by_sn[1].line_number == "QB-T-12"
    assert by_sn[1].line_description == "Inlet scrubber"
    assert by_sn[2].line_number == "QB-P 4A"
    assert by_sn[2].line_description == "Discharge cooler"


def test_other_facility_pdf_builds_without_the_sample_title(tmp_path: Path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(
        fitz.Rect(36, 36, 560, 780),
        "\n".join(
            [
                "Location EAP-GN",
                "VISUAL INSPECTION REPORT",
                "SUMMARY OF FINDINGS",
                "S/N",
                "Equipment ID",
                "1",
                "EAP-V-1001",
                "Test Separator",
                "Light corrosion on the shell",
                "Photos # 01-02",
                "3",
            ]
        ),
        fontsize=11,
    )
    pdf_path = tmp_path / "eap_vessel.pdf"
    doc.save(pdf_path)
    doc.close()
    result = build_vessel_result(pdf_path, tmp_path / "work")
    assert result.metadata.facility == "EAP-GN"
    assert result.metadata.layout == "pressure_vessel"
    assert result.findings[0].line_number == "EAP-V-1001"
    assert result.findings[0].line_description.startswith("Test Separator")


def _jpeg(color: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_field_photos_keep_the_pdf_image_and_skip_logos(tmp_path: Path):
    photo = _jpeg((180, 40, 40), (327, 245))
    logo = _jpeg((10, 10, 10), (106, 51))
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(486, 35, 554, 67), stream=logo)
    page.insert_image(fitz.Rect(48, 138, 293, 322), stream=photo)
    page.insert_image(fitz.Rect(48, 138, 293, 322), stream=photo)
    found = _field_photos(page, 90, 800, set())
    assert len(found) == 1
    xref = found[0][4]
    assert doc.extract_image(xref)["image"] == photo
    doc.close()


def test_saved_picture_keeps_the_caption_under_the_photo(tmp_path: Path):
    photo = _jpeg((180, 40, 40), (327, 245))
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(48, 138, 293, 322), stream=photo)
    page.insert_text(
        (52, 336),
        "Moderate corrosion on barrel door and hinge",
        fontsize=8,
    )
    page.insert_text((52, 348), "Photo #03", fontsize=8)
    xref = _field_photos(page, 90, 800, set())[0][4]
    dest = tmp_path / "with_caption.png"
    caption = _write_photo_with_caption(doc, page, xref, (48, 138, 293, 322), dest)
    assert "Moderate corrosion on barrel door and hinge" in caption
    assert "Photo #03" in caption
    with Image.open(dest) as saved:
        assert saved.width == 327
        assert saved.height > 245
    doc.close()


def test_vessel_workbook_uses_equipment_headers_and_blank_judgment(tmp_path: Path):
    img = tmp_path / "photo.png"
    Image.new("RGB", (80, 60), color=(20, 40, 80)).save(img)
    result = ExtractionResult(
        metadata=ReportMetadata(facility="USARI-FR", layout="pressure_vessel"),
        findings=[
            Finding(
                asset_class=AssetClass.PRESSURE_VESSEL,
                insp_date=None,
                line_number="NGFR-M-3395",
                line_description="OSO HP Gas Pig Receiver",
                anomaly_description="Moderate corrosion on barrel door Photo #03-04",
                photo_image_paths=[img],
            ).apply_judgment_blanks()
        ],
    )
    # The model backfill must not invent a date when metadata has none.
    assert result.findings[0].insp_date is None
    out = tmp_path / "vessel.xlsx"
    RbwsExcelWriter().write(result, out, embed_images=True, include_pof_note=False)
    wb = load_workbook(out)
    ws = wb.active
    headers = [ws.cell(2, c).value for c in range(2, 18)]
    assert headers[2] == "Equipment #"
    assert headers[4] == "Equipment Description"
    assert headers[0] == HEADERS[0]
    assert ws.cell(3, 3).value is None
    assert ws.cell(3, 9).value is None
    assert ws.cell(3, 16).value is None
    assert len(ws._images) == 1


def test_fire_water_date_still_fills_when_metadata_has_a_date():
    result = ExtractionResult(
        metadata=ReportMetadata(facility="UBIT-GP", inspection_date=date(2024, 10, 9)),
        findings=[
            Finding(
                asset_class=AssetClass.FIRE_WATER_PIPING,
                line_number="WF-1",
                anomaly_description="Scale",
            ).apply_judgment_blanks()
        ],
    )
    assert result.findings[0].insp_date == date(2024, 10, 9)
