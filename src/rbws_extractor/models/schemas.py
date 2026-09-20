"""JSON schema helpers for RBWS extraction payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rbws_extractor.models.findings import ExtractionResult, Finding


def finding_json_schema() -> dict[str, Any]:
    return Finding.model_json_schema()


def extraction_result_json_schema() -> dict[str, Any]:
    return ExtractionResult.model_json_schema()


def write_schemas(output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    finding_path = output_dir / "finding.schema.json"
    result_path = output_dir / "extraction_result.schema.json"
    finding_path.write_text(json.dumps(finding_json_schema(), indent=2))
    result_path.write_text(json.dumps(extraction_result_json_schema(), indent=2))
    return finding_path, result_path