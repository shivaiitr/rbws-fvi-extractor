"""Minimal page classifier for OCR-only RBWS extraction.

This project intentionally avoids any LLM-backed classification workflow. The
compatibility layer below still exposes the same API used by the checkpoint tests
and any resume logic, but falls back to deterministic rule-based heuristics when
no external client is configured.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import ClassifiedPage, ContentMode, PageType
from rbws_extractor.pdf.checkpoint import ClassificationCheckpoint
from rbws_extractor.pdf.renderer import RenderedPage
from rbws_extractor.pdf.text_layer import classify_page_content

logger = get_logger("pdf.classifier")


class PageClassifier:
    """Rule-based page classifier with checkpoint resume support."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        prefer_text_heuristics: bool = True,
        default_page_type: PageType = PageType.OTHER,
    ) -> None:
        self.client = client
        self.prefer_text_heuristics = prefer_text_heuristics
        self.default_page_type = default_page_type

    def _heuristic_classification(self, page: RenderedPage) -> ClassifiedPage:
        text = (page.native_text or "").strip()
        page_type = self.default_page_type
        confidence = 0.5
        section_hint: str | None = None

        if text:
            lowered = text.lower()
            if "summary of inspection findings" in lowered:
                page_type = PageType.SUMMARY
                confidence = 0.94
            elif "equipment id" in lowered or "photo #" in lowered:
                page_type = PageType.EQUIPMENT_PHOTO_BLOCK
                confidence = 0.9
            elif "work order" in lowered or "date of inspection" in lowered:
                page_type = PageType.METADATA_TOC
                confidence = 0.88
            elif "fire water" in lowered or "observation" in lowered:
                page_type = PageType.OBSERVATION_TABLE
                confidence = 0.8
            elif "iso" in lowered or "drawing" in lowered:
                page_type = PageType.ISO_SKETCH
                confidence = 0.82
            else:
                page_type = PageType.OTHER
                confidence = 0.6

            if page.content_mode == ContentMode.IMAGE and page_type == PageType.OTHER:
                confidence = max(confidence, 0.55)

        else:
            if page.image_count >= 3:
                page_type = PageType.EQUIPMENT_PHOTO_BLOCK
                confidence = 0.8
            else:
                page_type = PageType.OTHER
                confidence = 0.4

        content_mode = page.content_mode or ContentMode.IMAGE
        if page_type == PageType.OTHER and page.native_text:
            content_mode = classify_page_content(
                page.native_text,
                page.image_count,
                min_chars=80,
                min_words=12,
            )

        return ClassifiedPage(
            page_number=page.page_number,
            image_path=page.image_path,
            page_type=page_type,
            confidence=confidence,
            native_text=page.native_text or None,
            content_mode=content_mode,
            char_count=page.char_count,
            image_count=page.image_count,
            section_hint=section_hint,
        )

    def _classify_with_client(self, page: RenderedPage) -> ClassifiedPage:
        if self.client is None or not hasattr(self.client, "complete_model"):
            return self._heuristic_classification(page)

        result = self.client.complete_model(page)
        page_type = getattr(result, "page_type", PageType.OTHER)
        section_hint = getattr(result, "section_hint", None)
        text_preview = getattr(result, "text_preview", None)
        confidence = float(getattr(result, "confidence", 0.5) or 0.5)

        if not isinstance(page_type, PageType):
            try:
                page_type = PageType(page_type)
            except Exception:
                page_type = PageType.OTHER

        return ClassifiedPage(
            page_number=page.page_number,
            image_path=page.image_path,
            page_type=page_type,
            confidence=confidence,
            section_hint=section_hint,
            text_preview=text_preview,
            native_text=page.native_text or None,
            content_mode=page.content_mode,
            char_count=page.char_count,
            image_count=page.image_count,
        )

    def classify(self, page: RenderedPage) -> ClassifiedPage:
        if self.prefer_text_heuristics:
            return self._heuristic_classification(page)
        if self.client is not None and hasattr(self.client, "complete_model"):
            return self._classify_with_client(page)
        return self._heuristic_classification(page)

    def classify_all(
        self,
        pages: Iterable[RenderedPage],
        *,
        show_progress: bool = False,
        request_pause_s: float = 0.0,
        checkpoint_path: str | Path | None = None,
        retry_failed: bool = False,
        max_retry_passes: int = 0,
    ) -> list[ClassifiedPage]:
        items = list(pages)
        checkpoint = None
        if checkpoint_path is not None:
            checkpoint = ClassificationCheckpoint(Path(checkpoint_path))
            seeded = checkpoint.successful_pages
            if seeded:
                current_by_page = {p.page_number: p for p in items}
                result: list[ClassifiedPage] = []
                for page in items:
                    saved = seeded.get(page.page_number)
                    if saved is not None:
                        current = current_by_page.get(page.page_number)
                        if current is not None:
                            result.append(
                                saved.model_copy(
                                    update={
                                        "image_path": current.image_path,
                                        "native_text": current.native_text or page.native_text,
                                        "content_mode": current.content_mode,
                                        "char_count": current.char_count,
                                        "image_count": current.image_count,
                                    }
                                )
                            )
                        else:
                            result.append(saved)
                    elif self.client is not None and hasattr(self.client, "complete_model"):
                        result.append(self._classify_with_client(page))
                    else:
                        result.append(self._heuristic_classification(page))
                return result

        result: list[ClassifiedPage] = []
        for page in items:
            if checkpoint is not None:
                saved = checkpoint.get(page.page_number)
                if saved is not None:
                    result.append(
                        saved.model_copy(
                            update={
                                "image_path": page.image_path,
                                "native_text": page.native_text or saved.native_text,
                                "content_mode": page.content_mode,
                                "char_count": page.char_count,
                                "image_count": page.image_count,
                            }
                        )
                    )
                    continue

            if self.client is not None and hasattr(self.client, "complete_model"):
                classified = self._classify_with_client(page)
            else:
                classified = self._heuristic_classification(page)

            if checkpoint is not None:
                checkpoint.put(classified, persist=False)
            result.append(classified)

        if checkpoint is not None:
            checkpoint.save()
        return result
