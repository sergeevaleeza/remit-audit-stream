"""Marcia appends, the optional catch-all, and the employee audit columns.

Two changes covered here:

* **Marcia now behaves like Ana and Oxana** -- she fills existing rows *and*
  appends further sessions for patients already in her tab. She was previously
  fill-only.
* **Each provider tab gains `Processed On` and `Remit Check/EFT #`**, created
  on that tab's own header row (Ana/Marcia row 1, Oxana row 2) and populated
  only for rows the app filled or appended this run.

Every patient here is fictional (see `tests/fixtures/README.md`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from remit import employees as employees_module
from remit.config import (
    COL_CHECK_EFT,
    COL_PROCESSED_ON,
    DATE_FMT,
    EMP_ACTION_APPEND,
    EMP_ACTION_AUTO_PLACED,
    EMP_ACTION_FILL,
    EMP_ACTION_UNASSIGNED,
    EMPLOYEE_APPEND_SHEETS,
    EMPLOYEE_SHEETS,
    MARCIA_CATCH_ALL_UNASSIGNED,
)
from remit.employees import (
    build_updated_employees,
    column_for,
    ensure_audit_columns,
    find_header_row,
    header_key,
    load_employees_workbook,
    plan_employee_changes,
    read_sheets,
    schedule_visits_from_plan,
)
from remit.excel_updater import (
    get_schedule_sheet,
    load_schedule_workbook,
    read_schedule_rows,
    resolve_columns,
)
from remit.matching import build_plan

EMPLOYEES_XLSX = Path(__file__).parent / "fixtures" / "AMSMC_employees_sample.xlsx"
STAMP = date(2026, 9, 10)
STAMPED = STAMP.strftime(DATE_FMT)
PAYING_EFT = "900000001"

HEADER_ROWS = {"Ana": 1, "Marcia": 1, "Oxana": 2}


@pytest.fixture()
def employees_bytes() -> bytes:
    return EMPLOYEES_XLSX.read_bytes()


@pytest.fixture()
def sheets(employees_bytes):
    return read_sheets(load_employees_workbook(employees_bytes))


@pytest.fixture()
def schedule_visits(schedule_bytes, visits):
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))
    plan = build_plan(visits, rows, today=STAMP)
    return schedule_visits_from_plan(rows, plan)


@pytest.fixture()
def changes(sheets, schedule_visits):
    return plan_employee_changes(sheets, schedule_visits, today=STAMP)


@pytest.fixture()
def applied(employees_bytes, changes):
    updated, stats = build_updated_employees(employees_bytes, changes)
    return load_employees_workbook(updated.getvalue()), stats


def tab_rows(workbook, name: str) -> list[dict]:
    """Every data row of one provider tab, keyed by header text."""
    worksheet = workbook[name]
    header_row = find_header_row(worksheet)
    headers = {
        c: worksheet.cell(row=header_row, column=c).value
        for c in range(1, worksheet.max_column + 1)
    }
    patient_col = next(c for c, h in headers.items()
                       if header_key(h) in ("patient name", "patient"))

    out = []
    for row in range(header_row + 1, worksheet.max_row + 1):
        if worksheet.cell(row=row, column=patient_col).value in (None, ""):
            continue
        record = {headers[c]: worksheet.cell(row=row, column=c).value
                  for c in headers if headers[c]}
        record["_row"] = row
        out.append(record)
    return out


# --- Change 1: Marcia appends ----------------------------------------------

def test_marcia_is_an_append_sheet():
    assert set(EMPLOYEE_APPEND_SHEETS) == set(EMPLOYEE_SHEETS)
    assert "Marcia" in EMPLOYEE_APPEND_SHEETS


def test_marcia_still_fills_existing_rows(changes):
    fill = next(c for c in changes
                if c.sheet == "Marcia" and c.action == EMP_ACTION_FILL)
    assert fill.patient.startswith("Whitfield")
    assert fill.session_date == "03/04/2026"


def test_marcia_appends_a_new_session_for_her_own_patient(changes):
    append = next(c for c in changes
                  if c.sheet == "Marcia" and c.action == EMP_ACTION_APPEND)
    assert append.patient.startswith("Whitfield")
    assert append.session_date == "04/02/2026"
    assert append.accepted


def test_marcia_append_lands_at_the_bottom_of_her_tab(applied):
    workbook, _ = applied
    rows = tab_rows(workbook, "Marcia")
    assert len(rows) == 2
    last = rows[-1]
    assert last["Patient Name"] == "Whitfield, Harold"
    assert last["Date of Session"] == "04/02/2026"
    assert last["Paid by Insurance"] == 166.37
    assert last["Co-pay by EOB"] == 42.44


def test_marcia_appended_row_inherits_styling(applied):
    workbook, _ = applied
    worksheet = workbook["Marcia"]
    template, appended = 2, 3
    for column in (1, 2, 6):
        assert (worksheet.cell(row=appended, column=column).number_format
                == worksheet.cell(row=template, column=column).number_format)
        assert (worksheet.cell(row=appended, column=column).font.name
                == worksheet.cell(row=template, column=column).font.name)


def test_marcia_does_not_double_add_on_a_rerun(employees_bytes, changes, schedule_visits):
    updated, _ = build_updated_employees(employees_bytes, changes)
    once = updated.getvalue()

    reread = read_sheets(load_employees_workbook(once))
    second = plan_employee_changes(reread, schedule_visits, today=STAMP)
    _, stats = build_updated_employees(once, second)

    assert stats["appended_rows"] == 0
    assert stats["filled_cells"] == 0
    assert len(tab_rows(load_employees_workbook(once), "Marcia")) == 2


def test_marcia_comment_cross_check_still_applies(sheets, schedule_visits):
    """A `Comment` naming another tab still blocks a silent placement."""
    from remit.employees import _comment_conflict

    marcia_visit = next(v for v in schedule_visits if v.patient.startswith("Whitfield"))
    assert not _comment_conflict(marcia_visit, "Marcia")  # blank/physician comment

    oxana_flagged = next(v for v in schedule_visits if v.comment == "Oxana")
    assert _comment_conflict(oxana_flagged, "Marcia")


# --- Change 1b: the optional catch-all -------------------------------------

def test_catch_all_is_off_by_default():
    assert MARCIA_CATCH_ALL_UNASSIGNED is False


def test_no_tab_patient_stays_unassigned_by_default(changes):
    unassigned = [c for c in changes if c.action == EMP_ACTION_UNASSIGNED]
    assert unassigned
    for change in unassigned:
        assert change.sheet == ""
        assert not change.accepted
        assert not change.fills


def test_catch_all_appends_to_marcia_when_enabled(monkeypatch, sheets, schedule_visits):
    monkeypatch.setattr(employees_module, "MARCIA_CATCH_ALL_UNASSIGNED", True)
    changes = plan_employee_changes(sheets, schedule_visits, today=STAMP)

    auto = [c for c in changes if c.action == EMP_ACTION_AUTO_PLACED]
    assert auto
    for change in auto:
        assert change.sheet == "Marcia"
        assert change.fills          # it would write, if accepted
        assert not change.accepted   # ...but never without review
        assert "auto-placed" in change.note
    assert not [c for c in changes if c.action == EMP_ACTION_UNASSIGNED]


def test_catch_all_rows_are_flagged_for_review(monkeypatch, sheets, schedule_visits):
    monkeypatch.setattr(employees_module, "MARCIA_CATCH_ALL_UNASSIGNED", True)
    changes = plan_employee_changes(sheets, schedule_visits, today=STAMP)
    auto = next(c for c in changes if c.action == EMP_ACTION_AUTO_PLACED)
    assert auto.needs_review


def test_catch_all_writes_nothing_unless_accepted(monkeypatch, employees_bytes,
                                                  sheets, schedule_visits):
    monkeypatch.setattr(employees_module, "MARCIA_CATCH_ALL_UNASSIGNED", True)
    changes = plan_employee_changes(sheets, schedule_visits, today=STAMP)

    before = len(tab_rows(load_employees_workbook(employees_bytes), "Marcia"))
    updated, _ = build_updated_employees(employees_bytes, changes)
    after = tab_rows(load_employees_workbook(updated.getvalue()), "Marcia")
    # Only the accepted Whitfield append lands; the auto-placed ones do not.
    assert len(after) == before + 1


def test_catch_all_rows_land_when_accepted(monkeypatch, employees_bytes,
                                           sheets, schedule_visits):
    monkeypatch.setattr(employees_module, "MARCIA_CATCH_ALL_UNASSIGNED", True)
    changes = plan_employee_changes(sheets, schedule_visits, today=STAMP)
    auto = [c for c in changes if c.action == EMP_ACTION_AUTO_PLACED]
    for change in auto:
        change.accepted = True

    updated, stats = build_updated_employees(employees_bytes, changes)
    rows = tab_rows(load_employees_workbook(updated.getvalue()), "Marcia")
    names = {r["Patient Name"] for r in rows}
    assert any(c.patient in names for c in auto)


# --- Change 2: audit columns ------------------------------------------------

@pytest.mark.parametrize("name", EMPLOYEE_SHEETS)
def test_each_tab_gains_both_audit_columns(applied, name):
    workbook, _ = applied
    worksheet = workbook[name]
    header_row = HEADER_ROWS[name]
    headers = [worksheet.cell(row=header_row, column=c).value
               for c in range(1, worksheet.max_column + 1)]
    assert COL_PROCESSED_ON in headers
    assert COL_CHECK_EFT in headers


@pytest.mark.parametrize("name", EMPLOYEE_SHEETS)
def test_audit_headers_sit_on_that_tabs_header_row(applied, name):
    """Oxana's headers are on row 2, so hers must be too."""
    workbook, _ = applied
    worksheet = workbook[name]
    header_row = HEADER_ROWS[name]
    assert find_header_row(worksheet) == header_row

    columns = {header_key(worksheet.cell(row=header_row, column=c).value): c
               for c in range(1, worksheet.max_column + 1)}
    processed = columns[header_key(COL_PROCESSED_ON)]
    # Nothing above the header row, and the row above Oxana's is her title.
    assert worksheet.cell(row=header_row, column=processed).value == COL_PROCESSED_ON


@pytest.mark.parametrize("name", EMPLOYEE_SHEETS)
def test_audit_headers_go_after_the_existing_ones(applied, name):
    workbook, _ = applied
    worksheet = workbook[name]
    header_row = HEADER_ROWS[name]
    headers = [worksheet.cell(row=header_row, column=c).value
               for c in range(1, worksheet.max_column + 1)]
    assert headers[-2:] == [COL_PROCESSED_ON, COL_CHECK_EFT]


@pytest.mark.parametrize("name", EMPLOYEE_SHEETS)
def test_audit_headers_match_the_header_row_formatting(applied, name):
    workbook, _ = applied
    worksheet = workbook[name]
    header_row = HEADER_ROWS[name]
    columns = {header_key(worksheet.cell(row=header_row, column=c).value): c
               for c in range(1, worksheet.max_column + 1)}
    template = worksheet.cell(row=header_row, column=1)
    created = worksheet.cell(row=header_row, column=columns[header_key(COL_PROCESSED_ON)])
    assert created.font.bold == template.font.bold
    assert created.fill.fgColor.rgb == template.fill.fgColor.rgb


def test_filled_rows_are_stamped(applied):
    workbook, _ = applied
    row = next(r for r in tab_rows(workbook, "Marcia")
               if r["Date of Session"] == "03/04/2026")
    assert row[COL_PROCESSED_ON] == STAMPED
    assert row[COL_CHECK_EFT] == PAYING_EFT


def test_appended_rows_are_stamped(applied):
    workbook, _ = applied
    row = next(r for r in tab_rows(workbook, "Marcia")
               if r["Date of Session"] == "04/02/2026")
    assert row[COL_PROCESSED_ON] == STAMPED
    assert row[COL_CHECK_EFT] == PAYING_EFT


def test_untouched_rows_are_not_stamped(applied, changes):
    """The practitioner-mismatch row is never written to."""
    workbook, _ = applied
    row = next(r for r in tab_rows(workbook, "Ana")
               if r["Date of Session"] == "03/09/2026")
    assert row["Patient Name"] == "Marlowe, Diane"
    assert row[COL_PROCESSED_ON] is None
    assert row[COL_CHECK_EFT] is None


def test_stamp_uses_the_configured_date_format(applied):
    workbook, _ = applied
    row = next(r for r in tab_rows(workbook, "Marcia")
               if r["Date of Session"] == "03/04/2026")
    assert row[COL_PROCESSED_ON] == "09/10/2026"
    assert isinstance(row[COL_PROCESSED_ON], str)


def test_audit_columns_are_reused_not_duplicated(employees_bytes, changes):
    """A second run must not append another pair of audit columns."""
    once, _ = build_updated_employees(employees_bytes, changes)
    workbook = load_employees_workbook(once.getvalue())
    before = workbook["Marcia"].max_column

    sheets = read_sheets(workbook)
    ensure_audit_columns(workbook["Marcia"], sheets["Marcia"])
    assert workbook["Marcia"].max_column == before


def test_audit_columns_only_on_tabs_that_were_written(employees_bytes, changes):
    """A tab the run never touches keeps its original shape."""
    for change in changes:
        change.accepted = change.sheet == "Marcia" and change.writes

    updated, _ = build_updated_employees(employees_bytes, changes)
    workbook = load_employees_workbook(updated.getvalue())

    ana_headers = [workbook["Ana"].cell(row=1, column=c).value
                   for c in range(1, workbook["Ana"].max_column + 1)]
    assert COL_PROCESSED_ON not in ana_headers

    marcia_headers = [workbook["Marcia"].cell(row=1, column=c).value
                      for c in range(1, workbook["Marcia"].max_column + 1)]
    assert COL_PROCESSED_ON in marcia_headers


# --- The EFT must be the paying remit's, never a duplicate's ---------------

def test_check_eft_is_the_authoritative_one(changes):
    for change in changes:
        if change.check_eft:
            assert change.check_eft == PAYING_EFT


def test_a_duplicate_eft_never_reaches_the_employees_file():
    """OA-18 duplicates supply no amounts, so no audit number either."""
    from remit.pdf_parser import parse_remittances

    fixtures = Path(__file__).parent / "fixtures"
    _, visits = parse_remittances([
        (str(fixtures / "RemitDoc-0000000002.PDF"), "RemitDoc-0000000002.PDF"),
        (str(fixtures / "RemitDoc-0000000003.PDF"), "RemitDoc-0000000003.PDF"),
    ])
    from .fixtures.synthetic_remit_data import DEDUCT_CHECK_EFT, DUPLICATE_CHECK_EFT

    restated = next(v for v in visits
                    if v.ignored_duplicate_efts and not v.duplicate_only)
    assert restated.authoritative_eft == DEDUCT_CHECK_EFT
    assert DUPLICATE_CHECK_EFT not in (restated.authoritative_eft or "")
    # ...even though the duplicate is still on the audit trail.
    assert DUPLICATE_CHECK_EFT in restated.check_efts


def test_several_authoritative_remits_are_joined():
    from remit.pdf_parser import RemitDocument, ServiceLine, aggregate_visits

    def document(name, billed, eft, provpd):
        return RemitDocument(
            filename=name, billed_date=billed, check_eft=eft, claim_count=1,
            service_lines=[ServiceLine(
                patient="TEST, PATIENT", npi="1000000001",
                service_date=date(2026, 3, 31), proc="90834",
                coins=10.00, prov_pd=provpd, source_file=name,
                check_eft=eft, codes=("CO-45",),
            )],
        )

    visit = aggregate_visits([
        document("a.pdf", date(2026, 7, 1), "900000010", 50.00),
        document("b.pdf", date(2026, 8, 1), "900000011", 104.27),
    ])[0]
    assert visit.authoritative_eft == "900000010; 900000011"


# --- Existing values are still safe ----------------------------------------

def test_existing_employee_values_are_untouched(applied):
    workbook, _ = applied
    row = next(r for r in tab_rows(workbook, "Ana")
               if r["Date of Session"] == "04/09/2026")
    assert row["Paid by Ins toAna"] == 999.99


def test_unmapped_columns_stay_blank_on_appends(applied):
    workbook, _ = applied
    row = next(r for r in tab_rows(workbook, "Marcia")
               if r["Date of Session"] == "04/02/2026")
    assert row["Memo"] is None
    assert row["Billed"] is None
    assert row["Paid to Marcia"] is None


def test_every_sheet_survives(applied):
    workbook, _ = applied
    assert workbook.sheetnames == ["Ana", "Marcia", "Oxana", "Notes"]
    assert workbook["Notes"]["A1"].value == "Not a provider tab"
