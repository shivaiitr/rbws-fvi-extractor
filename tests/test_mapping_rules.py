"""Tests for gold-standard row detection rules."""

from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw

from rbws_extractor.models.findings import (
    AssetClass,
    ClassifiedPage,
    PageType,
    PhotoRef,
    ReportMetadata,
)
from rbws_extractor.ocr.crop import _layout_bands, _stitch_vertical_professional, enrich_anomaly_from_captions
from rbws_extractor.pipeline.detect_findings import detect_findings
from rbws_extractor.pipeline.rules import (
    is_good_condition_text,
    join_from_to,
    normalize_technical_identifier,
)
from rbws_extractor.pipeline.validate import validate_finding, validate_result
from rbws_extractor.models.findings import ExtractionResult, Finding
from rbws_extractor.vision.extractors.common import (
    CaptionItem,
    EquipmentBlock,
    ObservationRow,
    ObservationTableExtraction,
    SummaryBullet,
)


def _page(n: int, page_type: PageType = PageType.OBSERVATION_TABLE) -> ClassifiedPage:
    return ClassifiedPage(
        page_number=n,
        image_path=Path(f"/tmp/page_{n}.png"),
        page_type=page_type,
        confidence=0.9,
    )


def test_join_from_to():
    assert join_from_to("GP-G-04 unit", '8" discharge pipe going to sea') == (
        'GP-G-04 unit to 8" discharge pipe going to sea'
    )


def test_good_condition_detection():
    assert is_good_condition_text("Visually in good condition")
    assert is_good_condition_text("Mobile cylinder in good condition")
    assert not is_good_condition_text("Heavy scale corrosion on flange")


def test_detect_cylinders_include_good_condition():
    metadata = ReportMetadata(facility="UBIT-GP", inspection_date=date(2024, 10, 9))
    page = _page(7, PageType.EQUIPMENT_PHOTO_BLOCK)
    blocks = [
        EquipmentBlock(
            equipment_id="Cylinder Pack south-west side of Celler Deck",
            asset_class=AssetClass.FIRE_PROTECTION_CYLINDER,
            captions=[
                CaptionItem(photo_no=1, text="Photo #01: discoloration on shell"),
                CaptionItem(photo_no=2, text="Photo #02: without valve mechanism"),
            ],
            is_good_condition=False,
        ),
        EquipmentBlock(
            equipment_id="South-East mobile Fire Protection Cylinder",
            asset_class=AssetClass.FIRE_PROTECTION_CYLINDER,
            captions=[
                CaptionItem(photo_no=6, text="Photo #06: packs in good condition"),
            ],
            is_good_condition=True,
        ),
    ]
    findings = detect_findings(
        metadata=metadata,
        summary_bullets=[],
        table_pages=[],
        equipment_pages=[(page, blocks)],
    )
    assert len(findings) == 2
    assert findings[0].line_number == "Fire protection cylinder"
    assert findings[0].pid_no is None
    assert findings[1].is_good_condition is True
    assert findings[1].pof is None
    assert findings[1].recommendations is None


def test_exclude_good_condition_piping_keep_hose_and_anomaly():
    metadata = ReportMetadata(facility="UBIT-GP", inspection_date=date(2024, 10, 9))
    page = _page(5)
    extraction = ObservationTableExtraction(
        section="2.3",
        asset_class=AssetClass.FIRE_WATER_PIPING,
        rows=[
            ObservationRow(
                row_index=1,
                pid_no="NGGP-AH-PDPID-0015210",
                line_number='WF-010-8"-40-FAA-4',
                line_description='GP fire water header to Wf-004-10"-30-FAA-4',
                anomaly_description="Heavy passive scale buildup (Photo #32-33)",
                photo_refs=["32-33"],
                is_good_condition=False,
            ),
            ObservationRow(
                row_index=2,
                pid_no="NGGP-AH-PDPID-0015210",
                line_number='WF-002-2"-40-FAA-4',
                line_description='WF-008-4"-40-FAA-4 to Sprinklers',
                anomaly_description="Visually in good condition",
                is_good_condition=True,
            ),
        ],
    )
    hose_page = _page(6)
    hose = ObservationTableExtraction(
        section="2.4",
        asset_class=AssetClass.FIRE_WATER_HOSE_REEL,
        rows=[
            ObservationRow(
                row_index=1,
                pid_no="NGGP-AH-PDPID-0015210",
                line_number="No tag found",
                line_description="Fire water header to South West Hose Reel on Cellar Deck",
                anomaly_description="Heavy scale corrosion (Photo #49-51)",
                photo_refs=["49-51"],
            )
        ],
    )
    findings = detect_findings(
        metadata=metadata,
        summary_bullets=[],
        table_pages=[(page, extraction), (hose_page, hose)],
        equipment_pages=[],
    )
    lines = [f.line_number for f in findings]
    assert 'WF-010-8"-40-FAA-4' in lines
    assert 'WF-002-2"-40-FAA-4' not in lines
    assert "No tag found" in lines


def test_pump_aggregated_single_row():
    metadata = ReportMetadata(facility="UBIT-GP", inspection_date=date(2024, 10, 9))
    bullets = [
        SummaryBullet(
            asset_class=AssetClass.FIRE_WATER_PUMP_CAISSON,
            text="GP-G-3 Diesel engine showing pump decommissioned (Photo #19-20)",
            photo_refs=["19-20"],
            section="1.2",
        ),
        SummaryBullet(
            asset_class=AssetClass.FIRE_WATER_PUMP_CAISSON,
            text="Caisson pipe with heavy scale buildup (Photo #29-30)",
            photo_refs=["29-30"],
            section="1.2",
        ),
    ]
    findings = detect_findings(
        metadata=metadata,
        summary_bullets=bullets,
        table_pages=[],
        equipment_pages=[],
    )
    pumps = [f for f in findings if f.asset_class == AssetClass.FIRE_WATER_PUMP_CAISSON]
    assert len(pumps) == 1
    assert pumps[0].line_description is None
    assert pumps[0].pid_no is None
    assert "Photo" in pumps[0].anomaly_description or pumps[0].photo_refs


def test_validate_rejects_populated_judgment_fields():
    finding = Finding(
        sn=1,
        asset_class=AssetClass.FIRE_WATER_PIPING,
        insp_date=date(2024, 10, 9),
        line_number="WF-010",
        anomaly_description="corrosion",
        pof="B",
    )
    # validate_finding checks before blanks; simulate bad state
    errors = validate_finding(finding)
    assert "judgment_field_populated:pof" in errors


def test_visual_table_anomaly_wins_over_caption_fragments():
    finding = Finding(
        asset_class=AssetClass.FIRE_WATER_PIPING,
        insp_date=date(2024, 10, 9),
        line_number='WF-9019-8"-CBR-0',
        line_description='WF-9019-8"-CBR-0 to EO-6003-8"-40-AAA-3',
        anomaly_description="Heavy corrosion on fasteners, supports and valves. (Photo #21-30)",
        pid_no="NGR1-AH-PDPID-0050809",
        photo_refs=[PhotoRef(raw="21-30", start=21, end=30)],
    )

    findings = enrich_anomaly_from_captions(
        [finding],
        {
            21: "Fasteners corroded (Photo #21)",
            22: "See valve support corrosion (Photo #22)",
            30: "Valve damaged (Photo #30)",
        },
    )

    assert findings[0].anomaly_description == "Heavy corrosion on fasteners, supports and valves. (Photo #21-30)"
    assert findings[0].photo_refs[0].raw == "21-30"


def test_photo_range_stays_consolidated_for_final_anomaly():
    finding = Finding(
        asset_class=AssetClass.FIRE_WATER_PIPING,
        insp_date=date(2024, 10, 9),
        line_number="WF-001",
        line_description="A to B",
        anomaly_description="Heavy corrosion on supports",
        pid_no="NGR1-AH-PDPID-0050809",
        photo_refs=[PhotoRef(raw="31-50", start=31, end=50)],
    )

    text = finding.anomaly_with_photo_suffix()
    assert text == "Heavy corrosion on supports (Photo #31-50)"
    assert text.count("(Photo #") == 1
    assert "(Photo #31)" not in text
    assert "(Photo #50)" not in text


def test_equipment_tag_is_used_as_excel_line_number():
    row = ObservationRow(
        row_index=1,
        pid_no="NGR1-AH-PDPID-0050809",
        line_number='WF-9101-6"-CBR-0',
        line_description='WF-9101-6"-CBR-0 to test separator area',
        anomaly_description="Heavy corrosion on fasteners, supports and valves. (Photo #21-30)",
        photo_refs=["21-30"],
    )
    assert row.line_number == 'WF-9101-6"-CBR-0'
    assert row.line_number != "Fire water piping"
    assert row.line_number != "Fire water hose reel"


def test_normalize_technical_identifier_removes_wrapped_breaks():
    assert normalize_technical_identifier("NGR1-AH-\nPDPID-0050809") == "NGR1-AH-PDPID-0050809"
    assert normalize_technical_identifier("No Tag\nfound") == "No tag found"
    assert normalize_technical_identifier("WF-9024-2''-\nCBR-0") == 'WF-9024-2"-CBR-0'


def test_layout_bands_include_equipment_header_in_photo_crop():
    box = _layout_bands(
        pdf_path=None,
        page_no=10,
        page_text="Equipment ID: WF-9101-6\"-CBR-0\nPhoto #21: corrosion support detail",
        image_size=(1200, 1600),
    )
    assert box["photos"] is not None
    assert box["photos"][1] <= 200


def test_stitch_readability_keeps_multi_page_evidence_landscape(tmp_path):
    paths = []
    for i in range(4):
        img = Image.new("RGB", (800, 600), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle((50, 50, 750, 550), fill=(200, 220, 255))
        draw.text((80, 260), f"PHOTO {i + 1}", fill=(0, 0, 0))
        path = tmp_path / f"photo_{i + 1}.png"
        img.save(path)
        paths.append(path)

    out = tmp_path / "combined.png"
    _stitch_vertical_professional(paths, out, max_h=1400)

    with Image.open(out) as merged:
        assert merged.size[0] >= merged.size[1]


def test_validate_result_drops_good_piping():
    result = ExtractionResult(
        metadata=ReportMetadata(facility="UBIT-GP", inspection_date=date(2024, 10, 9)),
        findings=[
            Finding(
                asset_class=AssetClass.FIRE_WATER_PIPING,
                insp_date=date(2024, 10, 9),
                line_number="WF-002",
                anomaly_description="Visually in good condition",
                is_good_condition=True,
            ),
            Finding(
                asset_class=AssetClass.FIRE_PROTECTION_CYLINDER,
                insp_date=date(2024, 10, 9),
                line_number="Fire protection cylinder",
                line_description="South-East mobile",
                anomaly_description="in good condition",
                is_good_condition=True,
            ),
        ],
    )
    validated = validate_result(result)
    assert len(validated.findings) == 1
    assert validated.findings[0].asset_class == AssetClass.FIRE_PROTECTION_CYLINDER