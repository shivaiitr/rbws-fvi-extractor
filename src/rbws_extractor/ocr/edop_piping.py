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
    pid_no: str | None = None
    anomaly_header: str | None = None
    caption_bullets: list[str] = field(default_factory=list)
    photo_refs: list[PhotoRef] = field(default_factory=list)
    photo_paths: list[Path] = field(default_factory=list)
    iso_path: Path | None = None
    has_photos: bool = False
    source_pdf: Path | None = None


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
    raw = re.sub(r"\s+", "", "".join(parts)).upper()
    if not raw:
        return None
    # Normalize to NGQP-EP-PDPID-####### when possible
    m = re.search(r"(NGQP-EP-)?(PDPID-)?(\d{5,})", raw)
    if m:
        return f"NGQP-EP-PDPID-{m.group(3)}"
    if "QP-PR" in raw:
        m = re.search(r"(QP-PR-?\d+)", raw)
        if m:
            return m.group(1)
    return raw


def parse_summary_rows(doc: fitz.Document) -> list[SummaryRow]:
    """Parse Visual Inspection Observations Checklist using SN vertical bands.

    Line/From/To cells often sit slightly above the S/N number, so words are
    assigned by midpoint bands between consecutive S/N anchors—not by flush-on-SN.
    """
    by_sn: dict[int, dict[str, list[tuple[float, float, str]]]] = {}

    started = False
    for page_index in range(doc.page_count):
        page = doc[page_index]
        text = page.get_text("text") or ""
        if not started:
            if "VISUAL INSPECTION OBSERVATIONS CHECKLIST" not in text.upper():
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
                r"(?i)^page$|EDOP-QP|PROCESS|FULL|VISUAL|INSPECTION|REPORT|QP/FVI",
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


def _extract_photos_from_page(
    doc: fitz.Document,
    page_index: int,
    out_dir: Path,
    stem: str,
) -> list[Path]:
    page = doc[page_index]
    page_h = float(page.rect.height)
    infos = page.get_image_info(xrefs=True)
    infos.sort(key=lambda im: (round(im["bbox"][1], 1), round(im["bbox"][0], 1)))
    paths: list[Path] = []
    seen_xref: set[int] = set()
    for i, im in enumerate(infos):
        w, h = int(im.get("width", 0)), int(im.get("height", 0))
        if w < _MIN_PHOTO_PX_W or h < _MIN_PHOTO_PX_H:
            continue
        bbox = im["bbox"]
        bw = float(bbox[2] - bbox[0])
        bh = float(bbox[3] - bbox[1])
        if bw < _MIN_PAGE_W or bh < _MIN_PAGE_H:
            continue
        # Skip header logos / footer signature stamps
        if bbox[1] < page_h * 0.12:
            continue
        if bbox[3] > page_h * 0.92:
            continue
        xref = im.get("xref")
        if not xref or xref in seen_xref:
            continue
        seen_xref.add(xref)
        try:
            pix = fitz.Pixmap(doc, xref)
            if pix.n >= 5:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        except Exception:  # noqa: BLE001
            continue
        out = out_dir / f"{stem}_p{page_index + 1:02d}_img{i + 1:02d}.png"
        img.save(out, format="PNG", optimize=True)
        paths.append(out)
    return paths


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


def _collage(
    paths: list[Path],
    out: Path,
    *,
    photo_numbers: list[int] | None = None,
    cols: int = 2,
) -> Path:
    """Build a clean labeled contact sheet for the Photos Excel cell."""
    if not paths:
        raise ValueError("no photos for collage")
    nums = list(photo_numbers or [])
    while len(nums) < len(paths):
        nums.append(len(nums) + 1)

    # Prefer 2 columns; use 1 column when only one photo.
    cols = 1 if len(paths) == 1 else min(cols, 2)
    cell_w, img_h, label_h = 420, 300, 28
    tile_h = img_h + label_h
    pad = 12
    font = _label_font(14)
    tiles: list[Image.Image] = []

    for path, num in zip(paths, nums):
        with Image.open(path) as src:
            im = src.convert("RGB")
            im.thumbnail((cell_w - 8, img_h - 8), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (cell_w, tile_h), (255, 255, 255))
        x = (cell_w - im.width) // 2
        y = (img_h - im.height) // 2
        tile.paste(im, (x, y))
        draw = ImageDraw.Draw(tile)
        draw.rectangle([0, 0, cell_w - 1, img_h - 1], outline=(160, 160, 160), width=1)
        draw.rectangle(
            [0, img_h, cell_w - 1, tile_h - 1],
            fill=(245, 245, 245),
            outline=(160, 160, 160),
            width=1,
        )
        label = f"Photo #{int(num):02d}"
        draw.text((8, img_h + 6), label, fill=(30, 30, 30), font=font)
        tiles.append(tile)

    rows = (len(tiles) + cols - 1) // cols
    width = cols * cell_w + (cols + 1) * pad
    height = rows * tile_h + (rows + 1) * pad
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    for idx, tile in enumerate(tiles):
        r, c = divmod(idx, cols)
        canvas.paste(tile, (pad + c * (cell_w + pad), pad + r * (tile_h + pad)))

    # Cap very tall sheets so Excel cells stay reviewable.
    max_h = 1600
    if canvas.height > max_h:
        nw = max(1, int(canvas.width * (max_h / canvas.height)))
        canvas = canvas.resize((nw, max_h), Image.Resampling.LANCZOS)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, format="PNG", optimize=True)
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
        m = re.search(r"(?i)P\s*&\s*ID\s*NUMBER\s*:\s*([^\n]+)", full)
        if m:
            result.pid_no = re.sub(r"\s+", "", m.group(1).strip())

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
        photo_paths: list[Path] = []
        iso_pages: list[int] = []
        for i in range(doc.page_count):
            page_text = doc[i].get_text("text") or ""
            if re.search(r"(?i)drawing\s+not\s+to\s+scale", page_text):
                iso_pages.append(i)
                continue
            photo_paths.extend(_extract_photos_from_page(doc, i, photos_dir, stem))

        uniq: list[Path] = []
        seen_sizes: set[tuple[int, int, int]] = set()
        for p in photo_paths:
            with Image.open(p) as im:
                key = (im.size[0], im.size[1], p.stat().st_size)
            if key in seen_sizes:
                continue
            seen_sizes.add(key)
            uniq.append(p)
        result.has_photos = bool(uniq)
        if uniq:
            collage = photos_dir / f"{stem}_photos.png"
            # Number tiles from photo refs when available, else 1..N
            numbers: list[int] = []
            if result.photo_refs:
                for ref in result.photo_refs:
                    for n in range(ref.start, ref.end + 1):
                        numbers.append(n)
            if len(numbers) < len(uniq):
                numbers = list(range(1, len(uniq) + 1))
            else:
                numbers = numbers[: len(uniq)]
            result.photo_paths = [
                _collage(uniq, collage, photo_numbers=numbers)
            ]
        else:
            result.photo_paths = []

        if iso_pages:
            page_index = iso_pages[-1]
            page = doc[page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
            w, h = im.size
            # Trim header / signature bands; keep the sketch readable.
            crop = im.crop(
                (int(w * 0.05), int(h * 0.08), int(w * 0.95), int(h * 0.86))
            )
            # Soft white border so ISO sits cleanly in the cell
            bordered = Image.new(
                "RGB",
                (crop.width + 16, crop.height + 16),
                (255, 255, 255),
            )
            bordered.paste(crop, (8, 8))
            iso_out = iso_dir / f"{stem}_iso.png"
            bordered.save(iso_out, format="PNG", optimize=True)
            result.iso_path = iso_out
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

    if not summary_rows:
        raise ValueError(
            "Could not read the Visual Inspection Observations Checklist from the "
            "summary PDF. Upload the Process Piping Summary Report (not a line PDF) "
            "in the summary slot."
        )
    if not re.search(r"(?i)checklist|process piping|FVI", sample):
        logger.warning("Summary PDF text looks unusual: %s", summary_pdf.name)

    summary_by_sn = {r.sn: r for r in summary_rows}
    line_pdfs, discover_notes = discover_line_pdfs(line_pdf_dir)
    if not line_pdfs:
        raise ValueError(
            "No line-number PDFs were recognized. Keep names like "
            "`8._AI-001-….pdf` (or `8_AI-001-….pdf`), or ensure each PDF has "
            "`JV Piping List S/N` inside."
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
        pid_no = (summary.pid_no if summary else None) or parsed.pid_no

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
    warnings = [
        f"edop_line_pdfs={len(findings)}",
        f"summary_rows={len(summary_rows)}",
        f"line_descriptions_matched={matched_with_desc}",
        f"rows_with_photos={with_photos}",
        f"rows_no_data={no_data}",
    ]
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
