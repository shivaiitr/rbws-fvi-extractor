"""Excel writer tests."""

from datetime import date
from pathlib import Path

from openpyxl import load_workbook
from PIL import Image

from rbws_extractor.excel.writer import HEADERS, RbwsExcelWriter, photo_grid_columns
from rbws_extractor.models.findings import (
    AssetClass,
    ExtractionResult,
    Finding,
    ReportMetadata,
)


def test_excel_writer_headers_and_blank_judgment(tmp_path: Path):
    img_path = tmp_path / "photo.png"
    Image.new("RGB", (120, 80), color=(200, 40, 40)).save(img_path)

    result = ExtractionResult(
        metadata=ReportMetadata(facility="UBIT-GP", inspection_date=date(2024, 10, 9)),
        findings=[
            Finding(
                sn=1,
                asset_class=AssetClass.FIRE_WATER_PIPING,
                insp_date=date(2024, 10, 9),
                line_number='WF-010-8"-40-FAA-4',
                line_description="GP fire water header",
                anomaly_description="Heavy passive scale buildup (Photo #32-33)",
                pid_no="NGGP-AH-PDPID-0015210",
                photos_image_path=img_path,
            ).apply_judgment_blanks()
        ],
    )
    out = tmp_path / "RBWS.xlsx"
    RbwsExcelWriter().write(result, out, embed_images=True)
    assert out.exists()

    wb = load_workbook(out)
    ws = wb.active
    assert ws.title == "UBIT-GP"
    headers = [ws.cell(2, c).value for c in range(2, 18)]
    assert headers == HEADERS

    assert ws.cell(3, 2).value == 1
    assert ws.cell(3, 4).value == 'WF-010-8"-40-FAA-4'
    assert ws.cell(3, 9).value is None  # POF blank
    assert ws.cell(3, 16).value is None  # recommendations blank
    assert ws.cell(3, 3).number_format == "mmm-yy"
    assert len(ws._images) == 1


def test_each_uploaded_vessel_report_gets_its_own_sheet(tmp_path: Path):
    from openpyxl import Workbook

    wb = Workbook()
    writer = RbwsExcelWriter()
    out = tmp_path / "combined.xlsx"
    for facility, tag in (("EAP-GN", "EAP-V-1001"), ("YOHO-YA", "YO-D-12")):
        result = ExtractionResult(
            metadata=ReportMetadata(facility=facility, layout="pressure_vessel"),
            findings=[
                Finding(
                    asset_class=AssetClass.PRESSURE_VESSEL,
                    line_number=tag,
                    line_description="Vessel",
                    anomaly_description="- corrosion",
                ).apply_judgment_blanks()
            ],
        )
        writer.write(result, out, embed_images=False, include_pof_note=False, workbook=wb, save=False)
    wb.remove(wb.worksheets[0])
    wb.save(out)
    book = load_workbook(out)
    assert book.sheetnames == ["EAP-GN", "YOHO-YA"]
    assert book["EAP-GN"].cell(3, 4).value == "EAP-V-1001"
    assert book["YOHO-YA"].cell(3, 4).value == "YO-D-12"


def test_two_across_stays_when_the_photos_fit():
    assert photo_grid_columns(4, tile_h=180, max_h=545) == 2


def test_four_across_when_two_across_would_shrink():
    assert photo_grid_columns(10, tile_h=220, max_h=545) == 4


def test_vessel_row_uses_four_across_and_skips_a_duplicate_file(tmp_path: Path):
    paths = []
    for index in range(6):
        path = tmp_path / f"p{index}.png"
        Image.new("RGB", (200, 180), color=(index * 20, 40, 80)).save(path)
        paths.append(path)
    paths.append(paths[0])
    result = ExtractionResult(
        metadata=ReportMetadata(facility="USARI-FR", layout="pressure_vessel"),
        findings=[
            Finding(
                asset_class=AssetClass.PRESSURE_VESSEL,
                line_number="NGFR-M-3395",
                line_description="Receiver",
                anomaly_description="- one",
                photo_image_paths=paths,
            ).apply_judgment_blanks()
        ],
    )
    out = tmp_path / "vessel.xlsx"
    RbwsExcelWriter().write(result, out, embed_images=True, include_pof_note=False)
    wb = load_workbook(out)
    ws = wb.active
    assert len(ws._images) == 6
    tops = []
    for image in ws._images:
        marker = image.anchor._from
        tops.append(marker.rowOff)
    assert tops[2] == tops[0]
    assert tops[4] > tops[0]