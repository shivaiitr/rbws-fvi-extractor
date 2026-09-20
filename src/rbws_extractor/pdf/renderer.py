"""PDF page rendering with PyMuPDF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import fitz
from tqdm import tqdm

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import ContentMode
from rbws_extractor.pdf.text_layer import PageTextInfo, inspect_pdf_text, page_text_map

logger = get_logger("pdf.renderer")


@dataclass(frozen=True)
class RenderedPage:
    page_number: int  # 1-based
    image_path: Path
    width: int
    height: int
    native_text: str = ""
    content_mode: ContentMode = ContentMode.IMAGE
    char_count: int = 0
    image_count: int = 0


class PdfRenderer:
    """Rasterize PDF pages and attach native text-layer metadata."""

    def __init__(self, dpi: int = 200) -> None:
        if dpi < 72:
            raise ValueError("dpi must be >= 72")
        self.dpi = dpi
        self.zoom = dpi / 72.0

    def render(
        self,
        pdf_path: Path,
        output_dir: Path,
        max_pages: int = 0,
        show_progress: bool = True,
        *,
        min_text_chars: int = 80,
        min_text_words: int = 12,
    ) -> list[RenderedPage]:
        pdf_path = Path(pdf_path)
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        profile = inspect_pdf_text(
            pdf_path,
            max_pages=max_pages,
            min_chars=min_text_chars,
            min_words=min_text_words,
        )
        text_by_page = page_text_map(profile)

        doc = fitz.open(pdf_path)
        try:
            total = len(doc)
            limit = total if max_pages <= 0 else min(total, max_pages)
            logger.info(
                "Rendering %s pages from %s at %s DPI (document_mode=%s)",
                limit,
                pdf_path.name,
                self.dpi,
                profile.document_mode.value,
            )

            pages: list[RenderedPage] = []
            iterator: Iterator[int] = range(limit)
            if show_progress:
                iterator = tqdm(iterator, total=limit, desc="Render PDF pages")

            matrix = fitz.Matrix(self.zoom, self.zoom)
            for i in iterator:
                page = doc[i]
                pix = page.get_pixmap(matrix=matrix, alpha=False)
                out_path = output_dir / f"page_{i + 1:03d}.png"
                pix.save(str(out_path))

                info: PageTextInfo | None = text_by_page.get(i + 1)
                pages.append(
                    RenderedPage(
                        page_number=i + 1,
                        image_path=out_path,
                        width=pix.width,
                        height=pix.height,
                        native_text=info.text if info else "",
                        content_mode=info.content_mode if info else ContentMode.IMAGE,
                        char_count=info.char_count if info else 0,
                        image_count=info.image_count if info else 0,
                    )
                )
            return pages
        finally:
            doc.close()

    def page_count(self, pdf_path: Path) -> int:
        doc = fitz.open(pdf_path)
        try:
            return len(doc)
        finally:
            doc.close()