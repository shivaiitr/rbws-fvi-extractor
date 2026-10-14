"""Parse Jimcel FVI reports from clean native PDF text (tables + Equipment ID)."""

from __future__ import annotations

import re
from datetime import date

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import (
    AssetClass,
    Finding,
    FindingSource,
    PhotoRef,
    ReportMetadata,
)
from rbws_extractor.pipeline.rules import (
    compress_photo_refs as _compress_photo_refs,
    default_line_number_for_class,
    is_good_condition_text,
    normalize_whitespace,
)
from rbws_extractor.vision.extractors.common import parse_photo_refs

logger = get_logger("ocr.jimcel_native")

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

LINE_CBR_RE = re.compile(
    r"(?is)\b(WF-\d{3,5}-\d{1,2}\s*[\"”″'\u2019\u2018]{0,2}\s*-?\s*CBR-?\d+)\b"
)
LINE_FAA_RE = re.compile(
    r"(?is)\b(W[EF]-?\d{1,4}-\d{1,2}\s*[\"”″'\u2019\u2018]?\s*-\s*\d{1,2}\s*-\s*FAA-?\d+)\b"
)
PID_RE = re.compile(r"(?is)\b((?:NGGP|NGR1)-AH-?\s*PDPID-\d{7})\b")
PHOTO_RANGE_RE = re.compile(
    r"(?is)Photo\s*#?\s*(\d+)\s*(?:[-–—&to]+\s*(\d+))?"
)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"[ \t]+", " ", text.strip())
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def _norm_line(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.upper().replace("WE-", "WF-")
    text = re.sub(r"\s+", "", text)
    text = (
        text.replace("\u2019", '"')
        .replace("\u2018", '"')
        .replace("”", '"')
        .replace("″", '"')
        .replace("''", '"')
        .replace("'", '"')
    )
    # WF-9024-2"-CBR-0
    m = re.match(r'WF-(\d+)-(\d+)"?-CBR-?(\d+)', text.replace('"', ""))
    if m:
        return f'WF-{m.group(1)}-{m.group(2)}"-CBR-{m.group(3)}'
    m = re.match(r"WF-(\d+)-(\d+)-(\d+)-FAA-?(\d+)", text.replace('"', ""))
    if m:
        return f'WF-{m.group(1)}-{m.group(2)}"-{m.group(3)}-FAA-{m.group(4)}'
    return normalize_whitespace(raw)


def _norm_pid(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"\s+", "", raw.upper())
    m = re.match(r"((?:NGGP|NGR1)-AH-PDPID-\d{7})", text)
    return m.group(1) if m else normalize_whitespace(raw)


def _photos(text: str) -> list[PhotoRef]:
    refs: list[PhotoRef] = []
    for m in PHOTO_RANGE_RE.finditer(text):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        refs.append(
            PhotoRef(
                raw=f"{a:02d}-{b:02d}" if a != b else f"{a:02d}",
                start=a,
                end=b,
            )
        )
    return _compress_photo_refs(refs)


def _photo_suffix(refs: list[PhotoRef]) -> str:
    return f" {refs[0].format_excel()}" if refs else ""


def _joined(texts: dict[int, str], pages: list[int] | None = None) -> str:
    keys = pages if pages is not None else sorted(texts)
    return "\n".join(texts.get(i, "") for i in keys)


def _section(full: str, start: str, end: str | None = None) -> str:
    m = re.search(start, full, flags=re.I)
    if not m:
        return ""
    body = full[m.end() :]
    if end:
        m2 = re.search(end, body, flags=re.I)
        if m2:
            body = body[: m2.start()]
    return body


def looks_like_jimcel_native(texts: dict[int, str]) -> bool:
    sample = _joined(texts, list(range(1, min(6, max(texts) + 1))))
    return bool(
        re.search(r"SUMMARY OF INSPECTION FINDINGS", sample, flags=re.I)
        and re.search(r"(?:Fire\s+Water|Firewater)", sample, flags=re.I)
        and sum(len(texts.get(i, "")) for i in range(1, min(6, max(texts) + 1))) > 2000
    )


def parse_metadata(texts: dict[int, str]) -> ReportMetadata:
    joined = _joined(texts, [1, 2, 3])
    facility = None
    for pat in (r"\b(YOHO-YA)\b", r"\b(UBIT-GP)\b", r"\b(UBIT\s*-?\s*GP)\b"):
        m = re.search(pat, joined, flags=re.I)
        if m:
            facility = re.sub(r"\s+", "", m.group(1).upper().replace(" ", ""))
            if "UBIT" in facility:
                facility = "UBIT-GP"
            break

    inspection_date: date | None = None
    m = re.search(
        r"(?i)Date\s+of\s+Inspection\s+([0-9]{1,2}(?:ST|ND|RD|TH)?\s+[A-Za-z]+,?\s+[0-9]{4})",
        joined,
    )
    if m:
        raw = m.group(1).upper().replace(",", "")
        raw = re.sub(r"(ST|ND|RD|TH)", "", raw)
        bits = raw.split()
        if len(bits) >= 3:
            day = int(re.sub(r"\D", "", bits[0]) or "0")
            month = MONTHS.get(bits[1], 0)
            year = int(bits[2])
            if day and month and year:
                inspection_date = date(year, month, day)
    if inspection_date is None:
        m = re.search(
            r"(?i)Date\s+of\s+Inspection\s+([A-Za-z]+)\s+(\d{4})",
            joined,
        )
        if m:
            month = MONTHS.get(m.group(1).upper(), 0)
            year = int(m.group(2))
            if month and year:
                inspection_date = date(year, month, 1)
        else:
            m = re.search(r"(?i)\b([A-Za-z]+)\s+(20\d{2})\b", joined)
            if m and m.group(1).upper() in MONTHS:
                inspection_date = date(int(m.group(2)), MONTHS[m.group(1).upper()], 1)

    report_id = None
    m = re.search(r"\b([A-Z]{2}/FVI/\d{2}/\d+\s*F?W?)\b", joined, flags=re.I)
    if m:
        report_id = re.sub(r"\s+", "", m.group(1).upper())

    work_order = None
    m = re.search(r"Work\s*Order\s*(?:Number|#|No\.?)?\s*(\d+)", joined, flags=re.I)
    if m:
        work_order = m.group(1)

    pids = sorted(set(PID_RE.findall(joined)))
    return ReportMetadata(
        report_id=report_id,
        work_order=work_order,
        facility=facility,
        inspection_date=inspection_date,
        pid_numbers=pids,
        title="FIRE WATER DISTRIBUTION, PROTECTION & APPLICATION SYSTEMS",
    )


def parse_cylinder_blocks(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    """§3.1 Equipment ID blocks → one row each."""
    start_page = None
    end_page = None
    for page_no in sorted(texts):
        t = texts[page_no]
        if start_page is None and re.search(
            r"3\.1\s+Fire\s*(?:water\s*)?Protection\s+Cylinders", t, flags=re.I
        ):
            start_page = page_no
        if start_page is not None and re.search(
            r"3\.2\s+Fire\s+Water\s+Pump", t, flags=re.I
        ):
            end_page = page_no
            break
    if start_page is None:
        return []
    if end_page is None:
        end_page = max(texts)

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

    token_re = re.compile(
        r"(?is)Equipment\s*ID\s*\n?\s*(.+?)(?=\n\s*Photo\s*#|\n\s*Anonaly|\n\s*Anomaly|$)"
        r"|Photo\s*#?\s*(\d+)\s*:?\s*(.+?)(?=Photo\s*#|Equipment\s*ID|3\.2\s+Fire|$)"
    )

    for page_no in range(start_page, end_page + 1):
        text = texts.get(page_no, "")
        # Cut at 3.2 if on same page
        m_cut = re.search(r"3\.2\s+Fire\s+Water\s+Pump", text, flags=re.I)
        if m_cut:
            text = text[: m_cut.start()]
        for m in token_re.finditer(text):
            if m.group(1):
                eid = _clean(m.group(1))
                eid = re.sub(r"\s+", " ", eid).strip(" :-")
                if len(eid) < 6:
                    continue
                flush()
                current_id = eid
                current_pages = [page_no]
                continue
            photo_no = int(m.group(2))
            body = re.sub(r"\s+", " ", _clean(m.group(3) or ""))
            if current_id is None:
                continue
            current_caps.append((photo_no, body))
            if page_no not in current_pages:
                current_pages.append(page_no)
    flush()

    findings: list[Finding] = []
    seen: set[str] = set()
    for eid, caps, pages in blocks:
        key = eid.lower()
        if key in seen:
            continue
        seen.add(key)
        lines: list[str] = []
        refs: list[PhotoRef] = []
        for photo_no, body in caps:
            refs.append(PhotoRef(raw=f"{photo_no:02d}", start=photo_no, end=photo_no))
            if not body:
                continue
            suffix = f" (Photo #{photo_no:02d})"
            line = body if "photo #" in body.lower() else f"{body}{suffix}"
            lines.append(f"-{line}" if len(caps) > 1 else line)
        anomaly = "\n".join(lines) if lines else "Observation details unavailable."
        finding = Finding(
            asset_class=AssetClass.FIRE_PROTECTION_CYLINDER,
            insp_date=metadata.inspection_date,
            line_number=default_line_number_for_class(
                AssetClass.FIRE_PROTECTION_CYLINDER.value
            ),
            line_description=eid,
            anomaly_description=anomaly,
            pid_no=None,
            photo_refs=_compress_photo_refs(refs),
            is_good_condition=is_good_condition_text(anomaly),
            source=FindingSource(section="3.1", pages=pages, equipment_id=eid),
            confidence=0.9,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)
    logger.info("Native cylinders: %s", len(findings))
    return findings


def _section_not_applicable(texts: dict[int, str], heading: str) -> bool:
    sample = _joined(texts, list(range(1, min(8, max(texts) + 1))))
    return bool(
        re.search(
            rf"(?is){heading}\s*\(?\s*NOT\s+APPLICABLE\s*\)?",
            sample,
        )
    )


def parse_piping_table(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    """§2.3 piping observation table rows."""
    full = _joined(texts, list(range(1, min(8, max(texts) + 1))))
    section = _section(
        full,
        r"2\.3\s+Fire\s+Water\s+System\s+Piping",
        r"2\.4\s+Fire\s+Water\s+Hose",
    )
    if not section.strip():
        return []

    # Split on row numbers that precede a PID.
    chunks = re.split(r"(?=\n\s*\d+\s*\n\s*(?:NGR1|NGGP)-AH-)", "\n" + section)
    findings: list[Finding] = []
    default_pid = None
    pids = PID_RE.findall(section)
    if pids:
        default_pid = pids[0]

    for idx, chunk in enumerate(chunks):
        lines = LINE_CBR_RE.findall(chunk) or LINE_FAA_RE.findall(chunk)
        if not lines:
            continue
        line_no = _norm_line(lines[0])
        if not line_no:
            continue

        refs = _photos(chunk)
        anomaly = ""
        am = re.search(
            r"(?is)((?:Heavy|Moderate|Light|Visually|Temporary|Broken|Corrosion|Metal\s+loss)[\s\S]{5,300}?)"
            r"(?:Photo\s*#?\s*\d+\s*(?:[-–—&to]+\s*\d+)?\s*\)?)",
            chunk,
        )
        if am:
            anomaly = _clean(am.group(0))
            anomaly = re.sub(r"\s+", " ", anomaly)
            if refs and anomaly.lower().count("photo") == 0:
                anomaly = f"{anomaly}{_photo_suffix(refs)}"
            elif refs and anomaly.rstrip().endswith(")") is False and "photo" in anomaly.lower():
                anomaly = anomaly.rstrip() + ")"

        if not anomaly:
            continue
        if is_good_condition_text(anomaly):
            continue

        # Prefer the exact From/To relationship from the table, not a partial clause.
        desc = None
        from_to = re.search(
            r"(?is)(?:From\s+)?(?P<from>.*?)(?:\s+to\s+|\s+To\s+)(?P<to>.*?)(?=\s*(?:Heavy|Moderate|Light|Visually|Temporary|Broken|Corrosion|Photo|$))",
            chunk,
        )
        if from_to:
            from_text = re.sub(r"\s+", " ", _clean(from_to.group('from'))).strip(" -:")
            to_text = re.sub(r"\s+", " ", _clean(from_to.group('to'))).strip(" -:")
            if from_text and to_text:
                desc = f"{from_text} to {to_text}"
                if line_no and from_text.lower().startswith(line_no.lower()):
                    desc = desc[len(line_no) + 1 :].strip() if len(desc) > len(line_no) + 1 else desc
        if not desc:
            dm = re.search(
                r"(?is)((?:From\s+)?(?:fire\s+water|WF-|to\s+)[\s\S]{5,160}?)"
                r"(?=\n(?:Heavy|Moderate|Light|Visually)|Photo)",
                chunk,
            )
            if dm:
                desc = re.sub(r"\s+", " ", _clean(dm.group(1)))
                if line_no:
                    desc = re.sub(
                        re.escape(line_no).replace('\\"', "[\"'’″]*"),
                        "",
                        desc,
                        count=1,
                        flags=re.I,
                    )
                desc = re.sub(r"(?i)^WF-\d+[^\s]*\s*", "", desc).strip(" -:")
                desc = re.sub(r"(?i)^CBR-?\d+\s*", "", desc).strip(" -:")
                m2 = re.search(r"(?i)((?:From|to)\b.+)$", desc)
                if m2 and len(m2.group(1)) > 8:
                    desc = m2.group(1).strip()
                desc = re.sub(r"\s+", " ", desc).strip() or None
        if not desc and len(lines) > 1:
            extras = [_norm_line(x) for x in lines[1:]]
            extras = [x for x in extras if x and x != line_no]
            if extras:
                desc = " to ".join(extras[:2])

        pid = _norm_pid(default_pid)
        pm = PID_RE.search(chunk)
        if pm:
            pid = _norm_pid(pm.group(1))

        finding = Finding(
            asset_class=AssetClass.FIRE_WATER_PIPING,
            insp_date=metadata.inspection_date,
            line_number=line_no,
            line_description=desc,
            anomaly_description=anomaly,
            pid_no=pid,
            photo_refs=refs,
            is_good_condition=False,
            source=FindingSource(section="2.3", pages=[5], table_row_index=idx),
            confidence=0.9,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)

    # Deduplicate by line number keeping first
    seen: set[str] = set()
    unique: list[Finding] = []
    for f in findings:
        key = f.line_number.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    logger.info("Native piping: %s", len(unique))
    return unique


def parse_hose_table(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    """§2.4 hose reel table (No Tag found rows)."""
    if _section_not_applicable(texts, r"1\.4\s+Fire\s+Water\s+Hose\s+Reel"):
        # Still may have rows — check table
        pass
    full = _joined(texts, list(range(1, min(8, max(texts) + 1))))
    if re.search(r"(?is)2\.4\s+Fire\s+Water\s+Hose\s+Reel\s*\(?\s*NOT\s+APPLICABLE", full):
        return []
    section = _section(
        full,
        r"2\.4\s+Fire\s+Water\s+Hose\s+Reel",
        r"2\.5\s+Fire\s+Water\s+Monitor|3\.0\s+DESCRIPTION",
    )
    if not section.strip() or "No Tag" not in section:
        # Hose table sometimes sits after 2.6 header on same page as 2.4 label
        section = _section(
            full,
            r"2\.4\s+Fire\s+Water\s+Hose\s+Reel",
            r"3\.0\s+DESCRIPTION",
        )

    chunks = re.split(r"(?i)(?=No\s+Tag\s+found)", section)
    findings: list[Finding] = []
    default_pid = None
    pids = PID_RE.findall(section)
    if pids:
        default_pid = pids[0]

    for idx, chunk in enumerate(chunks):
        if not re.search(r"(?i)No\s+Tag\s+found", chunk):
            continue
        refs = _photos(chunk)
        if not refs:
            continue
        am = re.search(
            r"(?is)((?:Heavy|Moderate|Light|Hose)[\s\S]{5,200}?)"
            r"(?:Photo\s*#?\s*\d+\s*(?:[-–—&to]+\s*\d+)?)",
            chunk,
        )
        anomaly = re.sub(r"\s+", " ", _clean(am.group(0))) if am else ""
        if not anomaly:
            continue
        if "photo #" not in anomaly.lower():
            anomaly = f"{anomaly}{_photo_suffix(refs)}"

        dm = re.search(
            r"(?is)(Fire\s+water\s+header[\s\S]{5,100}?(?:deck|Deck))",
            chunk,
        )
        desc = re.sub(r"\s+", " ", _clean(dm.group(1))) if dm else None

        finding = Finding(
            asset_class=AssetClass.FIRE_WATER_HOSE_REEL,
            insp_date=metadata.inspection_date,
            line_number="No tag found",
            line_description=desc,
            anomaly_description=anomaly,
            pid_no=default_pid,
            photo_refs=refs,
            is_good_condition=False,
            source=FindingSource(section="2.4", pages=[5], table_row_index=idx),
            confidence=0.9,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)
    logger.info("Native hose reels: %s", len(findings))
    return findings


def parse_monitor_table(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    if _section_not_applicable(texts, r"(?:1\.5|2\.5)\s+Fire\s+Water\s+Monitor"):
        logger.info("Native monitors: N/A")
        return []
    return []


def parse_diesel_table(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    if _section_not_applicable(texts, r"(?:1\.6|2\.6)\s+Diesel\s+Storage\s+Tank"):
        logger.info("Native diesel: N/A")
        return []
    return []


def parse_pump(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> Finding | None:
    if _section_not_applicable(texts, r"(?:1\.2|2\.2|3\.2)\s+Fire\s+Water\s+Pump"):
        logger.info("Native pump/caisson: N/A")
        return None
    return None


def parse_jimcel_native(
    texts: dict[int, str],
) -> tuple[ReportMetadata, list[Finding]] | None:
    """Full native-text extraction for Jimcel FVI firewater reports."""
    if not looks_like_jimcel_native(texts):
        return None

    metadata = parse_metadata(texts)
    findings: list[Finding] = []
    findings.extend(parse_cylinder_blocks(texts, metadata))
    pump = parse_pump(texts, metadata)
    if pump:
        findings.append(pump)
    findings.extend(parse_piping_table(texts, metadata))
    findings.extend(parse_hose_table(texts, metadata))
    findings.extend(parse_monitor_table(texts, metadata))
    findings.extend(parse_diesel_table(texts, metadata))

    for idx, finding in enumerate(findings, start=1):
        finding.sn = idx
        if finding.insp_date is None:
            finding.insp_date = metadata.inspection_date

    logger.info(
        "Native Jimcel parse: facility=%s findings=%s date=%s",
        metadata.facility,
        len(findings),
        metadata.inspection_date,
    )
    return metadata, findings
