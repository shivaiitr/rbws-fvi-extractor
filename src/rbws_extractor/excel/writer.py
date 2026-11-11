"""Excel writer matching gold-standard RBWS layout."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
from openpyxl.drawing.xdr import XDRPositiveSize2D
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils.units import pixels_to_EMU
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
    "N": 35.8,  # Photos — same shell as the manual EAP-GN workbook
    "O": 36.4,  # ISO sketch
    "P": 50.8,
    "Q": 27.9,
}

# openpyxl column letters for image embeds (1-based col index).
_PHOTO_COL = 14  # N
_ISO_COL = 15  # O

# Excel clamps row height near 409pt.
_MAX_IMG_ROW_PT = 409.0
# Pictures sit inside the cell with a small margin, aspect locked.
_CELL_INSET = 0.04
_GRID_INSET = 8
_GRID_GAP = 6

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
    """Excel column width (character units) → pixels at 96 DPI, Calibri 11."""
    return max(40, int(((256 * width_chars + int(128 / 7)) / 256) * 7))


def _px_to_row_points(height_px: float) -> float:
    """Convert image pixel height to Excel row-height points (96 DPI)."""
    return height_px * 72.0 / 96.0


def _sheet_title(name: str, taken: set[str] | None = None) -> str:
    """Excel sheet names are at most 31 characters and must be unique in the workbook."""
    cleaned = re.sub(r"[\[\]:*?/\\]", " ", name or "RBWS")
    cleaned = re.sub(r"\s+", " ", cleaned).strip() or "RBWS"
    base = cleaned[:31]
    used = taken or set()
    if base not in used:
        return base
    for number in range(2, 50):
        suffix = f" {number}"
        candidate = base[: 31 - len(suffix)] + suffix
        if candidate not in used:
            return candidate
    return base[:28] + " x"


def _px_to_col_width(px: int) -> float:
    """Pixels at 96 DPI back to an Excel column width that can hold them."""
    width = ((px + 2) * 256 / 7 - int(128 / 7)) / 256
    return max(GOLD_COL_WIDTHS["N"], round(width, 2))


def photo_grid_columns(count: int, tile_h: int, max_h: int) -> int:
    """2 across while the photos fit at their own size. 4 across when a 2-grid would shrink them."""
    if count <= 1:
        return 1
    rows = (count + 1) // 2
    height = _GRID_INSET * 2 + rows * tile_h + max(0, rows - 1) * _GRID_GAP
    if height <= max_h:
        return 2
    return 4


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
        workbook: Workbook | None = None,
        save: bool = True,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        own_book = workbook is None
        wb = workbook if workbook is not None else Workbook()
        facility = result.metadata.facility or "RBWS"
        if own_book:
            ws = wb.active
            ws.title = _sheet_title(facility)
        else:
            ws = wb.create_sheet(_sheet_title(facility, taken={sheet.title for sheet in wb.worksheets}))

        # Header row at row 2 to match gold workbook.
        ws.row_dimensions[2].height = 55.5
        headers = list(HEADERS)
        if result.metadata.layout == "pressure_vessel":
            headers[2] = "Equipment #"
            headers[4] = "Equipment Description"
        for col_idx, header in enumerate(headers, start=2):
            cell = ws.cell(row=2, column=col_idx, value=header)
            cell.font = Font(bold=True, size=11, name="Calibri")
            cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
            cell.border = THIN

        photo_max_w = _col_width_to_px(GOLD_COL_WIDTHS["N"])
        iso_max_w = _col_width_to_px(GOLD_COL_WIDTHS["O"])
        fit_budget_h = int(_MAX_IMG_ROW_PT * 96.0 / 72.0)
        vessel = result.metadata.layout == "pressure_vessel"
        vessel_photo_px = photo_max_w

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
                photo_paths = [
                    Path(p)
                    for p in (finding.photo_image_paths or [])
                    if p and Path(p).exists()
                ]
                if not photo_paths and finding.photos_image_path:
                    photo_paths = [Path(finding.photos_image_path)]
                if vessel:
                    ph, used_w = self._place_vessel_photos(
                        ws,
                        row_idx,
                        photo_paths,
                        max_h=fit_budget_h,
                    )
                    vessel_photo_px = max(vessel_photo_px, used_w)
                else:
                    ph = self._place_images(
                        ws,
                        row_idx,
                        col=_PHOTO_COL,
                        paths=photo_paths,
                        max_w=photo_max_w,
                        max_h=fit_budget_h,
                        columns=2,
                    )
                ih = self._place_images(
                    ws,
                    row_idx,
                    col=_ISO_COL,
                    paths=[Path(finding.iso_image_path)] if finding.iso_image_path else [],
                    max_w=iso_max_w,
                    max_h=fit_budget_h,
                    columns=1,
                )
                if ph:
                    display_heights.append(ph)
                if ih:
                    display_heights.append(ih)

            if display_heights:
                text = finding.anomaly_description or ""
                text_need = 36.0 + max(1, text.count("\n") + 1) * 14.0
                needed = max(display_heights) + 8.0
                ws.row_dimensions[row_idx].height = min(
                    _MAX_IMG_ROW_PT, max(needed, min(text_need, _MAX_IMG_ROW_PT))
                )
            else:
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
        if vessel:
            ws.column_dimensions["N"].width = _px_to_col_width(vessel_photo_px)
        ws.freeze_panes = "B3"
        ws.print_title_rows = "2:2"
        if save:
            wb.save(output_path)
        logger.info(
            "Wrote RBWS workbook: %s (%s findings)", output_path, len(result.findings)
        )
        return output_path

    @staticmethod
    def _place_images(
        ws,
        row_idx: int,
        col: int,
        paths: list[Path],
        *,
        max_w: int,
        max_h: int,
        columns: int,
    ) -> float | None:
        """Tile original images inside the cell. Display size only; files stay full resolution."""
        existing = [Path(p) for p in paths if p and Path(p).exists()]
        if not existing:
            return None
        try:
            natives: list[tuple[Path, int, int]] = []
            for path in existing:
                with PILImage.open(path) as im:
                    natives.append((path, im.width, im.height))

            cols = 1 if len(natives) == 1 else max(1, columns)
            inset = max(2, int(max_w * _CELL_INSET))
            inner_w = max(40, max_w - 2 * inset)
            gap = 6 if cols > 1 else 0
            tile_w = (inner_w - gap * (cols - 1)) / cols

            placed: list[tuple[Path, int, int, int, int]] = []
            y = inset
            row_h = 0
            for idx, (path, nw, nh) in enumerate(natives):
                if idx % cols == 0:
                    y += row_h + (gap if idx else 0)
                    row_h = 0
                scale = min(1.0, tile_w / max(nw, 1))
                dw = max(1, int(nw * scale))
                dh = max(1, int(nh * scale))
                x = inset + (idx % cols) * (int(tile_w) + gap)
                placed.append((path, x, y, dw, dh))
                row_h = max(row_h, dh)
            total_h = y + row_h + inset

            if total_h > max_h:
                shrink = max_h / total_h
                placed = [
                    (path, int(x * shrink), int(y * shrink), max(1, int(dw * shrink)), max(1, int(dh * shrink)))
                    for path, x, y, dw, dh in placed
                ]
                total_h = max_h

            for path, x, y, dw, dh in placed:
                img = XLImage(str(path))
                img.anchor = OneCellAnchor(
                    _from=AnchorMarker(
                        col=col - 1,
                        colOff=pixels_to_EMU(x),
                        row=row_idx - 1,
                        rowOff=pixels_to_EMU(y),
                    ),
                    ext=XDRPositiveSize2D(cx=pixels_to_EMU(dw), cy=pixels_to_EMU(dh)),
                )
                ws.add_image(img)
            return _px_to_row_points(total_h)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed placing images at row %s: %s", row_idx, exc)
            return None

    def _place_vessel_photos(
        self,
        ws,
        row_idx: int,
        paths: list[Path],
        *,
        max_h: int,
    ) -> tuple[float | None, int]:
        """Place each PDF photo once, at its own pixels, 4 across only when 2 across would shrink it."""
        existing = [Path(p) for p in paths if p and Path(p).exists()]
        if not existing:
            return None, 0
        try:
            natives: list[tuple[Path, int, int]] = []
            seen_hash: set[bytes] = set()
            for path in existing:
                digest = hashlib.sha256(path.read_bytes()).digest()
                if digest in seen_hash:
                    continue
                seen_hash.add(digest)
                with PILImage.open(path) as im:
                    natives.append((path, im.width, im.height))
            if not natives:
                return None, 0
            tile_h = max(nh for _, _, nh in natives)
            cols = photo_grid_columns(len(natives), tile_h, max_h)
            placed, total_h, content_w = _pack_native(natives, cols)
            if total_h > max_h:
                shrink = max_h / total_h
                placed = [
                    (path, int(x * shrink), int(y * shrink), max(1, int(dw * shrink)), max(1, int(dh * shrink)))
                    for path, x, y, dw, dh in placed
                ]
                total_h = max_h
                content_w = max(1, int(content_w * shrink))
            for path, x, y, dw, dh in placed:
                img = XLImage(str(path))
                img.anchor = OneCellAnchor(
                    _from=AnchorMarker(
                        col=_PHOTO_COL - 1,
                        colOff=pixels_to_EMU(x),
                        row=row_idx - 1,
                        rowOff=pixels_to_EMU(y),
                    ),
                    ext=XDRPositiveSize2D(cx=pixels_to_EMU(dw), cy=pixels_to_EMU(dh)),
                )
                ws.add_image(img)
            return _px_to_row_points(total_h), content_w
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed placing vessel photos at row %s: %s", row_idx, exc)
            return None, 0

    @staticmethod
    def _set_column_widths(ws) -> None:
        for letter, width in GOLD_COL_WIDTHS.items():
            ws.column_dimensions[letter].width = width


def _pack_native(
    natives: list[tuple[Path, int, int]],
    cols: int,
) -> tuple[list[tuple[Path, int, int, int, int]], int, int]:
    """Lay photos out at their own pixel size. No resample."""
    gap = _GRID_GAP if cols > 1 else 0
    rows = [natives[i : i + cols] for i in range(0, len(natives), cols)]
    placed: list[tuple[Path, int, int, int, int]] = []
    y = _GRID_INSET
    content_w = 0
    row_h = 0
    for row in rows:
        tile_w = max(nw for _, nw, _ in row)
        row_h = max(nh for _, _, nh in row)
        x = _GRID_INSET
        for path, nw, nh in row:
            placed.append((path, x, y, nw, nh))
            x += tile_w + gap
        content_w = max(content_w, x - gap + _GRID_INSET)
        y += row_h + gap
    total_h = y - gap + _GRID_INSET
    return placed, total_h, content_w
