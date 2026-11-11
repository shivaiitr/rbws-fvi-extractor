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
    _match_section,
    _parse_summary,
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
    ]
    assert _match_section("NGFR-M-3395", sections).tag == "NGFR-MR-3395"
    assert _match_section("NGFR-D-3140", sections).tag == "FR-D-3140"


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
