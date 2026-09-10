"""Read the schedule workbook and write back only the approved changes.

The workbook is loaded and saved with openpyxl directly -- never round-tripped
through pandas -- so every other sheet, all styling and all formulas survive
into the downloaded copy.
"""

from __future__ import annotations

import io
import re
import zipfile
from copy import copy
from datetime import date
from typing import Any, Sequence

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

from .config import (
    ACTION_FILL,
    ACTION_NEW,
    ACTION_UPDATE,
    ALL_COLUMNS,
    AUDIT_COLUMNS,
    COL_BILLED,
    COL_CHECK_EFT,
    COL_COMMENT,
    COL_COPAY,
    COL_CPT,
    COL_DATA,
    COL_DX,
    COL_INS,
    COL_PATIENT,
    COL_PAYMENT,
    COL_PROCESSED_ON,
    DATA_START_ROW,
    HEADER_ROW,
    NEVER_TOUCH_COLUMNS,
    REQUIRED_COLUMNS,
    SHEET_NAME,
)
from .matching import Change, ScheduleRow, is_blank, new_row_values

# Excel writes font family values that openpyxl's schema rejects (it caps the
# attribute at 14, real files carry 18/34). Clamping them lets an otherwise
# valid workbook load instead of failing outright.
_FAMILY_RE = re.compile(rb'(<family val=")(\d+)(")')
_MAX_FONT_FAMILY = 14
_DEFAULT_FONT_FAMILY = b"2"


class ScheduleError(RuntimeError):
    """The uploaded workbook cannot be used as a schedule."""


def _clamp_font_family(match: "re.Match[bytes]") -> bytes:
    value = int(match.group(2))
    replacement = match.group(2) if value <= _MAX_FONT_FAMILY else _DEFAULT_FONT_FAMILY
    return match.group(1) + replacement + match.group(3)


def repair_workbook_bytes(data: bytes) -> bytes:
    """Rewrite the xlsx zip with out-of-range font family values clamped.

    Only that one attribute is touched; every other part is copied byte for
    byte, so no formatting, formula or sheet is affected.
    """
    source = io.BytesIO(data)
    target = io.BytesIO()
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            payload = zin.read(item.filename)
            if item.filename.endswith(".xml"):
                payload = _FAMILY_RE.sub(_clamp_font_family, payload)
            zout.writestr(item, payload)
    return target.getvalue()


def load_schedule_workbook(data: bytes) -> openpyxl.Workbook:
    """Load the workbook, repairing the known invalid-XML case on failure.

    ``data_only`` stays False so formula cells keep their formulas on save.
    """
    try:
        return openpyxl.load_workbook(io.BytesIO(data))
    except ValueError:
        return openpyxl.load_workbook(io.BytesIO(repair_workbook_bytes(data)))


def resolve_columns(worksheet: Worksheet) -> dict[str, int]:
    """Map header text in row 2 to column indexes, never hardcoding letters."""
    found: dict[str, int] = {}
    wanted = {header.strip().lower(): header for header in ALL_COLUMNS}

    for column in range(1, worksheet.max_column + 1):
        value = worksheet.cell(row=HEADER_ROW, column=column).value
        if value is None:
            continue
        key = str(value).strip().lower()
        if key in wanted and wanted[key] not in found:
            found[wanted[key]] = column

    missing = [header for header in REQUIRED_COLUMNS if header not in found]
    if missing:
        raise ScheduleError(
            f"Sheet '{worksheet.title}' row {HEADER_ROW} is missing required "
            f"column(s): {', '.join(missing)}"
        )
    return found


def get_schedule_sheet(workbook: openpyxl.Workbook) -> Worksheet:
    if SHEET_NAME not in workbook.sheetnames:
        raise ScheduleError(
            f"Workbook has no sheet named '{SHEET_NAME}'. "
            f"Found: {', '.join(workbook.sheetnames)}"
        )
    return workbook[SHEET_NAME]


def last_data_row(worksheet: Worksheet, columns: dict[str, int]) -> int:
    """Last row with a patient name, so appends land after real data."""
    patient_col = columns[COL_PATIENT]
    last = DATA_START_ROW - 1
    for row in range(DATA_START_ROW, worksheet.max_row + 1):
        if not is_blank(worksheet.cell(row=row, column=patient_col).value):
            last = row
    return last


def read_schedule_rows(worksheet: Worksheet, columns: dict[str, int]) -> list[ScheduleRow]:
    """Every data row of the sheet, as plain values for matching."""
    rows: list[ScheduleRow] = []
    for row in range(DATA_START_ROW, last_data_row(worksheet, columns) + 1):
        def cell(header: str) -> Any:
            index = columns.get(header)
            return worksheet.cell(row=row, column=index).value if index else None

        if is_blank(cell(COL_PATIENT)):
            continue

        rows.append(
            ScheduleRow(
                row_num=row,
                patient=cell(COL_PATIENT),
                ins=cell(COL_INS),
                data=cell(COL_DATA),
                billed=cell(COL_BILLED),
                payment=cell(COL_PAYMENT),
                copay=cell(COL_COPAY),
                comment=cell(COL_COMMENT),
                dx=cell(COL_DX),
                cpt=cell(COL_CPT),
                processed_on=cell(COL_PROCESSED_ON),
                check_eft=cell(COL_CHECK_EFT),
            )
        )
    return rows


def _copy_style(source_cell, target_cell) -> None:
    """Carry a row's look onto an appended row so the sheet stays uniform."""
    if source_cell.has_style:
        target_cell.font = copy(source_cell.font)
        target_cell.border = copy(source_cell.border)
        target_cell.fill = copy(source_cell.fill)
        target_cell.alignment = copy(source_cell.alignment)
        target_cell.number_format = source_cell.number_format
        target_cell.protection = copy(source_cell.protection)


def _first_empty_header_column(worksheet: Worksheet) -> int:
    """The first column whose header cell in row 2 is empty."""
    column = 1
    while not is_blank(worksheet.cell(row=HEADER_ROW, column=column).value):
        column += 1
    return column


def ensure_audit_columns(worksheet: Worksheet,
                         columns: dict[str, int]) -> dict[str, int]:
    """Locate the app's audit columns, creating their headers if missing.

    `Processed On` and `Remit Check/EFT #` land in the first empty header
    cells after the existing columns (L and M in the standard layout) and
    inherit the header row's formatting.
    """
    resolved = dict(columns)
    template = worksheet.cell(row=HEADER_ROW, column=max(columns.values()))

    for header in AUDIT_COLUMNS:
        if header in resolved:
            continue
        column = _first_empty_header_column(worksheet)
        cell = worksheet.cell(row=HEADER_ROW, column=column, value=header)
        _copy_style(template, cell)
        resolved[header] = column

    return resolved


#: Text that marks a row as a totals/summary structure rather than patient data.
_SUMMARY_RE = re.compile(r"\b(total|totals|subtotal|sum|grand\s+total)\b", re.IGNORECASE)


def _row_is_summary_like(worksheet: Worksheet, columns: dict[str, int], row: int) -> bool:
    """A row with no patient but other content, or one saying 'total'."""
    if row > worksheet.max_row:
        return False

    patient_blank = is_blank(worksheet.cell(row=row, column=columns[COL_PATIENT]).value)
    has_content = False
    for column in range(1, worksheet.max_column + 1):
        value = worksheet.cell(row=row, column=column).value
        if is_blank(value):
            continue
        has_content = True
        if isinstance(value, str) and _SUMMARY_RE.search(value):
            return True
    return patient_blank and has_content


def insertion_blocked_reason(worksheet: Worksheet, columns: dict[str, int],
                             anchor: int) -> str | None:
    """Why inserting directly below `anchor` would be unsafe, or None.

    `insert_rows` does not adjust merged ranges, so splitting one would
    corrupt the sheet; and inserting above a totals row would silently put
    data outside whatever that row summarises. In either case the caller
    appends at the bottom instead.
    """
    for merged in worksheet.merged_cells.ranges:
        if merged.min_row <= anchor + 1 and merged.max_row >= anchor:
            return f"would split merged cells {merged.coord}"

    if _row_is_summary_like(worksheet, columns, anchor + 1):
        return f"row {anchor + 1} looks like a totals/summary row"

    return None


def annotate_placement(worksheet: Worksheet, columns: dict[str, int],
                       changes: Sequence[Change]) -> None:
    """Mark new rows whose anchor is unsafe, so the preview can say so."""
    for change in changes:
        if change.effective_action != ACTION_NEW or change.anchor_row is None:
            continue
        change.placement_fallback = (
            insertion_blocked_reason(worksheet, columns, change.anchor_row) or ""
        )


def _write_new_row(worksheet: Worksheet, columns: dict[str, int],
                   row: int, style_row: int, change: Change) -> None:
    """Fill one blank row with a change's values, styled like `style_row`."""
    values = new_row_values(
        change.visit,
        dx=change.dx_value,
        processed_on=change.processed_on,
        check_eft=change.check_eft_display,
    )
    for header, column in columns.items():
        if header in NEVER_TOUCH_COLUMNS:
            continue
        target = worksheet.cell(row=row, column=column)
        if style_row >= DATA_START_ROW:
            _copy_style(worksheet.cell(row=style_row, column=column), target)
        value = values.get(header)
        # DX is absent from values unless the Mutual file supplied one.
        if value not in (None, ""):
            target.value = value


def _stamp_audit(worksheet: Worksheet, columns: dict[str, int],
                 row_num: int, change: Change) -> None:
    """Write this run's audit stamps onto a row the app created or filled.

    These are the app's own columns, so unlike every other column they are
    overwritten rather than only filled when blank.
    """
    for header, value in (
        (COL_PROCESSED_ON, change.processed_on),
        (COL_CHECK_EFT, change.check_eft_display),
    ):
        column = columns.get(header)
        if column and value:
            worksheet.cell(row=row_num, column=column).value = value


def apply_changes(workbook: openpyxl.Workbook, changes: Sequence[Change]) -> dict[str, int]:
    """Apply accepted fills and appends in place. Returns what was written.

    Blank cells are the only cells ever written on an existing row: every
    proposed value is re-checked against the live cell immediately before
    writing, so a populated cell is left exactly as it was.
    """
    worksheet = get_schedule_sheet(workbook)
    columns = resolve_columns(worksheet)

    applies = [
        change for change in changes
        if change.accepted
        and change.effective_action in (ACTION_FILL, ACTION_NEW, ACTION_UPDATE)
    ]
    # Only touch the header row when there is actually something to stamp.
    if applies:
        columns = ensure_audit_columns(worksheet, columns)

    filled_cells = 0
    filled_rows = 0
    appended_rows = 0
    skipped_non_blank = 0

    for change in applies:
        if change.effective_action != ACTION_FILL or change.row_num is None:
            continue
        wrote_any = False
        for header, value in change.fills.items():
            if header in NEVER_TOUCH_COLUMNS:
                continue
            column = columns.get(header)
            if column is None:
                continue
            cell = worksheet.cell(row=change.row_num, column=column)
            if not is_blank(cell.value):
                skipped_non_blank += 1
                continue
            cell.value = value
            filled_cells += 1
            wrote_any = True
        if wrote_any:
            filled_rows += 1
            _stamp_audit(worksheet, columns, change.row_num, change)

    # --- Adjusted EOBs -----------------------------------------------------
    # The only place the app overwrites a populated cell. Reached solely by an
    # accepted `Updated (adjusted EOB)` change, whose old -> new values the
    # user saw in the preview before confirming.
    updated_rows = 0
    updated_cells = 0
    for change in applies:
        if change.effective_action != ACTION_UPDATE or change.row_num is None:
            continue
        if not change.updates:
            continue
        for header, value in change.updates.items():
            if header in NEVER_TOUCH_COLUMNS:
                continue
            column = columns.get(header)
            if column is None:
                continue
            worksheet.cell(row=change.row_num, column=column).value = value
            updated_cells += 1
        updated_rows += 1
        _stamp_audit(worksheet, columns, change.row_num, change)

    # Optional telehealth `Ins` correction, off by default. Only ever set when
    # OVERWRITE_INS_FOR_TELEHEALTH is on and the EOB contradicts the row.
    for change in applies:
        if not change.ins_update or change.row_num is None:
            continue
        column = columns.get(COL_INS)
        if column is not None:
            worksheet.cell(row=change.row_num, column=column).value = change.ins_update
            updated_cells += 1

    # --- New rows ----------------------------------------------------------
    # Placement is decided against the ORIGINAL layout (fills above have not
    # moved any row), then applied bottom-up so that inserting lower down
    # cannot shift an anchor that is still to be processed.
    new_changes = [c for c in applies if c.effective_action == ACTION_NEW]

    grouped: dict[int, list[Change]] = {}
    to_append: list[Change] = []
    for change in new_changes:
        anchor = change.anchor_row
        if anchor is None:
            to_append.append(change)
            continue
        blocked = insertion_blocked_reason(worksheet, columns, anchor)
        if blocked:
            change.placement_fallback = blocked
            to_append.append(change)
            continue
        change.placement_fallback = ""
        grouped.setdefault(anchor, []).append(change)

    inserted_rows = 0
    for anchor in sorted(grouped, reverse=True):
        block = sorted(grouped[anchor], key=lambda c: c.visit.service_date)
        worksheet.insert_rows(anchor + 1, len(block))
        for offset, change in enumerate(block):
            row = anchor + 1 + offset
            # insert_rows leaves the new cells unstyled; match the anchor row
            # so the inserted rows look like the rest of that patient's block.
            _write_new_row(worksheet, columns, row, anchor, change)
            inserted_rows += 1

    # Bottom appends go last, against the final layout.
    appended_rows = 0
    if to_append:
        template_row = last_data_row(worksheet, columns)
        next_row = template_row + 1
        for change in sorted(to_append, key=lambda c: (c.visit.patient, c.visit.service_date)):
            _write_new_row(worksheet, columns, next_row, template_row, change)
            appended_rows += 1
            next_row += 1

    return {
        "filled_rows": filled_rows,
        "filled_cells": filled_cells,
        "updated_rows": updated_rows,
        "updated_cells": updated_cells,
        "inserted_rows": inserted_rows,
        "appended_rows": appended_rows,
        "new_rows": inserted_rows + appended_rows,
        "skipped_non_blank": skipped_non_blank,
    }


def workbook_to_bytes(workbook: openpyxl.Workbook) -> io.BytesIO:
    """Serialise the whole workbook to an in-memory buffer for download."""
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def build_updated_workbook(data: bytes, changes: Sequence[Change]) -> tuple[io.BytesIO, dict[str, int]]:
    """Apply changes to a fresh copy of the upload and return it for download."""
    workbook = load_schedule_workbook(data)
    stats = apply_changes(workbook, changes)
    return workbook_to_bytes(workbook), stats


def download_filename(original: str = "List_of_Patients_Schedule.xlsx",
                      today: date | None = None) -> str:
    """Date-stamped name so successive archived downloads never collide."""
    stem = re.sub(r"\.xlsx$", "", original, flags=re.IGNORECASE) or "List_of_Patients_Schedule"
    stamp = (today or date.today()).isoformat()
    return f"{stem}_updated_{stamp}.xlsx"
