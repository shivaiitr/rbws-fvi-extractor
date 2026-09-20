"""Detect and extract native PDF text layers (vs image-only pages)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import fitz

from rbws_extractor.logging_config import get_logger

logger = get_logger("pdf.text_layer")


class ContentMode(str, Enum):
    """How a page/document should be read."""

    TEXT = "text"  # usable native text layer
    IMAGE = "image"  # scanned / print-to-PDF / no text
    HYBRID = "hybrid"  # some text + important visual content


@dataclass(frozen=True)
class PageTextInfo:
    page_number: int  # 1-based
    text: str
    char_count: int
    word_count: int
    image_count: int
    content_mode: ContentMode


@dataclass(frozen=True)
class DocumentTextProfile:
    pdf_path: Path
    pages: list[PageTextInfo]
    document_mode: ContentMode
    text_page_count: int
    image_page_count: int
    hybrid_page_count: int


_WORD_RE = re.compile(r"[A-Za-z0-9]{2,}")


def _normalize_text(text: str) -> str:
    return re.sub(r"[ \t]+", " ", (text or "").replace("\r", "")).strip()


def classify_page_content(
    text: str,
    image_count: int,
    *,
    min_chars: int = 80,
    min_words: int = 12,
) -> ContentMode:
    """
    Decide extraction mode for one page.

    - IMAGE: almost no extractable text (typical scanned FVI reports)
    - TEXT: enough native text for structured extraction without vision
    - HYBRID: some text exists but page also has many images (photo blocks)
    """
    cleaned = _normalize_text(text)
    words = _WORD_RE.findall(cleaned)
    char_count = len(cleaned)
    word_count = len(words)

    has_text = char_count >= min_chars and word_count >= min_words
    if not has_text:
        return ContentMode.IMAGE

    # Photo / ISO pages often still have captions in a text layer.
    if image_count >= 4:
        return ContentMode.HYBRID
    return ContentMode.TEXT


def inspect_pdf_text(
    pdf_path: Path,
    max_pages: int = 0,
    *,
    min_chars: int = 80,
    min_words: int = 12,
) -> DocumentTextProfile:
    """Inspect every page for native text vs image-only content."""
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    try:
        total = len(doc)
        limit = total if max_pages <= 0 else min(total, max_pages)
        pages: list[PageTextInfo] = []

        for i in range(limit):
            page = doc[i]
            text = _normalize_text(page.get_text("text"))
            images = page.get_images(full=True)
            mode = classify_page_content(
                text,
                len(images),
                min_chars=min_chars,
                min_words=min_words,
            )
            words = _WORD_RE.findall(text)
            pages.append(
                PageTextInfo(
                    page_number=i + 1,
                    text=text,
                    char_count=len(text),
                    word_count=len(words),
                    image_count=len(images),
                    content_mode=mode,
                )
            )

        text_n = sum(1 for p in pages if p.content_mode == ContentMode.TEXT)
        image_n = sum(1 for p in pages if p.content_mode == ContentMode.IMAGE)
        hybrid_n = sum(1 for p in pages if p.content_mode == ContentMode.HYBRID)

        if image_n == len(pages):
            doc_mode = ContentMode.IMAGE
        elif text_n == len(pages):
            doc_mode = ContentMode.TEXT
        else:
            doc_mode = ContentMode.HYBRID

        profile = DocumentTextProfile(
            pdf_path=pdf_path,
            pages=pages,
            document_mode=doc_mode,
            text_page_count=text_n,
            image_page_count=image_n,
            hybrid_page_count=hybrid_n,
        )
        logger.info(
            "PDF text profile %s: mode=%s text_pages=%s image_pages=%s hybrid_pages=%s",
            pdf_path.name,
            doc_mode.value,
            text_n,
            image_n,
            hybrid_n,
        )
        return profile
    finally:
        doc.close()


def page_text_map(profile: DocumentTextProfile) -> dict[int, PageTextInfo]:
    return {p.page_number: p for p in profile.pages}