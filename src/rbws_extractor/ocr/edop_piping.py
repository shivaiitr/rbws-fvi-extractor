"""EDOP-QP Process Piping FVI: summary checklist + per-line PDF → RBWS rows."""

from __future__ import annotations

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


def _mixed_band_fracs(page: fitz.Page) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return ((photo_y0, photo_y1), (iso_y0, iso_y1)) as page-height fractions."""
    page_h = float(page.rect.height) or 1.0
    photo_ys: list[float] = []
    iso_ys: list[float] = []
    for winfo in page.get_text("words") or []:
        token = (winfo[4] or "").strip()
        y0 = float(winfo[1]) / page_h
        low = token.lower()
        if re.search(r"(?i)^photo$|^#\d+", token) or re.search(
            r"(?i)general|close|further|sectional", token
        ):
            photo_ys.append(y0)
        if re.search(r"(?i)^drawing$|^line$|^number$|^ref$", token) or "scale" in low:
            iso_ys.append(y0)
        if re.search(r"(?i)^prepared$|^verified$|^reviewed$|^approved$", token):
            iso_ys.append(y0)

    text = page.get_text("text") or ""
    # Stronger anchors via full-text word positions already collected; fallbacks:
    if not photo_ys and re.search(r"(?i)\(\s*Photo\s*#", text):
        photo_ys = [0.15, 0.45]
    if not iso_ys and (
        "drawing not to scale" in text.lower()
        or re.search(r"(?i)LINE\s*NUMBER\s*:", text)
    ):
        iso_ys = [0.55, 0.75]

    if photo_ys and iso_ys:
        photo_mid = sum(photo_ys) / len(photo_ys)
        iso_mid = sum(iso_ys) / len(iso_ys)
        if photo_mid < iso_mid:
            split = (max(photo_ys) + min(iso_ys)) / 2.0
            return (0.10, min(0.92, split)), (max(0.08, split), 0.88)
        split = (max(iso_ys) + min(photo_ys)) / 2.0
        return (max(0.08, split), 0.92), (0.08, min(0.88, split))
    if photo_ys and not iso_ys:
        return (0.10, 0.88), (0.50, 0.88)
    if iso_ys and not photo_ys:
        return (0.10, 0.45), (0.12, 0.88)
    return (0.10, 0.48), (0.48, 0.88)


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


def _crop_photos_from_shot(
    page: fitz.Page,
    shot: Image.Image,
    *,
    zoom: float,
    out_dir: Path,
    stem: str,
    page_index: int,
    y_band: tuple[float, float] | None = None,
) -> list[Path]:
    """Crop each field photo tile from a page screenshot (original pixels)."""
    page_h = float(page.rect.height) or 1.0
    page_w = float(page.rect.width) or 1.0
    sw, sh = shot.size
    y_lo, y_hi = y_band if y_band is not None else (0.10, 0.90)
    infos = page.get_image_info(xrefs=True)
    infos.sort(key=lambda im: (round(im["bbox"][1], 1), round(im["bbox"][0], 1)))

    slots: dict[tuple[int, int], tuple[float, float, float, Image.Image]] = {}
    for im in infos:
        bbox = im["bbox"]
        bw = float(bbox[2] - bbox[0])
        bh = float(bbox[3] - bbox[1])
        if bw < _MIN_PAGE_W or bh < _MIN_PAGE_H:
            continue
        cy_frac = ((bbox[1] + bbox[3]) / 2.0) / page_h
        if cy_frac < y_lo or cy_frac > y_hi:
            continue
        if bbox[1] < page_h * 0.08:
            continue
        if bbox[3] > page_h * 0.94:
            continue
        if bw < page_w * 0.16 or bh < page_h * 0.09:
            continue
        # Map PDF points → screenshot pixels
        x0 = max(0, int(bbox[0] * zoom) - 2)
        y0 = max(0, int(bbox[1] * zoom) - 2)
        x1 = min(sw, int(bbox[2] * zoom) + 2)
        y1 = min(sh, int(bbox[3] * zoom) + 2)
        if x1 - x0 < 80 or y1 - y0 < 60:
            continue
        crop = shot.crop((x0, y0, x1, y1))
        if not _is_field_photo_crop(crop):
            continue
        cx = int(((bbox[0] + bbox[2]) / 2.0) / 36.0)
        cy = int(((bbox[1] + bbox[3]) / 2.0) / 36.0)
        area = bw * bh
        key = (cx, cy)
        prev = slots.get(key)
        if prev is None or area > prev[0]:
            slots[key] = (area, float(bbox[1]), float(bbox[0]), crop)

    ordered = sorted(slots.values(), key=lambda t: (t[1], t[2]))
    paths: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, (_area, _y, _x, crop) in enumerate(ordered):
        out = out_dir / f"{stem}_p{page_index + 1:02d}_img{i + 1:02d}.png"
        crop.save(out, format="PNG", optimize=True)
        paths.append(out)
    return paths


def _assign_photo_numbers(
    n_tiles: int,
    caption_by_num: dict[int, str],
    photo_refs: list[PhotoRef],
) -> list[int]:
    """Assign Photo # labels in reading order without cross-page duplicates."""
    if n_tiles <= 0:
        return []
    from_refs: list[int] = []
    for ref in photo_refs:
        for n in range(ref.start, ref.end + 1):
            if n not in from_refs:
                from_refs.append(n)
    if len(from_refs) == n_tiles:
        return from_refs
    from_caps = sorted(caption_by_num.keys())
    if len(from_caps) == n_tiles:
        return from_caps
    # Prefer caption/ref numbers for the leading tiles; fill the rest sequentially.
    base = from_refs or from_caps
    if not base:
        return list(range(1, n_tiles + 1))
    out = list(base[:n_tiles])
    nxt = (max(out) if out else 0) + 1
    while len(out) < n_tiles:
        out.append(nxt)
        nxt += 1
    return out


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

    y_top_frac = max(0.04, min(0.85, y_top_frac))
    y_bot_frac = max(y_top_frac + 0.12, min(0.94, y_bot_frac))
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


def _collage(
    paths: list[Path],
    out: Path,
    *,
    photo_numbers: list[int] | None = None,
    captions: dict[int, str] | None = None,
    cols: int = 4,
) -> Path:
    """4-column contact sheet: every photo + original caption under each tile."""
    if not paths:
        raise ValueError("no photos for collage")
    nums = list(photo_numbers or [])
    while len(nums) < len(paths):
        nums.append(len(nums) + 1)
    captions = captions or {}

    # >10 tiles always lock to 4 columns (4×N); 4–10 also 4-col; 1–3 stay narrow.
    cols = _collage_cols(len(paths)) if cols == 4 else max(1, min(4, len(paths), cols))
    # Large tiles so Excel column-fill stays engineer-readable (file size OK).
    if cols >= 4:
        cell_w = 420 if len(paths) > 10 else 460
    elif cols == 3:
        cell_w = 480
    else:
        cell_w = 560
    img_h = int(cell_w * 0.82)
    caption_h = 88
    tile_h = img_h + caption_h
    pad = 6
    font = _label_font(14)
    tiles: list[Image.Image] = []

    for path, num in zip(paths, nums):
        with Image.open(path) as src:
            src_im = src.convert("RGB")
            # Cover-fit: fill the photo frame (crop center) — no empty gutters in-tile.
            tw, th = cell_w - 4, img_h - 4
            scale = max(tw / max(src_im.width, 1), th / max(src_im.height, 1))
            rw = max(1, int(src_im.width * scale))
            rh = max(1, int(src_im.height * scale))
            scaled = src_im.resize((rw, rh), Image.Resampling.LANCZOS)
            left = max(0, (rw - tw) // 2)
            top = max(0, (rh - th) // 2)
            im = scaled.crop((left, top, left + tw, top + th))
        tile = Image.new("RGB", (cell_w, tile_h), (255, 255, 255))
        tile.paste(im, (2, 2))
        draw = ImageDraw.Draw(tile)
        draw.rectangle([0, 0, cell_w - 1, img_h - 1], outline=(140, 140, 140), width=1)
        draw.rectangle(
            [0, img_h, cell_w - 1, tile_h - 1],
            fill=(245, 245, 245),
            outline=(140, 140, 140),
            width=1,
        )
        header = f"Photo #{int(num):02d}"
        body = re.sub(r"\s+", " ", captions.get(int(num), "") or "").strip()
        body = re.sub(rf"(?i)^photo\s*#?\s*{int(num)}\s*:?\s*", "", body).strip(" -:")
        caption = f"{header}: {body}" if body else header
        ty = img_h + 4
        for line in _wrap_caption(caption, max_chars=max(28, cell_w // 8)):
            draw.text((5, ty), line, fill=(20, 20, 20), font=font)
            ty += 15
        tiles.append(tile)

    rows = (len(tiles) + cols - 1) // cols
    width = cols * cell_w + (cols + 1) * pad
    height = rows * tile_h + (rows + 1) * pad
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    for idx, tile in enumerate(tiles):
        r, c = divmod(idx, cols)
        canvas.paste(tile, (pad + c * (cell_w + pad), pad + r * (tile_h + pad)))

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

        photo_paths: list[Path] = []
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
            shot.save(shot_path, format="PNG", optimize=True)

            if kind == "iso_page":
                iso_jobs.append((i, None, False))
                continue
            if kind == "photo_page":
                photo_paths.extend(
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
                photo_band, iso_band = _mixed_band_fracs(page)
                photo_paths.extend(
                    _crop_photos_from_shot(
                        page,
                        shot,
                        zoom=_SHOT_ZOOM,
                        out_dir=singles_dir,
                        stem=stem,
                        page_index=i,
                        y_band=photo_band,
                    )
                )
                iso_jobs.append((i, iso_band, True))

        # Dedupe identical screenshot crops; preserve page reading order.
        uniq_paths: list[Path] = []
        seen_sizes: set[tuple[int, int, int]] = set()
        for path in photo_paths:
            with Image.open(path) as im:
                key = (im.size[0], im.size[1], path.stat().st_size)
            if key in seen_sizes:
                continue
            seen_sizes.add(key)
            uniq_paths.append(path)

        result.has_photos = bool(uniq_paths)
        caption_by_num = _caption_map_from_bullets(result.caption_bullets)
        if uniq_paths:
            numbers = _assign_photo_numbers(
                len(uniq_paths), caption_by_num, result.photo_refs
            )
            collage = photos_dir / f"{stem}_photos.png"
            result.photo_paths = [
                _collage(
                    uniq_paths,
                    collage,
                    photo_numbers=numbers,
                    captions=caption_by_num,
                    cols=_collage_cols(len(uniq_paths)),
                )
            ]
        else:
            result.photo_paths = []

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
