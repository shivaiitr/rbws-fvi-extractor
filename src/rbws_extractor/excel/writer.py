"""Excel writer matching gold-standard RBWS layout."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from PIL import Image as PILImage

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

# Photos / ISO columns sized for a clear engineer review viewport.
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
    "N": 110.0,  # Photos — wide so 4×N tiles stay readable
    "O": 78.0,  # ISO sketch
    "P": 50.8,
    "Q": 27.9,
}

# openpyxl column letters for image embeds (1-based col index).
_PHOTO_COL = 14  # N
_ISO_COL = 15  # O

# Excel clamps row height near 409pt — use the full budget for engineer review.
_MIN_IMG_ROW_PT = 200.0
_MAX_IMG_ROW_PT = 409.0
_CELL_PAD_PX = 4

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


def _col_width_to_px(width_chars: float) -> int:
    """Approximate Excel column width (character units) → screen pixels."""
    return max(40, int(width_chars * 7.0 + 5))


def _px_to_row_points(height_px: int) -> float:
    """Convert image pixel height to Excel row-height points (96 DPI)."""
    return height_px * 72.0 / 96.0


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

        photo_max_w = _col_width_to_px(GOLD_COL_WIDTHS["N"]) - _CELL_PAD_PX
        iso_max_w = _col_width_to_px(GOLD_COL_WIDTHS["O"]) - _CELL_PAD_PX
        # Excel row-height ceiling in pixels; used only as a soft display cap.
        fit_budget_h = int(_MAX_IMG_ROW_PT * 96.0 / 72.0) - _CELL_PAD_PX

        data_start = 3
        for offset, finding in enumerate(result.findings):
            row_idx = data_start + offset
            values = _finding_row_values(finding)
            for col_offset, value in enumerate(values):
                cell = ws.cell(row=row_idx, column=2 + col_offset, value=value)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                cell.font = Font(name="Calibri", size=10)
                cell.border = THIN
                if col_offset == 1 and value is not None:
                    cell.number_format = "mmm-yy"

            display_heights: list[float] = []
            if embed_images:
                ph = self._embed_image(
                    ws,
                    row_idx,
                    col=_PHOTO_COL,
                    path=finding.photos_image_path,
                    max_w=photo_max_w,
                    max_h=fit_budget_h,
                    fill_width=True,
                )
                ih = self._embed_image(
                    ws,
                    row_idx,
                    col=_ISO_COL,
                    path=finding.iso_image_path,
                    max_w=iso_max_w,
                    max_h=fit_budget_h,
                    fill_width=True,
                )
                if ph:
                    display_heights.append(ph)
                if ih:
                    display_heights.append(ih)

            if display_heights:
                # Match row height to the embedded image (Excel clamps ~409pt).
                needed = max(display_heights) + 6.0
                ws.row_dimensions[row_idx].height = min(
                    _MAX_IMG_ROW_PT, max(_MIN_IMG_ROW_PT, needed)
                )
            else:
                # Text-only / no-data rows stay compact.
                text = finding.anomaly_description or ""
                lines = max(2, text.count("\n") + 1)
                ws.row_dimensions[row_idx].height = min(120.0, 36.0 + lines * 14.0)

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
    def _embed_image(
        ws,
        row_idx: int,
        col: int,
        path: Path | None,
        *,
        max_w: int,
        max_h: int,
        fill_width: bool = True,
    ) -> float | None:
        """Embed image filling the column width (no side gaps). Returns height in points."""
        if path is None or not Path(path).exists():
            return None
        try:
            src = Path(path)
            with PILImage.open(src) as im:
                im = im.convert("RGB")
                if fill_width:
                    # Stretch to column width; keep aspect. Upscale when source is small
                    # so tiles stay recognizable for engineers.
                    scale = max_w / max(im.width, 1)
                    new_w = max_w
                    new_h = max(1, int(im.height * scale))
                    # If taller than Excel's practical row budget, scale to that height
                    # while still preferring as much width as possible.
                    if new_h > max_h:
                        scale_h = max_h / max(im.height, 1)
                        scale_w = max_w / max(im.width, 1)
                        # Prefer width fill when aspect is landscape; otherwise height cap.
                        if im.width >= im.height:
                            scale = scale_w
                            new_w = max_w
                            new_h = max(1, int(im.height * scale))
                            if new_h > max_h:
                                new_h = max_h
                                new_w = max(1, int(im.width * (max_h / max(im.height, 1))))
                                new_w = min(new_w, max_w)
                        else:
                            scale = min(scale_w, scale_h)
                            new_w = max(1, int(im.width * scale))
                            new_h = max(1, int(im.height * scale))
                    im = im.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
                else:
                    scale = min(max_w / max(im.width, 1), max_h / max(im.height, 1))
                    new_w = max(1, int(im.width * scale))
                    new_h = max(1, int(im.height * scale))
                    im = im.resize((new_w, new_h), PILImage.Resampling.LANCZOS)

                # No letterbox — image is the embed (avoids empty side gutters).
                fitted = src.with_name(f"{src.stem}_cell{src.suffix}")
                im.save(fitted, format="PNG", optimize=False)
                display_w, display_h_px = im.size

            img = XLImage(str(fitted))
            img.width = display_w
            img.height = display_h_px
            img.anchor = f"{get_column_letter(col)}{row_idx}"
            ws.add_image(img)
            return _px_to_row_points(display_h_px)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed embedding image %s at %s: %s", path, row_idx, exc)
            return None

    @staticmethod
    def _set_column_widths(ws) -> None:
        for letter, width in GOLD_COL_WIDTHS.items():
            ws.column_dimensions[letter].width = width
