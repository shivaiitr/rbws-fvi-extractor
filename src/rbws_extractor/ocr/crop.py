"""Professional Photos / ISO attachment with multi-page equipment support."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageOps

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import AssetClass, Finding, PhotoRef
from rbws_extractor.pipeline.rules import compress_photo_refs as _compress_photo_refs

logger = get_logger("ocr.crop")

_SUMMARY_PAGE_MAX = 5

_CAPTION_RE = re.compile(
    r"(?is)Photo\s*#?\s*(\d+)\s*:?\s*(.+?)(?=Photo\s*#|LINE\s*NUMBER|Equipment\s*ID|"
    r"DRAWING\s+NOT\s+TO\s+SCALE|Anonaly\s+Description|Anomaly\s+Description|"
    r"Prepared\s+By|3\.\d\s+Fire|$)"
)


def build_photo_page_index(texts: dict[int, str]) -> dict[int, list[int]]:
    index: dict[int, list[int]] = defaultdict(list)
    for page_no, text in texts.items():
        lower = text.lower()
        is_detail = (
            "equipment id" in lower
            or "description of anomalies" in lower
            or re.search(r"photo\s*#?\s*\d+\s*:?", lower) is not None
            or "hose,reel" in lower.replace(" ", "")
            or "hose reel" in lower
        )
        if page_no <= _SUMMARY_PAGE_MAX and not is_detail:
            continue
        for m in re.finditer(r"(?i)Photo\s*#?\s*(\d+)", text):
            num = int(m.group(1))
            if page_no not in index[num]:
                index[num].append(page_no)
        # Ranges like "Photo #52 to 55" / "Photo #76-77"
        for m in re.finditer(
            r"(?i)Photo\s*#?\s*(\d+)\s*(?:[-–—to&]+|to)\s*(\d+)",
            text,
        ):
            a, b = int(m.group(1)), int(m.group(2))
            if b < a:
                a, b = b, a
            if b - a > 20:
                continue
            for num in range(a, b + 1):
                if page_no not in index[num]:
                    index[num].append(page_no)
    return dict(index)


def _fill_photo_page_gaps(
    photo_index: dict[int, list[int]], nums: list[int]
) -> dict[int, list[int]]:
    """Infer pages for Photo # gaps using neighboring known photo→page mappings."""
    if not nums:
        return photo_index
    known = sorted((n, pages[0]) for n, pages in photo_index.items() if pages)
    if len(known) < 2:
        return photo_index
    enriched = {k: list(v) for k, v in photo_index.items()}
    for num in nums:
        if enriched.get(num):
            continue
        prev = [pair for pair in known if pair[0] < num]
        nxt = [pair for pair in known if pair[0] > num]
        if prev and nxt:
            page = int(round((prev[-1][1] + nxt[0][1]) / 2))
            enriched.setdefault(num, []).append(page)
        elif prev:
            enriched.setdefault(num, []).append(prev[-1][1])
        elif nxt:
            enriched.setdefault(num, []).append(nxt[0][1])
    return enriched


def build_caption_index(texts: dict[int, str]) -> dict[int, str]:
    captions: dict[int, str] = {}
    for page_no, text in texts.items():
        lower = text.lower()
        is_detail = (
            "equipment id" in lower
            or "description of anomalies" in lower
            or re.search(r"photo\s*#?\s*\d+\s*:", lower) is not None
        )
        if page_no <= _SUMMARY_PAGE_MAX and not is_detail:
            continue
        for m in _CAPTION_RE.finditer(text):
            num = int(m.group(1))
            body = re.sub(r"\s+", " ", (m.group(2) or "").strip())
            body = re.sub(
                r"(?i)\b(?:GP/FVI|YA/FVI|Page\s+\d+\s+of\s+\d+|LEGEND|Weld joint|"
                r"Pipe support|Flow direction|Ref\.?\s*Phtoo|Ref\.?\s*Photo).*$",
                "",
                body,
            ).strip(" :-")
            if len(body) < 3:
                continue
            if num not in captions or len(body) > len(captions[num]):
                captions[num] = body
    return captions


def _is_iso_sketch_page(text: str) -> bool:
    lower = text.lower()
    if "drawing not to scale" in lower:
        return True
    # OCR often misses the stamp; LINE NUMBER + sketch legend is enough.
    # Require a real sketch legend token (not the word "anomaly" alone — that
    # appears on observation tables too).
    has_line = re.search(r"(?i)(?:LINE|Jg?INE)\s*NUMBER\s*:", text) is not None
    has_legend = re.search(
        r"(?i)pipe\s*support|weld\s*joint|flow\s*direction|ref\.?\s*photo",
        text,
    ) is not None
    return bool(has_line and has_legend)


def build_iso_page_index(texts: dict[int, str]) -> list[tuple[int, str, str, list[int]]]:
    """
    ISO sketch pages: (page, line_hint, lower_text, ref_photo_nums).

    ref_photo_nums come from 'Ref Photo # 21-24' callouts on the sketch.
    """
    rows: list[tuple[int, str, str, list[int]]] = []
    for page_no, text in texts.items():
        if not _is_iso_sketch_page(text):
            continue
        lower = text.lower()
        line_hint = ""
        m = re.search(r"(?i)(?:LINE|Jg?INE|INE)\s*NUMBER\s*:?\s*(.+)", text)
        if m:
            line_hint = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
        if not line_hint:
            m = re.search(
                r"(?i)((?:Fire\s*-?\s*Water\s+(?:Hose[,\s]*Reel|Monitor)|"
                r"\d+[\"”″']?\s*LINE\s+from[^\n]{0,80}|WF-?[A-Z0-9\"”″'\-]+))",
                text,
            )
            if m:
                line_hint = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
        refs: list[int] = []
        for rm in re.finditer(
            r"(?i)Ref[.,]?\s*Photo\s*[*#]?\s*(\d+)\s*(?:[-–—to&]+|to)\s*(\d+)",
            text,
        ):
            a, b = int(rm.group(1)), int(rm.group(2))
            if b < a:
                a, b = b, a
            if b - a <= 20:
                refs.extend(range(a, b + 1))
        for rm in re.finditer(
            r"(?i)Ref[.,]?\s*Photo\s*[*#]?\s*(\d+)",
            text,
        ):
            refs.append(int(rm.group(1)))
        # Sketch pages also cite evidence as plain Photo # ranges.
        for rm in re.finditer(
            r"(?i)Photo\s*#?\s*(\d+)\s*(?:[-–—&to]+|to|&)\s*(\d+)",
            text,
        ):
            a, b = int(rm.group(1)), int(rm.group(2))
            if b < a:
                a, b = b, a
            if b - a <= 20:
                refs.extend(range(a, b + 1))
        for rm in re.finditer(r"(?i)Photo\s*#?\s*(\d+)", text):
            refs.append(int(rm.group(1)))
        rows.append((page_no, line_hint, lower, sorted(set(refs))))
    return rows


def _photo_numbers(finding: Finding) -> list[int]:
    nums: list[int] = []
    for ref in finding.photo_refs:
        nums.extend(range(ref.start, ref.end + 1))
    for m in re.finditer(
        r"(?i)photo\s*#?\s*(\d+)\s*(?:[-–—&to]+\s*(\d+))?",
        finding.anomaly_description or "",
    ):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        nums.extend(range(a, b + 1))
    return sorted(set(nums))


def _norm_key(text: str) -> str:
    return (
        (text or "")
        .lower()
        .replace(" ", "")
        .replace("”", '"')
        .replace("″", '"')
        .replace("'", "")
        .replace('"', "")
        .replace("\u2019", "")
        .replace("we-", "wf-")
        .replace("wef-", "wf-")
    )


def _layout_bands(
    pdf_path: Path | None,
    page_no: int,
    page_text: str,
    image_size: tuple[int, int],
) -> dict[str, tuple[int, int, int, int] | None]:
    """
    Detect photo vs ISO vertical bands on a mixed page.

    Returns optional boxes for 'photos' and 'iso' in pixel coords of the
    rendered page image.
    """
    w, h = image_size
    has_drawing = "drawing not to scale" in page_text.lower()
    has_photo = bool(re.search(r"(?i)photo\s*#\s*\d+", page_text))

    photo_y_frac: list[float] = []
    iso_y_frac: list[float] = []

    if pdf_path and Path(pdf_path).exists():
        try:
            doc = fitz.open(pdf_path)
            page = doc[page_no - 1]
            page_h = float(page.rect.height) or 1.0
            blocks = page.get_text("dict").get("blocks", [])
            for block in blocks:
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "") for span in line.get("spans", []))
                    if not text.strip():
                        continue
                    y0 = line["bbox"][1] / page_h
                    low = text.lower()
                    if re.search(r"photo\s*#\s*\d+", low):
                        photo_y_frac.append(y0)
                    if "drawing not to scale" in low or re.search(
                        r"line\s*number\s*:", low
                    ):
                        iso_y_frac.append(y0)
                    if re.search(r"ref\.?\s*pht?oo?", low):
                        iso_y_frac.append(y0)
            doc.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("layout detect failed page %s: %s", page_no, exc)

    def clamp_box(y0: float, y1: float) -> tuple[int, int, int, int]:
        top = max(0, int(h * y0))
        bot = min(h, int(h * y1))
        if bot - top < int(h * 0.12):
            bot = min(h, top + int(h * 0.25))
        return (int(w * 0.04), top, int(w * 0.96), bot)

    photos_box = None
    iso_box = None

    if has_photo and has_drawing and photo_y_frac and iso_y_frac:
        photo_mid = sum(photo_y_frac) / len(photo_y_frac)
        iso_mid = sum(iso_y_frac) / len(iso_y_frac)
        if photo_mid < iso_mid:
            # Photos on top, ISO below (classic Jimcel)
            split = (max(photo_y_frac) + min(iso_y_frac)) / 2
            photos_box = clamp_box(0.12, split)
            iso_box = clamp_box(split, 0.93)
        else:
            # ISO on top, photos below (continuation pages)
            split = (max(iso_y_frac) + min(photo_y_frac)) / 2
            iso_box = clamp_box(0.10, split)
            photos_box = clamp_box(split, 0.92)
    elif has_photo and not has_drawing:
        photos_box = clamp_box(0.10, 0.88)
    elif has_drawing and not has_photo:
        iso_box = clamp_box(0.12, 0.92)
    elif has_drawing and has_photo:
        # Fallback heuristic without text positions
        photos_box = clamp_box(0.12, 0.48)
        iso_box = clamp_box(0.48, 0.92)

    return {"photos": photos_box, "iso": iso_box}


def _crop_box(
    image: Image.Image,
    box: tuple[int, int, int, int] | None,
) -> Image.Image:
    if box is None:
        return image.copy()
    return image.crop(box)


def _wrap_text(text: str, max_chars: int = 42) -> list[str]:
    words = (text or "").split()
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
    return lines[:5]  # keep caption block compact


def _caption_font(size: int = 14):
    from PIL import ImageFont

    for path in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _professional_photo_collage(
    photo_paths: list[Path],
    output_path: Path,
    *,
    captions: dict[int, str] | None = None,
    photo_numbers: list[int] | None = None,
    cols: int = 2,
    cell_w: int = 440,
    pad: int = 10,
    bg: tuple[int, int, int] = (255, 255, 255),
) -> Path:
    """
    2-column contact sheet: photo on top, caption text just below each image
    (same as report layout).
    """
    if not photo_paths:
        raise ValueError("no photos")

    captions = captions or {}
    # Align paths with photo numbers when provided
    if photo_numbers is None:
        photo_numbers = []
        for p in photo_paths:
            m = re.search(r"photo_(\d+)", p.name, flags=re.I)
            photo_numbers.append(int(m.group(1)) if m else 0)

    img_h = int(cell_w * 0.78)
    caption_h = 88
    tile_h = img_h + caption_h
    tiles: list[Image.Image] = []
    font = _caption_font(14)

    for path, num in zip(photo_paths, photo_numbers):
        im = Image.open(path).convert("RGB")
        im.thumbnail((cell_w - 4, img_h - 4), Image.Resampling.LANCZOS)
        tile = Image.new("RGB", (cell_w, tile_h), bg)
        x = (cell_w - im.width) // 2
        y = (img_h - im.height) // 2
        tile.paste(im, (x, y))

        draw = ImageDraw.Draw(tile)
        draw.rectangle([0, 0, cell_w - 1, img_h - 1], outline=(170, 170, 170), width=1)

        # Caption just below the image (report-style)
        body = captions.get(num, "")
        body = re.sub(r"\s+", " ", body).strip()
        header = f"Photo #{num:02d}" if num else "Photo"
        if body:
            # Avoid duplicating "Photo # N:" if already in caption body
            body_clean = re.sub(rf"(?i)^photo\s*#?\s*{num}\s*:?\s*", "", body).strip()
            caption = f"{header}: {body_clean}" if body_clean else header
        else:
            caption = header
        lines = _wrap_text(caption, max_chars=max(28, cell_w // 9))
        ty = img_h + 6
        for line in lines:
            draw.text((6, ty), line, fill=(20, 20, 20), font=font)
            ty += 16

        draw.rectangle(
            [0, 0, cell_w - 1, tile_h - 1],
            outline=(150, 150, 150),
            width=1,
        )
        tiles.append(tile)

    rows = (len(tiles) + cols - 1) // cols
    width = cols * cell_w + (cols + 1) * pad
    height = rows * tile_h + (rows + 1) * pad
    sheet = Image.new("RGB", (width, height), bg)
    for i, tile in enumerate(tiles):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = pad + r * (tile_h + pad)
        sheet.paste(tile, (x, y))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    max_h = 2400
    if sheet.height > max_h:
        nw = int(sheet.width * (max_h / sheet.height))
        sheet = sheet.resize((max(1, nw), max_h), Image.Resampling.LANCZOS)
    sheet.save(output_path, format="PNG", optimize=True)
    return output_path


def _stitch_vertical_professional(
    paths: list[Path],
    output_path: Path,
    *,
    max_h: int = 1600,
) -> Path:
    """Create a compact, readable evidence montage instead of one excessively tall strip.

    When multiple evidence pages belong to the same record, preserve the original
    photo arrangement in a landscape-friendly grid. This keeps the Images area
    readable and avoids the usual 900x2400 and 1500x3000 style crops that are
    hard to review in Excel.
    """
    if not paths:
        raise ValueError("no images to stitch")

    images = [Image.open(p).convert("RGB") for p in paths]
    try:
        gap = 12
        cols = min(2, len(images)) if len(images) > 1 else 1
        rows = (len(images) + cols - 1) // cols

        max_w = max(im.width for im in images)
        max_h_per = max(im.height for im in images)
        cell_w = max(480, max_w)
        cell_h = max(360, max_h_per)

        width = cols * cell_w + gap * (cols + 1)
        height = rows * cell_h + gap * (rows + 1)

        scale = 1.0
        if height > max_h:
            scale = min(scale, max_h / height)
        if width > max_h * 1.8:
            scale = min(scale, (max_h * 1.8) / width)

        if scale < 1.0:
            width = max(1, int(width * scale))
            height = max(1, int(height * scale))
            cell_w = max(1, int(cell_w * scale))
            cell_h = max(1, int(cell_h * scale))

        canvas = Image.new("RGB", (width, height), (255, 255, 255))

        for idx, im in enumerate(images):
            r, c = divmod(idx, cols)
            x0 = gap + c * (cell_w + gap)
            y0 = gap + r * (cell_h + gap)

            target_w = cell_w
            target_h = cell_h
            if im.width != target_w or im.height != target_h:
                im = im.resize((target_w, target_h), Image.Resampling.LANCZOS)
            im = ImageOps.expand(im, border=1, fill=(200, 200, 200))
            canvas.paste(im, (x0, y0))

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, format="PNG", optimize=True)
    finally:
        for im in images:
            im.close()
    return output_path


def _match_iso_pages(
    finding: Finding,
    iso_index: list[tuple[int, str, str, list[int]]],
) -> list[int]:
    """
    All ISO sketch pages for this equipment.

    Match by line number and/or Ref Photo # overlap. Multi-page sketches
    are all returned. Asset classes without ISO stay blank.
    """
    # Gold leaves ISO blank for cylinders / pump / diesel only.
    if finding.asset_class in {
        AssetClass.FIRE_PROTECTION_CYLINDER,
        AssetClass.FIRE_WATER_PUMP_CAISSON,
        AssetClass.DIESEL_STORAGE_TANK,
    }:
        return []

    line_compact = _norm_key(finding.line_number or "")
    desc_compact = _norm_key(finding.line_description or "")
    photo_nums = set(_photo_numbers(finding))
    if (not line_compact or line_compact in {"notagfound"}) and not photo_nums:
        return []

    scored: list[tuple[int, int]] = []
    for page_no, line_hint, lower, ref_photos in iso_index:
        # iso_index is already sketch-filtered; do not require the OCR stamp text.
        # Skip early TOC / observation-table false positives (no sketch legend).
        if page_no <= _SUMMARY_PAGE_MAX and "drawing not to scale" not in lower:
            continue
        hint = _norm_key(line_hint).replace("wef-", "wf-").replace("we-", "wf-")
        score = 0
        wf_parts = re.findall(r"wf-\d+", line_compact)
        if wf_parts and any(part in hint for part in wf_parts):
            score += 6
        if line_compact[:14] and line_compact[:14] in hint and line_compact not in {
            "notagfound"
        }:
            score += 5
        # Ref Photo callouts linking sketch → this finding's photos
        overlap = photo_nums.intersection(ref_photos)
        if overlap:
            score += 3 + min(3, len(overlap) // 2)
            # Prefer real sketch pages over table pages that only mention Photo #.
            if "drawing not to scale" in lower or "pipe support" in lower or "weld" in lower:
                score += 2
        if finding.asset_class == AssetClass.UNTAGGED_LINE:
            if line_compact.startswith("6") and ("6" in hint or "6" in lower):
                score += 4
            if line_compact.startswith("8") and ("8" in hint or "8" in lower):
                score += 4
        # Hose / monitor: Ref Photo overlap + location keywords on sketch page.
        if finding.asset_class in {
            AssetClass.FIRE_WATER_HOSE_REEL,
            AssetClass.FIRE_WATER_MONITOR,
        }:
            if overlap:
                score += 2
            loc_hits = 0
            hay = (hint + lower).replace("-", "").replace(" ", "").replace(",", "")
            for token in (
                "southeast",
                "southwest",
                "northeast",
                "northwest",
                "cellar",
                "celler",
                "maindeck",
                "hosereel",
                "hose",
                "monitor",
            ):
                if token in desc_compact and token in hay:
                    loc_hits += 1
            score += min(4, loc_hits)
        if score >= 3:
            scored.append((score, page_no))

    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1]))
    # Keep all pages within 2 of best score (multi-page ISO for one line)
    top = scored[0][0]
    return sorted({p for s, p in scored if s >= top - 2})


def _photo_extracts_usable(photo_map: dict[int, Path], nums: list[int]) -> bool:
    """Reject scanned-PDF strip/header extracts that are not real photos."""
    paths = [photo_map[n] for n in nums if n in photo_map]
    if not paths:
        return False
    ok = 0
    for path in paths:
        try:
            with Image.open(path) as im:
                w, h = im.size
                if w < 160 or h < 120:
                    continue
                aspect = w / max(h, 1)
                # Real inspection photos are roughly square/landscape; scanned
                # page tiles are often very wide strips (~1275x205).
                if aspect > 2.8 or aspect < 0.35:
                    continue
                sample = im.convert("L").resize((48, 48))
                pixels = list(sample.getdata())
                mean = sum(pixels) / len(pixels)
                var = sum((p - mean) ** 2 for p in pixels) / len(pixels)
                if mean > 245 and var < 400:
                    continue
                if var < 250:
                    continue
                ok += 1
        except Exception:  # noqa: BLE001
            continue
    return ok >= max(1, (len(paths) + 1) // 2)


def _build_page_photo_crops(
    *,
    nums: list[int],
    photo_index: dict[int, list[int]],
    pages_dir: Path,
    texts: dict[int, str],
    pdf_path: Path | None,
    photos_dir: Path,
    sn: int,
) -> list[Path]:
    """Gold-style Photos: crop Equipment-ID + photo bands from rendered pages."""
    page_nos = sorted({p for n in nums for p in photo_index.get(n, [])})
    crops: list[Path] = []
    for page_no in page_nos:
        src = pages_dir / f"page_{page_no:03d}.png"
        if not src.exists():
            continue
        with Image.open(src) as img:
            bands = _layout_bands(
                pdf_path, page_no, texts.get(page_no, ""), img.size
            )
            box = bands.get("photos")
            if box is None:
                # Fallback: keep header + photo body, drop footer noise.
                w, h = img.size
                box = (int(w * 0.03), int(h * 0.08), int(w * 0.97), int(h * 0.92))
            cropped = _crop_box(img, box).convert("RGB")
            tmp = photos_dir / f"sn_{sn:03d}_p{page_no:03d}.png"
            cropped.save(tmp, format="PNG", optimize=True)
            crops.append(tmp)
    return crops


def _finalize_photo_sheet(crops: list[Path], out: Path) -> Path:
    """Compose one Excel Photos cell image from one or more page crops."""
    if len(crops) == 1:
        # Normalize to a landscape-friendly max size like gold media.
        with Image.open(crops[0]) as im:
            im = im.convert("RGB")
            max_w, max_h = 1400, 900
            scale = min(max_w / im.width, max_h / im.height, 1.0)
            if scale < 1.0:
                im = im.resize(
                    (max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            im.save(out, format="PNG", optimize=True)
        return out
    _stitch_vertical_professional(crops, out, max_h=1200)
    return out


def enrich_anomaly_from_captions(
    findings: list[Finding],
    texts: dict[int, str],
) -> list[Finding]:
    captions = build_caption_index(texts)
    logger.info("Caption index: %s photo captions", len(captions))
    for finding in findings:
        nums = _photo_numbers(finding)
        if not nums:
            continue

        range_text = None
        if finding.photo_refs:
            range_text = ", ".join(ref.format_excel() for ref in finding.photo_refs)

        # Respect the structured visual-table anomaly as the source of truth.
        # Only use captions to supplement missing detail when the table text is short
        # or when only a photo range is present without a complete narrative.
        if finding.anomaly_description and "photo #" in finding.anomaly_description.lower():
            # Preserve the visual inspection wording and just ensure the photo range stays
            # consolidated as a single range reference.
            if finding.photo_refs:
                compact = " ".join(ref.format_excel() for ref in finding.photo_refs)
                if "photo #" not in finding.anomaly_description.lower():
                    finding.anomaly_description = f"{finding.anomaly_description.strip()} {compact}".strip()
            continue

        if finding.anomaly_description and len(finding.anomaly_description.strip()) > 40:
            # Already a meaningful structured anomaly; captions are only for verification.
            continue

        lines: list[str] = []
        used: list[int] = []
        for n in nums:
            body = captions.get(n)
            if not body:
                continue
            used.append(n)
            if re.search(rf"(?i)photo\s*#?\s*{n}\b", body):
                lines.append(f"-{body}")
            else:
                lines.append(f"-{body} (Photo #{n:02d})")

        if lines:
            candidate = "\n".join(lines)
            if finding.anomaly_description and finding.anomaly_description.strip().lower() not in {"n/a", "na"}:
                finding.anomaly_description = finding.anomaly_description.strip()
            else:
                finding.anomaly_description = candidate.lstrip("-")

            if finding.photo_refs:
                finding.photo_refs = _compress_photo_refs(finding.photo_refs)
            elif used:
                finding.photo_refs = _compress_photo_refs(
                    [PhotoRef(raw=f"{n:02d}", start=n, end=n) for n in used]
                )
            finding.source.notes = (
                (finding.source.notes or "") + " | anomaly_from_photo_captions"
            ).strip(" |")
    return findings


def attach_images_from_ocr(
    findings: list[Finding],
    *,
    texts: dict[int, str],
    pages_dir: Path,
    output_dir: Path,
    skip_images: bool = False,
    pdf_path: Path | None = None,
) -> list[Finding]:
    """
    Principal-engineer rules:
    - Photos = all anomaly/equipment photos for the finding (multi-page OK)
    - ISO = all matching line sketches (multi-page OK); blank if none
    - Never mix photos into ISO or vice versa
    - Professional crop + paste collage
    """
    if skip_images:
        logger.info("Skipping image crops (--skip-images)")
        return findings

    pages_dir = Path(pages_dir)
    photos_dir = Path(output_dir) / "photos"
    iso_dir = Path(output_dir) / "iso"
    singles_dir = Path(output_dir) / "photo_singles"
    photos_dir.mkdir(parents=True, exist_ok=True)
    iso_dir.mkdir(parents=True, exist_ok=True)

    photo_map: dict[int, Path] = {}
    stitch_fn = None
    if pdf_path is not None:
        try:
            from rbws_extractor.ocr.pdf_photos import extract_photo_map

            photo_map = extract_photo_map(Path(pdf_path), singles_dir, texts=texts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("PDF photo extract failed: %s", exc)

    photo_index = build_photo_page_index(texts)
    iso_index = build_iso_page_index(texts)
    captions = build_caption_index(texts)
    logger.info(
        "Image index: photos=%s singles=%s iso_pages=%s captions=%s",
        len(photo_index),
        len(photo_map),
        len(iso_index),
        len(captions),
    )

    for finding in findings:
        sn = finding.sn or 0
        nums = _photo_numbers(finding)
        finding.photos_image_path = None
        finding.iso_image_path = None
        local_photo_index = _fill_photo_page_gaps(photo_index, nums)

        # ---- Photos (gold-style page crops; collage only for real embeds) ----
        single_paths = [photo_map[n] for n in nums if n in photo_map]
        used_nums = [n for n in nums if n in photo_map]
        out = photos_dir / f"sn_{sn:03d}_photos.png"
        use_singles = bool(single_paths) and _photo_extracts_usable(photo_map, nums)
        if use_singles:
            try:
                _professional_photo_collage(
                    single_paths,
                    out,
                    captions=captions,
                    photo_numbers=used_nums,
                    cols=2,
                    cell_w=440,
                )
                finding.photos_image_path = out
            except Exception as exc:  # noqa: BLE001
                logger.warning("Photo collage failed SN=%s: %s", sn, exc)
                finding.photos_image_path = single_paths[0]
        else:
            # Scanned Jimcel PDFs: crop Equipment ID + photo bands from page renders
            # (same visual structure as the gold workbook Photos column).
            if single_paths and not use_singles:
                logger.info(
                    "SN=%s: ignoring unusable PDF photo extracts; using page crops",
                    sn,
                )
            crops = _build_page_photo_crops(
                nums=nums,
                photo_index=local_photo_index,
                pages_dir=pages_dir,
                texts=texts,
                pdf_path=pdf_path,
                photos_dir=photos_dir,
                sn=sn,
            )
            if crops:
                try:
                    finding.photos_image_path = _finalize_photo_sheet(crops, out)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Multi-page photo stitch failed SN=%s: %s", sn, exc)
                    finding.photos_image_path = crops[0]

        # ---- ISO (all matching sketch pages; blank if none) ----
        iso_pages = _match_iso_pages(finding, iso_index)
        if not iso_pages:
            continue

        iso_crops: list[Path] = []
        for iso_page in iso_pages:
            page_text = texts.get(iso_page, "")
            if "drawing not to scale" not in page_text.lower():
                continue
            src = pages_dir / f"page_{iso_page:03d}.png"
            if not src.exists():
                continue
            with Image.open(src) as img:
                bands = _layout_bands(pdf_path, iso_page, page_text, img.size)
                box = bands.get("iso")
                if box is None:
                    # full-page sketch
                    w, h = img.size
                    box = (int(w * 0.04), int(h * 0.12), int(w * 0.96), int(h * 0.92))
                cropped = _crop_box(img, box)
                tmp = iso_dir / f"sn_{sn:03d}_iso_p{iso_page:03d}.png"
                cropped.convert("RGB").save(tmp, format="PNG", optimize=True)
                iso_crops.append(tmp)

        if not iso_crops:
            finding.iso_image_path = None
        elif len(iso_crops) == 1:
            # normalize name
            final = iso_dir / f"sn_{sn:03d}_iso.png"
            Image.open(iso_crops[0]).convert("RGB").save(final, format="PNG", optimize=True)
            finding.iso_image_path = final
        else:
            final = iso_dir / f"sn_{sn:03d}_iso.png"
            try:
                _stitch_vertical_professional(iso_crops, final, max_h=1400)
                finding.iso_image_path = final
            except Exception as exc:  # noqa: BLE001
                logger.warning("Multi-page ISO stitch failed SN=%s: %s", sn, exc)
                finding.iso_image_path = iso_crops[0]

    logger.info(
        "Attached images: photos=%s/%s iso=%s/%s",
        sum(1 for f in findings if f.photos_image_path),
        len(findings),
        sum(1 for f in findings if f.iso_image_path),
        len(findings),
    )
    return findings
