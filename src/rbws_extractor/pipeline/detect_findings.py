"""Finding detection — emulate the manual RBWS row creation workflow."""

from __future__ import annotations

from collections import defaultdict

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import (
    AssetClass,
    ClassifiedPage,
    Finding,
    FindingSource,
    PhotoRef,
    ReportMetadata,
)
from rbws_extractor.pipeline.rules import (
    compress_photo_refs as _compress_photo_refs,
    default_line_number_for_class,
    is_good_condition_text,
    load_rbws_config,
    normalize_technical_identifier,
    normalize_whitespace,
    should_blank_pid,
)
from rbws_extractor.vision.extractors.common import (
    EquipmentBlock,
    ObservationRow,
    ObservationTableExtraction,
    SummaryBullet,
    parse_photo_refs,
)

logger = get_logger("pipeline.detect")


def _anomaly_from_captions(block: EquipmentBlock) -> str:
    lines: list[str] = []
    refs: list[PhotoRef] = []
    for caption in block.captions:
        text = normalize_whitespace(caption.text) or ""
        if not text:
            continue
        # Strip leading "Photo # N:" if present; keep body.
        body = text
        lower = body.lower()
        if "photo" in lower and ":" in body:
            body = body.split(":", 1)[1].strip()
        photo_no = caption.photo_no
        suffix = f" (Photo #{photo_no:02d})" if photo_no is not None else ""
        lines.append(f"-{body}{suffix}" if len(block.captions) > 1 else f"{body}{suffix}")
        if photo_no is not None:
            refs.append(PhotoRef(raw=f"{photo_no:02d}", start=photo_no, end=photo_no))
    if not lines:
        return "Observation details unavailable from photo captions."
    if len(lines) == 1:
        return lines[0].lstrip("-")
    return "\n".join(lines)


def findings_from_cylinder_blocks(
    blocks_by_page: list[tuple[ClassifiedPage, list[EquipmentBlock]]],
    metadata: ReportMetadata,
) -> list[Finding]:
    """One Section 3 Equipment ID block for cylinders = one Excel row."""
    findings: list[Finding] = []
    seen_ids: set[str] = set()

    for page, blocks in blocks_by_page:
        for block in blocks:
            if block.asset_class != AssetClass.FIRE_PROTECTION_CYLINDER:
                # Heuristic fallback from equipment_id text
                eid = (block.equipment_id or "").lower()
                if "cylinder" not in eid and "afff" not in eid:
                    continue

            key = normalize_whitespace(block.equipment_id) or ""
            key_l = key.lower()
            if key_l in seen_ids:
                # Same equipment continued on next page: merge captions into existing.
                for existing in findings:
                    if (existing.source.equipment_id or "").lower() == key_l:
                        extra = _anomaly_from_captions(block)
                        existing.anomaly_description = (
                            f"{existing.anomaly_description.rstrip()}\n{extra}"
                        ).strip()
                        existing.photo_refs = _compress_photo_refs(
                            existing.photo_refs
                            + parse_photo_refs(
                                [str(c.photo_no) for c in block.captions if c.photo_no]
                            )
                        )
                        existing.source.pages = sorted(
                            set(existing.source.pages + [page.page_number])
                        )
                        existing.is_good_condition = (
                            existing.is_good_condition and block.is_good_condition
                        )
                        break
                continue

            seen_ids.add(key_l)
            anomaly = _anomaly_from_captions(block)
            good = block.is_good_condition or is_good_condition_text(anomaly)
            photo_refs = _compress_photo_refs(
                parse_photo_refs([str(c.photo_no) for c in block.captions if c.photo_no])
            )
            finding = Finding(
                asset_class=AssetClass.FIRE_PROTECTION_CYLINDER,
                insp_date=metadata.inspection_date,
                line_number=default_line_number_for_class(
                    AssetClass.FIRE_PROTECTION_CYLINDER.value
                ),
                line_description=key or None,
                anomaly_description=anomaly,
                pid_no=None,
                photo_refs=photo_refs,
                is_good_condition=good,
                source=FindingSource(
                    section="3.1",
                    pages=[page.page_number],
                    equipment_id=key or None,
                ),
                confidence=0.85,
            )
            finding.apply_judgment_blanks()
            findings.append(finding)

    logger.info("Detected %s cylinder findings", len(findings))
    return findings


def finding_from_pump_aggregate(
    summary_bullets: list[SummaryBullet],
    table_rows: list[tuple[ClassifiedPage, ObservationTableExtraction]],
    equipment_blocks: list[tuple[ClassifiedPage, list[EquipmentBlock]]],
    metadata: ReportMetadata,
) -> Finding | None:
    """Collapse all pump/caisson evidence into one RBWS row."""
    texts: list[str] = []
    pages: set[int] = set()
    refs: list[PhotoRef] = []

    for bullet in summary_bullets:
        if bullet.asset_class == AssetClass.FIRE_WATER_PUMP_CAISSON or any(
            k in (bullet.text or "").lower() for k in ("pump", "caisson")
        ):
            texts.append(f"-{bullet.text.strip()}")
            refs.extend(parse_photo_refs(bullet.photo_refs))

    for page, extraction in table_rows:
        if extraction.asset_class == AssetClass.FIRE_WATER_PUMP_CAISSON:
            for row in extraction.rows:
                if row.anomaly_description and row.anomaly_description.upper() != "N/A":
                    texts.append(f"-{row.anomaly_description.strip()}")
                    refs.extend(parse_photo_refs(row.photo_refs))
                    pages.add(page.page_number)

    for page, blocks in equipment_blocks:
        for block in blocks:
            eid = (block.equipment_id or "").lower()
            if block.asset_class == AssetClass.FIRE_WATER_PUMP_CAISSON or any(
                k in eid for k in ("pump", "caisson", "nggp-g")
            ):
                texts.append(_anomaly_from_captions(block))
                refs.extend(
                    parse_photo_refs([str(c.photo_no) for c in block.captions if c.photo_no])
                )
                pages.add(page.page_number)

    if not texts:
        return None

    # Deduplicate near-identical lines
    unique_texts: list[str] = []
    seen: set[str] = set()
    for line in texts:
        key = line.lower().strip("- ").strip()
        if key in seen:
            continue
        seen.add(key)
        unique_texts.append(line if line.startswith("-") else f"-{line}")

    finding = Finding(
        asset_class=AssetClass.FIRE_WATER_PUMP_CAISSON,
        insp_date=metadata.inspection_date,
        line_number=default_line_number_for_class(
            AssetClass.FIRE_WATER_PUMP_CAISSON.value
        ),
        line_description=None,
        anomaly_description="\n".join(unique_texts),
        pid_no=None,
        photo_refs=_compress_photo_refs(refs),
        is_good_condition=False,
        source=FindingSource(section="1.2/2.2/3.2", pages=sorted(pages)),
        confidence=0.8,
    )
    finding.apply_judgment_blanks()
    logger.info("Detected aggregated pump/caisson finding")
    return finding


def _asset_class_from_section(
    extraction: ObservationTableExtraction,
) -> AssetClass:
    if extraction.asset_class != AssetClass.UNKNOWN:
        return extraction.asset_class
    section = (extraction.section or "").lower()
    if section.startswith("2.3"):
        return AssetClass.FIRE_WATER_PIPING
    if section.startswith("2.4"):
        return AssetClass.FIRE_WATER_HOSE_REEL
    if section.startswith("2.5"):
        return AssetClass.FIRE_WATER_MONITOR
    if section.startswith("2.6"):
        return AssetClass.DIESEL_STORAGE_TANK
    if section.startswith("4"):
        return AssetClass.UNTAGGED_LINE
    return AssetClass.UNKNOWN


def findings_from_observation_tables(
    table_pages: list[tuple[ClassifiedPage, ObservationTableExtraction]],
    metadata: ReportMetadata,
) -> list[Finding]:
    """
    Create rows from §2 / §4 tables.

    Gold rules:
    - piping / untagged: exclude good-condition rows
    - hose reel / monitor / diesel: keep all rows (anomalies expected)
    - pump checklist rows are handled by aggregate function, skip here
    """
    cfg = load_rbws_config()
    findings: list[Finding] = []

    for page, extraction in table_pages:
        asset_class = _asset_class_from_section(extraction)
        if asset_class == AssetClass.FIRE_WATER_PUMP_CAISSON:
            continue

        for row in extraction.rows:
            anomaly = normalize_whitespace(row.anomaly_description) or ""
            if not anomaly:
                continue
            good = row.is_good_condition or is_good_condition_text(anomaly, cfg)

            if asset_class in {
                AssetClass.FIRE_WATER_PIPING,
                AssetClass.UNTAGGED_LINE,
            } and good:
                logger.debug(
                    "Skipping good-condition piping/untagged row on page %s: %s",
                    page.page_number,
                    anomaly[:80],
                )
                continue

            line_number = normalize_technical_identifier(row.line_number) or "No tag found"
            line_description = normalize_whitespace(row.line_description)
            pid_no = normalize_technical_identifier(row.pid_no)
            if should_blank_pid(asset_class.value, cfg):
                pid_no = None

            # Diesel tanks in gold excel use equipment tag as Line # and synthesized description
            if asset_class == AssetClass.DIESEL_STORAGE_TANK:
                if line_number and line_number.lower() != "no tag found":
                    if not line_description:
                        line_description = f"{line_number} Diesel engine tank"

            finding = Finding(
                asset_class=asset_class
                if asset_class != AssetClass.UNKNOWN
                else AssetClass.FIRE_WATER_PIPING,
                insp_date=metadata.inspection_date,
                line_number=line_number,
                line_description=line_description,
                anomaly_description=anomaly,
                pid_no=pid_no,
                photo_refs=parse_photo_refs(row.photo_refs),
                is_good_condition=good,
                source=FindingSource(
                    section=extraction.section,
                    pages=[page.page_number],
                    table_row_index=row.row_index,
                ),
                confidence=0.9,
            )
            finding.apply_judgment_blanks()
            findings.append(finding)

    logger.info("Detected %s observation-table findings", len(findings))
    return findings


def enrich_from_summary(
    findings: list[Finding],
    summary_bullets: list[SummaryBullet],
) -> list[Finding]:
    """
    Optionally enrich anomaly text using summary bullets that share photo refs / line numbers.
    Does not create new rows (avoids duplicates).
    """
    by_line: dict[str, list[SummaryBullet]] = defaultdict(list)
    for bullet in summary_bullets:
        key = (bullet.line_number or "").strip().lower()
        if key:
            by_line[key].append(bullet)

    for finding in findings:
        if finding.asset_class in {
            AssetClass.FIRE_PROTECTION_CYLINDER,
            AssetClass.FIRE_WATER_PUMP_CAISSON,
        }:
            continue
        key = finding.line_number.strip().lower()
        matches = by_line.get(key, [])
        if not matches:
            continue
        # If finding anomaly is short and summary has richer multi-bullet detail, prefer merge later.
        finding.source.notes = (
            (finding.source.notes or "")
            + f" | summary_matches={len(matches)}"
        ).strip(" |")
    return findings


def detect_findings(
    *,
    metadata: ReportMetadata,
    summary_bullets: list[SummaryBullet],
    table_pages: list[tuple[ClassifiedPage, ObservationTableExtraction]],
    equipment_pages: list[tuple[ClassifiedPage, list[EquipmentBlock]]],
) -> list[Finding]:
    """Run the full row-detection workflow in gold-standard order."""
    cylinders = findings_from_cylinder_blocks(equipment_pages, metadata)
    pump = finding_from_pump_aggregate(
        summary_bullets, table_pages, equipment_pages, metadata
    )
    table_findings = findings_from_observation_tables(table_pages, metadata)
    table_findings = enrich_from_summary(table_findings, summary_bullets)

    ordered: list[Finding] = []
    ordered.extend(cylinders)
    if pump:
        ordered.append(pump)
    ordered.extend(table_findings)

    for idx, finding in enumerate(ordered, start=1):
        finding.sn = idx
        if finding.insp_date is None:
            finding.insp_date = metadata.inspection_date

    logger.info("Total findings detected: %s", len(ordered))
    return ordered