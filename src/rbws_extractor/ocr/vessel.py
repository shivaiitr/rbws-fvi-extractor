"""Pressure vessel workflow.

One FVI pressure-vessel PDF becomes one RBWS row per equipment.

Rules, in the order they run:

1. The Summary of Findings table is the master list, in that order. Column
   positions are read from that PDF's own header, however many pages the table
   runs, and for whichever facility printed it. Equipment tags and descriptions
   are taken from those columns as written. When a PDF has no summary table,
   each Equipment ID photo section is one row.
2. Every summary row is kept, including a decommissioned or out-of-site
   item. Its S/N is the number printed on that equipment's photo heading.
   A heading that prints several numbers (51 & 52) is one row, and that
   row keeps every equipment ID named there.
3. A photographed equipment is kept even when the manual sheet omitted it.
4. Equipment # is the summary-table tag. A shared photo heading joins
   each named tag with " & ".
5. Equipment Description is the summary-table description when that table
   is present. When a PDF has no summary table, the description is the
   name written on that Equipment ID heading.
6. Anomaly text is the caption printed under each photo for that equipment
   ID only. Each line starts with a dash. A calculation table (millimetres,
   pit size, ultrasonic thickness) is not a photo and is not part of the text.
7. Each saved picture is that PDF photo plus the full caption printed under
   it. The photo itself stays the original file. A repeated embed is kept
   once. Photos sit 2 across, or 4 across when 2 across would shrink them.
   This report family has no ISO sketch.
8. Insp Date, P&ID, pressure, POF, recommendations, and remarks stay blank.
   Those values in the manual workbook are engineer judgment, not PDF text.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import (
    AssetClass,
    ExtractionResult,
    Finding,
    FindingSource,
    ReportMetadata,
)
from rbws_extractor.ocr.edop_piping import PairedPhoto

logger = get_logger("ocr.vessel")

LAYOUT = "pressure_vessel"

_TITLE_RE = re.compile(
    r"(?i)full visual inspection of pressure vessels|pressure vessel\s*&\s*tank"
)
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_ \-–—]{1,}$")
_PHOTO_LINE_RE = re.compile(r"(?i)^photos?\s*#")
_PAGE_LINE_RE = re.compile(r"^\d+\s*[-–—]\s*\d+$|^--$")
_GOOD_RE = re.compile(r"(?i)\bgood (?:visual )?condition\b")
_OUT_OF_SITE_RE = re.compile(r"(?i)decommissioned|out of site")
_FINDING_LINE_RE = re.compile(
    r"(?i)corrosion|coating|stain|damage|debris|wrap|pit|crack|leak|"
    r"metal loss|scale|discolour|discolor|blast|observed|photo|"
    r"good condition|decommissioned|out of site|generally"
)
_PIT_LABEL_RE = re.compile(r"\b(PP\d+)\b", re.I)
_STOP_CAPTION_RE = re.compile(r"(?i)^(prepared|reviewed|verified|approved)$")


@dataclass
class SummaryEquipment:
    """One row of the Summary of Findings table."""

    sn: int
    tag: str
    description: str
    anomaly: str
    out_of_site: bool = False


@dataclass
class PhotoSection:
    """One ``Equipment ID`` block in the anomaly pages."""

    name: str
    tag: str
    page_index: int
    y: float
    tags: list[str] | None = None
    serials: list[int] | None = None

    def __post_init__(self) -> None:
        if not self.tags:
            self.tags = [self.tag] if self.tag else []
        if self.serials is None:
            self.serials = []


def is_pressure_vessel_report(text: str) -> bool:
    """True when the PDF is a pressure-vessel FVI, not Fire Water or piping."""
    return bool(_TITLE_RE.search(text or ""))


def build_vessel_result(
    pdf_path: Path,
    work_dir: Path,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> ExtractionResult:
    """Read one pressure-vessel PDF into findings. Photos are cropped into work_dir."""
    pdf_path = Path(pdf_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    try:
        opening = "\n".join(doc[i].get_text("text") or "" for i in range(min(3, doc.page_count)))
        summary_error: ValueError | None = None
        try:
            summary = _parse_summary(doc)
        except ValueError as exc:
            summary = []
            summary_error = exc
        sections = _photo_sections(doc)
        if summary:
            groups = _equipment_rows(summary, sections)
        else:
            groups = _rows_from_headings(sections)
        if not groups:
            if summary_error is not None:
                raise summary_error
            raise ValueError(
                "No equipment was found in this PDF. "
                "Use a pressure vessel FVI with a Summary of Findings table "
                "or Equipment ID photo sections."
            )
        findings: list[Finding] = []
        warnings: list[str] = []
        total = len(groups)
        if on_progress is not None:
            on_progress(0, total, "Reading the summary of findings")
        photo_cache: dict[tuple[int, float], list] = {}
        for index, (members, shared) in enumerate(groups, start=1):
            item = members[0]
            label = _join_unique(member.tag for member in members)
            if shared is not None:
                matched = [shared]
            else:
                matched = _match_sections(item.tag, sections, item.sn)
            photos = []
            for section in matched:
                cache_key = (section.page_index, round(section.y, 1))
                if cache_key not in photo_cache:
                    photo_cache[cache_key] = _photos_for_section(
                        doc, section, sections, work_dir, label
                    )
                photos.extend(photo_cache[cache_key])
            section = matched[0] if matched else None
            descriptions = []
            for member in members:
                text = member.description or (section.name if section is not None else "") or member.tag
                if text and text not in descriptions:
                    descriptions.append(text)
            anomaly = _anomaly_text(photos)
            if not anomaly:
                anomaly = _tidy(" ".join(member.anomaly for member in members if member.anomaly))
            if not anomaly:
                anomaly = "No anomaly text found"
                warnings.append(f"{label}: anomaly text was empty")
            paths = [p.path for p in photos]
            serials = _join_unique(str(member.sn) for member in members)
            findings.append(
                Finding(
                    asset_class=AssetClass.PRESSURE_VESSEL,
                    sn=item.sn,
                    line_number=label,
                    line_description=" & ".join(descriptions),
                    anomaly_description=anomaly,
                    photos_image_path=paths[0] if paths else None,
                    photo_image_paths=paths,
                    source=FindingSource(
                        section="pressure vessel",
                        equipment_id=label,
                        pages=[section.page_index + 1] if section else [],
                        notes=f"sn_label={serials}" if " & " in serials else f"summary_sn={item.sn}",
                    ),
                ).apply_judgment_blanks()
            )
            if on_progress is not None:
                on_progress(index, total, f"Equipment {label}")
        if not findings:
            raise ValueError("No pressure-vessel equipment rows were found in the summary table.")
        facility = _facility(opening) or "RBWS"
        result = ExtractionResult(
            metadata=ReportMetadata(
                facility=facility,
                title="Pressure vessel FVI",
                layout=LAYOUT,
            ),
            findings=findings,
            warnings=warnings,
            source_pdf=pdf_path,
        )
        # The shared result model numbers rows 1..n. Vessel S/N stays the
        # serial printed on the Equipment ID heading. A shared heading keeps
        # the first of its serials here; the workbook writes the full label.
        for finding, (members, _shared) in zip(result.findings, groups):
            finding.sn = members[0].sn
        return result
    finally:
        doc.close()


@dataclass
class _Header:
    sn_x: float
    id_x: float
    desc_x: float
    anomaly_x: float
    page_x: float
    y: float


@dataclass
class _Cols:
    sn_max: float
    id_max: float
    desc_max: float
    page_min: float


def _page_words(page) -> list:
    getter = getattr(page, "get_text", None)
    if getter is None:
        return []
    try:
        words = getter("words")
    except Exception:
        return []
    if not words or not isinstance(words, list):
        return []
    first = words[0]
    if not isinstance(first, (list, tuple)) or len(first) < 5:
        return []
    return words


def _page_text(page) -> str:
    getter = getattr(page, "get_text", None)
    if getter is None:
        return ""
    try:
        text = getter("text")
    except Exception:
        return ""
    return text or ""


def _header_anchors(words: list) -> _Header | None:
    """Column starts from the summary header line that names Equipment and Anomaly."""
    for word in words:
        label = (word[4] or "").upper().replace(" ", "")
        if label not in {"S/N", "SN"}:
            continue
        y = float(word[1])
        same = sorted(
            (item for item in words if abs(float(item[1]) - y) < 4 and float(item[0]) > float(word[0]) + 4),
            key=lambda item: float(item[0]),
        )
        equipment = [item for item in same if (item[4] or "").lower() == "equipment"]
        anomaly = next((item for item in same if (item[4] or "").lower() == "anomaly"), None)
        page = next((item for item in same if (item[4] or "").lower() == "page"), None)
        if not equipment or anomaly is None:
            continue
        desc_x = float(equipment[1][0]) if len(equipment) > 1 else float(anomaly[0])
        page_x = float(page[0]) if page is not None else float(anomaly[0]) + 160
        return _Header(
            sn_x=float(word[0]),
            id_x=float(equipment[0][0]),
            desc_x=desc_x,
            anomaly_x=float(anomaly[0]),
            page_x=page_x,
            y=y,
        )
    return None


def _aligned_left(words: list, header_x: float, y_min: float, y_max: float) -> float:
    """The x where that column's text actually starts, which can sit left of the heading."""
    grouped: dict[int, int] = {}
    for word in words:
        x = float(word[0])
        y = float(word[1])
        if not (y_min < y < y_max and header_x - 55 <= x <= header_x + 8):
            continue
        grouped[round(x)] = grouped.get(round(x), 0) + 1
    if not grouped:
        return header_x
    best = max(grouped.values())
    floor = 1 if best < 2 else max(2, int(best * 0.35))
    candidates = [x for x, count in grouped.items() if count >= floor]
    return float(min(candidates))


def _columns_for(header: _Header, words: list, page_height: float) -> _Cols:
    """Split the table where this report's own serials, ids, and descriptions sit."""
    y_max = page_height - 24
    y_min = header.y + 6
    body = [word for word in words if y_min < float(word[1]) < y_max]
    desc_left = _aligned_left(words, header.desc_x, y_min, y_max)
    anomaly_left = _aligned_left(words, header.anomaly_x, y_min, y_max)
    serial_rights = [
        float(word[2])
        for word in body
        if abs(float(word[0]) - header.sn_x) <= 36 and re.fullmatch(r"\d{1,3}", (word[4] or "").strip())
    ]
    sn_right = max(serial_rights) if serial_rights else header.sn_x + 16
    id_lefts = [
        float(word[0])
        for word in body
        if sn_right + 1 < float(word[0]) < desc_left - 4 and re.search(r"[A-Za-z]", word[4] or "")
    ]
    id_left = min(id_lefts) if id_lefts else header.id_x
    sn_max = (sn_right + id_left) / 2 if id_left > sn_right + 4 else sn_right + 6
    id_max = desc_left - 6
    if id_max <= sn_max + 8:
        id_max = max(id_left, header.id_x) + 48
    desc_max = anomaly_left - 6
    if desc_max <= id_max + 8:
        desc_max = id_max + 36
    return _Cols(
        sn_max=sn_max,
        id_max=id_max,
        desc_max=desc_max,
        page_min=header.page_x - 10,
    )


def _column_of(x: float, cols: _Cols) -> str | None:
    if x >= cols.page_min:
        return None
    if x < cols.sn_max:
        return "sn"
    if x < cols.id_max:
        return "id"
    if x < cols.desc_max:
        return "desc"
    return "anom"


def _word_lines(words: list, y_min: float, y_max: float) -> list[list]:
    chosen = [word for word in words if y_min < float(word[1]) < y_max]
    chosen.sort(key=lambda word: (float(word[1]), float(word[0])))
    lines: list[list] = []
    for word in chosen:
        y = float(word[1])
        if lines and abs(y - lines[-1][0]) <= 2.8:
            lines[-1][1].append(word)
        else:
            lines.append([y, [word]])
    return lines


def _sn_token(tokens: list[str]) -> int | None:
    for token in tokens:
        if re.fullmatch(r"\d{1,3}", token):
            return int(token)
    return None


def _blank_row(sn: int, y: float) -> dict:
    return {"sn": sn, "y": y, "id": [], "desc": [], "anom": []}


def _anchors_from_lines(lines: list[list], cols: _Cols) -> list[dict]:
    """One anchor per serial. A tag left below the last serial belongs to the next page's number."""
    parsed = [(line, _line_buckets(line, cols)) for line in lines]
    anchors = [
        _blank_row(sn, float(line[0]))
        for line, buckets in parsed
        if (sn := _sn_token(buckets["sn"])) is not None
    ]
    sn_ys = [anchor["y"] for anchor in anchors]
    id_lines = [
        (float(line[0]), " ".join(buckets["id"]).strip())
        for line, buckets in parsed
        if buckets["id"]
    ]
    primary: set[float] = set()
    for sy in sn_ys:
        near = [item for item in id_lines if abs(item[0] - sy) <= 22]
        if near:
            primary.add(min(near, key=lambda item: abs(item[0] - sy))[0])
    previous = ""
    for y, text in id_lines:
        if not text or y in primary:
            previous = text
            continue
        nearest = min(sn_ys, key=lambda sy: abs(sy - y)) if sn_ys else None
        wrapped = previous.endswith("-") or previous.endswith("(") or text.startswith("(")
        if nearest is None or (y > nearest + 14 and not wrapped):
            anchors.append(_blank_row(0, y))
        previous = text
    anchors.sort(key=lambda anchor: (anchor["y"], anchor["sn"] == 0))
    return anchors


def _fill_anchors(anchors: list[dict], lines: list[list], cols: _Cols, y_min: float, y_max: float) -> None:
    """Description and anomaly often start above the serial, so each row owns the band around it."""
    if not anchors:
        return
    bounds: list[tuple[float, float]] = []
    for index, anchor in enumerate(anchors):
        top = y_min if index == 0 else (anchors[index - 1]["y"] + anchor["y"]) / 2
        if index + 1 == len(anchors):
            bottom = y_max
        else:
            bottom = (anchor["y"] + anchors[index + 1]["y"]) / 2
        bounds.append((top, bottom))
    for line in lines:
        y = float(line[0])
        buckets = _line_buckets(line, cols)
        for index, (top, bottom) in enumerate(bounds):
            last = index + 1 == len(bounds)
            if top <= y < bottom or (last and top <= y <= bottom):
                _append_fields(anchors[index], buckets)
                break


def _tidy_tag(parts: list[str]) -> str:
    text = " ".join(parts).replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip()
    # A hyphen split from the next character by a line wrap is still one id.
    text = re.sub(r"\s*-\s*", "-", text)
    return text.strip(" -")


def _row_from(raw: dict) -> SummaryEquipment | None:
    tag = _tidy_tag(raw["id"])
    description = _tidy(" ".join(raw["desc"]))
    anomaly = _tidy(" ".join(raw["anom"]))
    if not tag and not description:
        return None
    return SummaryEquipment(
        sn=int(raw["sn"]),
        tag=tag or description,
        description=description,
        anomaly=anomaly,
        out_of_site=bool(_OUT_OF_SITE_RE.search(anomaly) or _OUT_OF_SITE_RE.search(description)),
    )


def _append_fields(raw: dict, buckets: dict[str, list[str]]) -> None:
    if buckets["id"]:
        raw["id"].append(" ".join(buckets["id"]))
    if buckets["desc"]:
        raw["desc"].append(" ".join(buckets["desc"]))
    if buckets["anom"]:
        raw["anom"].append(" ".join(buckets["anom"]))


def _continuation_top(words: list, page_height: float) -> float:
    """Body starts after the running title block. That block's height differs by report."""
    limit = min(page_height * 0.22, 180)
    lines = sorted({round(float(word[1]), 1) for word in words if 0 < float(word[1]) < limit})
    cut = 28.0
    best = 0.0
    for above, below in zip(lines, lines[1:]):
        gap = below - above
        if gap >= 12 and gap > best:
            best = gap
            cut = above + 2
    return cut


def _serials_continue(lines: list[list], cols: _Cols, last_sn: int) -> bool:
    """A later page is still the summary when its serials carry on from the previous page."""
    serials = [
        sn
        for line in lines
        if (sn := _sn_token(_line_buckets(line, cols)["sn"])) is not None
    ]
    if not serials:
        return False
    if last_sn <= 0:
        return True
    return any(last_sn < sn <= last_sn + 5 for sn in serials)


def _parse_summary_columns(doc: fitz.Document) -> list[SummaryEquipment]:
    """Read a Summary of Findings table by the columns printed on that report.

    Later pages often omit the header. Reading stops when the serials stop,
    which is the photo section, whatever that section is titled.
    """
    pages = list(doc)
    start: int | None = None
    header: _Header | None = None
    header_words: list = []
    header_page = None
    for index, page in enumerate(pages):
        words = _page_words(page)
        found = _header_anchors(words)
        if found is None:
            continue
        if re.search(r"(?i)summary of findings", _page_text(page)):
            start = index
            header = found
            header_words = words
            header_page = page
            break
    if start is None or header is None or header_page is None:
        return []
    height = float(getattr(getattr(header_page, "rect", None), "height", 800) or 800)
    cols = _columns_for(header, header_words, height)
    completed: list[dict] = []
    carry: dict | None = None
    last_sn = 0
    for index in range(start, len(pages)):
        page = pages[index]
        words = header_words if index == start else _page_words(page)
        if index > start and not words:
            break
        page_height = float(getattr(getattr(page, "rect", None), "height", height) or height)
        y_min = header.y + 6 if index == start else _continuation_top(words, page_height)
        y_max = page_height - 24
        lines = _word_lines(words, y_min, y_max)
        if index > start and not _serials_continue(lines, cols, last_sn):
            break
        anchors = _anchors_from_lines(lines, cols)
        _fill_anchors(anchors, lines, cols, y_min, y_max)
        if carry is not None:
            if anchors and anchors[0]["sn"] and not _tidy_tag(anchors[0]["id"]):
                anchors[0]["id"] = carry["id"] + anchors[0]["id"]
                anchors[0]["desc"] = carry["desc"] + anchors[0]["desc"]
                anchors[0]["anom"] = carry["anom"] + anchors[0]["anom"]
            elif _tidy_tag(carry["id"]) or carry["desc"] or carry["anom"]:
                if carry["sn"] == 0 and completed:
                    carry["sn"] = int(completed[-1]["sn"]) + 1
                completed.append(carry)
            carry = None
        if anchors and anchors[-1]["sn"] == 0:
            carry = anchors.pop()
        for anchor in anchors:
            if anchor["sn"]:
                last_sn = int(anchor["sn"])
        completed.extend(anchors)
    if carry is not None and _tidy_tag(carry["id"]):
        if carry["sn"] == 0 and completed:
            carry["sn"] = int(completed[-1]["sn"]) + 1
        completed.append(carry)
    rows = [row for raw in completed if (row := _row_from(raw)) is not None]
    return rows


def _line_buckets(line: list, cols: _Cols) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = {"sn": [], "id": [], "desc": [], "anom": []}
    for word in sorted(line[1], key=lambda item: float(item[0])):
        column = _column_of(float(word[0]), cols)
        if column is None:
            continue
        token = (word[4] or "").strip()
        if token:
            buckets[column].append(token)
    return buckets


def _summary_pages_text(doc: fitz.Document) -> str:
    """Summary table text, including a table that continues onto the next page."""
    chunks: list[str] = []
    started = False
    for page in doc:
        page_text = page.get_text("text") or ""
        if not started:
            if re.search(r"(?i)summary of findings", page_text) and re.search(r"(?i)equipment", page_text):
                started = True
                chunks.append(page_text)
            continue
        serials = re.findall(r"(?m)^\s*\d{1,3}\s*$", page_text)
        if len(serials) >= 2:
            chunks.append(page_text)
            continue
        break
    return "\n".join(chunks)


def _parse_summary(doc: fitz.Document) -> list[SummaryEquipment]:
    """Read the Summary of Findings table. Later pages are photo boards."""
    rows = _parse_summary_columns(doc)
    if rows:
        return rows
    return _parse_summary_lines(doc)


def _parse_summary_lines(doc: fitz.Document) -> list[SummaryEquipment]:
    """Line-order summary, used when the PDF has no column positions."""
    text = _summary_pages_text(doc)
    if not text:
        raise ValueError("The pressure-vessel PDF has no Summary of Findings table.")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    start = 0
    for i, line in enumerate(lines):
        if line == "Page #" and i + 1 < len(lines) and re.fullmatch(r"\d{1,2}", lines[i + 1]):
            start = i + 1
            break
    rows: list[SummaryEquipment] = []
    i = start
    while i < len(lines):
        started = _summary_row_start(lines, i)
        if started is None:
            i += 1
            continue
        tag, sn, i = started
        body: list[str] = []
        while i < len(lines) and _summary_row_start(lines, i) is None:
            body.append(lines[i])
            i += 1
        description, anomaly = _split_summary_body(body)
        rows.append(
            SummaryEquipment(
                sn=sn,
                tag=tag,
                description=description,
                anomaly=anomaly,
                out_of_site=bool(_OUT_OF_SITE_RE.search(anomaly) or _OUT_OF_SITE_RE.search(description)),
            )
        )
    if not rows:
        raise ValueError("The Summary of Findings table had no equipment rows.")
    return rows


def _is_equipment_tag(text: str) -> bool:
    """An equipment id: letters and digits, including a code that contains spaces."""
    token = re.sub(r"\s+", " ", (text or "").strip())
    if not token or len(token) > 80 or len(token.split()) > 8:
        return False
    if re.match(r"^\d{1,3}[.)]\s+", token):
        return False
    if not _TAG_RE.fullmatch(token):
        return False
    if not re.search(r"\d", token) or not re.search(r"[A-Za-z]", token):
        return False
    words = token.split()
    if len(words) > 3 and sum(1 for word in words if re.search(r"[a-z]{3,}", word)) >= 2:
        return False
    return True


def _summary_row_start(lines: list[str], index: int) -> tuple[str, int, int] | None:
    """S/N then the equipment tag, or both on one line. Returns tag, serial, next index."""
    line = lines[index]
    same_line = re.match(r"^(\d{1,3})[.)]\s+(\S+)$", line)
    if same_line and _is_equipment_tag(same_line.group(2)):
        return same_line.group(2), int(same_line.group(1)), index + 1
    if re.fullmatch(r"\d{1,3}", line) and index + 1 < len(lines) and _is_equipment_tag(lines[index + 1]):
        return lines[index + 1], int(line), index + 2
    return None


def _split_summary_body(body: list[str]) -> tuple[str, str]:
    """Description is the text before the anomaly or the photo/page trailer."""
    kept: list[str] = []
    for line in body:
        if _PHOTO_LINE_RE.match(line) or _PAGE_LINE_RE.match(line) or line == "--":
            break
        kept.append(line)
    if not kept:
        return "", ""
    # The first line is the equipment name. A wrapped name continues until a finding.
    name_lines = [kept[0]]
    rest = kept[1:]
    while rest and not _FINDING_LINE_RE.search(rest[0]) and not _GOOD_RE.search(rest[0]):
        name_lines.append(rest.pop(0))
        if len(name_lines) == 2:
            break
    return _tidy(" ".join(name_lines)), _tidy(" ".join(rest))


_CALC_RE = re.compile(
    r"(?i)pit\s+measurement|all readings in\s+mm|ultrasonic thickness"
)


def _photo_sections(doc: fitz.Document) -> list[PhotoSection]:
    """Each ``Equipment ID`` heading starts that equipment's photos, until the next heading."""
    sections: list[PhotoSection] = []
    for page_index, page in enumerate(doc):
        words = page.get_text("words") or []
        for j, word in enumerate(words):
            token = (word[4] or "").strip()
            nxt = (words[j + 1][4] if j + 1 < len(words) else "") or ""
            glued = bool(re.match(r"(?i)equipmentid(?:[-–—]\d*)?$", token))
            if token.lower() != "equipment" and not glued:
                continue
            if not glued and nxt.lower() != "id" and not re.match(r"(?i)id(?:[-–—]\d*)?$", nxt):
                continue
            y = float(word[1])
            heading = _heading_text(words, float(word[0]), y)
            if re.search(r"(?i)\bdescription\b", heading):
                continue
            name, tags, serials = _parse_equipment_heading(heading)
            if not tags and not serials:
                continue
            sections.append(
                PhotoSection(
                    name=name,
                    tag=tags[0] if tags else "",
                    page_index=page_index,
                    y=y,
                    tags=tags,
                    serials=serials,
                )
            )
    return sections


def _heading_text(words: list, origin_x: float, heading_y: float) -> str:
    """The Equipment ID line, plus a short wrap that finishes the tag."""
    parts = [
        (float(word[0]), (word[4] or "").strip())
        for word in words
        if (word[4] or "").strip() and abs(float(word[1]) - heading_y) <= 3 and float(word[0]) >= origin_x - 2
    ]
    parts.sort()
    line = " ".join(token for _, token in parts).strip()
    if not _compact_ids(line):
        wrap = [
            (float(word[0]), (word[4] or "").strip())
            for word in words
            if (word[4] or "").strip() and heading_y + 4 < float(word[1]) < heading_y + 22
        ]
        wrap.sort()
        extra = " ".join(token for _, token in wrap).strip()
        if extra and not re.match(r"(?i)photo\b", extra):
            line = f"{line} {extra}"
    return re.sub(r"\s+", " ", line).strip()


def _parse_equipment_heading(line: str) -> tuple[str, list[str], list[int]]:
    """Name, equipment ids, and the serials printed on an Equipment ID heading."""
    body = re.sub(r"(?i)^equipment\s*id\s*", "", line).strip()
    body = re.sub(r"^[-–—]\s*", "", body)
    serial_prefix = re.match(r"^(\d{1,3}(?:\s*&\s*\d{1,3})*)\b", body)
    serials = [int(number) for number in re.findall(r"\d{1,3}", serial_prefix.group(1))] if serial_prefix else []
    if serial_prefix:
        body = body[serial_prefix.end() :].strip()
    tags: list[str] = []
    for group in re.findall(r"\(([^)]*)\)", body):
        tags.extend(_expand_tag_group(group))
    if not tags and re.search(r"(?i)no tag", body):
        tags = ["No tag available"]
    if not tags:
        tags = _compact_ids(body)
    name = body
    for group in re.findall(r"\([^)]*\)", body):
        name = name.replace(group, " ")
    for tag in tags:
        name = re.sub(re.escape(tag), " ", name, count=1)
    name = _tidy(re.sub(r"(?i)\b(insulated|out of service|not found at site|decommissioned.*|for future use)\b", " ", name))
    # Keep the first distinct ids, in order.
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        key = _tag_key(tag)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(tag)
    return name, unique, serials


def _tidy_equipment_id(text: str) -> str:
    token = text.replace("–", "-").replace("—", "-")
    token = re.sub(r"\s+", " ", token).strip()
    token = re.sub(r"\s*-\s*", "-", token)
    return token.strip(" -")


def _compact_ids(text: str) -> list[str]:
    """Equipment ids written as hyphenated codes, including a code split after a hyphen."""
    normalized = text.replace("–", "-").replace("—", "-")
    # Join a code broken at a hyphen. Leave a spaced dash before the next word alone.
    normalized = re.sub(r"(?<=[A-Za-z0-9])-\s+(?=[A-Za-z0-9])", "-", normalized)
    normalized = re.sub(r"(?<=[A-Za-z0-9])\s+-(?=[A-Za-z0-9])", "-", normalized)
    found: list[str] = []
    for match in re.finditer(r"\b[A-Za-z]{2,}(?:-[A-Za-z0-9]+)+\b", normalized):
        token = _tidy_equipment_id(match.group(0))
        if re.fullmatch(r"(?i)id-?\d+", token):
            continue
        if _is_equipment_tag(token) and re.search(r"\d", token):
            found.append(token)
    return found


def _expand_tag_group(text: str) -> list[str]:
    """``TAG-1 & 2`` and ``TAG-A/B`` are the equipment ids named in one heading."""
    cleaned = _tidy_equipment_id(text)
    if "/" in cleaned:
        sides = [_tidy_equipment_id(side) for side in cleaned.split("/")]
        if len(sides) == 2 and _is_equipment_tag(sides[0]) and re.search(r"\d", sides[1]) and len(sides[1]) > 1:
            right = sides[1]
            if "-" not in right and "-" in sides[0]:
                right = f"{sides[0].rsplit('-', 1)[0]}-{right}"
            if _is_equipment_tag(right):
                return [sides[0], right]
    slash = re.match(r"^(.*?)([A-Za-z0-9])\s*/\s*([A-Za-z0-9])$", cleaned)
    if slash:
        stem = slash.group(1)
        return [
            _tidy_equipment_id(f"{stem}{slash.group(2)}"),
            _tidy_equipment_id(f"{stem}{slash.group(3)}"),
        ]
    if not cleaned:
        return []
    if "&" not in cleaned:
        return [cleaned] if _is_equipment_tag(cleaned) else []
    pieces = [_tidy_equipment_id(piece) for piece in cleaned.split("&")]
    pieces = [piece for piece in pieces if piece]
    if not pieces or not _is_equipment_tag(pieces[0]):
        return []
    tags = [pieces[0]]
    for extra in pieces[1:]:
        if _is_equipment_tag(extra) and not re.fullmatch(r"\d+[A-Za-z]?|[A-Za-z]", extra):
            tags.append(extra)
            continue
        swapped = _swap_id_suffix(tags[0], extra)
        if swapped:
            tags.append(swapped)
    return tags


def _swap_id_suffix(base: str, extra: str) -> str:
    extra = extra.strip(" -")
    if re.fullmatch(r"\d+", extra):
        return re.sub(r"\d+$", extra, base)
    if re.fullmatch(r"[A-Za-z]", extra):
        if re.search(r"[A-Za-z]$", base):
            return base[:-1] + extra.upper()
        return f"{base}-{extra.upper()}"
    return ""


def _name_and_tag(words: list, heading_y: float, id_x: float) -> tuple[str, str]:
    """The same line as ``Equipment ID`` is ``Name (TAG)``. The summary header is not a section."""
    parts: list[tuple[float, str]] = []
    for word in words:
        tok = (word[4] or "").strip()
        y = float(word[1])
        x = float(word[0])
        if not tok or abs(y - heading_y) > 3 or x <= id_x + 8:
            continue
        parts.append((x, tok))
    parts.sort()
    line = re.sub(r"\s+", " ", " ".join(tok for _, tok in parts)).strip()
    if re.search(r"(?i)\bdescription\b", line):
        return "", ""
    match = re.search(r"^(.*?)\(\s*([^)]+)\s*\)\s*$", line)
    if match and _is_equipment_tag(match.group(2).strip()):
        return _tidy(match.group(1)), match.group(2).strip()
    tokens = line.split()
    if tokens and _is_equipment_tag(tokens[-1]):
        return _tidy(" ".join(tokens[:-1])), tokens[-1]
    return "", ""


def _section_tags(section: PhotoSection) -> list[str]:
    tags = list(section.tags or [])
    if section.tag and section.tag not in tags:
        tags.insert(0, section.tag)
    return tags


def _same_equipment_id(summary_tag: str, photo_tag: str) -> bool:
    """True when both strings are the same equipment id, allowing a short written variant."""
    want = _tag_key(summary_tag)
    have = _tag_key(photo_tag)
    if not want or not have:
        return False
    if want == have:
        return True
    want_nums = tuple(re.findall(r"\d+", summary_tag))
    have_nums = tuple(re.findall(r"\d+", photo_tag))
    if not want_nums or want_nums != have_nums:
        return False
    if len(want) >= 6 and len(have) >= 6 and (want in have or have in want):
        return True
    return _one_letter_apart(want, have)


def _one_letter_apart(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) != 1:
        return False
    short, long = (left, right) if len(left) < len(right) else (right, left)
    for index in range(len(long)):
        if long[:index] + long[index + 1 :] == short:
            return long[index].isalpha()
    return False


def _ids_named(text: str) -> list[str]:
    """Every equipment id written in a summary cell or a photo heading."""
    named = [text] if text else []
    named.extend(_compact_ids(text or ""))
    for group in re.findall(r"\(([^)]*)\)", text or ""):
        named.extend(_expand_tag_group(group))
    unique: list[str] = []
    seen: set[str] = set()
    for tag in named:
        key = _tag_key(tag)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(tag)
    return unique


def _join_unique(parts) -> str:
    seen: list[str] = []
    for part in parts:
        text = (part or "").strip()
        if text and text not in seen:
            seen.append(text)
    return " & ".join(seen)


def _rows_from_headings(
    sections: list[PhotoSection],
) -> list[tuple[list[SummaryEquipment], PhotoSection | None]]:
    """No summary table: one row per Equipment ID heading, using that heading's description."""
    rows: list[tuple[list[SummaryEquipment], PhotoSection | None]] = []
    next_sn = 1
    for section in sections:
        tags = [tag for tag in _section_tags(section) if tag]
        serials = [sn for sn in (section.serials or []) if sn]
        description = section.name or (tags[0] if tags else "")
        if not tags and not description:
            continue
        if not tags:
            tags = ["No tag available"]
        if not serials:
            serials = [next_sn]
            next_sn += 1
        else:
            next_sn = max(next_sn, max(serials) + 1)
        count = max(len(tags), len(serials))
        members = [
            SummaryEquipment(
                sn=serials[index] if index < len(serials) else serials[-1],
                tag=tags[index] if index < len(tags) else tags[-1],
                description=description,
                anomaly="",
            )
            for index in range(count)
        ]
        rows.append((members, section))
    return rows


def _equipment_rows(
    summary: list[SummaryEquipment],
    sections: list[PhotoSection],
) -> list[tuple[list[SummaryEquipment], PhotoSection | None]]:
    """One Excel row per equipment. A photo heading with several serials is one row."""
    by_sn = {item.sn: item for item in summary}
    owner: dict[int, PhotoSection] = {}
    for section in sections:
        serials = section.serials or []
        if len(serials) < 2:
            continue
        for sn in serials:
            if sn in by_sn and sn not in owner:
                owner[sn] = section
    rows: list[tuple[list[SummaryEquipment], PhotoSection | None]] = []
    consumed: set[int] = set()
    for item in summary:
        if item.sn in consumed:
            continue
        section = owner.get(item.sn)
        if section is None:
            rows.append(([item], None))
            consumed.add(item.sn)
            continue
        member_sns = {sn for sn in (section.serials or []) if sn in by_sn and sn not in consumed}
        members = [candidate for candidate in summary if candidate.sn in member_sns]
        for member in members:
            consumed.add(member.sn)
        rows.append((members, section))
    return rows


def _match_sections(
    summary_tag: str,
    sections: list[PhotoSection],
    summary_sn: int | None = None,
) -> list[PhotoSection]:
    """Every photo block whose heading names this equipment id."""
    wanted = {_tag_key(tag) for tag in _ids_named(summary_tag)}
    wanted.discard("")
    exact = [
        section
        for section in sections
        if any(_tag_key(tag) in wanted for tag in _section_tags(section))
    ]
    if exact:
        if summary_sn is not None and all(section.serials for section in exact):
            numbered = [section for section in exact if summary_sn in (section.serials or [])]
            if numbered:
                return numbered
        return exact
    close = [
        section
        for section in sections
        if any(_same_equipment_id(summary_tag, tag) for tag in _section_tags(section))
    ]
    if len(close) == 1:
        return close
    if summary_sn is not None and re.search(r"(?i)not available|no tag", summary_tag or ""):
        numbered = [section for section in sections if summary_sn in (section.serials or [])]
        if len(numbered) == 1:
            return numbered
    return []


def _match_section(
    summary_tag: str,
    sections: list[PhotoSection],
    summary_sn: int | None = None,
) -> PhotoSection | None:
    """The photo block whose heading names this equipment id."""
    found = _match_sections(summary_tag, sections, summary_sn)
    return found[0] if found else None


def _photos_for_section(
    doc: fitz.Document,
    section: PhotoSection,
    sections: list[PhotoSection],
    work_dir: Path,
    summary_tag: str,
) -> list:
    """Save each PDF photo for this equipment, original bytes, until the next Equipment ID."""
    later = [s for s in sections if (s.page_index, s.y) > (section.page_index, section.y)]
    end = later[0] if later else None
    paired: list[PairedPhoto] = []
    seen: set[int] = set()
    last_page = end.page_index if end is not None else doc.page_count - 1
    work_dir.mkdir(parents=True, exist_ok=True)
    stem = _tag_key(summary_tag)
    for page_index in range(section.page_index, last_page + 1):
        page = doc[page_index]
        height = float(page.rect.height) or 1.0
        y_lo = 0.06 * height
        y_hi = 0.94 * height
        if page_index == section.page_index:
            # The heading sits just above the first photo. Do not cut that photo off.
            y_lo = max(y_lo, section.y + 8.0)
        if end is not None and page_index == end.page_index:
            y_hi = min(y_hi, end.y - 4.0)
        if y_hi - y_lo < 24:
            continue
        bands = _calc_bands(page)
        placed = [
            box
            for box in _field_photos(page, y_lo, y_hi, seen)
            if not _inside_calc(box[1], bands)
        ]
        rows: list[list[tuple[float, float, float, float, int]]] = []
        for box in sorted(placed, key=lambda item: item[1]):
            if not rows or box[1] - rows[-1][0][1] > 36.0:
                rows.append([box])
            else:
                rows[-1].append(box)
        ordered = [box for row in rows for box in sorted(row, key=lambda item: item[0])]
        for seq, (x0, y0, x1, y1, xref) in enumerate(ordered, start=1):
            out = work_dir / f"{stem}_p{page_index + 1:02d}_{seq:02d}.png"
            caption = _write_photo_with_caption(doc, page, xref, (x0, y0, x1, y1), out)
            paired.append(
                PairedPhoto(
                    path=out,
                    photo_num=seq,
                    caption=caption,
                    y0=y0,
                    x0=x0,
                )
            )
    return paired


def _measurements_for_section(
    doc: fitz.Document,
    section: PhotoSection,
    sections: list[PhotoSection],
) -> list[str]:
    """Pit-measurement rows on this equipment's pages, tied to its equipment ID."""
    later = [s for s in sections if (s.page_index, s.y) > (section.page_index, section.y)]
    end = later[0] if later else None
    last_page = end.page_index if end is not None else doc.page_count - 1
    lines: list[str] = []
    for page_index in range(section.page_index, last_page + 1):
        page = doc[page_index]
        if not _is_measurement_page(page.get_text("text") or ""):
            continue
        lines.extend(_pit_lines(page))
    return lines


def _is_measurement_page(text: str) -> bool:
    return bool(_CALC_RE.search(text or "") or _PIT_LABEL_RE.search(text or ""))


def _calc_bands(page: fitz.Page) -> list[tuple[float, float]]:
    """Vertical span of a millimetre or pit-calculation table, up to the next equipment heading."""
    words = page.get_text("words") or []
    lines: dict[int, list] = {}
    for word in words:
        lines.setdefault(round(float(word[1])), []).append(word)
    starts: list[float] = []
    headings: list[float] = []
    for y, group in lines.items():
        text = " ".join(word[4] for word in sorted(group, key=lambda item: float(item[0])))
        if _CALC_RE.search(text):
            starts.append(float(y))
        if re.match(r"(?i)equipment\s+id\b", text):
            headings.append(float(y))
    height = float(page.rect.height)
    bands: list[tuple[float, float]] = []
    for start in starts:
        end = height
        for heading in headings:
            if heading > start + 6:
                end = min(end, heading)
        bands.append((start, end))
    return bands


def _inside_calc(top: float, bands: list[tuple[float, float]]) -> bool:
    return any(start - 2 <= top < end for start, end in bands)


def _pit_lines(page: fitz.Page) -> list[str]:
    """One line per pit: label, size, depth, UTG readings, remaining wall."""
    words = page.get_text("words") or []
    buckets: dict[int, list[tuple[float, str, float]]] = {}
    for word in words:
        token = (word[4] or "").strip()
        y = float(word[1])
        if not token or y < 80 or y > float(page.rect.height) - 36:
            continue
        buckets.setdefault(int(y // 4), []).append((float(word[0]), token, y))
    ordered: list[tuple[float, str]] = []
    for key in sorted(buckets):
        items = sorted(buckets[key])
        ordered.append((items[0][2], " ".join(token for _, token, _ in items)))
    lines: list[str] = []
    for index, (_, text) in enumerate(ordered):
        match = _PIT_LABEL_RE.search(text)
        if not match:
            continue
        chunk = text
        if index > 0 and re.fullmatch(r"[\d.\s]+", ordered[index - 1][1].strip()):
            chunk = f"{ordered[index - 1][1]} {text}"
        lines.append(_format_pit(match.group(1).upper(), chunk))
    return lines


def _format_pit(label: str, chunk: str) -> str:
    size = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)", chunk, flags=re.I)
    if size:
        before = chunk[: size.start()]
        after_numbers = re.findall(r"\d+(?:\.\d+)?", chunk[size.end() :])
    else:
        before = chunk
        after_numbers = []
    before = re.sub(rf"\b{re.escape(label)}\b", " ", before, flags=re.I)
    depth = after_numbers[0] if after_numbers else None
    readings = re.findall(r"\d+(?:\.\d+)?", before) + after_numbers[1:]
    remaining = readings[-1] if readings else None
    utg = readings[:-1] if readings else []
    parts = [label]
    if size:
        parts.append(f"size {size.group(1)} x {size.group(2)}")
    if depth:
        parts.append(f"max depth {depth}")
    if utg:
        parts.append("UTG " + ", ".join(utg))
    if remaining:
        parts.append(f"remaining wall {remaining}")
    return "- " + ", ".join(parts)


def _field_photos(
    page: fitz.Page,
    y_lo: float,
    y_hi: float,
    seen: set[int],
) -> list[tuple[float, float, float, float, int]]:
    """One copy of each PDF photo. Logos, stamps, and a repeated embed are left out."""
    found = []
    on_page: set[tuple[int, int]] = set()
    for image in page.get_image_info(xrefs=True) or []:
        x0, y0, x1, y1 = (float(v) for v in image["bbox"])
        if (x1 - x0) < 120 or (y1 - y0) < 80:
            continue
        if y0 < y_lo or y0 >= y_hi:
            continue
        xref = int(image.get("xref") or 0)
        if xref <= 0:
            continue
        place = (int(x0), int(y0))
        if place in on_page:
            continue
        on_page.add(place)
        seen.add(xref)
        found.append((x0, y0, x1, y1, xref))
    return found


def _write_photo_with_caption(
    doc: fitz.Document,
    page: fitz.Page,
    xref: int,
    box: tuple[float, float, float, float],
    dest: Path,
) -> str:
    """Original photo, with every caption line under it kept in the picture."""
    x0, y0, x1, y1 = box
    caption, text_bottom = _caption_under(page, x0, x1, y1)
    extracted = doc.extract_image(xref)
    photo = Image.open(io.BytesIO(extracted["image"])).convert("RGB")
    if text_bottom <= y1 + 0.5:
        photo.save(dest, format="PNG")
        return caption
    zoom = photo.width / max(x1 - x0, 1.0)
    clip = fitz.Rect(x0, y1, x1, text_bottom)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)
    strip = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if strip.width != photo.width:
        strip = strip.resize((photo.width, max(1, strip.height)), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (photo.width, photo.height + strip.height), "white")
    canvas.paste(photo, (0, 0))
    canvas.paste(strip, (0, photo.height))
    canvas.save(dest, format="PNG")
    return caption


def _caption_under(page: fitz.Page, left: float, right: float, bottom: float) -> tuple[str, float]:
    """Full caption under one photo, and the page y where that caption ends."""
    footer = float(page.rect.height) - 36.0
    next_top = footer
    for start, _end in _calc_bands(page):
        if start > bottom - 2:
            next_top = min(next_top, start)
    for image in page.get_image_info() or []:
        other_top = float(image["bbox"][1])
        other_left = float(image["bbox"][0])
        other_right = float(image["bbox"][2])
        if other_top <= bottom + 1:
            continue
        if other_left < right - 8 and other_right > left + 8:
            next_top = min(next_top, other_top)
    words = page.get_text("words") or []
    for index, word in enumerate(words):
        token = (word[4] or "").strip()
        nxt = (words[index + 1][4] if index + 1 < len(words) else "") or ""
        if token.lower() == "equipment" and nxt.lower() == "id" and float(word[1]) > bottom - 2:
            next_top = min(next_top, float(word[1]))
        if _STOP_CAPTION_RE.match(token) and float(word[1]) > bottom - 2:
            next_top = min(next_top, float(word[1]))
    lines: dict[int, list[tuple[float, str]]] = {}
    text_bottom = bottom
    limit = min(next_top - 0.2, footer)
    for word in words:
        token = (word[4] or "").strip()
        if not token:
            continue
        wx0, wy0, wx1, wy1 = (float(word[0]), float(word[1]), float(word[2]), float(word[3]))
        center = (wx0 + wx1) / 2.0
        if center < left - 8 or center > right + 8:
            continue
        if wy0 < bottom - 2 or wy0 >= limit:
            continue
        lines.setdefault(int(wy0 // 4), []).append((wx0, token))
        text_bottom = max(text_bottom, min(wy1 + 0.4, next_top - 0.15))
    ordered = []
    for key in sorted(lines):
        ordered.append(" ".join(token for _, token in sorted(lines[key])))
    return re.sub(r"\s+", " ", " ".join(ordered)).strip(), text_bottom


def _anomaly_text(photos: list) -> str:
    """One dashed line per photo, so each caption stays separate."""
    lines = []
    for photo in photos:
        caption = re.sub(r"\s+", " ", getattr(photo, "caption", "") or "").strip()
        if caption:
            lines.append(f"- {caption}")
    return "\n".join(lines)


def _facility(text: str) -> str | None:
    match = re.search(r"(?i)Location\s+([A-Z][A-Z0-9-]+)", text)
    if match:
        return match.group(1)
    match = re.search(r"\b([A-Z]{2,}(?:-[A-Z0-9]{2,})+)\b", text)
    return match.group(1) if match else None


def _tag_key(tag: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (tag or "").upper())


def _tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip(" .:-")
