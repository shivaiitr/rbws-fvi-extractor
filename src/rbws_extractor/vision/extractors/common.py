"""Lightweight compatibility layer for legacy OCR/parsing imports.

This keeps the Python-only extraction path working without the removed LLM-based
vision modules.
"""

from __future__ import annotations

import re
from typing import Iterable

from pydantic import BaseModel, Field

from rbws_extractor.models.findings import AssetClass, PhotoRef


class CaptionItem(BaseModel):
    photo_no: int | None = None
    text: str = ""


class EquipmentBlock(BaseModel):
    equipment_id: str | None = None
    asset_class: AssetClass = AssetClass.UNKNOWN
    captions: list[CaptionItem] = Field(default_factory=list)
    is_good_condition: bool = False


class ObservationRow(BaseModel):
    row_index: int | None = None
    pid_no: str | None = None
    line_number: str | None = None
    line_description: str | None = None
    anomaly_description: str | None = None
    photo_refs: list[str] = Field(default_factory=list)
    is_good_condition: bool = False


class ObservationTableExtraction(BaseModel):
    section: str | None = None
    asset_class: AssetClass = AssetClass.UNKNOWN
    rows: list[ObservationRow] = Field(default_factory=list)


class SummaryBullet(BaseModel):
    asset_class: AssetClass = AssetClass.UNKNOWN
    text: str | None = None
    line_number: str | None = None
    line_description: str | None = None
    photo_refs: list[str] = Field(default_factory=list)
    section: str | None = None


def parse_photo_refs(values: Iterable[str | int] | str | None) -> list[PhotoRef]:
    """Normalize photo references like 01-05 or 32 to PhotoRef objects."""
    if values is None:
        return []
    if isinstance(values, str):
        items = [values]
    else:
        items = list(values)

    refs: list[PhotoRef] = []
    for value in items:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        match = re.search(r"(\d+)\s*(?:[-–—&to]+\s*(\d+))?", text, flags=re.I)
        if not match:
            continue
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start:
            start, end = end, start
        refs.append(PhotoRef(raw=f"{start:02d}-{end:02d}" if start != end else f"{start:02d}", start=start, end=end))
    return refs
