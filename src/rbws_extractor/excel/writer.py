"""Excel writer matching gold-standard RBWS layout."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter

from rbws_extractor.logging_config import get_logger
from rbws_extractor.models.findings import ExtractionResult, Finding
from rbws_extractor.pipeline.rules import load_rbws_config

logger = get_logger("excel.writer")


HEADERS = [
    "S/N",
    "Insp Date",
    "Line #",
    "Line Pressure (Psi)",
    "Line Description",
    "Anomaly Description",
    "P&ID No",
    "POF",
    "Consq",
    "Driver",
    "Risk",
    "IRU",
    "Photos",
    "ISO",
    "Quest  Recommendations",
    "Remarks",
]

# Matched to gold UBIT workbook.
GOLD_COL_WIDTHS = {
    "B": 9.1,
    "C": 10.5,
    "D": 20.1,
    "E": 13.4,
    "F": 28.7,
    "G": 50.8,
    "H": 18.3,
    "I": 7.6,
    "J": 8.1,
    "K": 8.6,
    "L": 7.0,
    "M": 13.0,
    "N": 35.8,
    "O": 36.4,
    "P": 50.8,
    "Q": 27.9,
}

THIN = Border(
    left=Side(style="thin", color="B0B0B0"),
    right=Side(style="thin", color="B0B0B0"),
    top=Side(style="thin", color="B0B0B0"),
    bottom=Side(style="thin", color="B0B0B0"),
)


def _as_excel_date(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


def _finding_row_values(finding: Finding) -> list[Any]:
    return [
        finding.sn,
        _as_excel_date(finding.insp_date),
        finding.line_number,
        finding.line_pressure_psi,
        finding.line_description,
        finding.anomaly_description,
        finding.pid_no,
        finding.pof,
        finding.consq,
        finding.driver,
        finding.risk,
        finding.iru,
        None,  # Photos image
        None,  # ISO image
        finding.recommendations,
        finding.remarks,
    ]


class RbwsExcelWriter:
    """Write ExtractionResult to an OpenPyXL workbook."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config = load_rbws_config(str(config_path) if config_path else None)

    def write(
        self,
        result: ExtractionResult,
        output_path: Path,
        embed_images: bool = True,
        include_pof_note: bool = True,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        wb = Workbook()
        ws = wb.active
        facility = result.metadata.facility or "RBWS"
        ws.title = facility[:31]

        # Header row at row 2 to match gold workbook.
        ws.row_dimensions[2].height = 55.5
        for col_idx, header in enumerate(HEADERS, start=2):
            cell = ws.cell(row=2, column=col_idx, value=header)
            cell.font = Font(bold=True, size=11, name="Calibri")
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
            cell.border = THIN

        data_start = 3
        for offset, finding in enumerate(result.findings):
            row_idx = data_start + offset
            values = _finding_row_values(finding)
            for col_offset, value in enumerate(values):
                cell = ws.cell(row=row_idx, column=2 + col_offset, value=value)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                cell.font = Font(name="Calibri", size=10)
                cell.border = THIN
                # Date format for Insp Date
                if col_offset == 1 and value is not None:
                    cell.number_format = "YYYY-MM-DD"

            # Gold uses ~150pt rows for image-bearing findings.
            has_img = bool(
                embed_images
                and (finding.photos_image_path or finding.iso_image_path)
            )
            n_photos = sum(ref.end - ref.start + 1 for ref in finding.photo_refs) or 2
            if has_img:
                # Grow slightly for multi-photo collages, stay near gold.
                ws.row_dimensions[row_idx].height = min(220, max(150, 110 + n_photos * 8))
            else:
                ws.row_dimensions[row_idx].height = 60

            if embed_images:
                self._embed_image(ws, row_idx, col=14, path=finding.photos_image_path)
                self._embed_image(ws, row_idx, col=15, path=finding.iso_image_path)

        if include_pof_note:
            note_row = data_start + len(result.findings) + 2
            note = self.config.get("pof_note")
            if note:
                cell = ws.cell(row=note_row, column=2, value=str(note).strip())
                cell.alignment = Alignment(wrap_text=True)
                ws.merge_cells(
                    start_row=note_row,
                    start_column=2,
                    end_row=note_row,
                    end_column=8,
                )

        self._set_column_widths(ws)
        ws.freeze_panes = "B3"
        ws.print_title_rows = "2:2"
        wb.save(output_path)
        logger.info(
            "Wrote RBWS workbook: %s (%s findings)", output_path, len(result.findings)
        )
        return output_path

    @staticmethod
    def _embed_image(ws, row_idx: int, col: int, path: Path | None) -> None:
        if path is None or not Path(path).exists():
            return
        try:
            img = XLImage(str(path))
            # Professional cell fit (gold-like): readable, not oversized.
            max_w, max_h = 260, 155
            if img.width and img.height:
                scale = min(max_w / img.width, max_h / img.height, 1.0)
                img.width = max(1, int(img.width * scale))
                img.height = max(1, int(img.height * scale))
            img.anchor = f"{get_column_letter(col)}{row_idx}"
            ws.add_image(img)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed embedding image %s at %s: %s", path, row_idx, exc)

    @staticmethod
    def _set_column_widths(ws) -> None:
        for letter, width in GOLD_COL_WIDTHS.items():
            ws.column_dimensions[letter].width = width
