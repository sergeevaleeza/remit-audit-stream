"""The `Processed On` / `Remit Check/EFT #` audit columns (Change 4)."""

from __future__ import annotations

import io
from datetime import date

import openpyxl
import pytest

from remit.config import (
    ACTION_FILL,
    COL_CHECK_EFT,
    COL_PROCESSED_ON,
    DATE_FMT,
    HEADER_ROW,
    SHEET_NAME,
)
from remit.excel_updater import (
    build_updated_workbook,
    ensure_audit_columns,
    get_schedule_sheet,
    load_schedule_workbook,
    resolve_columns,
)
from remit.matching import build_plan, plan_change

from .conftest import find_visit

STAMP_DATE = date(2026, 9, 8)
STAMPED = STAMP_DATE.strftime(DATE_FMT)

PROCESSED_ON_COL = 12  # L
CHECK_EFT_COL = 13     # M


@pytest.fixture()
def applied(schedule_bytes, visits, schedule_rows):
    """The fixture workbook with the whole plan applied, plus its rows."""
    plan = build_plan(visits, schedule_rows, today=STAMP_DATE)
    updated, stats = build_updated_workbook(schedule_bytes, plan)
    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]
    return worksheet, plan, stats


# --- Header creation -------------------------------------------------------

def test_headers_are_created_at_l_and_m(applied):
    worksheet, _, _ = applied
    assert worksheet.cell(row=HEADER_ROW, column=PROCESSED_ON_COL).value == COL_PROCESSED_ON
    assert worksheet.cell(row=HEADER_ROW, column=CHECK_EFT_COL).value == COL_CHECK_EFT


def test_headers_land_after_cpt_code(applied):
    worksheet, _, _ = applied
    assert worksheet.cell(row=HEADER_ROW, column=11).value == "CPT Code"


def test_header_formatting_matches_the_header_row(applied):
    worksheet, _, _ = applied
    template = worksheet.cell(row=HEADER_ROW, column=11)
    for column in (PROCESSED_ON_COL, CHECK_EFT_COL):
        created = worksheet.cell(row=HEADER_ROW, column=column)
        assert created.font.bold == template.font.bold
        assert created.fill.fgColor.rgb == template.fill.fgColor.rgb


def test_existing_audit_headers_are_reused_not_duplicated(schedule_bytes):
    """Running twice must not append a second pair of audit columns."""
    workbook = load_schedule_workbook(schedule_bytes)
    worksheet = get_schedule_sheet(workbook)
    columns = resolve_columns(worksheet)

    first = ensure_audit_columns(worksheet, columns)
    second = ensure_audit_columns(worksheet, resolve_columns(worksheet))

    assert first[COL_PROCESSED_ON] == second[COL_PROCESSED_ON]
    assert first[COL_CHECK_EFT] == second[COL_CHECK_EFT]
    assert worksheet.max_column == CHECK_EFT_COL


def test_headers_are_not_created_when_nothing_is_applied(schedule_bytes, visits, schedule_rows):
    """A plan with nothing accepted must not touch the header row."""
    plan = build_plan(visits, schedule_rows, today=STAMP_DATE)
    for change in plan:
        change.accepted = False

    updated, _ = build_updated_workbook(schedule_bytes, plan)
    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]
    assert worksheet.cell(row=HEADER_ROW, column=PROCESSED_ON_COL).value is None


# --- Stamping --------------------------------------------------------------

def test_filled_rows_are_stamped(applied):
    worksheet, plan, _ = applied
    filled = [c for c in plan if c.effective_action == ACTION_FILL and c.accepted]
    assert filled
    for change in filled:
        assert worksheet.cell(row=change.row_num, column=PROCESSED_ON_COL).value == STAMPED
        assert worksheet.cell(row=change.row_num, column=CHECK_EFT_COL).value == "900000001"


def test_appended_rows_are_stamped(applied, schedule_rows):
    worksheet, _, stats = applied
    first_new = len(schedule_rows) + 3
    for row in range(first_new, first_new + stats["appended_rows"]):
        assert worksheet.cell(row=row, column=PROCESSED_ON_COL).value == STAMPED
        assert worksheet.cell(row=row, column=CHECK_EFT_COL).value == "900000001"


def test_skipped_rows_are_not_stamped(applied, schedule_rows, visits):
    """Already-paid rows stay completely untouched."""
    worksheet, plan, _ = applied
    skipped = [c for c in plan if c.action == "Skip (already paid)"]
    assert skipped
    for change in skipped:
        assert worksheet.cell(row=change.row_num, column=PROCESSED_ON_COL).value is None
        assert worksheet.cell(row=change.row_num, column=CHECK_EFT_COL).value is None


def test_untouched_rows_are_not_stamped(applied, schedule_rows):
    """`Delacroix, Owen` is in the sheet but not the remit."""
    worksheet, _, _ = applied
    delacroix = next(r for r in schedule_rows if str(r.patient).startswith("Delacroix"))
    assert worksheet.cell(row=delacroix.row_num, column=PROCESSED_ON_COL).value is None


def test_stamp_uses_the_configured_date_format(applied):
    worksheet, plan, _ = applied
    change = next(c for c in plan if c.effective_action == ACTION_FILL and c.accepted)
    stamped = worksheet.cell(row=change.row_num, column=PROCESSED_ON_COL).value
    assert stamped == "09/08/2026"
    assert isinstance(stamped, str)


def test_stamp_defaults_to_today(visits, schedule_rows):
    change = plan_change(
        find_visit(visits, "BYSTRITSKAYA, ANNA", date(2026, 4, 14)), schedule_rows
    )
    assert change.processed_on == date.today().strftime(DATE_FMT)


# --- Check/EFT -------------------------------------------------------------

def test_check_eft_comes_from_the_pdf_header(visits, schedule_rows):
    change = plan_change(
        find_visit(visits, "BYSTRITSKAYA, ANNA", date(2026, 4, 14)), schedule_rows
    )
    assert change.check_eft_display == "900000001"


def test_multiple_checks_are_joined(visits, schedule_rows):
    """A visit fed by two remits records both numbers."""
    visit = find_visit(visits, "BYSTRITSKAYA, ANNA", date(2026, 4, 14))
    visit.check_efts = {"900000001", "900000002"}
    try:
        change = plan_change(visit, schedule_rows)
        assert change.check_eft_display == "900000001; 900000002"
    finally:
        visit.check_efts = {"900000001"}


def test_audit_stamp_is_refreshed_on_a_later_run(schedule_bytes, visits, schedule_rows):
    """The audit columns are the app's own, so re-touching a row restamps it."""
    row = next(r for r in schedule_rows if str(r.patient).startswith("Bystritskaya"))
    visit = find_visit(visits, "BYSTRITSKAYA, ANNA", date(2026, 4, 14))

    first = build_plan([visit], schedule_rows, today=date(2026, 1, 1))
    once, _ = build_updated_workbook(schedule_bytes, first)
    worksheet = load_schedule_workbook(once.getvalue())[SHEET_NAME]
    assert worksheet.cell(row=row.row_num, column=PROCESSED_ON_COL).value == "01/01/2026"

    # Blank the Payment again so the row is fillable a second time.
    workbook = load_schedule_workbook(once.getvalue())
    workbook[SHEET_NAME].cell(row=row.row_num, column=5).value = None
    buffer = io.BytesIO()
    workbook.save(buffer)

    second = build_plan([visit], schedule_rows, today=date(2026, 2, 2))
    twice, _ = build_updated_workbook(buffer.getvalue(), second)
    worksheet = load_schedule_workbook(twice.getvalue())[SHEET_NAME]
    assert worksheet.cell(row=row.row_num, column=PROCESSED_ON_COL).value == "02/02/2026"


# --- Interaction with existing guarantees ----------------------------------

def test_audit_columns_do_not_break_idempotency(schedule_bytes, visits, schedule_rows):
    from remit.excel_updater import read_schedule_rows

    plan = build_plan(visits, schedule_rows, today=STAMP_DATE)
    updated, _ = build_updated_workbook(schedule_bytes, plan)
    updated_bytes = updated.getvalue()

    worksheet = get_schedule_sheet(load_schedule_workbook(updated_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))
    second = build_plan(visits, rows, today=STAMP_DATE)

    _, stats = build_updated_workbook(updated_bytes, second)
    assert stats["filled_cells"] == 0
    assert stats["appended_rows"] == 0


def test_other_sheets_still_survive(applied, schedule_bytes):
    workbook = load_schedule_workbook(schedule_bytes)
    plan_workbook = openpyxl.load_workbook(io.BytesIO(schedule_bytes))
    assert plan_workbook.sheetnames == workbook.sheetnames == ["2026 Medicare", "2026 Medical"]
