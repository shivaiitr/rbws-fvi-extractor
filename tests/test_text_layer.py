"""Tests for native text vs image PDF detection."""

from pathlib import Path

import fitz

from rbws_extractor.pdf.text_layer import ContentMode, classify_page_content, inspect_pdf_text
from rbws_extractor.vision.page_input import extraction_inputs_for_pages
from rbws_extractor.models.findings import ClassifiedPage, ContentMode as ModelContentMode


def test_classify_image_page_with_no_text():
    assert classify_page_content("", image_count=20) == ContentMode.IMAGE


def test_classify_text_page():
    text = "SUMMARY OF INSPECTION FINDINGS\n" + ("line anomaly corrosion flange support " * 20)
    assert classify_page_content(text, image_count=0) == ContentMode.TEXT


def test_classify_hybrid_photo_page():
    text = (
        "Equipment ID Cylinder Pack south-west side of Cellar Deck. "
        "Photo #01 General view showing discoloration on shell and corrosion at base."
    )
    assert classify_page_content(text, image_count=8) == ContentMode.HYBRID


def test_inspect_native_text_pdf(tmp_path: Path):
    pdf_path = tmp_path / "native.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (40, 60),
        (
            "UBIT-GP FIRE WATER DISTRIBUTION INSPECTION REPORT\n"
            "Date of Inspection 9TH OCTOBER, 2024\n"
            "Visual Inspection Observation Anomaly Description Line Number P&ID\n"
            "WF-010 heavy passive scale buildup on LCV bonnet body and flanges"
        ),
    )
    doc.save(pdf_path)
    doc.close()

    profile = inspect_pdf_text(pdf_path, min_chars=40, min_words=8)
    assert profile.document_mode in {ContentMode.TEXT, ContentMode.HYBRID}
    assert profile.pages[0].char_count > 0
    assert profile.pages[0].content_mode != ContentMode.IMAGE


def test_inspect_image_only_pdf(tmp_path: Path):
    # Create a PDF page with only an embedded image and no text operators.
    pdf_path = tmp_path / "image_only.pdf"
    img_path = tmp_path / "page.png"

    # solid image
    import struct
    import zlib

    # minimal 1x1 PNG
    def write_png(path: Path) -> None:
        width, height = 8, 8
        raw = b"".join(b"\x00" + (b"\xff\x00\x00" * width) for _ in range(height))
        compressed = zlib.compress(raw)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + tag
                + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", compressed)
            + chunk(b"IEND", b"")
        )
        path.write_bytes(png)

    write_png(img_path)
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_image(page.rect, filename=str(img_path))
    doc.save(pdf_path)
    doc.close()

    profile = inspect_pdf_text(pdf_path)
    assert profile.pages[0].content_mode == ContentMode.IMAGE
    assert profile.document_mode == ContentMode.IMAGE


def test_extraction_inputs_auto_skips_images_for_text_pages(tmp_path: Path):
    img = tmp_path / "p.png"
    img.write_bytes(b"not-a-real-image")
    page = ClassifiedPage(
        page_number=1,
        image_path=img,
        native_text="Date of Inspection 9 October 2024 " * 5,
        content_mode=ModelContentMode.TEXT,
        char_count=200,
    )
    images, text = extraction_inputs_for_pages([page], force_mode="auto")
    assert images == []
    assert "Date of Inspection" in text
