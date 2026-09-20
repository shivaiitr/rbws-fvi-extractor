"""Crop Photos and ISO regions from classified report pages."""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import AssetClass, ClassifiedPage, Finding, PageType

logger = get_logger("pipeline.crop")


def _page_for_photos(
    finding: Finding,
    pages: list[ClassifiedPage],
) -> ClassifiedPage | None:
    preferred_types = {
        PageType.EQUIPMENT_PHOTO_BLOCK,
        PageType.ISO_SKETCH,
        PageType.UNTAGGED_LINES,
    }
    source_pages = set(finding.source.pages)
    if source_pages:
        for page in pages:
            if page.page_number in source_pages and page.page_type in preferred_types:
                return page
        for page in pages:
            if page.page_number in source_pages:
                return page

    needle = (finding.source.equipment_id or finding.line_number or "").lower()
    if needle:
        for page in pages:
            preview = (page.text_preview or "").lower()
            if needle[:20] in preview:
                return page
    return None


def _page_for_iso(
    finding: Finding,
    pages: list[ClassifiedPage],
) -> ClassifiedPage | None:
    if finding.asset_class in {
        AssetClass.FIRE_PROTECTION_CYLINDER,
        AssetClass.FIRE_WATER_PUMP_CAISSON,
        AssetClass.DIESEL_STORAGE_TANK,
    }:
        return None

    line_key = (finding.line_number or "").lower()
    for page in pages:
        if page.page_type not in {PageType.ISO_SKETCH, PageType.EQUIPMENT_PHOTO_BLOCK}:
            continue
        preview = (page.text_preview or "").lower()
        if "drawing not to scale" in preview or "line number" in preview:
            if line_key and line_key.split()[0] in preview:
                return page
            if finding.pid_no and finding.pid_no.lower() in preview:
                return page

    page = _page_for_photos(finding, pages)
    if page and page.page_type in {PageType.EQUIPMENT_PHOTO_BLOCK, PageType.ISO_SKETCH}:
        return page
    return None


def _crop_box(image: Image.Image, kind: str) -> tuple[int, int, int, int]:
    w, h = image.size
    if kind == "photos":
        return (int(w * 0.03), int(h * 0.12), int(w * 0.97), int(h * 0.78))
    if kind == "iso":
        return (int(w * 0.05), int(h * 0.55), int(w * 0.95), int(h * 0.92))
    raise ValueError(f"Unknown crop kind: {kind}")


def crop_region(image_path: Path, output_path: Path, kind: str) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as img:
        cropped = img.crop(_crop_box(img, kind))
        cropped.save(output_path, format="PNG")
    return output_path


def attach_images(
    findings: list[Finding],
    pages: list[ClassifiedPage],
    output_dir: Path,
    skip_images: bool = False,
) -> list[Finding]:
    if skip_images:
        logger.info("Skipping image crops (--skip-images)")
        return findings

    photos_dir = output_dir / "photos"
    iso_dir = output_dir / "iso"
    photos_dir.mkdir(parents=True, exist_ok=True)
    iso_dir.mkdir(parents=True, exist_ok=True)

    for finding in findings:
        sn = finding.sn or 0
        photo_page = _page_for_photos(finding, pages)
        if photo_page is not None:
            out = photos_dir / f"sn_{sn:03d}_photos.png"
            try:
                crop_region(photo_page.image_path, out, "photos")
                finding.photos_image_path = out
            except Exception as exc:  # noqa: BLE001
                logger.warning("Photo crop failed for SN=%s: %s", sn, exc)
                finding.needs_review = True
                finding.review_reasons.append(f"photo_crop_failed: {exc}")
        else:
            finding.needs_review = True
            finding.review_reasons.append("photo_page_not_found")

        iso_page = _page_for_iso(finding, pages)
        if iso_page is None:
            continue

        preview = (iso_page.text_preview or "").lower()
        likely_iso = (
            "drawing not to scale" in preview
            or "line number" in preview
            or finding.asset_class
            in {
                AssetClass.FIRE_WATER_PIPING,
                AssetClass.FIRE_WATER_HOSE_REEL,
                AssetClass.FIRE_WATER_MONITOR,
                AssetClass.UNTAGGED_LINE,
            }
        )
        if not likely_iso:
            continue

        out = iso_dir / f"sn_{sn:03d}_iso.png"
        try:
            crop_region(iso_page.image_path, out, "iso")
            finding.iso_image_path = out
        except Exception as exc:  # noqa: BLE001
            logger.warning("ISO crop failed for SN=%s: %s", sn, exc)

    return findings


def sanitize_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80]