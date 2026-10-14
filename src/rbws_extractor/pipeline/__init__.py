"""Pipeline package. Local OCR path is OcrPipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rbws_extractor.pipeline.ocr_pipeline import OcrPipeline

__all__ = ["OcrPipeline"]


def __getattr__(name: str):
    if name == "OcrPipeline":
        from rbws_extractor.pipeline.ocr_pipeline import OcrPipeline

        return OcrPipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
