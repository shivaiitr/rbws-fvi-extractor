"""Validation against gold-standard RBWS extraction rules."""

from __future__ import annotations

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import AssetClass, ExtractionResult, Finding
from rbws_extractor.pipeline.rules import is_good_condition_text, load_rbws_config, should_blank_pid

logger = get_logger("pipeline.validate")


def validate_finding(finding: Finding) -> list[str]:
    errors: list[str] = []
    if not finding.line_number:
        errors.append("missing_line_number")
    if not finding.anomaly_description:
        errors.append("missing_anomaly_description")
    if finding.insp_date is None:
        errors.append("missing_insp_date")

    if should_blank_pid(finding.asset_class.value) and finding.pid_no:
        errors.append("pid_should_be_blank_for_asset_class")

    # Judgment fields must remain blank in automated output.
    for field in (
        "line_pressure_psi",
        "pof",
        "consq",
        "driver",
        "risk",
        "iru",
        "recommendations",
        "remarks",
    ):
        if getattr(finding, field) not in (None, ""):
            errors.append(f"judgment_field_populated:{field}")

    if finding.asset_class == AssetClass.FIRE_WATER_PUMP_CAISSON and finding.line_description:
        errors.append("pump_row_should_have_blank_line_description")

    if finding.asset_class in {
        AssetClass.FIRE_WATER_PIPING,
        AssetClass.UNTAGGED_LINE,
    } and finding.is_good_condition:
        errors.append("good_condition_piping_should_be_excluded")

    if is_good_condition_text(finding.anomaly_description) and finding.asset_class in {
        AssetClass.FIRE_WATER_PIPING,
        AssetClass.UNTAGGED_LINE,
    }:
        errors.append("anomaly_text_looks_good_condition_for_piping")

    return errors


def validate_result(result: ExtractionResult) -> ExtractionResult:
    cfg = load_rbws_config()
    warnings = list(result.warnings)
    review: list[Finding] = []

    if result.metadata.inspection_date is None:
        warnings.append("metadata.inspection_date missing")
    if not result.metadata.facility:
        warnings.append("metadata.facility missing")

    cleaned: list[Finding] = []
    for finding in result.findings:
        finding.apply_judgment_blanks()
        errors = validate_finding(finding)
        if errors:
            finding.needs_review = True
            finding.review_reasons = sorted(set(finding.review_reasons + errors))
            # Drop illegal good-condition piping rows entirely.
            if "good_condition_piping_should_be_excluded" in errors or (
                "anomaly_text_looks_good_condition_for_piping" in errors
            ):
                warnings.append(
                    f"Dropped SN={finding.sn} good-condition piping/untagged row"
                )
                continue
            review.append(finding)
        cleaned.append(finding)

    # Re-number after drops
    for idx, finding in enumerate(cleaned, start=1):
        finding.sn = idx

    if not cleaned:
        warnings.append("no_findings_extracted")

    # Soft expectation from gold workbook: cylinders should exist if report is FWS scope
    cylinder_count = sum(
        1 for f in cleaned if f.asset_class == AssetClass.FIRE_PROTECTION_CYLINDER
    )
    if cylinder_count == 0:
        warnings.append("no_cylinder_findings_detected")

    include_good_cyl = cfg.get("row_rules", {}).get(
        "include_good_condition_cylinders", True
    )
    if include_good_cyl:
        good_cyl = sum(
            1
            for f in cleaned
            if f.asset_class == AssetClass.FIRE_PROTECTION_CYLINDER and f.is_good_condition
        )
        logger.info("Good-condition cylinder rows: %s", good_cyl)

    result.findings = cleaned
    result.needs_review = review
    result.warnings = warnings
    logger.info(
        "Validation complete: findings=%s needs_review=%s warnings=%s",
        len(cleaned),
        len(review),
        len(warnings),
    )
    return result