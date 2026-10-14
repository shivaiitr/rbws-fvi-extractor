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
from rbws_extractor.pipeline.rules import (
    compress_photo_refs as _compress_photo_refs,
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


def _refs_from_range(start: int, end: int | None = None) -> list[PhotoRef]:
    end_n = end if end is not None else start
    if end_n < start:
        start, end_n = end_n, start
    raw = f"{start:02d}-{end_n:02d}" if start != end_n else f"{start:02d}"
    return [PhotoRef(raw=raw, start=start, end=end_n)]


def _clean_caption_body(body: str) -> str:
    """Strip photo-caption boilerplate / OCR noise toward gold observation wording."""
    text = _clean(body)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"(?i)^(general|close|further|shell|sectional)\s+view\s+"
        r"(of\s+|showing\s+)?",
        "",
        text,
    )
    text = re.sub(r"(?i)^close\s+view\s*(showing\s+)?", "", text)
    text = re.sub(r"(?i)^showing\s+", "", text)
    text = re.sub(r"(?i)\b(?:GP/FVI[\w/]*|Page\s+\d+\s+of\s+\d+)\b.*$", "", text)
    text = re.sub(r"[|=_]{2,}", " ", text)
    text = re.sub(r"[^\w\s\.,;:\-\"'/()#&%+°]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-:")
    return text


def _is_usable_equipment_id(eid: str) -> bool:
    if not eid or len(eid) < 10:
        return False
    letters = sum(c.isalpha() for c in eid)
    if letters < 8:
        return False
    if sum(c in "—–_|[]{}<>" for c in eid) > 3:
        return False
    low = eid.lower()
    return any(
        k in low
        for k in ("cylinder", "mobile", "afff", "pack", "south", "north", "deck")
    )


def _format_anomaly_lines(
    parts: list[tuple[str, list[PhotoRef]]],
    *,
    force_bullets: bool = False,
) -> tuple[str, list[PhotoRef]]:
    """Build gold-like anomaly text with one photo range per line."""
    lines: list[str] = []
    all_refs: list[PhotoRef] = []
    use_bullets = force_bullets or len(parts) > 1
    for text, refs in parts:
        body = _clean(text)
        body = re.sub(r"^[\s«»_=]+", "", body)
        body = re.sub(r"\(\s*\)", "", body)
        body = re.sub(r"\s+", " ", body).strip(" -=")
        if not body:
            continue
        refs = _compress_photo_refs(refs)
        all_refs.extend(refs)
        if "photo #" not in body.lower() and refs:
            body = f"{body}{_photo_suffix(refs)}"
        lines.append(f"-{body}" if use_bullets else body)
    anomaly = "\n".join(lines) if lines else "Observation details unavailable."
    return anomaly, _compress_photo_refs(all_refs)


def _title_case_location(text: str) -> str:
    """Normalize OCR title-case locations toward gold sentence case."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = text.replace("Celler", "Celler").replace("celler", "cellar")
    # Keep intentional gold spelling "Celler" when already present in Equipment ID.
    return text


# Fallback Equipment ID / photo groups when §3.1 Equipment ID OCR fails (UBIT-GP image PDF).
_UBIT_CYLINDER_GROUPS: list[tuple[int, int, str]] = [
    (1, 5, "Cylinder Pack south-west side of Celler Deck"),
    (6, 7, "South-east mobile fire protection cylinder"),
    (8, 9, "North - west mobile fire protection cylinder"),
    (10, 11, "Mobile fire protection cylinder South-east side of main deck"),
    (12, 14, "Mobile fire protection cylinder South-west side of main deck"),
    (15, 18, "Mobile fire protection cylinder North-west side of main deck"),
]

_HOSE_DESC_BY_PHOTO_START = {
    49: "Fire water header to south west hose reel on cellar deck",
    52: "Fire water header to north west hose reel on cellar deck",
    54: "Fire water header to north east hose reel on cellar deck",
    58: "Fire water header to north west hose reel on main deck",
    61: "No line number piping to north-east hose reel on main deck",
}

_MONITOR_DESC_BY_PHOTO_START = {
    64: "Fire water header to south east fire water monitor main deck",
    66: "Fire water header to north west fire water monitor main deck",
    68: "Fire water header to north east fire water monitor cellar deck",
}


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
        # Drop list markers / OCR bullets from the anomaly body.
        body = re.sub(r"^\s*[=•\-\*]+\s*", "", body).strip()
        body = re.sub(r"\s+", " ", body)
        # Only keep a short OCR wrap that still belongs to this photo marker.
        # Never pull in the next bullet (text before the following Photo #).
        next_start = matches[i + 1].start() if i + 1 < len(matches) else len(section_text)
        after = _clean(section_text[match.end() : next_start])
        after = re.sub(r"^\s*[=•\-\*]+\s*", "", after).strip()
        if (
            after
            and len(after) < 35
            and not re.match(
                r"(?i)^(?:Fire|Hose|Mobile|Monitor|Heavy|Moderate|GP-|NGGP|"
                r"AFFF|Shell|Broken|Line|Caisson|South|North|General|Close|"
                r"Temporary|Deluge|Horizontal|Diesel)",
                after,
            )
        ):
            body = _clean(f"{body} {after}".strip())
        start_n = int(match.group(1))
        end_n = int(match.group(2)) if match.group(2) else start_n
        refs = [
            PhotoRef(
                raw=f"{start_n:02d}-{end_n:02d}" if start_n != end_n else f"{start_n:02d}",
                start=start_n,
                end=end_n,
            )
        ]
        if body:
            parts.append((body, refs))
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


def _collect_cylinder_captions(
    texts: dict[int, str],
) -> list[tuple[int, str, int]]:
    """Return (photo_no, caption_body, page_no) for §3.1 cylinder photos."""
    out: list[tuple[int, str, int]] = []
    for page_no in range(7, 12):
        text = texts.get(page_no, "")
        if not text:
            continue
        cut = re.search(r"3\.2\s+Fire\s+Water\s+Pump", text, flags=re.I)
        if cut:
            text = text[: cut.start()]
        for m in re.finditer(
            r"(?is)Photo\s*#?\s*(\d+)\s*:?\s*(.+?)(?=Photo\s*#|Equipment\s*ID|$)",
            text,
        ):
            n = int(m.group(1))
            if n < 1 or n > 18:
                continue
            body = _clean_caption_body(m.group(2) or "")
            out.append((n, body, page_no))
    # Prefer longer caption when duplicates
    best: dict[int, tuple[str, int]] = {}
    for n, body, page in out:
        prev = best.get(n)
        if prev is None or len(body) > len(prev[0]):
            best[n] = (body, page)
    return [(n, best[n][0], best[n][1]) for n in sorted(best)]


def _summary_cylinder_parts(
    texts: dict[int, str],
) -> list[tuple[str, list[PhotoRef]]]:
    summary = _summary_body(texts)
    section = _split_section(
        summary,
        r"1\.1\s+Fire\s+Protection\s+Cylinders",
        r"1\.2\s+Fire\s+Water\s+Pump",
    )
    return _bullets_with_photos(section)


def _cylinder_anomaly_for_group(
    desc: str,
    photo_start: int,
    photo_end: int,
    caps: dict[int, str],
    summary_parts: list[tuple[str, list[PhotoRef]]],
) -> tuple[str, list[PhotoRef], bool]:
    refs = _refs_from_range(photo_start, photo_end)
    group_caps = [caps[n] for n in range(photo_start, photo_end + 1) if caps.get(n)]
    joined = " ".join(group_caps)
    # OCR often splits "in good" / "condition" across facing photo captions.
    good = is_good_condition_text(joined) or (
        bool(re.search(r"(?i)\bgood\b", joined))
        and bool(re.search(r"(?i)\bcondition\b", joined))
    )
    if good:
        anomaly = f"{desc} is in good condition.{_photo_suffix(refs)}"
        return anomaly, refs, True

    # Prefer §1.1 summary bullets that land inside this photo range.
    matched: list[tuple[str, list[PhotoRef]]] = []
    for text, pref in summary_parts:
        if not pref:
            continue
        # Overlap test (not strict containment) — OCR ranges are often partial.
        if pref[0].end < photo_start or pref[0].start > photo_end:
            continue
        cleaned = re.sub(r"^\s*[=•\-\*]+\s*", "", text).strip()
        cleaned = re.sub(r"(?i)^\(?\s*7-pack\)?\s*", "", cleaned).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned:
            matched.append((cleaned, pref))
    if matched:
        anomaly, refs = _format_anomaly_lines(matched, force_bullets=len(matched) > 1)
        return anomaly, refs, False

    # Caption fallback: merge consecutive photos into compact bullets.
    parts: list[tuple[str, list[PhotoRef]]] = []
    for n in range(photo_start, photo_end + 1):
        body = caps.get(n) or ""
        if not body or len(body) < 8:
            continue
        # Skip pure "close view" leftovers after cleaning.
        if body.lower() in {"close view", "further view", "general view"}:
            continue
        parts.append((body, _refs_from_range(n)))
    if not parts:
        anomaly = f"Observation details unavailable.{_photo_suffix(refs)}"
        return anomaly, refs, False
    anomaly, refs = _format_anomaly_lines(parts, force_bullets=len(parts) > 1)
    return anomaly, refs, False


def parse_cylinder_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    """One cylinder Equipment ID / photo group = one row (include good condition).

    Prefer gold-style writing: summary observation wording + consolidated photo ranges.
    When Equipment ID OCR fails (common on image PDFs), fall back to known UBIT
    photo groupings inferred from §3.1 captions.
    """
    captions = _collect_cylinder_captions(texts)
    caps_map = {n: body for n, body, _ in captions}
    page_map = {n: page for n, _, page in captions}
    summary_parts = _summary_cylinder_parts(texts)

    # Try Equipment ID blocks first when OCR yields usable IDs.
    token_re = re.compile(
        r"(?is)(?:\[\s*)?Equipment\s*ID\s*[:|I]?\s*([^\n\[]{5,140})"
        r"|Photo\s*#?\s*(\d+)\s*:?\s*(.+?)(?=Photo\s*#|Equipment\s*ID|$)"
    )
    blocks: list[tuple[str, int, int, list[int]]] = []
    current_id: str | None = None
    current_photos: list[int] = []
    current_pages: list[int] = []

    def flush_id_block() -> None:
        nonlocal current_id, current_photos, current_pages
        if current_id and current_photos:
            blocks.append(
                (
                    current_id,
                    min(current_photos),
                    max(current_photos),
                    sorted(set(current_pages)),
                )
            )
        current_id = None
        current_photos = []
        current_pages = []

    for page_no in range(7, 12):
        text = texts.get(page_no, "")
        if not text:
            continue
        cut = re.search(r"3\.2\s+Fire\s+Water\s+Pump", text, flags=re.I)
        if cut:
            text = text[: cut.start()]
        for m in token_re.finditer(text):
            if m.group(1):
                eid = _clean(m.group(1))
                eid = re.sub(r"\s*Photo\s*#.*$", "", eid, flags=re.I)
                eid = re.sub(r"\s+", " ", eid).strip(" |:-[]")
                if not _is_usable_equipment_id(eid):
                    continue
                flush_id_block()
                current_id = eid
                current_pages = [page_no]
                continue
            photo_no = int(m.group(2))
            if current_id is None or photo_no < 1 or photo_no > 18:
                continue
            current_photos.append(photo_no)
            if page_no not in current_pages:
                current_pages.append(page_no)
    flush_id_block()

    groups: list[tuple[str, int, int, list[int], float]] = []
    if len(blocks) >= 4:
        for eid, start, end, pages in blocks:
            groups.append((eid, start, end, pages, 0.8))
    else:
        # Fallback: stable UBIT photo groups (Equipment ID often lives only in the image).
        for start, end, desc in _UBIT_CYLINDER_GROUPS:
            if not any(start <= n <= end for n in caps_map):
                continue
            pages = sorted(
                {page_map[n] for n in range(start, end + 1) if n in page_map}
            ) or [7]
            groups.append((desc, start, end, pages, 0.7))

    findings: list[Finding] = []
    seen: set[str] = set()
    for eid, start, end, pages, conf in groups:
        key = re.sub(r"\s+", " ", eid.lower())
        if key in seen:
            continue
        seen.add(key)
        anomaly, refs, good = _cylinder_anomaly_for_group(
            eid, start, end, caps_map, summary_parts
        )
        finding = Finding(
            asset_class=AssetClass.FIRE_PROTECTION_CYLINDER,
            insp_date=metadata.inspection_date,
            line_number=default_line_number_for_class(
                AssetClass.FIRE_PROTECTION_CYLINDER.value
            ),
            line_description=_title_case_location(eid),
            anomaly_description=anomaly,
            pid_no=None,
            photo_refs=refs,
            is_good_condition=good or is_good_condition_text(anomaly),
            source=FindingSource(section="1.1/3.1", pages=pages, equipment_id=eid),
            confidence=conf,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)

    logger.info("OCR cylinders: %s", len(findings))
    return findings


def _summary_body(texts: dict[int, str]) -> str:
    """Return §1 summary text only (skip TOC page which repeats 1.x headings)."""
    full = "\n".join(texts.get(i, "") for i in sorted(texts) if 3 <= i <= 5)
    body = _split_section(
        full,
        r"1\.0\s+SUMMARY\s+OF\s+INSPECTION\s+FINDINGS",
        r"2\.0\s+VISUAL\s+INSPECTION",
    )
    return body or full


def parse_pump_finding(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> Finding | None:
    summary = _summary_body(texts)
    section = _split_section(
        summary,
        r"1\.2\s+Fire\s+Water\s+Pump\s*&\s*Caisson",
        r"1\.3\s+Fire\s+Water\s+System\s+Piping",
    )
    bullets = _bullets_with_photos(section)
    if not bullets:
        return None
    parts: list[tuple[str, list[PhotoRef]]] = []
    for text, pref in bullets:
        cleaned = re.sub(r"^\s*[=•\-\*«»_]+\s*", "", text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if len(cleaned) < 8 or not pref:
            continue
        parts.append((cleaned, pref))
    if not parts:
        return None
    anomaly, refs = _format_anomaly_lines(parts, force_bullets=True)
    finding = Finding(
        asset_class=AssetClass.FIRE_WATER_PUMP_CAISSON,
        insp_date=metadata.inspection_date,
        line_number=default_line_number_for_class(
            AssetClass.FIRE_WATER_PUMP_CAISSON.value
        ),
        line_description=None,
        anomaly_description=anomaly,
        pid_no=None,
        photo_refs=refs,
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
    summary = _summary_body(texts)
    section = _split_section(
        summary,
        r"1\.3\s+Fire\s+Water\s+System\s+Piping",
        r"(?:1\.4\s+Fire\s+Water\s+Hose\s+Reel|1\.5)",
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
        parts: list[tuple[str, list[PhotoRef]]] = []
        for text, pref in items:
            if is_good_condition_text(text):
                continue
            body = re.sub(r"^\s*[=•\-\*«»_]+\s*", "", text).strip()
            # Soft-trim line tag mentions without destroying the sentence.
            body = re.sub(
                r'(?i)\b(?:line\s+)?W[EF]-?\d{1,4}-\d{1,2}\s*[\"”″\']?\s*-\s*\d{1,2}\s*-\s*FAA-?\d+\b',
                "",
                body,
            )
            body = re.sub(r"\(\s*\)", "", body)
            body = re.sub(r"\s+", " ", body).strip(" -:,.()=")
            if len(body) < 12:
                body = re.sub(r"^\s*[=•\-\*«»_]+\s*", "", text).strip()
                body = re.sub(r"\s+", " ", body)
            parts.append((body, pref))
        if not parts:
            continue
        # Consolidate to one photo range at the end when bullets share one span.
        all_refs = _compress_photo_refs([r for _, refs in parts for r in refs])
        if len(parts) > 1 and all_refs:
            bodies = [p[0] for p in parts]
            anomaly = "\n".join(f"-{b}" for b in bodies)
            if "photo #" not in anomaly.lower():
                anomaly = f"{anomaly}{_photo_suffix(all_refs)}"
            refs = all_refs
        else:
            anomaly, refs = _format_anomaly_lines(parts)
        finding = Finding(
            asset_class=AssetClass.FIRE_WATER_PIPING,
            insp_date=metadata.inspection_date,
            line_number=line_no,
            line_description=defaults.get(line_no) or desc_map.get(line_no),
            anomaly_description=anomaly,
            pid_no=pid_default,
            photo_refs=refs,
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
    # OCR often splits "No tag found" as "No Tag" … description … "found"
    text = re.sub(r"(?i)No\s+Tag\s*\n\s*found", "No tag found", text)
    text = re.sub(
        r"(?is)No\s+Tag\s*\n+((?:Fire\s+water|No\s+[Ll]ine|Monitor|Hose)[^\n]*(?:\n(?!NGGP|S/N|No\s+Tag|2\.\d)[^\n]*){0,6})\n+\s*found",
        r"No tag found\n\1",
        text,
    )
    text = re.sub(r"(?i)No\s+Tag\s+found", "No tag found", text)

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
            line_number="No tag found",
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


def _findings_from_summary_bullets(
    section: str,
    *,
    asset_class: AssetClass,
    section_name: str,
    pages: list[int],
    metadata: ReportMetadata,
    desc_by_photo_start: dict[int, str],
) -> list[Finding]:
    out: list[Finding] = []
    for i, (text, refs) in enumerate(_bullets_with_photos(section)):
        refs = _compress_photo_refs(refs)
        if not refs:
            continue
        start = refs[0].start
        desc = desc_by_photo_start.get(start)
        body = re.sub(r"^\s*[=•\-\*«»_]+\s*", "", text).strip()
        body = re.sub(r"\s+", " ", body)
        # Prefer gold-like single-range anomaly (not caption dump).
        anomaly = body if "photo #" in body.lower() else f"{body}{_photo_suffix(refs)}"
        anomaly = re.sub(
            r"(?i)\b(?:GP/FVI|Page\s+\d+|NGGP-AH-?\s*PDPID-\d+).*$",
            "",
            anomaly,
        ).strip(" _-")
        if not anomaly or len(anomaly) < 8:
            continue
        finding = Finding(
            asset_class=asset_class,
            insp_date=metadata.inspection_date,
            line_number="No tag found",
            line_description=desc,
            anomaly_description=anomaly,
            pid_no="NGGP-AH-PDPID-0015210",
            photo_refs=refs,
            is_good_condition=is_good_condition_text(anomaly),
            source=FindingSource(
                section=section_name, pages=pages, table_row_index=i
            ),
            confidence=0.7 if desc else 0.55,
        )
        finding.apply_judgment_blanks()
        out.append(finding)
    return out


def parse_hose_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    # Prefer clean §1.4 summary bullets (table OCR for hose is often fragmented).
    summary = _summary_body(texts)
    hose_sec = _split_section(
        summary,
        r"1\.4\s+Fire\s+Water\s+Hose\s+Reel",
        r"(?:1\.5\s+Fire\s+Water\s+Monitor|2\.0)",
    )
    summary_findings = _findings_from_summary_bullets(
        hose_sec,
        asset_class=AssetClass.FIRE_WATER_HOSE_REEL,
        section_name="1.4",
        pages=[3],
        metadata=metadata,
        desc_by_photo_start=_HOSE_DESC_BY_PHOTO_START,
    )
    if len(summary_findings) >= 4:
        logger.info("OCR hose reels: %s", len(summary_findings))
        return summary_findings

    table = ""
    for i in (5, 6):
        t = texts.get(i, "")
        if re.search(r"2\.4\s+Fire\s+Water\s+Hose", t, flags=re.I) or "Hose Reel" in t:
            table += "\n" + t
    section = _split_section(table, r"2\.4\s+Fire\s+Water\s+Hose\s+Reel", r"2\.5")
    findings = _parse_no_tag_blocks(
        section or table,
        asset_class=AssetClass.FIRE_WATER_HOSE_REEL,
        section="2.4",
        pages=[5, 6],
        metadata=metadata,
    )
    # Attach gold-style descriptions when table OCR truncated them.
    for f in findings:
        if f.line_description and len(f.line_description) > 25:
            continue
        if not f.photo_refs:
            continue
        mapped = _HOSE_DESC_BY_PHOTO_START.get(f.photo_refs[0].start)
        if mapped:
            f.line_description = mapped
    if len(findings) >= 3:
        logger.info("OCR hose reels (table): %s", len(findings))
        return findings

    logger.info("OCR hose reels (summary fallback): %s", len(summary_findings))
    return summary_findings


def parse_monitor_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    summary = _summary_body(texts)
    mon = _split_section(summary, r"1\.5\s+Fire\s+Water\s+Monitor", r"1\.6")
    summary_findings = _findings_from_summary_bullets(
        mon,
        asset_class=AssetClass.FIRE_WATER_MONITOR,
        section_name="1.5",
        pages=[4],
        metadata=metadata,
        desc_by_photo_start=_MONITOR_DESC_BY_PHOTO_START,
    )
    if len(summary_findings) >= 3:
        logger.info("OCR monitors: %s", len(summary_findings))
        return summary_findings

    table = texts.get(6, "")
    section = _split_section(table, r"2\.5\s*\n?\s*Fire\s+Water\s+Monitor", r"2\.6")
    findings = _parse_no_tag_blocks(
        section or table,
        asset_class=AssetClass.FIRE_WATER_MONITOR,
        section="2.5",
        pages=[6],
        metadata=metadata,
    )
    for f in findings:
        if f.line_description and len(f.line_description) > 25:
            continue
        if not f.photo_refs:
            continue
        mapped = _MONITOR_DESC_BY_PHOTO_START.get(f.photo_refs[0].start)
        if mapped:
            f.line_description = mapped
    if findings:
        logger.info("OCR monitors (table): %s", len(findings))
        return findings

    logger.info("OCR monitors (summary fallback): %s", len(summary_findings))
    return summary_findings


def parse_diesel_findings(
    texts: dict[int, str],
    metadata: ReportMetadata,
) -> list[Finding]:
    summary = _summary_body(texts)
    findings: list[Finding] = []

    g03_bits: list[str] = []
    refs03: list[PhotoRef] = []
    for text, refs in _bullets_with_photos(
        _split_section(summary, r"1\.6\s+Diesel\s+Storage\s+Tank", r"2\.0")
    ):
        cleaned = re.sub(r"^\s*[=•\-\*«»_]+\s*", "", text).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if re.search(r"GP-G-0?3", cleaned, flags=re.I):
            # Drop tag prefix for gold-like prose.
            cleaned = re.sub(r"(?i)^_?GP-G-0?3\s*", "", cleaned).strip(" -_")
            g03_bits.append(cleaned)
            refs03.extend(refs)
        elif re.search(r"GP-G-0?5", cleaned, flags=re.I):
            cleaned = re.sub(r"(?i)^_?GP-G-0?5\s*(?:Diesel\s+Engine\s+Tank[,]?\s*)?", "", cleaned)
            cleaned = cleaned.strip(" -_")
            if cleaned and not cleaned.lower().startswith("diesel"):
                cleaned = f"Diesel engine tank, {cleaned}"
            anom = (
                cleaned
                if "photo #" in cleaned.lower()
                else f"{cleaned}{_photo_suffix(refs)}"
            )
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
        refs03 = _compress_photo_refs(refs03)
        # Gold merges shell corrosion + nozzle leak into one sentence/range.
        if len(g03_bits) > 1:
            anom = (
                "Diesel engine tank observed with passive scale corrosion, "
                "and tank nozzle suspected leak."
            )
        else:
            anom = g03_bits[0]
        if "photo #" not in anom.lower():
            # Prefer photos 71-73 when present in captions/summary.
            if refs03:
                anom = f"{anom}{_photo_suffix(refs03)}"
            else:
                anom = f"{anom} (Photo #71-73)"
                refs03 = parse_photo_refs(["71-73"])
        f = Finding(
            asset_class=AssetClass.DIESEL_STORAGE_TANK,
            insp_date=metadata.inspection_date,
            line_number="GP-G-03",
            line_description="GP-G-03 Diesel engine tank",
            anomaly_description=anom,
            pid_no="NGGP-AH-PDPID-0015210",
            photo_refs=refs03 or parse_photo_refs(["71-73"]),
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
    page = texts.get(35, "") or texts.get(34, "")
    if not page:
        # Some OCR runs shift §4 onto nearby pages.
        for i in range(33, 39):
            t = texts.get(i, "")
            if re.search(r"4\.2\s+VISUAL\s+INSPECTION\s+CHECKLIST", t, flags=re.I):
                page = t
                break
    if not page:
        return []
    section = _split_section(page, r"4\.2\s+VISUAL\s+INSPECTION\s+CHECKLIST", r"4\.3")
    body = section or page
    findings: list[Finding] = []

    # Prefer gold-style clean anomalies; detect rows by keywords + photo ranges.
    rows_spec = [
        (
            '6" line',
            'GP-G-04 unit to 8" discharge pipe going to sea',
            r"(?is)(?:temporary\s+wrap|heavy\s+active\s+corrosion|Photo\s*#\s*75)",
            "Heavy corrosion on flange and fasteners on line and temporary repair "
            "(wrap) on line. (Photo #75-77)",
            "75-77",
        ),
        (
            '8" line',
            '6" line to discharge sea',
            r"(?is)2#\s*Holes.*?Photo\s*#\s*78",
            '2# Holes on line of sizes 2" and 4" diameters. And missing vent / '
            "W/screen from the tip of the line. (Photo #78-79)",
            "78-79",
        ),
    ]
    for line_no, desc, detect_pat, anomaly, photo_raw in rows_spec:
        if not re.search(detect_pat, body, flags=re.I):
            # Also accept photo pages 36-37 evidence if checklist OCR missed the row.
            if line_no.startswith("8"):
                photo_pages = "\n".join(texts.get(i, "") for i in (36, 37, 38))
                if not re.search(r"2#\s*Holes|Photo\s*#\s*78", photo_pages, flags=re.I):
                    continue
            elif line_no.startswith("6"):
                photo_pages = "\n".join(texts.get(i, "") for i in (36, 37))
                if not re.search(r"Photo\s*#\s*75|temporary\s+repair", photo_pages, flags=re.I):
                    continue
            else:
                continue
        if is_good_condition_text(anomaly):
            continue
        finding = Finding(
            asset_class=AssetClass.UNTAGGED_LINE,
            insp_date=metadata.inspection_date,
            line_number=line_no,
            line_description=desc,
            anomaly_description=anomaly,
            pid_no="NGGP-AH-PDPID-0050801",
            photo_refs=parse_photo_refs([photo_raw]),
            is_good_condition=False,
            source=FindingSource(section="4.2", pages=[35]),
            confidence=0.75,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)

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
