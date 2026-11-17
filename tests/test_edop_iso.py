"""Process-piping isometrics are the engineering drawings, one sheet or several."""

from pathlib import Path

import fitz
from openpyxl import load_workbook
from PIL import Image

from rbws_extractor.excel.writer import RbwsExcelWriter
from rbws_extractor.models.findings import AssetClass, ExtractionResult, Finding, ReportMetadata
from rbws_extractor.ocr.edop_piping import _classify_page, discover_line_pdfs, parse_line_pdf


def test_line_number_header_is_an_isometric_even_without_the_legend():
    assert _classify_page("LINE NUMBER: AP-15159-2-80-DAC-3\nP&ID NUMBER: NGQP-EP-PDPID-0017592") == (
        "iso_page"
    )
    assert _classify_page("Drawing not to scale\nWeld joints\nPipe support") == "iso_page"


def test_findings_and_drawing_on_one_page_keep_both():
    text = "\n".join(
        [
            "Anomaly description",
            "A sectional view of the line. (Photo #01)",
            "LINE NUMBER: AP-007-1-160-MAC-3",
            "P&ID NUMBER: NGQP-EP-PDPID-0017592",
        ]
    )
    assert _classify_page(text) == "mixed_page"


def test_pit_table_is_not_an_isometric():
    text = "\n".join(
        [
            "PIT MEASUREMENT TABLE",
            "All readings in mm",
            "Prepared By",
        ]
    )
    assert _classify_page(text) == "other"


def test_serial_printed_as_s_n_is_taken_without_a_numbered_filename(tmp_path: Path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((48, 80), "S/N:", fontsize=12)
    page.insert_text((48, 104), "55", fontsize=12)
    page.insert_text((48, 140), "Line Number", fontsize=12)
    page.insert_text((48, 164), "D-08154-2-80-AAA-3", fontsize=12)
    path = tmp_path / "D-08154-line.pdf"
    doc.save(path)
    doc.close()
    mapped, notes = discover_line_pdfs(tmp_path)
    assert mapped[55] == path
    assert notes == ["SN 55 from PDF content: D-08154-line.pdf"]


def test_each_isometric_sheet_is_kept(tmp_path: Path):
    doc = fitz.open()
    first = doc.new_page()
    first.insert_text((48, 160), "LINE NUMBER: D-08154-2-80-AAA-3", fontsize=12)
    first.insert_text((48, 190), "P&ID NUMBER: NGQP-EP-PDPID-0017594", fontsize=12)
    first.insert_text((48, 400), "From QP PLATFORM", fontsize=12)
    second = doc.new_page()
    second.insert_text((48, 140), "LINE NUMBER: D-08154-2-80-AAA-3", fontsize=12)
    second.insert_text((48, 180), "P&ID NUMBER: NGQP-EP-PDPID-0017594", fontsize=12)
    second.insert_text((48, 420), "To QI PLATFORM", fontsize=12)
    pdf_path = tmp_path / "55._D-08154.pdf"
    doc.save(pdf_path)
    doc.close()
    parsed = parse_line_pdf(pdf_path, tmp_path / "work")
    assert len(parsed.iso_paths) == 2
    assert all(path.exists() for path in parsed.iso_paths)
    assert parsed.iso_path == parsed.iso_paths[0]


def test_workbook_places_every_isometric(tmp_path: Path):
    sheets = []
    for name, color in (("one.png", (20, 40, 80)), ("two.png", (80, 40, 20))):
        path = tmp_path / name
        Image.new("RGB", (90, 70), color=color).save(path)
        sheets.append(path)
    result = ExtractionResult(
        metadata=ReportMetadata(facility="EDOP-QP"),
        findings=[
            Finding(
                asset_class=AssetClass.FIRE_WATER_PIPING,
                line_number="D-08154-2-80-AAA-3",
                anomaly_description="Scale on the flange",
                iso_image_path=sheets[0],
                iso_image_paths=sheets,
            ).apply_judgment_blanks()
        ],
    )
    out = tmp_path / "piping.xlsx"
    RbwsExcelWriter().write(result, out, embed_images=True)
    assert len(load_workbook(out).active._images) == 2
