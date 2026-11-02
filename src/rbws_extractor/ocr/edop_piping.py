"""EDOP-QP Process Piping FVI: summary checklist + per-line PDF → RBWS rows."""

from __future__ import annotations

import gc
import io
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import (
    AssetClass,
    ExtractionResult,
    Finding,
    FindingSource,
    PhotoRef,
    ReportMetadata,
)
from rbws_extractor.pipeline.rules import compress_photo_refs as _compress_photo_refs
from rbws_extractor.pipeline.rules import join_from_to, normalize_whitespace

logger = get_logger("ocr.edop_piping")

# Summary checklist column bands (PDF points) from header/data on checklist pages.
_COL_SN = (0.0, 52.0)
_COL_PID = (52.0, 110.0)
_COL_LINE = (110.0, 210.0)
_COL_FROM = (210.0, 300.0)
_COL_TO = (300.0, 390.0)
_COL_ANOM = (390.0, 900.0)

_DATE_RE = re.compile(
    r"(?i)Date\s+of\s+Inspection\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+),?\s+(\d{4})"
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_SN_RE = re.compile(
    r"(?i)(?:JV\s*)?Piping\s*List\s*S/?n\.?\s*(\d+)|(?:JV\s*)?Piping\s*list\s*s/?n\.?\s*(\d+)"
)

_PHOTO_CAPTION_RE = re.compile(
    r"(?is)((?:General|Close|Further|Sectional|Moderate|Heavy|Light|View|Scattered|"
    r"Composite|Coating|Pin|Line|Holes?)\b[^()]{2,180}?)"
    r"\(\s*Photo\s*#?\s*(\d+)\s*(?:[-–—&to]+\s*(\d+))?\s*\)"
)

_HEADER_TOKENS = {
    "s/n",
    "p&id",
    "number",
    "line",
    "from",
    "to",
    "anomaly",
    "description",
    "observations",
    "checklist",
}

_MIN_PHOTO_PX_W = 250
_MIN_PHOTO_PX_H = 200
_MIN_PAGE_H = 80.0
_MIN_PAGE_W = 100.0


@dataclass
class SummaryRow:
    sn: int
    pid_no: str | None = None
    line_number: str | None = None
    from_text: str | None = None
    to_text: str | None = None
    summary_anomaly: str | None = None


@dataclass
class LinePdfParse:
    sn: int | None = None
    line_number: str | None = None
    line_description: str | None = None
    pid_no: str | None = None
    anomaly_header: str | None = None
    caption_bullets: list[str] = field(default_factory=list)
    photo_refs: list[PhotoRef] = field(default_factory=list)
    photo_paths: list[Path] = field(default_factory=list)
    iso_path: Path | None = None
    has_photos: bool = False
    source_pdf: Path | None = None


def detect_pdf_kind(pdf_path: Path) -> str:
    """Classify an uploaded PDF for routing (fire water vs process piping)."""
    pdf_path = Path(pdf_path)
    try:
        doc = fitz.open(pdf_path)
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        sample = "\n".join(
            doc[i].get_text("text") or "" for i in range(min(4, doc.page_count))
        )
    finally:
        doc.close()
    name_u = pdf_path.name.upper()
    text_u = sample.upper()
    if "VISUAL INSPECTION OBSERVATIONS CHECKLIST" in text_u:
        return "process_piping_summary"
    if "SUMMARY" in name_u and "PIPING" in text_u:
        return "process_piping_summary"
    if re.search(
        r"(?i)JV\s*Piping\s*List|DESCRIPTION OF ANOMALIES|Piping\s*list\s*s/?n",
        sample,
    ):
        return "process_piping_line"
    if re.search(
        r"(?i)FIRE\s*WATER|HOSE\s*REEL|FIRE\s*PROTECTION\s*CYLINDER|"
        r"FIRE\s*WATER\s*PUMP|UBIT-GP|YOHO",
        sample,
    ):
        return "fire_water"
    if "PROCESS PIPING" in text_u or "FULL VISUAL INSPECTION" in text_u:
        return "process_piping_unknown"
    return "unknown"


def _col_for_x(x: float) -> str:
    if x < _COL_SN[1]:
        return "sn"
    if x < _COL_PID[1]:
        return "pid"
    if x < _COL_LINE[1]:
        return "line"
    if x < _COL_FROM[1]:
        return "from"
    if x < _COL_TO[1]:
        return "to"
    return "anom"


def _clean_join(parts: list[str]) -> str | None:
    kept = []
    for p in parts:
        if not p:
            continue
        if p.strip().lower() in _HEADER_TOKENS:
            continue
        kept.append(p)
    text = normalize_whitespace(" ".join(kept))
    if not text:
        return None
    text = re.sub(r"(?i)^(from|to)\s+", "", text)
    return text.strip(" -|") or None


def _normalize_line_tag(raw: str | None) -> str | None:
    if not raw:
        return None
    text = (
        raw.replace("\n", " ")
        .replace("\u2019", '"')
        .replace("\u201d", '"')
        .replace("\u2033", '"')
        .replace("″", '"')
        .replace("''", '"')
        .replace("”", '"')
    )
    text = re.sub(r"\s+", " ", text).strip()
    # Collapse spaces around inch marks: GI-11094- 3/4 "-160- → GI-11094-3/4"-160-
    text = re.sub(r"\s*\"\s*", '"', text)
    text = re.sub(r'"{2,}', '"', text)
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"-+", "-", text)
    text = re.sub(r"(?i)^(line\s*number|equipment\s*id)\s*", "", text)
    text = text.strip("- ")
    return text or None


def parse_summary_metadata(doc: fitz.Document) -> ReportMetadata:
    sample = "\n".join(doc[i].get_text("text") for i in range(min(3, doc.page_count)))
    insp: date | None = None
    m = _DATE_RE.search(sample)
    if m:
        day = int(m.group(1))
        month = _MONTHS.get(m.group(2).lower(), 0)
        year = int(m.group(3))
        if day and month and year:
            insp = date(year, month, day)
    report_id = None
    m = re.search(r"\b(QP/FVI/\d{2}/\d+P)\b", sample, flags=re.I)
    if m:
        report_id = m.group(1).upper()
    wo = None
    m = re.search(r"Work\s*Order\s*(?:Number|#)?\s*(\d+)", sample, flags=re.I)
    if m:
        wo = m.group(1)
    return ReportMetadata(
        facility="EDOP-QP",
        inspection_date=insp or date(2026, 1, 4),
        report_id=report_id,
        work_order=wo,
        title="FULL VISUAL INSPECTION OF PROCESS PIPING",
    )


def _assemble_pid(parts: list[str]) -> str | None:
    """Build P&ID, keeping multi-drawing suffixes like ``…0017591/92`` or ``…/96/97/99``."""
    raw = re.sub(r"\s+", "", "".join(parts)).upper()
    if not raw:
        return None
    # Primary drawing number + optional /NN or /NNN slash suffixes from the checklist.
    m = re.search(
        r"(?:NGQP-EP-)?(?:PDPID-)?(\d{5,})((?:/\d{2,4})+)?",
        raw,
    )
    if m:
        suffix = m.group(2) or ""
        return f"NGQP-EP-PDPID-{m.group(1)}{suffix}"
    if "QP-PR" in raw:
        m = re.search(r"(QP-PR-?\d+(?:/\d+)*)", raw)
        if m:
            return m.group(1)
    return raw


def _prefer_pid(*candidates: str | None) -> str | None:
    """Prefer the fullest P&ID (keep slash suffixes when present)."""
    best: str | None = None
    best_score = -1
    for raw in candidates:
        if not raw or not str(raw).strip():
            continue
        text = re.sub(r"\s+", "", str(raw)).strip()
        # Re-normalize through assembler when it looks like a PDPID family value.
        assembled = _assemble_pid([text]) if re.search(r"(?i)PDPID|\d{5,}", text) else text
        value = assembled or text
        score = len(value) + (10 if "/" in value else 0)
        if score > best_score:
            best = value
            best_score = score
    return best


def parse_summary_rows(doc: fitz.Document) -> list[SummaryRow]:
    """Parse Visual Inspection Observations Checklist using SN vertical bands.

    Line/From/To cells often sit slightly above the S/N number, so words are
    assigned by midpoint bands between consecutive S/N anchors—not by flush-on-SN.
    """
    by_sn: dict[int, dict[str, list[tuple[float, float, str]]]] = {}

    # Start once we see a checklist / SN table header — not EDOP-only wording.
    _CHECKLIST_START = re.compile(
        r"(?i)VISUAL\s+INSPECTION\s+OBSERVATIONS?\s+CHECKLIST|"
        r"OBSERVATIONS?\s+CHECKLIST|"
        r"S/?N\s+P\s*&?\s*ID\s+No|"
        r"Line\s*Number.{0,40}From.{0,20}To"
    )
    started = False
    for page_index in range(doc.page_count):
        page = doc[page_index]
        text = page.get_text("text") or ""
        if not started:
            if not _CHECKLIST_START.search(text):
                continue
            started = True
        if re.search(r"(?i)prepared\s+by|verified\s+by", text) and page_index > 20:
            # signature / trailing pages after checklist
            if "S/N" not in text and "Line Number" not in text:
                continue

        words = page.get_text("words")
        # S/N anchors: integer in the S/N column
        anchors: list[tuple[float, int]] = []
        for w in words:
            if w[0] > _COL_SN[1]:
                continue
            if re.fullmatch(r"\d{1,3}", w[4]):
                sn = int(w[4])
                if 1 <= sn <= 500:
                    anchors.append((float(w[1]), sn))
        anchors.sort()
        if not anchors:
            continue

        # Midpoint bands; allow content slightly above first SN on page
        bands: list[tuple[float, float, int]] = []
        for i, (y, sn) in enumerate(anchors):
            if i == 0:
                y0 = y - 28.0
            else:
                y0 = (anchors[i - 1][0] + y) / 2.0
            if i + 1 < len(anchors):
                y1 = (y + anchors[i + 1][0]) / 2.0
            else:
                y1 = y + 55.0
            bands.append((y0, y1, sn))

        for w in words:
            x, y, token = float(w[0]), float(w[1]), w[4]
            if y < 85:  # banner / report title band
                continue
            if re.search(
                r"(?i)^page$|EDOP-QP|EAP-GN|UBIT|YOHO|PROCESS|FULL|VISUAL|"
                r"INSPECTION|REPORT|QP/FVI|FVI",
                token,
            ):
                continue
            col = _col_for_x(x)
            if col == "sn":
                continue
            assigned = None
            for y0, y1, sn in bands:
                if y0 <= y < y1:
                    assigned = sn
                    break
            if assigned is None:
                continue
            by_sn.setdefault(
                assigned, {k: [] for k in ("pid", "line", "from", "to", "anom")}
            )
            by_sn[assigned][col].append((y, x, token))

        # Ensure every SN anchor creates a row even if sparse
        for _, sn in anchors:
            by_sn.setdefault(sn, {k: [] for k in ("pid", "line", "from", "to", "anom")})

    rows: list[SummaryRow] = []
    for sn in sorted(by_sn):
        buckets = by_sn[sn]

        def sorted_text(key: str, _buckets=buckets) -> list[str]:
            items = sorted(_buckets.get(key, []), key=lambda t: (t[0], t[1]))
            return [t[2] for t in items]

        row = SummaryRow(sn=sn)
        row.pid_no = _assemble_pid(sorted_text("pid"))
        row.line_number = _normalize_line_tag(_clean_join(sorted_text("line")))
        row.from_text = _clean_join(sorted_text("from"))
        row.to_text = _clean_join(sorted_text("to"))
        row.summary_anomaly = _clean_join(sorted_text("anom"))
        if row.line_number and re.search(
            r"(?i)verified|prepared|reviewed|inspector", row.line_number
        ):
            continue
        rows.append(row)

    logger.info("EDOP summary rows: %s", len(rows))
    return rows


# Higher zoom so caption text / corrosion detail stay readable in Excel.
_SHOT_ZOOM = 2.0


def _classify_page(text: str) -> str:
    """Hard-gate: mixed_page | iso_page | photo_page | other."""
    lower = text.lower()
    has_drawing = "drawing not to scale" in lower
    anomaly_views = bool(
        re.search(r"(?i)(?:General|Close|Further|Sectional)\s+view", text)
    )
    # Parenthetical view captions — not "Ref: photo #" callouts on sketches.
    view_photo_n = len(re.findall(r"(?i)\(\s*Photo\s*#\s*\d+", text))
    ref_photo_n = len(re.findall(r"(?i)Ref\.?\s*:?\s*photo\s*#", text))
    has_line_meta = bool(re.search(r"(?i)LINE\s*NUMBER\s*:", text))
    has_legend = bool(
        re.search(
            r"(?i)pipe\s*support|weld\s*joint|flow\s*direction|corroded\s*areas",
            text,
        )
    )
    has_equipment = bool(
        re.search(r"(?i)description of anomalies|equipment\s*id|anomaly description", text)
    )
    has_iso_signal = has_drawing or (has_line_meta and ref_photo_n > 0) or (
        has_line_meta and has_legend
    )
    has_anomaly_signal = anomaly_views or view_photo_n >= 1

    # Anomaly photos + ISO sketch on the same page → split both ways.
    if has_iso_signal and has_anomaly_signal:
        return "mixed_page"
    if has_drawing and (has_line_meta or has_legend or ref_photo_n > 0) and not anomaly_views:
        return "iso_page"
    if has_drawing and not anomaly_views and view_photo_n == 0:
        return "iso_page"
    if has_line_meta and ref_photo_n > 0 and not anomaly_views and not has_equipment:
        return "iso_page"
    if has_line_meta and has_legend and not anomaly_views and not has_equipment:
        return "iso_page"
    if has_anomaly_signal or has_equipment:
        return "photo_page"
    return "other"


def _line_split_frac(page: fitz.Page) -> float | None:
    """Page-height fraction of the LINE NUMBER label (start of the ISO sketch)."""
    page_h = float(page.rect.height) or 1.0
    ys: list[float] = []
    words = page.get_text("words") or []
    for i, winfo in enumerate(words):
        token = (winfo[4] or "").strip()
        nxt = (words[i + 1][4] if i + 1 < len(words) else "") or ""
        # Sketch header is uppercase "LINE NUMBER:" — not the form label "Line Number".
        if token == "LINE" and nxt.upper().startswith("NUMBER"):
            ys.append(float(winfo[1]) / page_h)
    if not ys:
        return None
    return min(ys)


def _prepared_frac(page: fitz.Page) -> float | None:
    page_h = float(page.rect.height) or 1.0
    ys = [
        float(w[1]) / page_h
        for w in (page.get_text("words") or [])
        if re.fullmatch(r"(?i)Prepared", (w[4] or "").strip())
    ]
    return min(ys) if ys else None


def _collage_cols(n: int) -> int:
    """1–3 comfortable; 4–10 and >10 always 4 columns (4×N)."""
    if n <= 0:
        return 1
    if n <= 3:
        return n
    return 4


def _is_field_photo_crop(img: Image.Image) -> bool:
    """Keep inspection photo crops; drop logos, stamps, near-blank tiles."""
    w, h = img.size
    if w < 120 or h < 100:
        return False
    aspect = w / max(h, 1)
    if aspect > 3.4 or aspect < 0.32:
        return False
    sample = img.convert("L").resize((64, 64))
    vals = list(sample.getdata())
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return var >= 160.0


def _render_page_shot(page: fitz.Page, zoom: float = _SHOT_ZOOM) -> Image.Image:
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


@dataclass
class PairedPhoto:
    """One field photo cropped from a screenshot, locked to its caption."""

    path: Path
    photo_num: int
    caption: str
    y0: float
    x0: float


def _view_caption_anchors(
    page: fitz.Page,
) -> list[tuple[int, float, float, str]]:
    """Return (photo_num, y0, x0, caption_body) for view captions (not Ref Photo).

    Handles both ``(Photo #01)`` and ``(Photo # 01)`` tokenizations.
    """
    words = page.get_text("words") or []
    anchors: list[tuple[int, float, float, str]] = []
    for i, winfo in enumerate(words):
        token = (winfo[4] or "").strip()
        prev = (words[i - 1][4] if i else "") or ""
        prev2 = (words[i - 2][4] if i >= 2 else "") or ""
        prev3 = (words[i - 3][4] if i >= 3 else "") or ""
        num: int | None = None
        # Single token: "(Photo#02)" or "(Photo #02)"
        m_one = re.fullmatch(r"\(?Photo\s*#\s*(\d{1,3})\)?", token, flags=re.I)
        if m_one:
            num = int(m_one.group(1))
        else:
            # "(Photo" "#" "01)"
            m_split = re.fullmatch(r"(\d{1,3})\)?", token)
            if m_split and re.fullmatch(r"#", prev) and re.search(r"(?i)photo", prev2):
                if re.search(r"(?i)^ref", prev3):
                    continue
                num = int(m_split.group(1))
            else:
                m = re.fullmatch(r"#(\d{1,3})\)?", token)
                if not m:
                    continue
                if not re.search(r"(?i)photo", prev):
                    continue
                if re.search(r"(?i)^ref", prev2):
                    continue
                num = int(m.group(1))
        cy = float(winfo[1])
        cx = float(winfo[0])
        anchors.append((num, cy, cx, ""))

    # Caption text is only the words in this photo's column, just above the
    # "(Photo #NN)" marker — not the caption of the photo beside it.
    filled: list[tuple[int, float, float, str]] = []
    for num, cy, cx, _body in anchors:
        left_limit = cx - 260.0
        for onum, ocy, ocx, _ in anchors:
            if onum == num and abs(ocy - cy) < 1 and abs(ocx - cx) < 1:
                continue
            if ocx < cx - 8 and abs(ocy - cy) < 18:
                left_limit = max(left_limit, ocx + 4.0)
        parts: list[tuple[float, float, str]] = []
        for w2 in words:
            y2, x2 = float(w2[1]), float(w2[0])
            tok2 = (w2[4] or "").strip()
            if not tok2 or re.search(r"(?i)^\(photo|^photo|^#\d|^#$|\d{1,3}\)$", tok2):
                continue
            if not (left_limit <= x2 < cx - 1.0):
                continue
            if not (cy - 48.0 <= y2 <= cy + 6.0):
                continue
            parts.append((y2, x2, tok2))
        parts.sort()
        body = re.sub(r"\s+", " ", " ".join(t for _, _, t in parts)).strip(" -:")
        body = re.sub(r"(?i)\(?\s*$", "", body).strip(" -:")
        filled.append((num, cy, cx, body))

    seen: set[tuple[int, int]] = set()
    uniq: list[tuple[int, float, float, str]] = []
    for a in sorted(filled, key=lambda t: (t[1], t[2])):
        key = (a[0], int(a[2] // 40))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)
    return uniq


def _text_under_photo(
    page: fitz.Page, x0: float, y0: float, x1: float, y1: float
) -> str:
    """Words sitting under this photo, inside its own column."""
    parts: list[tuple[float, float, str]] = []
    for w in page.get_text("words") or []:
        tok = (w[4] or "").strip()
        if not tok or re.search(
            r"(?i)^\(photo|^photo\s*#|^#\d|^#$|\(?photo#\d|\d{1,3}\)$", tok
        ):
            continue
        if re.search(
            r"(?i)^(prepared|verified|reviewed|approved|inspector|legend)$", tok
        ):
            continue
        wx0, wy0, wx1 = float(w[0]), float(w[1]), float(w[2])
        cx = (wx0 + wx1) / 2.0
        if cx < x0 - 8.0 or cx > x1 + 8.0:
            continue
        if not (y1 - 16.0 <= wy0 <= y1 + 46.0):
            continue
        parts.append((wy0, wx0, tok))
    parts.sort()
    body = re.sub(r"\s+", " ", " ".join(t for _, _, t in parts)).strip(" -:")
    return re.sub(r"(?i)\(?\s*$", "", body).strip(" -:")


def _caption_bottom(
    page: fitz.Page,
    x0: float,
    x1: float,
    y1: float,
    y_limit: float,
) -> float:
    """Bottom of the caption printed under this photo, in page points."""
    bottom = y1
    limit = min(y1 + 52.0, y_limit)
    for w in page.get_text("words") or []:
        tok = (w[4] or "").strip()
        if not tok:
            continue
        wx0, wy0, wx1, wy1 = (float(w[0]), float(w[1]), float(w[2]), float(w[3]))
        cx = (wx0 + wx1) / 2.0
        if cx < x0 - 8.0 or cx > x1 + 8.0:
            continue
        if y1 - 8.0 <= wy0 <= limit:
            bottom = max(bottom, wy1)
    return min(bottom + 2.0, y_limit)


def _merge_stacked_boxes(
    boxes: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Merge vertically adjacent same-column image fragments into one photo box."""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: (b[1], b[0]))
    merged: list[tuple[float, float, float, float]] = []
    for x0, y0, x1, y1 in boxes:
        if not merged:
            merged.append((x0, y0, x1, y1))
            continue
        px0, py0, px1, py1 = merged[-1]
        same_col = abs(x0 - px0) < 25 and abs(x1 - px1) < 25
        touching = abs(y0 - py1) < 8
        if same_col and touching:
            merged[-1] = (min(px0, x0), py0, max(px1, x1), max(py1, y1))
        else:
            merged.append((x0, y0, x1, y1))
    return merged


def _crop_photos_from_shot(
    page: fitz.Page,
    shot: Image.Image,
    *,
    zoom: float,
    out_dir: Path,
    stem: str,
    page_index: int,
    y_band: tuple[float, float] | None = None,
) -> list[PairedPhoto]:
    """Crop field photos and pair each with the caption sitting under it."""
    page_h = float(page.rect.height) or 1.0
    page_w = float(page.rect.width) or 1.0
    sw, sh = shot.size
    y_lo, y_hi = y_band if y_band is not None else (0.10, 0.90)
    infos = page.get_image_info(xrefs=True)

    raw_boxes: list[tuple[float, float, float, float]] = []
    for im in infos:
        bbox = im["bbox"]
        bw = float(bbox[2] - bbox[0])
        bh = float(bbox[3] - bbox[1])
        if bw < _MIN_PAGE_W or bh < _MIN_PAGE_H:
            continue
        cy_frac = ((bbox[1] + bbox[3]) / 2.0) / page_h
        if cy_frac < y_lo or cy_frac > y_hi:
            continue
        if bbox[1] < page_h * 0.08 or bbox[3] > page_h * 0.94:
            continue
        if bw < page_w * 0.16 or bh < page_h * 0.09:
            continue
        raw_boxes.append(
            (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        )

    # Slot-dedupe near-identical embeds, then stitch stacked halves.
    slots: dict[tuple[int, int], tuple[float, float, float, float]] = {}
    for x0, y0, x1, y1 in raw_boxes:
        key = (int(((x0 + x1) / 2.0) / 36.0), int(((y0 + y1) / 2.0) / 36.0))
        area = (x1 - x0) * (y1 - y0)
        prev = slots.get(key)
        if prev is None or area > (prev[2] - prev[0]) * (prev[3] - prev[1]):
            slots[key] = (x0, y0, x1, y1)
    photo_boxes = _merge_stacked_boxes(list(slots.values()))

    # Probe crops to drop logos / blank tiles before caption pairing.
    candidates: list[tuple[float, float, float, float, Image.Image]] = []
    for x0, y0, x1, y1 in photo_boxes:
        sx0 = max(0, int(x0 * zoom) - 2)
        sy0 = max(0, int(y0 * zoom) - 2)
        sx1 = min(sw, int(x1 * zoom) + 2)
        sy1 = min(sh, int(y1 * zoom) + 2)
        if sx1 - sx0 < 80 or sy1 - sy0 < 60:
            continue
        crop = shot.crop((sx0, sy0, sx1, sy1))
        if not _is_field_photo_crop(crop):
            continue
        candidates.append((x0, y0, x1, y1, crop))

    anchors = _view_caption_anchors(page)
    out_dir.mkdir(parents=True, exist_ok=True)
    paired: list[PairedPhoto] = []
    used_anchor: set[int] = set()
    # Reading order: same visual row (left to right), then the next row down.
    order: list[int] = []
    rows: list[list[int]] = []
    for i in sorted(range(len(candidates)), key=lambda i: candidates[i][1]):
        if not rows or candidates[i][1] - candidates[rows[-1][0]][1] > 36.0:
            rows.append([i])
        else:
            rows[-1].append(i)
    for row in rows:
        order.extend(sorted(row, key=lambda i: candidates[i][0]))
    for seq, i in enumerate(order, start=1):
        x0, y0, x1, y1, crop = candidates[i]
        best_a: int | None = None
        best_score: float | None = None
        for ai, (num, cy, cx, _body) in enumerate(anchors):
            if ai in used_anchor:
                continue
            if y1 > cy + 18.0:
                continue
            gap = cy - y1
            if gap > 160.0:
                continue
            photo_cx = (x0 + x1) / 2.0
            score = gap + 0.05 * abs(photo_cx - cx)
            if x0 - 20 <= cx <= x1 + 20:
                score -= 15.0
            if best_score is None or score < best_score:
                best_score = score
                best_a = ai
        if best_a is not None:
            used_anchor.add(best_a)
            num = anchors[best_a][0]
        else:
            num = seq
        body = _text_under_photo(page, x0, y0, x1, y1)
        # Keep the original screenshot pixels, including the caption under the photo.
        next_top = float(page.rect.height)
        for j, (ox0, oy0, ox1, oy1, _other) in enumerate(candidates):
            if j == i:
                continue
            if ox0 < x1 - 8 and ox1 > x0 + 8 and oy0 > y1 - 1:
                next_top = min(next_top, oy0)
        y_bot = _caption_bottom(page, x0, x1, y1, next_top - 1.5)
        sx0 = max(0, int(x0 * zoom) - 2)
        sy0 = max(0, int(y0 * zoom) - 2)
        sx1 = min(sw, int(x1 * zoom) + 2)
        sy1 = min(sh, int(y_bot * zoom) + 3)
        crop = shot.crop((sx0, sy0, sx1, sy1))
        out = out_dir / f"{stem}_p{page_index + 1:02d}_{seq:02d}_photo{num:02d}.png"
        crop.save(out, format="PNG", optimize=False)
        paired.append(
            PairedPhoto(path=out, photo_num=num, caption=body, y0=y0, x0=x0)
        )
    return paired


def _crop_iso_from_shot(
    page: fitz.Page,
    shot: Image.Image,
    out: Path,
    *,
    y_band: tuple[float, float] | None = None,
    allow_mixed: bool = False,
) -> Path | None:
    """Crop isometric sketch only — never include anomaly photo tiles."""
    text = page.get_text("text") or ""
    kind = _classify_page(text)
    if kind not in {"iso_page", "mixed_page"}:
        return None
    if kind == "mixed_page" and not allow_mixed:
        return None

    w, h = shot.size
    page_h = float(page.rect.height) or 1.0
    if y_band is not None:
        y_top_frac, y_bot_frac = y_band
    else:
        y_top_frac = 0.14
        y_bot_frac = 0.78
        line_meta_y: float | None = None
        for winfo in page.get_text("words") or []:
            token = (winfo[4] or "").strip()
            y0 = float(winfo[1]) / page_h
            if re.fullmatch(r"(?i)LINE", token):
                line_meta_y = y0 if line_meta_y is None else min(line_meta_y, y0)
            if re.search(r"(?i)^Prepared|^Verified|^Reviewed|^Approved", token):
                y_bot_frac = min(y_bot_frac, max(0.55, y0 - 0.01))
            if re.search(r"(?i)^Drawing$", token):
                y_bot_frac = max(y_bot_frac, min(0.88, y0 + 0.08))
        if line_meta_y is not None:
            y_top_frac = max(0.05, min(0.22, line_meta_y - 0.01))

    # Keep large field photos out of the ISO crop.
    for im in page.get_image_info(xrefs=True):
        bbox = im["bbox"]
        bw = float(bbox[2] - bbox[0])
        bh = float(bbox[3] - bbox[1])
        if bw < page.rect.width * 0.18 or bh < page.rect.height * 0.10:
            continue
        cy = ((bbox[1] + bbox[3]) / 2.0) / page_h
        if y_top_frac <= cy <= y_bot_frac:
            photo_bot = float(bbox[3]) / page_h
            photo_top = float(bbox[1]) / page_h
            if photo_bot < (y_top_frac + y_bot_frac) / 2:
                y_top_frac = max(y_top_frac, photo_bot + 0.01)
            else:
                y_bot_frac = min(y_bot_frac, photo_top - 0.01)

    y_top_frac = max(0.04, min(0.85, y_top_frac))
    y_bot_frac = max(y_top_frac + 0.12, min(0.94, y_bot_frac))
    if y_bot_frac - y_top_frac < 0.12:
        return None
    crop = shot.crop(
        (
            int(w * 0.04),
            int(h * y_top_frac),
            int(w * 0.96),
            int(h * y_bot_frac),
        )
    )
    bordered = Image.new("RGB", (crop.width + 16, crop.height + 16), (255, 255, 255))
    bordered.paste(crop, (8, 8))
    out.parent.mkdir(parents=True, exist_ok=True)
    bordered.save(out, format="PNG", optimize=True)
    return out


def _label_font(size: int = 15):
    for name in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_caption(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for w in words[1:]:
        if len(cur) + 1 + len(w) <= max_chars:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines[:5]


# Excel Photos cell: column N width 120, row capped at 409pt. The collage is
# drawn at that exact pixel shell so the embed fills the cell with no margin.
_PHOTO_SHELL_W = int(120.0 * 7.0 + 5) - 2
_PHOTO_SHELL_H = int(409.0 * 96.0 / 72.0) - 2


def _collage(
    paths: list[Path],
    out: Path,
    *,
    photo_numbers: list[int] | None = None,
    captions: dict[int, str] | None = None,
    caption_texts: list[str] | None = None,
    cols: int = 4,
) -> Path:
    """Contact sheet that fills the Excel Photos cell. Each photo fills its tile."""
    if not paths:
        raise ValueError("no photos for collage")
    nums = list(photo_numbers or [])
    while len(nums) < len(paths):
        nums.append(len(nums) + 1)
    captions = captions or {}

    cols = _collage_cols(len(paths)) if cols == 4 else max(1, min(4, len(paths), cols))
    rows = (len(paths) + cols - 1) // cols
    base_h, extra_h = divmod(_PHOTO_SHELL_H, rows)
    # Each row's tiles share the full cell width, including a short last row.
    tile_widths: list[int] = []
    for r in range(rows):
        n_in = min(cols, len(paths) - r * cols)
        bw, ew = divmod(_PHOTO_SHELL_W, n_in)
        for c in range(n_in):
            tile_widths.append(bw + (ew if c == n_in - 1 else 0))
    font = _label_font(13 if cols >= 3 else 15)
    tiles: list[tuple[Image.Image, int, int]] = []
    texts = list(caption_texts or [])

    for idx, (path, num) in enumerate(zip(paths, nums)):
        r, _c = divmod(idx, cols)
        cell_w = tile_widths[idx]
        tile_h = base_h + (extra_h if r == rows - 1 else 0)
        caption_h = max(44, min(92, tile_h // 4))
        img_h = max(1, tile_h - caption_h)
        with Image.open(path) as src:
            src_im = src.convert("RGB")
            # Cover-fit: the photo fills its frame inside the cell shell.
            tw, th = cell_w, img_h
            scale = max(tw / max(src_im.width, 1), th / max(src_im.height, 1))
            rw = max(1, int(src_im.width * scale))
            rh = max(1, int(src_im.height * scale))
            scaled = src_im.resize((rw, rh), Image.Resampling.LANCZOS)
            left = max(0, (rw - tw) // 2)
            top = max(0, (rh - th) // 2)
            im = scaled.crop((left, top, left + tw, top + th))
        tile = Image.new("RGB", (cell_w, tile_h), (255, 255, 255))
        tile.paste(im, (0, 0))
        draw = ImageDraw.Draw(tile)
        draw.rectangle(
            [0, img_h, cell_w - 1, tile_h - 1],
            fill=(245, 245, 245),
            outline=(90, 90, 90),
            width=1,
        )
        header = f"Photo #{int(num):02d}"
        if idx < len(texts) and texts[idx]:
            body = texts[idx]
        else:
            body = re.sub(r"\s+", " ", captions.get(int(num), "") or "").strip()
        body = re.sub(rf"(?i)^photo\s*#?\s*{int(num)}\s*:?\s*", "", body).strip(" -:")
        caption = f"{header}: {body}" if body else header
        ty = img_h + 3
        for line in _wrap_caption(caption, max_chars=max(22, cell_w // 7)):
            if ty > tile_h - 14:
                break
            draw.text((4, ty), line, fill=(20, 20, 20), font=font)
            ty += 15
        tiles.append((tile, cell_w, tile_h))

    canvas = Image.new("RGB", (_PHOTO_SHELL_W, _PHOTO_SHELL_H), (255, 255, 255))
    y = 0
    for r in range(rows):
        x = 0
        row_h = 0
        for c in range(cols):
            idx = r * cols + c
            if idx >= len(tiles):
                break
            tile, tw, th = tiles[idx]
            canvas.paste(tile, (x, y))
            x += tw
            row_h = th
        y += row_h

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, format="PNG", optimize=False)
    return out


def _caption_map_from_bullets(bullets: list[str]) -> dict[int, str]:
    """Map Photo # → original caption body for collage labels."""
    out: dict[int, str] = {}
    for bullet in bullets:
        text = bullet.lstrip("-• ").strip()
        m = re.search(
            r"(?is)^(.+?)\s*\(?\s*Photo\s*#?\s*(\d+)\s*(?:[-–—&to]+\s*\d+)?\s*\)?\s*$",
            text,
        )
        if not m:
            m = re.search(r"(?i)Photo\s*#?\s*(\d+)\s*[:.\-]\s*(.+)$", text)
            if m:
                out[int(m.group(1))] = re.sub(r"\s+", " ", m.group(2)).strip()
            continue
        body = re.sub(r"\s+", " ", m.group(1)).strip(" -:")
        num = int(m.group(2))
        if body:
            out[num] = body
    return out


_CAPTION_VIEW_RE = re.compile(
    r"(?i)\b(?:General view|Close view|Further view|Sectional view|View showing|"
    r"Scattered|Composite|Holes?\s+observed|Pin\s+hole)\b"
)


def _parse_captions(text: str) -> tuple[list[str], list[PhotoRef]]:
    bullets: list[str] = []
    refs: list[PhotoRef] = []
    for m in _PHOTO_CAPTION_RE.finditer(text):
        body = re.sub(r"\s+", " ", m.group(1)).strip(" -:")
        if len(body) < 4 or re.search(r"(?i)prepared|reviewed|verified|approved", body):
            continue
        # When the regex over-spans the anomaly header into a view caption,
        # keep only the trailing view phrase.
        if len(body) > 90 or re.search(r"(?i)anomaly|fasteners\.\s*Light", body):
            starts = list(_CAPTION_VIEW_RE.finditer(body))
            if starts:
                body = body[starts[-1].start() :].strip()
        if len(body) > 160:
            body = body[-160:].lstrip(" .,;")
        a = int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        ref = PhotoRef(
            raw=f"{a:02d}-{b:02d}" if a != b else f"{a:02d}",
            start=a,
            end=b,
        )
        refs.append(ref)
        suffix = ref.format_excel()
        line = body if "photo #" in body.lower() else f"{body} {suffix}"
        bullets.append(line)
    if len(bullets) > 1:
        bullets = [f"-{b}" for b in bullets]
    return bullets, _compress_photo_refs(refs)


def parse_line_pdf(pdf_path: Path, work_dir: Path) -> LinePdfParse:
    pdf_path = Path(pdf_path)
    work_dir = Path(work_dir)
    photos_dir = work_dir / "photos"
    iso_dir = work_dir / "iso"
    photos_dir.mkdir(parents=True, exist_ok=True)
    iso_dir.mkdir(parents=True, exist_ok=True)

    result = LinePdfParse(source_pdf=pdf_path)
    doc = fitz.open(pdf_path)
    try:
        full = "\n".join(doc[i].get_text("text") or "" for i in range(doc.page_count))
        m = _SN_RE.search(full)
        if m:
            result.sn = int(m.group(1) or m.group(2))
        m = re.search(r"(?i)LINE\s*NUMBER\s*:\s*([^\n]+)", full)
        if m:
            result.line_number = _normalize_line_tag(m.group(1))
        if not result.line_number:
            m = re.search(
                r"(?i)(?:Line\s*Number|Equipment\s*ID)\s*\n\s*([^\n]{3,80})",
                full,
            )
            if m:
                result.line_number = _normalize_line_tag(m.group(1).split("(")[0])
        # Parenthetical From–To under Equipment ID / line tag (fallback when
        # summary checklist is missing or a different facility format).
        m = re.search(
            r"(?is)(?:Equipment\s*ID|Line\s*Number)\s*\n\s*[^\n]+\n\s*\(([^)]{5,160})\)",
            full,
        )
        if not m:
            m = re.search(
                r"(?i)\(((?:From\s+)?[^()]{3,80}\s+to\s+[^()]{3,80})\)",
                full,
            )
        if m:
            paren = normalize_whitespace(m.group(1))
            if paren and re.search(r"(?i)\bto\b", paren):
                result.line_description = paren
        m = re.search(r"(?i)P\s*&\s*ID\s*NUMBER\s*:\s*([^\n]+)", full)
        if m:
            result.pid_no = _prefer_pid(m.group(1).strip())

        m = re.search(
            r"(?is)Anomaly\s*description\s*(.+?)(?=General view|Close view|Further|"
            r"Sectional|Prepared\s*By|LINE\s*NUMBER:|Drawing\s+not|"
            r"The line was not|Line not found|$)",
            full,
        )
        if m:
            header = re.sub(r"\s+", " ", m.group(1)).strip(" .\n")
            header = re.split(r"(?i)\(?\s*Photo\s*#", header)[0].strip(" .")
            if header and len(header) > 2:
                result.anomaly_header = header

        captions, refs = _parse_captions(full)
        result.caption_bullets = captions
        result.photo_refs = refs

        stem = re.sub(r"[^\w.-]+", "_", pdf_path.stem)[:60]
        shots_dir = work_dir / "shots"
        singles_dir = photos_dir / "singles"
        shots_dir.mkdir(parents=True, exist_ok=True)
        singles_dir.mkdir(parents=True, exist_ok=True)

        paired_photos: list[PairedPhoto] = []
        # (page_index, optional iso y_band, allow_mixed)
        iso_jobs: list[tuple[int, tuple[float, float] | None, bool]] = []
        for i in range(doc.page_count):
            page = doc[i]
            page_text = page.get_text("text") or ""
            kind = _classify_page(page_text)
            if kind == "other":
                continue
            shot = _render_page_shot(page, _SHOT_ZOOM)
            shot_path = shots_dir / f"{stem}_p{i + 1:02d}_{kind}.png"
            shot.save(shot_path, format="PNG", optimize=False)

            if kind == "iso_page":
                iso_jobs.append((i, None, False))
                continue
            if kind == "photo_page":
                paired_photos.extend(
                    _crop_photos_from_shot(
                        page,
                        shot,
                        zoom=_SHOT_ZOOM,
                        out_dir=singles_dir,
                        stem=stem,
                        page_index=i,
                    )
                )
                continue
            if kind == "mixed_page":
                # Photos are above LINE NUMBER; ISO sketch starts at that label.
                split = _line_split_frac(page) or 0.48
                prep = _prepared_frac(page)
                iso_bot = prep - 0.01 if prep and prep > split + 0.12 else 0.86
                paired_photos.extend(
                    _crop_photos_from_shot(
                        page,
                        shot,
                        zoom=_SHOT_ZOOM,
                        out_dir=singles_dir,
                        stem=stem,
                        page_index=i,
                        y_band=(0.08, max(0.20, split - 0.005)),
                    )
                )
                iso_jobs.append((i, (split - 0.005, iso_bot), True))

        # Keep every anomaly photo, in page reading order. Do not drop unpaired ones.
        ordered = sorted(paired_photos, key=lambda p: p.path.name)

        result.has_photos = bool(ordered)
        text_caps = _caption_map_from_bullets(result.caption_bullets)
        caption_texts: list[str] = []
        for p in ordered:
            body = (p.caption or text_caps.get(p.photo_num) or "").strip()
            body = re.sub(r"\s+", " ", body)
            body = re.sub(rf"(?i)\(?\s*Photo\s*#?\s*{p.photo_num}\s*\)?\s*$", "", body)
            caption_texts.append(body.strip(" -:"))

        if ordered:
            bullets: list[str] = []
            for p, body in zip(ordered, caption_texts):
                line = (
                    f"{body} (Photo #{p.photo_num:02d})"
                    if body
                    else f"Photo #{p.photo_num:02d}"
                )
                bullets.append(line)
            result.caption_bullets = (
                [f"-{b}" for b in bullets] if len(bullets) > 1 else bullets
            )
            result.photo_refs = _compress_photo_refs(
                [
                    PhotoRef(
                        raw=f"{p.photo_num:02d}",
                        start=p.photo_num,
                        end=p.photo_num,
                    )
                    for p in ordered
                ]
            )

        # Original crops only. Excel tiles them; nothing is resized here.
        result.photo_paths = [p.path for p in ordered] if ordered else []

        # Prefer dedicated iso_page, then mixed_page sketch band (no anomaly tiles).
        pure_iso = [j for j in iso_jobs if not j[2]]
        mixed_iso = [j for j in iso_jobs if j[2]]
        ordered_iso = list(reversed(pure_iso)) + list(reversed(mixed_iso))
        for page_index, iso_band, allow_mixed in ordered_iso:
            page = doc[page_index]
            shot = _render_page_shot(page, _SHOT_ZOOM)
            iso_out = iso_dir / f"{stem}_iso.png"
            rendered = _crop_iso_from_shot(
                page,
                shot,
                iso_out,
                y_band=iso_band,
                allow_mixed=allow_mixed,
            )
            if rendered is not None:
                result.iso_path = rendered
                break
    finally:
        doc.close()
    return result


def _sn_from_filename(name: str) -> int | None:
    """Accept ``8._AI-…``, ``8_AI-…``, ``8-AI-…``, ``8. AI-…`` (browser-safe)."""
    m = re.match(r"^(\d{1,3})(?:\._|[._\-\s]+)(?!\.)", name)
    if not m:
        m = re.match(r"^(\d{1,3})_", name)
    if not m:
        return None
    sn = int(m.group(1))
    if not 1 <= sn <= 500:
        return None
    return sn


def _is_summary_pdf(path: Path) -> bool:
    """True for the process-piping checklist / summary report (not a line pack)."""
    name_l = path.name.lower()
    if "summary" in name_l:
        return True
    try:
        doc = fitz.open(path)
    except Exception:  # noqa: BLE001
        return False
    try:
        if doc.page_count >= 8:
            sample = "\n".join(
                doc[i].get_text("text") or "" for i in range(min(4, doc.page_count))
            )
            if re.search(
                r"(?i)VISUAL INSPECTION OBSERVATIONS CHECKLIST", sample
            ) or re.search(r"(?i)FVI\s+On\s+Process\s+Piping\s+Summary", sample):
                return True
    finally:
        doc.close()
    return False


def _sn_from_pdf(path: Path) -> int | None:
    try:
        doc = fitz.open(path)
    except Exception:  # noqa: BLE001
        return None
    try:
        text = "\n".join(
            doc[i].get_text("text") or "" for i in range(min(2, doc.page_count))
        )
        m = _SN_RE.search(text)
        if not m:
            return None
        return int(m.group(1) or m.group(2))
    finally:
        doc.close()


def discover_line_pdfs(pdf_dir: Path) -> tuple[dict[int, Path], list[str]]:
    """Map JV Piping List S/N → line PDF. Returns (map, skip_notes)."""
    pdf_dir = Path(pdf_dir)
    out: dict[int, Path] = {}
    notes: list[str] = []
    for path in sorted(pdf_dir.glob("*.pdf")):
        if path.stat().st_size < 500:
            notes.append(f"skip empty/tiny: {path.name}")
            continue
        if _is_summary_pdf(path):
            notes.append(f"skip summary/checklist: {path.name}")
            continue
        sn_file = _sn_from_filename(path.name)
        sn_content = _sn_from_pdf(path)
        sn = sn_content or sn_file
        if sn is None:
            notes.append(f"skip unmapped (no S/N): {path.name}")
            continue
        if sn_content and sn_file and sn_content != sn_file:
            notes.append(
                f"SN mismatch file={sn_file} content={sn_content} → using {sn_content} ({path.name})"
            )
        elif sn_file is None and sn_content is not None:
            notes.append(f"SN {sn_content} from PDF content: {path.name}")
        prev = out.get(sn)
        if prev is not None:
            notes.append(f"SN {sn} duplicate; keeping {prev.name}, ignore {path.name}")
            continue
        out[sn] = path
    return out, notes


def build_edop_result(
    summary_pdf: Path,
    line_pdf_dir: Path,
    work_dir: Path,
    *,
    max_lines: int = 0,
) -> ExtractionResult:
    summary_pdf = Path(summary_pdf)
    line_pdf_dir = Path(line_pdf_dir)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if summary_pdf.stat().st_size < 1000:
        raise ValueError("Summary PDF is empty or too small.")

    doc = fitz.open(summary_pdf)
    try:
        metadata = parse_summary_metadata(doc)
        summary_rows = parse_summary_rows(doc)
        sample = "\n".join(
            doc[i].get_text("text") or "" for i in range(min(3, doc.page_count))
        )
    finally:
        doc.close()

    warnings: list[str] = []
    if not summary_rows:
        # General tool: do not hard-fail — continue from line PDFs alone.
        kind = detect_pdf_kind(summary_pdf)
        warnings.append(
            "summary_checklist_not_parsed: "
            f"treated upload as `{kind}`; Line Description will come from each "
            "line PDF when available. For richest From–To text, upload a Process "
            "Piping Visual Inspection Observations Checklist as the summary."
        )
        logger.warning("%s (%s)", warnings[-1], summary_pdf.name)
    elif not re.search(r"(?i)checklist|process piping|FVI", sample):
        warnings.append(f"summary_text_unusual:{summary_pdf.name}")
        logger.warning("Summary PDF text looks unusual: %s", summary_pdf.name)

    summary_by_sn = {r.sn: r for r in summary_rows}
    line_pdfs, discover_notes = discover_line_pdfs(line_pdf_dir)
    if not line_pdfs:
        kind = detect_pdf_kind(summary_pdf)
        if kind == "fire_water":
            raise ValueError(
                "This looks like a Fire Water FVI report. Switch to "
                "Workflow A (or Auto) and upload that single PDF there — "
                "Workflow B is for Process Piping summary + line packs."
            )
        raise ValueError(
            "No line-number PDFs were recognized. Upload the per-line PDFs "
            "(names like `8._AI-001-….pdf`) or a zip of them. "
            "For a single Fire Water report, use Workflow A instead."
        )

    sns = sorted(line_pdfs)
    if max_lines:
        sns = sns[:max_lines]

    findings: list[Finding] = []
    for sn in sns:
        pdf_path = line_pdfs[sn]
        logger.info("Parsing line PDF SN=%s %s", sn, pdf_path.name)
        parsed = parse_line_pdf(pdf_path, work_dir / f"sn_{sn:03d}")
        summary = summary_by_sn.get(sn)
        # Drop peak RSS between line PDFs (Railway OOM kills otherwise).
        gc.collect()

        line_number = (
            (summary.line_number if summary else None)
            or parsed.line_number
            or f"SN-{sn}"
        )
        line_description = None
        if summary:
            line_description = join_from_to(summary.from_text, summary.to_text)
        if not line_description:
            line_description = parsed.line_description
        pid_no = _prefer_pid(
            summary.pid_no if summary else None,
            parsed.pid_no,
        )

        if parsed.has_photos and parsed.caption_bullets:
            anomaly = "\n".join(parsed.caption_bullets)
        elif parsed.has_photos and parsed.anomaly_header:
            anomaly = parsed.anomaly_header
            if parsed.photo_refs:
                anomaly = f"{anomaly} {parsed.photo_refs[0].format_excel()}"
        else:
            anomaly = "no data"

        finding = Finding(
            sn=sn,
            asset_class=AssetClass.FIRE_WATER_PIPING,
            insp_date=metadata.inspection_date,
            line_number=line_number,
            line_description=line_description,
            anomaly_description=anomaly,
            pid_no=pid_no,
            photo_refs=parsed.photo_refs,
            photos_image_path=parsed.photo_paths[0] if parsed.photo_paths else None,
            photo_image_paths=list(parsed.photo_paths),
            iso_image_path=parsed.iso_path,
            source=FindingSource(
                section="edop_line_pdf",
                pages=[],
                table_row_index=sn,
                notes=str(pdf_path.name),
            ),
            confidence=0.8 if parsed.has_photos else 0.5,
        )
        finding.apply_judgment_blanks()
        findings.append(finding)

    # Keep JV list order (not filename lexical order after content-SN remap)
    findings.sort(key=lambda f: (f.source.table_row_index or 0, f.line_number or ""))

    matched_with_desc = sum(1 for f in findings if f.line_description)
    with_photos = sum(1 for f in findings if f.photos_image_path)
    no_data = sum(
        1 for f in findings if (f.anomaly_description or "").strip().lower() == "no data"
    )
    warnings.extend(
        [
            f"edop_line_pdfs={len(findings)}",
            f"summary_rows={len(summary_rows)}",
            f"line_descriptions_matched={matched_with_desc}",
            f"rows_with_photos={with_photos}",
            f"rows_no_data={no_data}",
        ]
    )
    warnings.extend(discover_notes[:40])
    if len(discover_notes) > 40:
        warnings.append(f"...and {len(discover_notes) - 40} more discovery notes")

    result = ExtractionResult(
        metadata=metadata,
        findings=findings,
        source_pdf=summary_pdf,
        warnings=warnings,
    )
    return result
