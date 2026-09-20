"""Unit tests for PhotoRef and Finding models."""

from datetime import date

import pytest

from rbws_extractor.models.findings import AssetClass, Finding, PhotoRef, ReportMetadata
from rbws_extractor.models.schemas import finding_json_schema, write_schemas


@pytest.mark.parametrize(
    "raw,start,end",
    [
        ("Photo #01-02", 1, 2),
        ("(Photo # 32-33)", 32, 33),
        ("Photo #64 & 65", 64, 65),
        ("Photo #74", 74, 74),
        ("15 - 18", 15, 18),
    ],
)
def test_photo_ref_parse(raw, start, end):
    ref = PhotoRef.parse(raw)
    assert ref is not None
    assert ref.start == start
    assert ref.end == end


def test_finding_blanks_judgment_fields():
    finding = Finding(
        asset_class=AssetClass.FIRE_WATER_PIPING,
        insp_date=date(2024, 10, 9),
        line_number='WF-010-8"-40-FAA-4',
        anomaly_description="Heavy scale (Photo #32-33)",
        pof="B",
        recommendations="Blast & coat",
    )
    finding.apply_judgment_blanks()
    assert finding.pof is None
    assert finding.recommendations is None
    assert finding.line_pressure_psi is None


def test_metadata_date_parser():
    meta = ReportMetadata(inspection_date="9TH OCTOBER, 2024")
    assert meta.inspection_date == date(2024, 10, 9)


def test_schema_export(tmp_path):
    finding_path, result_path = write_schemas(tmp_path)
    assert finding_path.exists()
    assert result_path.exists()
    schema = finding_json_schema()
    assert "properties" in schema
    assert "line_number" in schema["properties"]