"""RBWS workbook template helpers."""

from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Font

from rbws_extractor.excel.writer import HEADERS


def create_empty_rbws_workbook(sheet_name: str = "RBWS") -> Workbook:
    """Create a blank RBWS workbook with gold-standard headers at row 2."""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]
    for col_idx, header in enumerate(HEADERS, start=2):
        cell = ws.cell(row=2, column=col_idx, value=header)
        cell.font = Font(bold=True)
    return wb