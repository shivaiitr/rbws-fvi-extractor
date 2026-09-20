"""Compatibility wrapper for OCR-only input preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from rbws_extractor.models.findings import ClassifiedPage, ContentMode


def extraction_inputs_for_pages(
    pages: Iterable[ClassifiedPage],
    force_mode: str = "auto",
) -> tuple[list[Path], str]:
    """Return image paths and text payload for OCR-style processing.

    This is intentionally lightweight and keeps tests and older import sites
    working without reintroducing the removed LLM path.
    """
    image_paths: list[Path] = []
    text_chunks: list[str] = []

    for page in pages:
        content_mode = getattr(page, "content_mode", ContentMode.TEXT)
        text = page.native_text or page.text_preview or ""
        if text:
            text_chunks.append(text)

        if force_mode == "text":
            continue
        if force_mode == "vision":
            p = Path(page.image_path)
            if p.exists():
                image_paths.append(p)
            continue

        if content_mode in {ContentMode.IMAGE, ContentMode.HYBRID}:
            p = Path(page.image_path)
            if p.exists():
                image_paths.append(p)

    return image_paths, "\n".join(text_chunks)
