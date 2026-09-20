"""Extract individual inspection photos embedded in the FVI PDF."""

from __future__ import annotations

import io
import re
from collections import defaultdict
from pathlib import Path

import fitz
from PIL import Image

from rbws_extractor.logging_config import get_logger

logger = get_logger("ocr.pdf_photos")

# Logos / headers are small; real inspection photos are larger.
_MIN_W = 180
_MIN_H = 140


def _caption_photo_nums_on_page(text: str) -> list[int]:
    """Ordered photo numbers that have captions on this page."""
    nums: list[int] = []
    for m in re.finditer(r"(?i)Photo\s*#?\s*(\d+)\s*:", text):
        n = int(m.group(1))
        if n not in nums:
            nums.append(n)
    if nums:
        return nums
    # Fallback: any Photo # mention in reading order
    for m in re.finditer(r"(?i)Photo\s*#?\s*(\d+)", text):
        n = int(m.group(1))
        if n not in nums:
            nums.append(n)
    return nums


def extract_photo_map(
    pdf_path: Path,
    output_dir: Path,
    texts: dict[int, str] | None = None,
) -> dict[int, Path]:
    """
    Extract embedded raster photos from the PDF.

    Returns map: photo_number -> PNG path.
    Photos on a page are matched to Photo # captions in reading order
    (top-to-bottom, left-to-right).
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    photo_map: dict[int, Path] = {}
    try:
        for page_index in range(doc.page_count):
            page_no = page_index + 1
            page = doc[page_index]
            text = (texts or {}).get(page_no) or (page.get_text("text") or "")
            caption_nums = _caption_photo_nums_on_page(text)

            infos = [
                im
                for im in page.get_image_info(xrefs=True)
                if im.get("width", 0) >= _MIN_W and im.get("height", 0) >= _MIN_H
            ]
            # Reading order: top then left
            infos.sort(key=lambda im: (round(im["bbox"][1], 1), round(im["bbox"][0], 1)))

            if not infos:
                continue

            # If caption count mismatches image count, still export in order using
            # available caption numbers; orphan images get sequential gaps filled later.
            for i, im in enumerate(infos):
                xref = im.get("xref")
                if not xref:
                    continue
                try:
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n >= 5:  # CMYK
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Skip xref %s on page %s: %s", xref, page_no, exc)
                    continue

                if i < len(caption_nums):
                    photo_no = caption_nums[i]
                else:
                    # No caption pairing — skip rather than mis-label
                    out = output_dir / f"page{page_no:03d}_img{i+1:02d}.png"
                    img.save(out, format="PNG", optimize=True)
                    logger.debug("Unlabeled photo saved %s", out.name)
                    continue

                out = output_dir / f"photo_{photo_no:03d}.png"
                img.save(out, format="PNG", optimize=True)
                photo_map[photo_no] = out
    finally:
        doc.close()

    logger.info(
        "Extracted %s individual photos from PDF → %s",
        len(photo_map),
        output_dir,
    )
    return photo_map


def stitch_photos_grid(
    photo_paths: list[Path],
    output_path: Path,
    *,
    cols: int = 2,
    cell_w: int = 480,
) -> Path:
    """Stitch individual photos into a labeled 2-column contact sheet."""
    if not photo_paths:
        raise ValueError("no photos to stitch")
    images = []
    for p in photo_paths:
        im = Image.open(p).convert("RGB")
        # Fit width
        if im.width != cell_w:
            nh = int(im.height * (cell_w / im.width))
            im = im.resize((cell_w, nh), Image.Resampling.LANCZOS)
        images.append(im)

    rows = (len(images) + cols - 1) // cols
    # Use max height per row for alignment
    row_heights: list[int] = []
    for r in range(rows):
        chunk = images[r * cols : (r + 1) * cols]
        row_heights.append(max(im.height for im in chunk))

    gap = 8
    canvas_w = cols * cell_w + (cols + 1) * gap
    canvas_h = sum(row_heights) + (rows + 1) * gap
    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))

    y = gap
    for r in range(rows):
        x = gap
        chunk = images[r * cols : (r + 1) * cols]
        for im in chunk:
            canvas.paste(im, (x, y))
            x += cell_w + gap
        y += row_heights[r] + gap

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    for im in images:
        im.close()
    return output_path
