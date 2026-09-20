"""Pydantic data models for RBWS extraction."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class AssetClass(str, Enum):
    FIRE_PROTECTION_CYLINDER = "fire_protection_cylinder"
    FIRE_WATER_PUMP_CAISSON = "fire_water_pump_caisson"
    FIRE_WATER_PIPING = "fire_water_piping"
    FIRE_WATER_HOSE_REEL = "fire_water_hose_reel"
    FIRE_WATER_MONITOR = "fire_water_monitor"
    DIESEL_STORAGE_TANK = "diesel_storage_tank"
    UNTAGGED_LINE = "untagged_line"
    UNKNOWN = "unknown"


class PageType(str, Enum):
    COVER = "cover"
    METADATA_TOC = "metadata_toc"
    SUMMARY = "summary"
    OBSERVATION_TABLE = "observation_table"
    EQUIPMENT_PHOTO_BLOCK = "equipment_photo_block"
    ISO_SKETCH = "iso_sketch"
    UNTAGGED_LINES = "untagged_lines"
    SIGNATURE = "signature"
    OTHER = "other"


class PhotoRef(BaseModel):
    """Normalized photo reference such as 01-05 or 32-33."""

    raw: str
    start: int
    end: int

    @classmethod
    def parse(cls, text: str) -> Optional["PhotoRef"]:
        import re

        if not text:
            return None
        cleaned = text.strip()
        patterns = [
            r"(?i)photo\s*#?\s*(\d+)\s*[-–—&to]+\s*(\d+)",
            r"(?i)photo\s*#?\s*(\d+)\s*&\s*(\d+)",
            r"(?i)photo\s*#?\s*(\d+)",
            r"(\d+)\s*[-–—]\s*(\d+)",
            r"^(\d+)$",
        ]
        for pattern in patterns:
            m = re.search(pattern, cleaned)
            if not m:
                continue
            groups = m.groups()
            start = int(groups[0])
            end = int(groups[1]) if len(groups) > 1 and groups[1] else start
            if end < start:
                start, end = end, start
            raw = f"{start:02d}-{end:02d}" if start != end else f"{start:02d}"
            return cls(raw=raw, start=start, end=end)
        return None

    def format_excel(self) -> str:
        if self.start == self.end:
            return f"(Photo #{self.start:02d})"
        return f"(Photo #{self.start:02d}-{self.end:02d})"


class FindingSource(BaseModel):
    section: Optional[str] = None
    pages: list[int] = Field(default_factory=list)
    equipment_id: Optional[str] = None
    table_row_index: Optional[int] = None
    page_types: list[PageType] = Field(default_factory=list)
    notes: Optional[str] = None


class Finding(BaseModel):
    """One RBWS Excel row candidate."""

    sn: Optional[int] = None
    asset_class: AssetClass = AssetClass.UNKNOWN
    insp_date: Optional[date] = None
    line_number: str
    line_pressure_psi: Optional[str] = None
    line_description: Optional[str] = None
    anomaly_description: str
    pid_no: Optional[str] = None
    pof: Optional[str] = None
    consq: Optional[str] = None
    driver: Optional[str] = None
    risk: Optional[str] = None
    iru: Optional[str] = None
    photo_refs: list[PhotoRef] = Field(default_factory=list)
    photos_image_path: Optional[Path] = None
    iso_image_path: Optional[Path] = None
    recommendations: Optional[str] = None
    remarks: Optional[str] = None
    is_good_condition: bool = False
    source: FindingSource = Field(default_factory=FindingSource)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    @field_validator("line_number", "anomaly_description")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("required text field cannot be empty")
        return value

    @field_validator(
        "line_description",
        "pid_no",
        "pof",
        "recommendations",
        "remarks",
        mode="before",
    )
    @classmethod
    def empty_to_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def apply_judgment_blanks(self) -> "Finding":
        """Force Category C fields blank to match gold-standard automation scope."""
        self.line_pressure_psi = None
        self.pof = None
        self.consq = None
        self.driver = None
        self.risk = None
        self.iru = None
        self.recommendations = None
        self.remarks = None
        return self

    def anomaly_with_photo_suffix(self) -> str:
        text = self.anomaly_description.strip()
        if self.photo_refs and "photo #" not in text.lower():
            joined = ", ".join(ref.format_excel() for ref in self.photo_refs)
            return f"{text} {joined}".strip()
        return text


class ReportMetadata(BaseModel):
    report_id: Optional[str] = None
    work_order: Optional[str] = None
    facility: Optional[str] = None
    inspection_date: Optional[date] = None
    pid_numbers: list[str] = Field(default_factory=list)
    inspected_by: list[str] = Field(default_factory=list)
    title: Optional[str] = None
    source_pages: list[int] = Field(default_factory=list)

    @field_validator("inspection_date", mode="before")
    @classmethod
    def parse_date(cls, value: Any) -> Any:
        if value is None or isinstance(value, date):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            text = value.strip()
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%B %d, %Y"):
                try:
                    return datetime.strptime(text, fmt).date()
                except ValueError:
                    continue
            # Handle forms like "9TH OCTOBER, 2024"
            import re

            m = re.search(
                r"(\d{1,2})\s*(?:ST|ND|RD|TH)?\s*([A-Za-z]+),?\s*(\d{4})",
                text,
                flags=re.I,
            )
            if m:
                day = int(m.group(1))
                month_name = m.group(2).title()
                year = int(m.group(3))
                return datetime.strptime(f"{day} {month_name} {year}", "%d %B %Y").date()
        return value


class ContentMode(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    HYBRID = "hybrid"


class ClassifiedPage(BaseModel):
    page_number: int  # 1-based
    image_path: Path
    page_type: PageType = PageType.OTHER
    section_hint: Optional[str] = None
    confidence: float = 0.0
    text_preview: Optional[str] = None
    native_text: Optional[str] = None
    content_mode: ContentMode = ContentMode.IMAGE
    char_count: int = 0
    image_count: int = 0


class ExtractionResult(BaseModel):
    metadata: ReportMetadata
    findings: list[Finding] = Field(default_factory=list)
    classified_pages: list[ClassifiedPage] = Field(default_factory=list)
    needs_review: list[Finding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_pdf: Optional[Path] = None

    @model_validator(mode="after")
    def renumber(self) -> "ExtractionResult":
        for idx, finding in enumerate(self.findings, start=1):
            finding.sn = idx
            if finding.insp_date is None and self.metadata.inspection_date is not None:
                finding.insp_date = self.metadata.inspection_date
        return self


Finding.model_rebuild()
ExtractionResult.model_rebuild()