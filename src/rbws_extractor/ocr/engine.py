"""Tesseract OCR over rendered PDF pages with on-disk cache."""

from __future__ import annotations

from pathlib import Path

import pytesseract
from PIL import Image
from tqdm import tqdm

from rbws_extractor.logging_config import get_logger
from rbws_extractor.pdf.renderer import PdfRenderer, RenderedPage

logger = get_logger("ocr.engine")


class OcrEngine:
    """Render PDF pages (or reuse renders) and OCR them to text."""

    def __init__(self, dpi: int = 200) -> None:
        self.renderer = PdfRenderer(dpi=dpi)

    def ensure_pages(
        self,
        pdf_path: Path,
        pages_dir: Path,
        max_pages: int = 0,
    ) -> list[RenderedPage]:
        pages_dir = Path(pages_dir)
        existing = sorted(pages_dir.glob("page_*.png")) if pages_dir.exists() else []
        if existing and max_pages == 0:
            logger.info("Reusing %s existing page renders in %s", len(existing), pages_dir)
            from rbws_extractor.pdf.text_layer import ContentMode
            from PIL import Image as PILImage

            pages: list[RenderedPage] = []
            for i, path in enumerate(existing, start=1):
                with PILImage.open(path) as img:
                    width, height = img.size
                pages.append(
                    RenderedPage(
                        page_number=i,
                        image_path=path,
                        width=width,
                        height=height,
                        content_mode=ContentMode.IMAGE,
                        char_count=0,
                        image_count=1,
                    )
                )
            return pages
        return self.renderer.render(pdf_path, pages_dir, max_pages=max_pages)

    def extract_native_text(
        self,
        pdf_path: Path,
        text_dir: Path,
        max_pages: int = 0,
        min_chars_per_page: int = 80,
    ) -> dict[int, str] | None:
        """
        Prefer native PDF text when the document has a usable text layer.
        Returns None if too few pages have enough text (caller should OCR).
        """
        import fitz

        pdf_path = Path(pdf_path)
        text_dir = Path(text_dir)
        text_dir.mkdir(parents=True, exist_ok=True)
        doc = fitz.open(pdf_path)
        try:
            limit = doc.page_count if max_pages <= 0 else min(max_pages, doc.page_count)
            texts: dict[int, str] = {}
            rich = 0
            for i in range(limit):
                text = doc[i].get_text("text") or ""
                texts[i + 1] = text
                (text_dir / f"page_{i+1:03d}.txt").write_text(text, encoding="utf-8")
                if len(text.strip()) >= min_chars_per_page:
                    rich += 1
            if rich < max(1, limit // 3):
                logger.info(
                    "Native text weak (%s/%s rich pages); will fall back to OCR",
                    rich,
                    limit,
                )
                return None
            logger.info(
                "Using native PDF text for %s pages (%s rich) → %s",
                limit,
                rich,
                text_dir,
            )
            return texts
        finally:
            doc.close()

    def ocr_pages(
        self,
        rendered: list[RenderedPage],
        text_dir: Path,
        *,
        force: bool = False,
    ) -> dict[int, str]:
        text_dir = Path(text_dir)
        text_dir.mkdir(parents=True, exist_ok=True)
        texts: dict[int, str] = {}

        for page in tqdm(rendered, desc="OCR pages", unit="page"):
            cache = text_dir / f"page_{page.page_number:03d}.txt"
            if cache.exists() and not force:
                texts[page.page_number] = cache.read_text(encoding="utf-8")
                continue
            image = Image.open(page.image_path)
            # PSM 6: assume a single uniform block of text — good for report pages.
            text = pytesseract.image_to_string(image, config="--psm 6")
            cache.write_text(text, encoding="utf-8")
            texts[page.page_number] = text

        logger.info("OCR complete for %s pages → %s", len(texts), text_dir)
        return texts
