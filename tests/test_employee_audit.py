"""Marcia is fill-only again, and the employee audit columns.

Two things covered here.

* **Marcia fills but never appends.** Her sessions are billed incident-to
  under the supervising physician's NPI, so the remit can never prove a visit
  is hers -- her tab roster is what defines ownership. The roster-based append
  and the `MARCIA_CATCH_ALL_UNASSIGNED` overflow bucket that shipped in 1.6.0
  are both gone; a visit with no row waiting for it is listed, not placed.
* **Each provider tab carries `Processed On` and `Remit Check/EFT #`**,
  created on that tab's own header row (Ana/Marcia row 1, Oxana row 2) and
  populated only for rows the app filled or appended this run.

Every patient here is fictional (see `tests/fixtures/README.md`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from remit import config
from remit.config import (
    COL_CHECK_EFT,
    COL_PROCESSED_ON,
    DATE_FMT,
    EMP_ACTION_APPEND,
    EMP_ACTION_FILL,
    EMP_ACTION_UNASSIGNED,
    EMPLOYEE_APPEND_SHEETS,
    EMPLOYEE_NPI,
    EMPLOYEE_SHEETS,
)
from remit.employees import (
    build_updated_employees,
    ensure_audit_columns,
    eob_visits,
    find_header_row,
    header_key,
    load_employees_workbook,
    plan_employee_changes,
    read_sheets,
)

STAMP = date(2026, 9, 10)
STAMPED = STAMP.strftime(DATE_FMT)
PAYING_EFT = "900000001"
ASSOCIATES_EFT = "900000004"

HEADER_ROWS = {"Ana": 1, "Marcia": 1, "Oxana": 2}


@pytest.fixture()
def sheets(employee_sheets):
    return employee_sheets


@pytest.fixture()
def visits_for_tabs(all_visits):
    return eob_visits(all_visits)


@pytest.fixture()
def changes(sheets, visits_for_tabs):
    return plan_employee_changes(sheets, visits_for_tabs, today=STAMP)


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


# --- Change 1: Marcia is fill-only ------------------------------------------

def test_marcia_is_not_an_append_sheet():
    assert "Marcia" not in EMPLOYEE_APPEND_SHEETS
    assert set(EMPLOYEE_APPEND_SHEETS) == {"Ana", "Oxana"}


def test_marcia_has_no_npi_of_her_own():
    """Which is *why* she is fill-only: no EOB can be attributed to her."""
    assert "Marcia" not in EMPLOYEE_NPI
    assert set(EMPLOYEE_NPI) == {"Ana", "Oxana"}


def test_marcia_still_fills_existing_rows(changes):
    fills = [c for c in changes
             if c.sheet == "Marcia" and c.action == EMP_ACTION_FILL]
    assert {c.session_date for c in fills} == {"03/04/2026", "06/05/2026"}
    assert all(c.patient.startswith("Whitfield") for c in fills)


def test_marcia_never_appends_anything(changes):
    assert not [c for c in changes
                if c.sheet == "Marcia" and c.action == EMP_ACTION_APPEND]
    assert not [c for c in changes if c.sheet == "Marcia" and c.appends]


def test_a_marcia_visit_with_no_row_is_listed_not_added(changes):
    """`Whitfield` 04/02 and 06/24 have no row waiting for them."""
    for session in ("04/02/2026", "06/24/2026"):
        change = next(c for c in changes
                      if c.patient.startswith("Whitfield")
                      and c.session_date == session)
        assert change.action == EMP_ACTION_UNASSIGNED
        assert change.sheet == ""
        assert not change.accepted
        assert not change.fills
        assert "fill-only" in change.note


def test_marcias_tab_keeps_exactly_its_original_rows(employees_bytes, applied):
    before = tab_rows(load_employees_workbook(employees_bytes), "Marcia")
    workbook, _ = applied
    after = tab_rows(workbook, "Marcia")
    assert len(after) == len(before) == 3
    assert [r["Date of Session"] for r in after] == [
        "03/04/2026", "06/05/2026", "06/11/2026"
    ]


def test_marcia_does_not_double_add_on_a_rerun(employees_bytes, changes,
                                               visits_for_tabs):
    updated, _ = build_updated_employees(employees_bytes, changes)
    once = updated.getvalue()

    reread = read_sheets(load_employees_workbook(once))
    second = plan_employee_changes(reread, visits_for_tabs, today=STAMP)
    _, stats = build_updated_employees(once, second)

    assert stats["appended_rows"] == 0
    assert stats["filled_cells"] == 0
    assert len(tab_rows(load_employees_workbook(once), "Marcia")) == 3


def test_the_practitioner_cross_check_skips_marcias_tab(visits_for_tabs):
    """Her roster decides, so an associate NPI is not a conflict there."""
    from remit.employees import _practitioner_conflict

    oxana_visit = next(v for v in visits_for_tabs
                       if v.patient.startswith("Beaumont"))
    assert _practitioner_conflict(oxana_visit, "Ana") == "Oxana"
    assert _practitioner_conflict(oxana_visit, "Oxana") == ""
    assert _practitioner_conflict(oxana_visit, "Marcia") == ""


# --- Change 1b: the catch-all is gone ---------------------------------------

def test_the_catch_all_flag_no_longer_exists():
    """`MARCIA_CATCH_ALL_UNASSIGNED` was removed with the roster append."""
    assert not hasattr(config, "MARCIA_CATCH_ALL_UNASSIGNED")
    assert not hasattr(config, "EMPLOYEE_CATCH_ALL_SHEET")
    assert not hasattr(config, "EMP_ACTION_AUTO_PLACED")
    assert not hasattr(config, "FALLBACK_TO_COMMENT_FOR_NEW")


def test_no_tab_patient_stays_unassigned(changes):
    unassigned = [c for c in changes if c.action == EMP_ACTION_UNASSIGNED]
    assert unassigned
    for change in unassigned:
        assert change.sheet == ""
        assert not change.accepted
        assert not change.fills


def test_nothing_is_ever_swept_into_a_tab(employees_bytes, changes):
    """Only NPI-matched appends land; everything else is reported."""
    updated, stats = build_updated_employees(employees_bytes, changes)
    workbook = load_employees_workbook(updated.getvalue())
    appended = sum(1 for c in changes if c.accepted and c.appends)
    assert stats["appended_rows"] == appended == 2
    assert {r["Patient Name"] for r in tab_rows(workbook, "Marcia")} == {
        "Whitfield, Harold"
    }


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
    row = next(r for r in tab_rows(workbook, "Ana")
               if r["Date of Session"] == "06/16/2026")
    assert row[COL_PROCESSED_ON] == STAMPED
    assert row[COL_CHECK_EFT] == ASSOCIATES_EFT


def test_the_stamped_eft_is_the_one_that_paid_this_visit(applied):
    """Each row carries its own remit's number, not the run's first one."""
    workbook, _ = applied
    marcia = next(r for r in tab_rows(workbook, "Marcia")
                  if r["Date of Session"] == "06/05/2026")
    assert marcia[COL_CHECK_EFT] == ASSOCIATES_EFT


def test_untouched_rows_are_not_stamped(applied):
    """The practitioner-mismatch row is never written to."""
    workbook, _ = applied
    row = next(r for r in tab_rows(workbook, "Ana")
               if r["Date of Session"] == "06/10/2026")
    assert row["Patient Name"] == "Beaumont, Sylvie"
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

def test_check_eft_is_never_a_duplicates(changes):
    known = {PAYING_EFT, ASSOCIATES_EFT}
    for change in changes:
        if change.check_eft:
            assert change.check_eft in known


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

    # And the EOB-side view carries only the paying number forward.
    carried = next(v for v in eob_visits(visits)
                   if v.session_date == restated.service_date)
    assert carried.check_eft == DEDUCT_CHECK_EFT


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
    row = next(r for r in tab_rows(workbook, "Ana")
               if r["Date of Session"] == "06/16/2026")
    assert row["Memo"] is None
    assert row["Billed"] is None
    assert row["Paid to Ana"] is None


def test_every_sheet_survives(applied):
    workbook, _ = applied
    assert workbook.sheetnames == ["Ana", "Marcia", "Oxana", "Notes"]
    assert workbook["Notes"]["A1"].value == "Not a provider tab"
