"""PDF renderer smoke test."""

from pathlib import Path

import fitz

from rbws_extractor.pdf.renderer import PdfRenderer


def test_renderer_creates_pngs(tmp_path: Path):
    pdf_path = tmp_path / "tiny.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((50, 50), "UBIT-GP FVI TEST")
    doc.save(pdf_path)
    doc.close()

    out_dir = tmp_path / "pages"
    pages = PdfRenderer(dpi=100).render(pdf_path, out_dir, show_progress=False)
    assert len(pages) == 1
    assert pages[0].image_path.exists()
    assert pages[0].page_number == 1