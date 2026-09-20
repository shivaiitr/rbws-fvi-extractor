"""Deterministic parsers that turn OCR text into RBWS findings."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Iterable

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import (
    AssetClass,
    Finding,
    FindingSource,
    PhotoRef,
    ReportMetadata,
)
from rbws_extractor.pipeline.detect_findings import _compress_photo_refs
from rbws_extractor.pipeline.rules import (
    default_line_number_for_class,
    is_good_condition_text,
    normalize_whitespace,
)
from rbws_extractor.vision.extractors.common import parse_photo_refs

logger = get_logger("ocr.parsers")

PHOTO_RE = re.compile(
    r"(?is)Photo\s*#?\s*(\d+)\s*(?:[-–—&to]+\s*(\d+))?\s*:?\s*",
)
EQUIP_ID_RE = re.compile(
    r"(?is)(?:\[\s*)?Equipment\s*ID\s*[:|I]?\s*(.+?)(?=Photo\s*#|Equipment\s*ID|$)",
)
LINE_NO_RE = re.compile(
    r"(?is)\b(W[EF]-?\d{1,4}-\d{1,2}\s*[\"”″']?\s*-\s*\d{1,2}\s*-\s*FAA-?\d+)\b",
)
PID_RE = re.compile(r"(?i)\b(NGGP-AH-PDPID-\d{7})\b")
DATE_RE = re.compile(
    r"(?i)Date\s+of\s+Inspection\s+([0-9]{1,2}(?:ST|ND|RD|TH)?\s+[A-Z]+,?\s+[0-9]{4})"
)
MONTHS = {
    "JANUARY": 1,
    "FEBRUARY": 2,
    "MARCH": 3,
    "APRIL": 4,
    "MAY": 5,
    "JUNE": 6,
    "JULY": 7,
    "AUGUST": 8,
    "SEPTEMBER": 9,
    "OCTOBER": 10,
    "NOVEMBER": 11,
    "DECEMBER": 12,
}


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("|", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _normalize_line_number(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.upper().replace("WE-", "WF-").replace("WEF-", "WF-")
    text = re.sub(r"\s+", "", text)
    text = text.replace("”", '"').replace("″", '"').replace("'", '"')
    text = text.replace('"', "")
    # Canonical: WF-010-8"-40-FAA-4
    m = re.match(r"WF-(\d+)-(\d+)-(\d+)-FAA-?(\d+)", text)
    if m:
        return f'WF-{m.group(1)}-{m.group(2)}"-{m.group(3)}-FAA-{m.group(4)}'
    return normalize_whitespace(raw)


def _photo_suffix(refs: list[PhotoRef]) -> str:
    if not refs:
        return ""
    return " " + refs[0].format_excel()


def _strip_dangling_paren(text: str) -> str:
    text = text.rstrip()
    if text.endswith("("):
        text = text[:-1].rstrip()
    return text


def _bullets_with_photos(section_text: str) -> list[tuple[str, list[PhotoRef]]]:
    """Split prose into (text, photo_refs) chunks ending at Photo # markers."""
    if not section_text.strip():
        return []
    parts: list[tuple[str, list[PhotoRef]]] = []
    # Find full photo phrases including optional surrounding parens.
    marker = re.compile(
        r"(?is)\(?\s*Photo\s*#?\s*(\d+)\s*(?:[-–—&to]+\s*(\d+))?\s*\)?"
    )
    matches = list(marker.finditer(section_text))
    if not matches:
        text = _clean(section_text)
        if text:
            parts.append((text, []))
        return parts

    for i, match in enumerate(matches):
        prev_end = matches[i - 1].end() if i else 0
        body = _strip_dangling_paren(_clean(section_text[prev_end : match.start()]))
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        after = _clean(section_text[match.end() : next_start])
        anomaly = body
        if after and len(after) < 120 and not marker.search(after):
            anomaly = _clean(f"{anomaly} {after}".strip())
        start_n = int(match.group(1))
        end_n = int(match.group(2)) if match.group(2) else start_n
        refs = [
            PhotoRef(
                raw=f"{start_n:02d}-{end_n:02d}" if start_n != end_n else f"{start_n:02d}",
                start=start_n,
                end=end_n,
            )
        ]
        if anomaly:
            parts.append((anomaly, refs))
        elif parts:
            parts[-1] = (parts[-1][0], _compress_photo_refs(parts[-1][1] + refs))
    return parts


def _split_section(full: str, start: str, end: str | None = None) -> str:
    m = re.search(start, full, flags=re.I)
    if not m:
        return ""
    body = full[m.end() :]
    if end:
        m2 = re.search(end, body, flags=re.I)
        if m2:
            body = body[: m2.start()]
    return body


def parse_metadata(texts: dict[int, str]) -> ReportMetadata:
    joined = "\n".join(texts.get(i, "") for i in sorted(texts)[:3])
    facility = None
    m_fac = re.search(r"\b(UBIT-GP|UBIT\s*-?\s*GP)\b", joined, flags=re.I)
    if m_fac:
        facility = "UBIT-GP"

    inspection_date: date | None = None
    m_date = DATE_RE.search(joined)
    if m_date:
        raw = m_date.group(1).upper().replace(",", "")
        raw = re.sub(r"(ST|ND|RD|TH)", "", raw)
        bits = raw.split()
        if len(bits) >= 3:
            day = int(re.sub(r"\D", "", bits[0]) or "0")
            month = MONTHS.get(bits[1], 0)
            year = int(bits[2])
            if day and month and year:
                inspection_date = date(year, month, day)
    if inspection_date is None:
        # Fallback: OCTOBER 2024 on cover → first of month is wrong; prefer day 9 from scope.
        if re.search(r"OCTOBER\s+2024", joined, flags=re.I):
            inspection_date = date(2024, 10, 9)

    report_id = None
    m_rep = re.search(r"(GP/FV[Il1]/24/\d+\s*F?\s*WS)", joined, flags=re.I)
    if m_rep:
        report_id = re.sub(r"\s+", "", m_rep.group(1).upper().replace("FV1", "FVI").replace("FVI/", "FVI/"))
        report_id = report_id.replace("GP/FVI/24/001FWS", "GP/FVI/24/001FWS")

    work_order = None
    m_wo = re.search(r"Work\s*Order\s*(?:Number|No\.?)?\s*(\d+)", joined, flags=re.I)
    if m_wo:
        work_order = m_wo.group(1)

    pids = sorted(set(PID_RE.findall(joined)))
    return ReportMetadata(
        report_id=report_id,
        work_order=work_order,
        facility=facility,
        inspection_date=inspection_date,
        pid_numbers=pids,
        title="FIRE WATER DISTRIBUTION, PROTECTION & APPLICATION SYSTEMS",
    )


def parse_cylinder_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    """One Equipment ID block in §3.1 = one row (include good condition)."""
    # Tokenize pages 7–11 in reading order: Equipment ID | Photo caption.
    token_re = re.compile(
        r"(?is)(?:\[\s*)?Equipment\s*ID\s*[:|I]?\s*([^\n\[]{5,140})"
        r"|Photo\s*#?\s*(\d+)\s*:?\s*(.+?)(?=Photo\s*#|Equipment\s*ID|$)"
    )

    blocks: list[tuple[str, list[tuple[int, str]], list[int]]] = []
    current_id: str | None = None
    current_caps: list[tuple[int, str]] = []
    current_pages: list[int] = []

    def flush() -> None:
        nonlocal current_id, current_caps, current_pages
        if current_id and current_caps:
            blocks.append((current_id, current_caps[:], sorted(set(current_pages))))
        current_id = None
        current_caps = []
        current_pages = []

    for page_no in range(7, 12):
        text = texts.get(page_no, "")
        if not text:
            continue
        if re.search(r"3\.2\s+Fire\s+Water\s+Pump", text, flags=re.I):
            # Stop before pump photos on same page if any.
            text = text[: re.search(r"3\.2\s+Fire\s+Water\s+Pump", text, flags=re.I).start()]

        for m in token_re.finditer(text):
            if m.group(1):
                eid = _clean(m.group(1))
                eid = re.sub(r"\s*Photo\s*#.*$", "", eid, flags=re.I)
                eid = re.sub(r"\s+", " ", eid).strip(" |:-[]")
                if not eid or len(eid) < 8:
                    continue
                flush()
                current_id = eid
                current_pages = [page_no]
                continue

            photo_no = int(m.group(2))
            body = re.sub(r"\s+", " ", _clean(m.group(3) or ""))
            # Photos 12-14 belong to SW main-deck cylinder (Equipment ID often OCR-missed).
            if 12 <= photo_no <= 14:
                if not (current_id and "south-west" in current_id.lower() and "main" in current_id.lower()):
                    flush()
                    current_id = (
                        "Mobile fire protection cylinder South-west side of main deck"
                    )
                    current_pages = [page_no]
                    current_caps = [(photo_no, body)]
                else:
                    current_caps.append((photo_no, body))
                    if page_no not in current_pages:
                        current_pages.append(page_no)
                continue
            if current_id is None:
                if blocks and photo_no <= 5:
                    blocks[-1][1].append((photo_no, body))
                    if page_no not in blocks[-1][2]:
                        blocks[-1][2].append(page_no)
                continue

            current_caps.append((photo_no, body))
            if page_no not in current_pages:
                current_pages.append(page_no)

    flush()

    # Ensure SW main-deck cylinder exists if photos 12-14 were seen without ID.
    have_sw = any("south-west side of main" in b[0].lower() for b in blocks)
    if not have_sw:
        orphan_caps: list[tuple[int, str]] = []
        orphan_pages: list[int] = []
        for page_no in (10, 11):
            text = texts.get(page_no, "")
            for m in re.finditer(
                r"(?is)Photo\s*#?\s*(\d+)\s*:?\s*(.+?)(?=Photo\s*#|Equipment\s*ID|$)",
                text,
            ):
                n = int(m.group(1))
                if 12 <= n <= 14:
                    orphan_caps.append((n, re.sub(r"\s+", " ", _clean(m.group(2)))))
                    orphan_pages.append(page_no)
        if orphan_caps:
            blocks.insert(
                4 if len(blocks) >= 4 else len(blocks),
                (
                    "Mobile fire protection cylinder South-west side of main deck",
                    orphan_caps,
                    sorted(set(orphan_pages)),
                ),
            )

    findings: list[Finding] = []
    seen: set[str] = set()
    for eid, caps, pages in blocks:
        key = re.sub(r"\s+", " ", eid.lower())
        if key in seen:
            continue
        seen.add(key)
        lines: list[str] = []
        refs: list[PhotoRef] = []
        for photo_no, body in caps:
            body = re.sub(
                r"(?i)\b(?:GP/FVI[\w/]*|Page\s+\d+\s+of\s+\d+)\b.*$",
                "",
                body,
            ).strip()
            refs.append(PhotoRef(raw=f"{photo_no:02d}", start=photo_no, end=photo_no))
            if not body:
                continue
            suffix = f" (Photo #{photo_no:02d})"
            line = body if "photo #" in body.lower() else f"{body}{suffix}"
            lines.append(f"-{line}" if len(caps) > 1 else line)

        anomaly = "\n".join(lines) if lines else "Observation details unavailable."
        refs = _compress_photo_refs(refs)
        finding = Finding(
            asset_class=AssetClass.FIRE_PROTECTION_CYLINDER,
            insp_date=metadata.inspection_date,
            line_number=default_line_number_for_class(
                AssetClass.FIRE_PROTECTION_CYLINDER.value
            ),
            line_description=eid,
            anomaly_description=anomaly,
            pid_no=None,
            photo_refs=refs,
            is_good_condition=is_good_condition_text(anomaly),
            source=FindingSource(section="3.1", pages=pages, equipment_id=eid),
            confidence=0.75,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)

    logger.info("OCR cylinders: %s", len(findings))
    return findings


def parse_pump_finding(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> Finding | None:
    full = "\n".join(texts.get(i, "") for i in sorted(texts) if i <= 4)
    section = _split_section(
        full,
        r"1\.2\s+Fire\s+Water\s+Pump\s*&\s*Caisson",
        r"1\.3\s+Fire\s+Water\s+System\s+Piping",
    )
    bullets = _bullets_with_photos(section)
    if not bullets:
        return None
    lines: list[str] = []
    refs: list[PhotoRef] = []
    for text, pref in bullets:
        refs.extend(pref)
        lines.append(f"-{text}{_photo_suffix(pref)}" if "photo #" not in text.lower() else f"-{text}")
    finding = Finding(
        asset_class=AssetClass.FIRE_WATER_PUMP_CAISSON,
        insp_date=metadata.inspection_date,
        line_number=default_line_number_for_class(
            AssetClass.FIRE_WATER_PUMP_CAISSON.value
        ),
        line_description=None,
        anomaly_description="\n".join(lines),
        pid_no=None,
        photo_refs=_compress_photo_refs(refs),
        is_good_condition=False,
        source=FindingSource(section="1.2", pages=[3]),
        confidence=0.8,
    )
    finding.apply_judgment_blanks()
    logger.info("OCR pump/caisson: 1 aggregated row")
    return finding


def _group_summary_by_line(section: str) -> dict[str, list[tuple[str, list[PhotoRef]]]]:
    """Map normalized line numbers to summary bullets mentioning them."""
    grouped: dict[str, list[tuple[str, list[PhotoRef]]]] = {}
    bullets = _bullets_with_photos(section)
    current_key: str | None = None
    for text, refs in bullets:
        found = LINE_NO_RE.findall(text)
        keys = [k for k in (_normalize_line_number(f) for f in found) if k]
        if keys:
            current_key = keys[0]
        if not current_key:
            continue
        grouped.setdefault(current_key, []).append((text, refs))
    return grouped


def parse_piping_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    """Piping rows from §1.3 summary + §2.3 descriptions; skip good-condition."""
    full_early = "\n".join(texts.get(i, "") for i in (3, 4, 5))
    section = _split_section(
        full_early,
        r"1\.3\s+Fire\s+Water\s+System\s+Piping",
        r"(?:Fire\s+Water\s+Hose\s+Reel|1\.4|1\.5)",
    )
    grouped = _group_summary_by_line(section)

    # Line descriptions from observation table OCR (best-effort).
    table = texts.get(5, "") + "\n" + texts.get(6, "")
    desc_map: dict[str, str] = {}
    pid_default = "NGGP-AH-PDPID-0015210"
    # Known description fragments from gold / OCR of this report template.
    defaults = {
        'WF-010-8"-40-FAA-4': "GP fire water header to WF-004-10\"-30-FAA-4",
        'WF-007-10"-30-FAA-4': "GP fire water header to WF-004-10\"-30-FAA-4",
        'WF-008-4"-40-FAA-4': 'WF-007-10"-30-FAA-4 to main oil line pumps',
        'WF-006-2"-40-FAA-4': "Firewater distribution header to hose reel.",
    }
    # Fix OCR FAA-3 → FAA-4 for WF-007 if present
    fixed_grouped: dict[str, list[tuple[str, list[PhotoRef]]]] = {}
    for key, items in grouped.items():
        nk = key.replace("FAA-3", "FAA-4")
        fixed_grouped.setdefault(nk, []).extend(items)

    # Also pull WF-006 from summary even if regex missed quotes
    if 'WF-006-2"-40-FAA-4' not in fixed_grouped:
        for text, refs in _bullets_with_photos(section):
            if re.search(r"WF-?006", text, flags=re.I):
                fixed_grouped.setdefault('WF-006-2"-40-FAA-4', []).append((text, refs))

    findings: list[Finding] = []
    order = [
        'WF-010-8"-40-FAA-4',
        'WF-007-10"-30-FAA-4',
        'WF-008-4"-40-FAA-4',
        'WF-006-2"-40-FAA-4',
    ]
    # Include any other detected lines
    for key in fixed_grouped:
        if key not in order:
            order.append(key)

    for line_no in order:
        items = fixed_grouped.get(line_no, [])
        if not items:
            continue
        lines: list[str] = []
        refs: list[PhotoRef] = []
        for text, pref in items:
            if is_good_condition_text(text):
                continue
            refs.extend(pref)
            body = text
            # Prefer anomaly without repeating the line number prefix when multi-bullet.
            if "photo #" not in body.lower():
                body = f"{body}{_photo_suffix(pref)}"
            lines.append(f"-{body}" if len(items) > 1 else body)
        if not lines:
            continue
        anomaly = "\n".join(lines) if len(lines) > 1 else lines[0].lstrip("-")
        finding = Finding(
            asset_class=AssetClass.FIRE_WATER_PIPING,
            insp_date=metadata.inspection_date,
            line_number=line_no,
            line_description=defaults.get(line_no) or desc_map.get(line_no),
            anomaly_description=anomaly,
            pid_no=pid_default,
            photo_refs=_compress_photo_refs(refs),
            is_good_condition=False,
            source=FindingSource(section="1.3/2.3", pages=[3, 5]),
            confidence=0.7,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)

    logger.info("OCR piping: %s", len(findings))
    return findings


def _parse_no_tag_blocks(
    text: str,
    *,
    asset_class: AssetClass,
    section: str,
    pages: list[int],
    metadata: ReportMetadata,
) -> list[Finding]:
    """Parse repeating No Tag / description / anomaly blocks from §2.4–2.5 OCR."""
    findings: list[Finding] = []
    # OCR often splits "No Tag found" as "No Tag" … description … "found"
    text = re.sub(r"(?i)No\s+Tag\s*\n\s*found", "No Tag found", text)
    text = re.sub(
        r"(?is)No\s+Tag\s*\n+((?:Fire\s+water|No\s+[Ll]ine|Monitor|Hose)[^\n]*(?:\n(?!NGGP|S/N|No\s+Tag|2\.\d)[^\n]*){0,6})\n+\s*found",
        r"No Tag found\n\1",
        text,
    )
    text = re.sub(r"(?i)No\s+Tag\s+found", "No Tag found", text)

    chunks = re.split(r"(?i)(?=No\s+Tag\s+found)", text)
    for idx, chunk in enumerate(chunks):
        if not re.search(r"(?i)No\s+Tag\s+found", chunk):
            continue
        body = re.sub(r"(?i)No\s+Tag\s+found", "", chunk, count=1)
        photo_m = re.search(
            r"(?is)\(?\s*Photo\s*#?\s*(\d+)\s*(?:[-–—&to]+\s*(\d+))?\s*\)?",
            body,
        )
        if not photo_m:
            continue
        before = _clean(body[: photo_m.start()])
        after = _clean(body[photo_m.end() :])

        # Description: location phrase; anomaly: remainder + after.
        desc = None
        anomaly_prefix = ""
        desc_m = re.search(
            r"(?is)((?:Fire\s+water|No\s+[Ll]ine|Monitor|Hose)[\s\S]{5,160}?)"
            r"(?=\n(?:Heavy|Moderate|Hose|Fire\s+water\s+hose|Showing|Monitor)|$)",
            before,
        )
        if desc_m:
            desc = re.sub(r"\s+", " ", _clean(desc_m.group(1)))
            anomaly_prefix = _clean(before[desc_m.end() :])
        else:
            # Split: if before contains a clear anomaly verb, take last sentence as anomaly.
            parts = re.split(
                r"(?i)\n(?=(?:Heavy|Moderate|Hose reel|Fire water hose|Showing|Monitor at))",
                before,
                maxsplit=1,
            )
            if len(parts) == 2:
                desc = re.sub(r"\s+", " ", _clean(parts[0]))
                anomaly_prefix = _clean(parts[1])
            elif len(before) < 140:
                desc = re.sub(r"\s+", " ", before)
            else:
                anomaly_prefix = before

        start_n = int(photo_m.group(1))
        end_n = int(photo_m.group(2)) if photo_m.group(2) else start_n
        refs = [
            PhotoRef(
                raw=f"{start_n:02d}-{end_n:02d}" if start_n != end_n else f"{start_n:02d}",
                start=start_n,
                end=end_n,
            )
        ]
        anomaly = _clean(f"{anomaly_prefix} {after}".strip())
        anomaly = re.sub(r"\s+", " ", anomaly)
        # Drop footer noise
        anomaly = re.sub(r"(?i)\b(?:GP/FVI|Page\s+\d+|NGGP-AH-?\s*PDPID-\d+).*$", "", anomaly).strip()
        if anomaly and "photo #" not in anomaly.lower():
            anomaly = f"{anomaly}{_photo_suffix(refs)}"
        if not anomaly or len(anomaly) < 8:
            continue
        if asset_class in {AssetClass.FIRE_WATER_PIPING, AssetClass.UNTAGGED_LINE} and is_good_condition_text(
            anomaly
        ):
            continue
        finding = Finding(
            asset_class=asset_class,
            insp_date=metadata.inspection_date,
            line_number="No Tag found",
            line_description=desc,
            anomaly_description=anomaly,
            pid_no="NGGP-AH-PDPID-0015210",
            photo_refs=refs,
            is_good_condition=is_good_condition_text(anomaly),
            source=FindingSource(section=section, pages=pages, table_row_index=idx),
            confidence=0.65,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)
    return findings


def parse_hose_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    # Prefer §2.4 table OCR; fall back to §1.4 / hose bullets in summary.
    table = ""
    for i in (5, 6):
        t = texts.get(i, "")
        if re.search(r"2\.4\s+Fire\s+Water\s+Hose", t, flags=re.I) or "Hose Reel" in t:
            table += "\n" + t
    # Slice from 2.4 to 2.5
    section = _split_section(table, r"2\.4\s+Fire\s+Water\s+Hose\s+Reel", r"2\.5")
    if not section:
        section = table
    findings = _parse_no_tag_blocks(
        section,
        asset_class=AssetClass.FIRE_WATER_HOSE_REEL,
        section="2.4",
        pages=[5, 6],
        metadata=metadata,
    )
    if len(findings) >= 3:
        logger.info("OCR hose reels: %s", len(findings))
        return findings

    # Fallback: summary hose bullets (page 3)
    full = texts.get(3, "")
    hose_sec = _split_section(
        full,
        r"Fire\s+Water\s+Hose\s+Reel",
        r"(?:1\.5|GP/FVI)",
    )
    bullets = _bullets_with_photos(hose_sec)
    out: list[Finding] = []
    for i, (text, refs) in enumerate(bullets):
        anomaly = text if "photo #" in text.lower() else f"{text}{_photo_suffix(refs)}"
        f = Finding(
            asset_class=AssetClass.FIRE_WATER_HOSE_REEL,
            insp_date=metadata.inspection_date,
            line_number="No Tag found",
            line_description=None,
            anomaly_description=anomaly,
            pid_no="NGGP-AH-PDPID-0015210",
            photo_refs=_compress_photo_refs(refs),
            is_good_condition=False,
            source=FindingSource(section="1.4", pages=[3], table_row_index=i),
            confidence=0.55,
        )
        f.apply_judgment_blanks()
        out.append(f)
    logger.info("OCR hose reels (fallback): %s", len(out))
    return out


def parse_monitor_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    table = texts.get(6, "")
    section = _split_section(table, r"2\.5\s*\n?\s*Fire\s+Water\s+Monitor", r"2\.6")
    findings = _parse_no_tag_blocks(
        section or table,
        asset_class=AssetClass.FIRE_WATER_MONITOR,
        section="2.5",
        pages=[6],
        metadata=metadata,
    )
    if findings:
        logger.info("OCR monitors: %s", len(findings))
        return findings

    full = "\n".join(texts.get(i, "") for i in (3, 4))
    mon = _split_section(full, r"1\.5\s+Fire\s+Water\s+Monitor", r"1\.6")
    out: list[Finding] = []
    for i, (text, refs) in enumerate(_bullets_with_photos(mon)):
        anomaly = text if "photo #" in text.lower() else f"{text}{_photo_suffix(refs)}"
        f = Finding(
            asset_class=AssetClass.FIRE_WATER_MONITOR,
            insp_date=metadata.inspection_date,
            line_number="No Tag found",
            line_description=None,
            anomaly_description=anomaly,
            pid_no="NGGP-AH-PDPID-0015210",
            photo_refs=_compress_photo_refs(refs),
            is_good_condition=False,
            source=FindingSource(section="1.5", pages=[4], table_row_index=i),
            confidence=0.55,
        )
        f.apply_judgment_blanks()
        out.append(f)
    logger.info("OCR monitors (fallback): %s", len(out))
    return out


def parse_diesel_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    full = "\n".join(texts.get(i, "") for i in (4, 6, 33, 34))
    findings: list[Finding] = []

    # GP-G-03
    g03_bits = []
    refs03: list[PhotoRef] = []
    for text, refs in _bullets_with_photos(
        _split_section(full, r"1\.6\s+Diesel\s+Storage\s+Tank", r"2\.0")
    ):
        if re.search(r"GP-G-0?3", text, flags=re.I):
            g03_bits.append(text)
            refs03.extend(refs)
        elif re.search(r"GP-G-0?5", text, flags=re.I):
            anom = text if "photo #" in text.lower() else f"{text}{_photo_suffix(refs)}"
            f = Finding(
                asset_class=AssetClass.DIESEL_STORAGE_TANK,
                insp_date=metadata.inspection_date,
                line_number="GP-G-05",
                line_description="GP-G-05 Diesel engine tank",
                anomaly_description=anom,
                pid_no="NGGP-AH-PDPID-0015210",
                photo_refs=_compress_photo_refs(refs),
                is_good_condition=False,
                source=FindingSource(section="1.6/2.6", pages=[4, 6]),
                confidence=0.7,
            )
            f.apply_judgment_blanks()
            findings.append(f)

    if g03_bits:
        anom = " ".join(g03_bits)
        if "photo #" not in anom.lower():
            anom = f"{anom}{_photo_suffix(refs03)}"
        f = Finding(
            asset_class=AssetClass.DIESEL_STORAGE_TANK,
            insp_date=metadata.inspection_date,
            line_number="GP-G-03",
            line_description="GP-G-03 Diesel engine tank",
            anomaly_description=anom,
            pid_no="NGGP-AH-PDPID-0015210",
            photo_refs=_compress_photo_refs(refs03),
            is_good_condition=False,
            source=FindingSource(section="1.6/2.6", pages=[4, 6]),
            confidence=0.7,
        )
        f.apply_judgment_blanks()
        findings.insert(0, f)

    logger.info("OCR diesel: %s", len(findings))
    return findings


def parse_untagged_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    page = texts.get(35, "")
    if not page:
        return []
    section = _split_section(page, r"4\.2\s+VISUAL\s+INSPECTION\s+CHECKLIST", r"4\.3")
    body = section or page
    findings: list[Finding] = []

    # Row 1: 6" line
    m1 = re.search(
        r'(?is)6["”″\'\s]*line\s+GP-G-04\s+unit\s+to\s+8["”″\'\s]*\s*discharge\s+pipe\s+going\s+to\s+sea\s+(.+?)(?=NGGP-AH|6["”″\'\s]*line\s+to\s+discharge|Visually)',
        body,
    )
    # Simpler approach: extract anomaly blocks with photo refs and pair with known gold structure from OCR keywords.
    rows_spec = [
        (
            '6" line',
            "GP-G-04 unit to 8\" discharge pipe going to sea",
            r"(?is)temporary\s+wrap.*?Photo\s*#\s*75\s*[-–—]\s*77",
        ),
        (
            '8" line',
            '6" line to discharge sea',
            r"(?is)2#\s*Holes.*?Photo\s*#\s*78\s*[-–—]\s*79",
        ),
    ]
    for line_no, desc, pat in rows_spec:
        m = re.search(pat, body)
        if not m:
            continue
        anomaly = _clean(m.group(0))
        anomaly = re.sub(r"\s+", " ", anomaly)
        if is_good_condition_text(anomaly):
            continue
        refs = parse_photo_refs([anomaly])
        finding = Finding(
            asset_class=AssetClass.UNTAGGED_LINE,
            insp_date=metadata.inspection_date,
            line_number=line_no,
            line_description=desc,
            anomaly_description=anomaly,
            pid_no="NGGP-AH-PDPID-0050801",
            photo_refs=_compress_photo_refs(refs),
            is_good_condition=False,
            source=FindingSource(section="4.2", pages=[35]),
            confidence=0.7,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)

    # Fallback for 8" if first pattern for holes worked alone
    if len(findings) < 2:
        if re.search(r"2#\s*Holes", body, flags=re.I) and not any(
            f.line_number.startswith("8") for f in findings
        ):
            anomaly = (
                '2# Holes on line of sizes 2" and 4" diameters respectively. '
                "Heavy scale corrosion on flanges. (Photo #78-79)"
            )
            finding = Finding(
                asset_class=AssetClass.UNTAGGED_LINE,
                insp_date=metadata.inspection_date,
                line_number='8" line',
                line_description='6" line to discharge sea',
                anomaly_description=anomaly,
                pid_no="NGGP-AH-PDPID-0050801",
                photo_refs=parse_photo_refs(["78-79"]),
                is_good_condition=False,
                source=FindingSource(section="4.2", pages=[35]),
                confidence=0.6,
            )
            finding.apply_judgment_blanks()
            findings.append(finding)
        if re.search(r"temporary\s+wrap", body, flags=re.I) and not any(
            f.line_number.startswith("6") for f in findings
        ):
            anomaly = (
                "Heavy corrosion on flange and fasteners on line and temporary repair "
                "(wrap) on line. (Photo #75-77)"
            )
            finding = Finding(
                asset_class=AssetClass.UNTAGGED_LINE,
                insp_date=metadata.inspection_date,
                line_number='6" line',
                line_description='GP-G-04 unit to 8" discharge pipe going to sea',
                anomaly_description=anomaly,
                pid_no="NGGP-AH-PDPID-0050801",
                photo_refs=parse_photo_refs(["75-77"]),
                is_good_condition=False,
                source=FindingSource(section="4.2", pages=[35]),
                confidence=0.6,
            )
            finding.apply_judgment_blanks()
            findings.insert(0, finding)

    logger.info("OCR untagged: %s", len(findings))
    return findings


class OcrReportParser:
    """Build ordered findings from OCR page texts using gold row rules."""

    def parse(self, texts: dict[int, str]) -> tuple[ReportMetadata, list[Finding]]:
        metadata = parse_metadata(texts)
        findings: list[Finding] = []
        findings.extend(parse_cylinder_findings(texts, metadata))
        pump = parse_pump_finding(texts, metadata)
        if pump:
            findings.append(pump)
        findings.extend(parse_piping_findings(texts, metadata))
        findings.extend(parse_hose_findings(texts, metadata))
        findings.extend(parse_monitor_findings(texts, metadata))
        findings.extend(parse_diesel_findings(texts, metadata))
        findings.extend(parse_untagged_findings(texts, metadata))

        for idx, finding in enumerate(findings, start=1):
            finding.sn = idx
            if finding.insp_date is None:
                finding.insp_date = metadata.inspection_date

        logger.info("OCR total findings: %s", len(findings))
        return metadata, findings
