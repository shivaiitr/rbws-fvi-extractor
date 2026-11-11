"""Pressure vessel workflow.

One FVI pressure-vessel PDF becomes one RBWS row per equipment.

Rules, in the order they run:

1. The Summary of Findings table is the master list, in that order, for any
   facility. Equipment tags are taken as written. When a PDF has no summary
   table, each Equipment ID photo section is one row.
2. A summary row that is decommissioned or out of site, and has no photo
   section, is not a row.
3. A photographed equipment is kept even when the manual sheet omitted it.
4. Equipment # is the summary-table tag.
5. Equipment Description is the name on the photo-section ``Equipment ID``
   line, before the tag in parentheses.
6. Anomaly text is a copy of the words printed under each photo, in order.
   Each caption is one line and starts with a dash. Nothing is dropped,
   merged, or rewritten.
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
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_-]{2,}$")
_PHOTO_LINE_RE = re.compile(r"(?i)^photos?\s*#")
_PAGE_LINE_RE = re.compile(r"^\d+\s*[-–—]\s*\d+$|^--$")
_DEFECT_RE = re.compile(
    r"(?i)corrosion|coating|stain|damage|debris|wrap|pit|crack|leak|"
    r"metal loss|scale|discolour|discolor|blast"
)
_GOOD_RE = re.compile(r"(?i)\bgood (?:visual )?condition\b")
_OUT_OF_SITE_RE = re.compile(r"(?i)decommissioned|out of site")


@dataclass
class SummaryEquipment:
    """One row of the Summary of Findings table."""

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
        if not summary:
            summary = [
                SummaryEquipment(tag=section.tag or section.name, description=section.name, anomaly="")
                for section in sections
                if section.tag or section.name
            ]
        if not summary:
            if summary_error is not None:
                raise summary_error
            raise ValueError(
                "No equipment was found in this PDF. "
                "Use a pressure vessel FVI with a Summary of Findings table "
                "or Equipment ID photo sections."
            )
        findings: list[Finding] = []
        warnings: list[str] = []
        total = len(summary)
        if on_progress is not None:
            on_progress(0, total, "Reading the summary of findings")
        for index, item in enumerate(summary, start=1):
            section = _match_section(item.tag, sections)
            if item.out_of_site and section is None:
                if on_progress is not None:
                    on_progress(index, total, f"Equipment {item.tag}")
                continue
            photos = []
            if section is not None:
                photos = _photos_for_section(doc, section, sections, work_dir, item.tag)
            description = section.name if section is not None else item.description
            anomaly = _anomaly_text(photos) or _tidy(item.anomaly)
            if not anomaly:
                anomaly = "No anomaly text found"
                warnings.append(f"{item.tag}: anomaly text was empty")
            paths = [p.path for p in photos]
            findings.append(
                Finding(
                    asset_class=AssetClass.PRESSURE_VESSEL,
                    line_number=item.tag,
                    line_description=description or item.tag,
                    anomaly_description=anomaly,
                    photos_image_path=paths[0] if paths else None,
                    photo_image_paths=paths,
                    source=FindingSource(
                        section="pressure vessel",
                        equipment_id=item.tag,
                        pages=[section.page_index + 1] if section else [],
                    ),
                ).apply_judgment_blanks()
            )
            if on_progress is not None:
                on_progress(index, total, f"Equipment {item.tag}")
        if not findings:
            raise ValueError("No pressure-vessel equipment rows were found in the summary table.")
        facility = _facility(opening) or "RBWS"
        return ExtractionResult(
            metadata=ReportMetadata(
                facility=facility,
                title="Pressure vessel FVI",
                layout=LAYOUT,
            ),
            findings=findings,
            warnings=warnings,
            source_pdf=pdf_path,
        )
    finally:
        doc.close()


def _parse_summary(doc: fitz.Document) -> list[SummaryEquipment]:
    """Read the Summary of Findings table. Later pages are photo boards."""
    text = ""
    for page in doc:
        page_text = page.get_text("text") or ""
        if re.search(r"(?i)summary of findings", page_text):
            text = page_text
            break
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
        tag, i = started
        body: list[str] = []
        while i < len(lines) and _summary_row_start(lines, i) is None:
            body.append(lines[i])
            i += 1
        description, anomaly = _split_summary_body(body)
        rows.append(
            SummaryEquipment(
                tag=tag,
                description=description,
                anomaly=anomaly,
                out_of_site=bool(_OUT_OF_SITE_RE.search(anomaly)),
            )
        )
    if not rows:
        raise ValueError("The Summary of Findings table had no equipment rows.")
    return rows


def _is_equipment_tag(text: str) -> bool:
    """An equipment id as its own line: letters and digits, not a sentence."""
    token = (text or "").strip()
    if not _TAG_RE.fullmatch(token):
        return False
    if not re.search(r"\d", token) or not re.search(r"[A-Za-z]", token):
        return False
    return True


def _summary_row_start(lines: list[str], index: int) -> tuple[str, int] | None:
    """S/N then the equipment tag, or both on one line. Tags are not limited to one facility."""
    line = lines[index]
    same_line = re.match(r"^(\d{1,2})[.)]\s+(\S+)$", line)
    if same_line and _is_equipment_tag(same_line.group(2)):
        return same_line.group(2), index + 1
    if re.fullmatch(r"\d{1,2}", line) and index + 1 < len(lines) and _is_equipment_tag(lines[index + 1]):
        return lines[index + 1], index + 2
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
    # The first line is the equipment name. Following lines are the summary anomaly.
    description = kept[0]
    anomaly = " ".join(kept[1:]) if len(kept) > 1 else ""
    # A wrapped description has no defect words and the next line continues the name.
    if len(kept) > 1 and not _DEFECT_RE.search(kept[0]) and not _OUT_OF_SITE_RE.search(kept[0]):
        if not _DEFECT_RE.search(kept[1]) and not _OUT_OF_SITE_RE.search(kept[1]) and not _GOOD_RE.search(kept[1]):
            description = " ".join(kept[:2])
            anomaly = " ".join(kept[2:])
    return _tidy(description), _tidy(anomaly)


def _photo_sections(doc: fitz.Document) -> list[PhotoSection]:
    """Each ``Equipment ID`` heading starts one equipment's photos."""
    sections: list[PhotoSection] = []
    for page_index, page in enumerate(doc):
        words = page.get_text("words") or []
        for j, word in enumerate(words):
            token = (word[4] or "").strip()
            nxt = (words[j + 1][4] if j + 1 < len(words) else "") or ""
            if token.lower() != "equipment" or nxt.lower() != "id":
                continue
            y = float(word[1])
            id_x = float(words[j + 1][0])
            name, tag = _name_and_tag(words, y, id_x)
            if not tag:
                continue
            sections.append(PhotoSection(name=name, tag=tag, page_index=page_index, y=y))
    return sections


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


def _match_section(summary_tag: str, sections: list[PhotoSection]) -> PhotoSection | None:
    """Pair a summary tag with its photo section. ``NGFR-M-3395`` matches ``NGFR-MR-3395``."""
    want = _tag_key(summary_tag)
    want_nums = re.findall(r"\d{3,}", summary_tag.upper())
    for section in sections:
        if _tag_key(section.tag) == want:
            return section
    for section in sections:
        if want_nums and re.findall(r"\d{3,}", section.tag.upper()) == want_nums:
            return section
    return None


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
        y_lo = 0.10 * height
        y_hi = 0.92 * height
        if page_index == section.page_index:
            # The heading sits just above the first photo. Do not cut that photo off.
            y_lo = max(y_lo, section.y + 8.0)
        if end is not None and page_index == end.page_index:
            y_hi = min(y_hi, end.y - 4.0)
        if y_hi - y_lo < 24:
            continue
        placed = _field_photos(page, y_lo, y_hi, seen)
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
        if xref <= 0 or xref in seen:
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
