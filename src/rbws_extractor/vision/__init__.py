"""Compatibility package for OCR-only RBWS extraction.

This project intentionally runs without an LLM API dependency. The compatibility
module keeps legacy imports available for tests and rule-based parsers without
bringing back the deleted model-backed pipeline.
"""

__all__ = []
