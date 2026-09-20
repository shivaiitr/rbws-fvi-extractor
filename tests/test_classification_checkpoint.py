"""Tests for classification checkpoint / resume behavior."""

from pathlib import Path
from unittest.mock import MagicMock

from rbws_extractor.models.findings import ClassifiedPage, ContentMode, PageType
from rbws_extractor.pdf.checkpoint import (
    ClassificationCheckpoint,
    is_successful_classification,
)
from rbws_extractor.pdf.classifier import PageClassifier
from rbws_extractor.pdf.renderer import RenderedPage


def _rendered(n: int, tmp_path: Path) -> RenderedPage:
    img = tmp_path / f"page_{n:03d}.png"
    img.write_bytes(b"x")
    return RenderedPage(
        page_number=n,
        image_path=img,
        width=10,
        height=10,
        native_text="",
        content_mode=ContentMode.IMAGE,
        char_count=0,
        image_count=1,
    )


def test_checkpoint_persists_and_reloads_success(tmp_path: Path):
    ckpt_path = tmp_path / "classification_checkpoint.json"
    page = ClassifiedPage(
        page_number=3,
        image_path=tmp_path / "page_003.png",
        page_type=PageType.SUMMARY,
        confidence=0.95,
        content_mode=ContentMode.IMAGE,
    )
    ckpt = ClassificationCheckpoint(ckpt_path)
    ckpt.put(page)
    assert ckpt_path.exists()

    reloaded = ClassificationCheckpoint(ckpt_path)
    saved = reloaded.get(3)
    assert saved is not None
    assert saved.page_type == PageType.SUMMARY
    assert is_successful_classification(saved)


def test_fallback_not_treated_as_success():
    page = ClassifiedPage(
        page_number=8,
        image_path=Path("/tmp/x.png"),
        page_type=PageType.OTHER,
        confidence=0.0,
        section_hint="classify_fallback:APITimeoutError",
        content_mode=ContentMode.IMAGE,
    )
    assert not is_successful_classification(page)


def test_classify_all_resumes_from_checkpoint(tmp_path: Path):
    pages = [_rendered(i, tmp_path) for i in range(1, 4)]
    ckpt_path = tmp_path / "classification_checkpoint.json"
    ckpt = ClassificationCheckpoint(ckpt_path)
    ckpt.put(
        ClassifiedPage(
            page_number=1,
            image_path=pages[0].image_path,
            page_type=PageType.COVER,
            confidence=0.95,
            content_mode=ContentMode.IMAGE,
        )
    )
    ckpt.put(
        ClassifiedPage(
            page_number=2,
            image_path=pages[1].image_path,
            page_type=PageType.METADATA_TOC,
            confidence=0.9,
            content_mode=ContentMode.IMAGE,
        )
    )

    client = MagicMock()
    # Only page 3 should call the model.
    client.complete_model.return_value = MagicMock(
        page_type=PageType.SUMMARY,
        section_hint=None,
        confidence=0.91,
        text_preview=None,
    )
    classifier = PageClassifier(client, prefer_text_heuristics=False)
    result = classifier.classify_all(
        pages,
        show_progress=False,
        request_pause_s=0,
        checkpoint_path=ckpt_path,
        retry_failed=False,
        max_retry_passes=0,
    )
    assert len(result) == 3
    assert result[0].page_type == PageType.COVER
    assert result[1].page_type == PageType.METADATA_TOC
    assert result[2].page_type == PageType.SUMMARY
    assert client.complete_model.call_count == 1
