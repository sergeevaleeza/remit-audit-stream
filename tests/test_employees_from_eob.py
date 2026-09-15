"""The employees file is sourced from the EOBs, and the service-date cutoff.

Two changes are covered here.

* **The employees tabs are populated directly from the reconciled EOB
  visits**, not by reading values back out of the updated schedule. The
  schedule and the employees workbook are independent consumers of one visit
  set. Appends are gated on the EOB's **PERF PROV NPI**: a visit reaches Ana's
  or Oxana's tab only if she performed it. Marcia is fill-only, because her
  sessions bill incident-to under the supervising physician and the remit can
  never prove one is hers.
* **An optional service-date cutoff** drops visits served before a date, for
  the whole run, so re-processing a remit the providers have already archived
  cannot re-append rows they finished with by hand.

Every patient here is fictional (see `tests/fixtures/README.md`).
"""

from __future__ import annotations

from datetime import date

import pytest

from remit.config import (
    EMP_ACTION_AMBIGUOUS,
    EMP_ACTION_APPEND,
    EMP_ACTION_FILL,
    EMP_ACTION_MISMATCH,
    EMP_ACTION_UNASSIGNED,
    EMPLOYEE_APPEND_SHEETS,
    EMPLOYEE_NPI,
    EMPLOYEE_SHEETS,
)
from remit.employees import (
    EobVisit,
    build_updated_employees,
    eob_visits,
    load_employees_workbook,
    plan_employee_changes,
    read_sheets,
    tab_for_npi,
)
from remit.excel_updater import (
    build_updated_workbook,
    get_schedule_sheet,
    load_schedule_workbook,
    read_schedule_rows,
    resolve_columns,
)
from remit.matching import apply_service_date_cutoff, build_plan, cutoff_label

from .conftest import sheet_rows
from .fixtures.synthetic_remit_data import (
    ANA_NPI,
    OXANA_NPI,
    SECOND_PHYSICIAN_NPI,
    SUPERVISING_NPI,
)

STAMP = date(2026, 9, 10)


@pytest.fixture()
def visits_for_tabs(all_visits):
    """The reconciled EOB visits, in the shape the employees tabs consume."""
    return eob_visits(all_visits)


@pytest.fixture()
def changes(employee_sheets, visits_for_tabs):
    return plan_employee_changes(employee_sheets, visits_for_tabs, today=STAMP)


def change_for(changes, patient, session_date, sheet=None, action=None):
    """One planned change, located by patient surname + date (+ tab/action)."""
    surname = patient.split(",")[0]
    found = [
        c for c in changes
        if c.patient.startswith(surname)
        and c.session_date == session_date
        and (sheet is None or c.sheet == sheet)
        and (action is None or c.action == action)
    ]
    if not found:
        raise AssertionError(f"no change for {patient} on {session_date} "
                             f"(sheet={sheet}, action={action})")
    if len(found) > 1:
        raise AssertionError(f"{len(found)} changes matched {patient} "
                             f"on {session_date}: {[c.action for c in found]}")
    return found[0]


def tab_rows(workbook, name: str) -> list[dict]:
    """Every data row of one provider tab, keyed by header text."""
    from remit.employees import find_header_row, header_key

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


# --- The source is the EOB, not the schedule --------------------------------

def test_employees_take_the_eob_value_not_the_schedule_value(
        schedule_bytes, all_visits, employee_sheets, employees_bytes):
    """`Whitfield, Harold` 03/04 is recorded as `0.00` in the schedule.

    A `0.00` Payment is a *recorded* value, so the schedule is never rewritten
    and the old schedule-sourced path put that `0.00` into Marcia's tab. The
    EOB for the same visit pays `$130.46`, and that is what the tab must get.
    """
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))
    scheduled = next(r for r in rows
                     if str(r.patient).startswith("Whitfield")
                     and r.data_date == date(2026, 3, 4))
    assert scheduled.payment == 0.00  # what the schedule says

    changes = plan_employee_changes(employee_sheets, eob_visits(all_visits),
                                    today=STAMP)
    change = change_for(changes, "Whitfield", "03/04/2026", sheet="Marcia")
    assert change.action == EMP_ACTION_FILL
    assert change.payment == 130.46  # ...what the EOB says

    updated, _ = build_updated_employees(employees_bytes, changes)
    row = next(r for r in tab_rows(load_employees_workbook(updated.getvalue()), "Marcia")
               if r["Date of Session"] == "03/04/2026")
    assert row["Paid by Insurance"] == 130.46


def test_the_schedule_is_never_read_by_the_employees_path(employee_sheets, all_visits):
    """Planning the tabs needs no schedule at all -- not even an empty one."""
    changes = plan_employee_changes(employee_sheets, eob_visits(all_visits),
                                    today=STAMP)
    assert [c for c in changes if c.action == EMP_ACTION_FILL]


def test_an_edited_schedule_cannot_change_the_employees_values(
        schedule_bytes, all_visits, employee_sheets):
    """Applying the schedule plan first leaves the tabs' values identical."""
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))
    plan = build_plan(all_visits, rows, today=STAMP)
    build_updated_workbook(schedule_bytes, [c for c in plan if c.accepted])

    before = plan_employee_changes(employee_sheets, eob_visits(all_visits),
                                   today=STAMP)
    after = plan_employee_changes(employee_sheets, eob_visits(all_visits),
                                  today=STAMP)
    assert [(c.sheet, c.action, c.patient, c.payment) for c in before] == \
           [(c.sheet, c.action, c.patient, c.payment) for c in after]


def test_eob_visits_carry_the_npi_and_the_paying_eft(visits_for_tabs):
    visit = next(v for v in visits_for_tabs
                 if v.patient.startswith("Ravensworth")
                 and v.session_date == date(2026, 6, 16))
    assert visit.npi == ANA_NPI
    assert visit.check_eft == "900000004"
    assert visit.insurance == "Medicare"


# --- NPI ownership ----------------------------------------------------------

def test_tab_for_npi_resolves_the_associates():
    assert tab_for_npi(ANA_NPI) == "Ana"
    assert tab_for_npi(OXANA_NPI) == "Oxana"


def test_a_supervising_physician_npi_owns_no_tab():
    """Marcia's sessions bill under it, so it can never route by itself."""
    assert tab_for_npi(SUPERVISING_NPI) is None
    assert tab_for_npi(SECOND_PHYSICIAN_NPI) is None
    assert tab_for_npi("9999999999") is None


def test_only_tabs_with_their_own_npi_accept_appends():
    assert set(EMPLOYEE_APPEND_SHEETS) == {"Ana", "Oxana"}
    assert "Marcia" not in EMPLOYEE_APPEND_SHEETS
    assert "Marcia" not in EMPLOYEE_NPI


# --- Ana / Oxana appends are NPI-gated --------------------------------------

def test_visit_under_anas_npi_appends_to_her_tab(changes):
    change = change_for(changes, "Ravensworth", "06/16/2026", sheet="Ana")
    assert change.action == EMP_ACTION_APPEND
    assert change.npi == ANA_NPI
    assert change.accepted


def test_visit_under_oxanas_npi_appends_to_her_tab(changes):
    change = change_for(changes, "Nakamura", "06/17/2026", sheet="Oxana")
    assert change.action == EMP_ACTION_APPEND
    assert change.npi == OXANA_NPI


def test_a_different_npi_never_appends_to_ana(changes):
    """`Ravensworth` 06/09 is missing from Ana's tab and she plainly owns the
    patient -- but the physician performed that session, so it is listed."""
    change = change_for(changes, "Ravensworth", "06/09/2026")
    assert change.action == EMP_ACTION_UNASSIGNED
    assert change.sheet == ""
    assert change.npi == SUPERVISING_NPI
    assert not change.accepted
    assert not [c for c in changes
                if c.sheet == "Ana" and c.session_date == "06/09/2026"]


def test_a_known_patients_other_sessions_do_not_follow_them_in(changes):
    """`Castellano` is Ana's, but 04/21 and 05/07 billed under the physician.

    Under the old roster-based rule these were appended to Ana's tab purely
    because she already had the patient. NPI gating is what stops that.
    """
    for session in ("04/21/2026", "05/07/2026"):
        change = change_for(changes, "Castellano", session)
        assert change.action == EMP_ACTION_UNASSIGNED
        assert change.sheet == ""
    assert not [c for c in changes
                if c.sheet == "Ana" and c.action == EMP_ACTION_APPEND
                and c.patient.startswith("Castellano")]


def test_no_append_when_the_visit_is_already_in_the_tab(changes):
    """`Ravensworth` 06/02 carries Ana's NPI but her tab already has it."""
    assert not [c for c in changes
                if c.action == EMP_ACTION_APPEND and c.session_date == "06/02/2026"]
    assert change_for(changes, "Ravensworth", "06/02/2026",
                      sheet="Ana").action == EMP_ACTION_FILL


def test_appends_land_in_the_written_workbook(employees_bytes, changes):
    updated, stats = build_updated_employees(employees_bytes, changes)
    workbook = load_employees_workbook(updated.getvalue())

    ana = tab_rows(workbook, "Ana")[-1]
    assert ana["Patient Name"] == "Ravensworth, Cecily"
    assert ana["Date of Session"] == "06/16/2026"
    assert ana["Paid by Ins toAna"] == 186.31
    assert ana["Co-pay by EOB"] == 47.53

    oxana = tab_rows(workbook, "Oxana")[-1]
    assert oxana["Patient"] == "Nakamura, Hiroshi"
    assert oxana["Paid by Insurance"] == 130.46

    assert stats["appended_rows"] == 2


# --- Marcia never appends ---------------------------------------------------

def test_marcia_fills_an_existing_row_from_the_eob(changes):
    """A supervising-physician NPI is not a conflict on her tab."""
    change = change_for(changes, "Whitfield", "06/05/2026", sheet="Marcia")
    assert change.action == EMP_ACTION_FILL
    assert change.npi == SUPERVISING_NPI
    assert change.payment == 130.46


def test_marcia_never_appends_a_visit_with_no_row(changes):
    """`Whitfield` 06/24 is hers by any reading, and is still not added."""
    change = change_for(changes, "Whitfield", "06/24/2026")
    assert change.action == EMP_ACTION_UNASSIGNED
    assert change.sheet == ""
    assert "fill-only" in change.note


def test_marcia_has_no_appends_at_all(changes):
    assert not [c for c in changes if c.sheet == "Marcia" and c.appends]


def test_marcia_tab_gains_no_rows(employees_bytes, changes):
    before = len(tab_rows(load_employees_workbook(employees_bytes), "Marcia"))
    updated, _ = build_updated_employees(employees_bytes, changes)
    after = len(tab_rows(load_employees_workbook(updated.getvalue()), "Marcia"))
    assert after == before


# --- Ambiguity and practitioner mismatch ------------------------------------

def test_two_eob_visits_on_one_date_prefer_the_tabs_own_npi(changes):
    """`Ravensworth` 06/02 was billed by both Ana and the physician."""
    change = change_for(changes, "Ravensworth", "06/02/2026", sheet="Ana")
    assert change.action == EMP_ACTION_FILL
    assert change.npi == ANA_NPI
    assert change.payment == 166.37  # Ana's line, not the physician's 130.46


def test_a_tie_on_marcias_tab_is_flagged(changes):
    """Two physicians billed 06/11 and Marcia has no NPI to choose with."""
    change = change_for(changes, "Whitfield", "06/11/2026", sheet="Marcia")
    assert change.action == EMP_ACTION_AMBIGUOUS
    assert change.needs_review
    assert not change.accepted
    assert "no NPI of her own" in change.note


def test_another_associates_npi_on_an_existing_row_is_a_mismatch(changes):
    """`Beaumont` 06/10 sits in Ana's tab but Oxana performed it."""
    change = change_for(changes, "Beaumont", "06/10/2026", sheet="Ana")
    assert change.action == EMP_ACTION_MISMATCH
    assert not change.accepted
    assert "Oxana" in change.note


def test_the_same_visit_still_fills_the_tab_that_does_own_it(changes):
    change = change_for(changes, "Beaumont", "06/10/2026", sheet="Oxana")
    assert change.action == EMP_ACTION_FILL


def test_a_mismatched_row_is_never_written(employees_bytes, changes):
    updated, _ = build_updated_employees(employees_bytes, changes)
    row = next(r for r in tab_rows(load_employees_workbook(updated.getvalue()), "Ana")
               if r["Date of Session"] == "06/10/2026")
    assert row["Paid by Ins toAna"] is None
    assert row["Insurance"] is None


# --- Reconciliation still applies -------------------------------------------

def test_oa18_duplicates_do_not_zero_the_employees_values(employee_sheets):
    """The paying EOB feeds the tabs; the duplicate supplies nothing.

    The duplicate remittance re-adjudicates every visit at `$0.00`. Feeding
    the tabs from the reconciled visit set means the real payment survives.
    """
    from pathlib import Path

    from remit.pdf_parser import parse_remittances

    fixtures = Path(__file__).parent / "fixtures"
    _, visits = parse_remittances([
        (str(fixtures / "RemitDoc-0000000002.PDF"), "RemitDoc-0000000002.PDF"),
        (str(fixtures / "RemitDoc-0000000003.PDF"), "RemitDoc-0000000003.PDF"),
    ])
    restated = next(v for v in eob_visits(visits)
                    if v.session_date == date(2026, 3, 31))
    assert restated.payment == 104.27          # not zeroed by the duplicate
    assert restated.check_eft == "900000002"   # the paying remit's number


def test_a_duplicate_only_visit_is_never_recorded_silently(employee_sheets):
    """No remit actually paid it, so its `$0.00` is surfaced, never written."""
    from pathlib import Path

    from remit.pdf_parser import parse_remittances

    fixtures = Path(__file__).parent / "fixtures"
    _, visits = parse_remittances([
        (str(fixtures / "RemitDoc-0000000003.PDF"), "RemitDoc-0000000003.PDF"),
    ])
    duplicate_only = [v for v in eob_visits(visits) if v.review_note]
    assert duplicate_only
    for visit in duplicate_only:
        assert "Duplicate only" in visit.review_note

    changes = plan_employee_changes(employee_sheets, duplicate_only, today=STAMP)
    assert changes
    assert not [c for c in changes if c.accepted]


# --- The service-date cutoff ------------------------------------------------

def test_no_cutoff_processes_everything(all_visits):
    assert apply_service_date_cutoff(all_visits, None) == list(all_visits)


def test_a_visit_before_the_cutoff_is_dropped(all_visits):
    kept = apply_service_date_cutoff(all_visits, date(2026, 6, 1))
    assert kept
    assert all(v.service_date >= date(2026, 6, 1) for v in kept)
    assert not [v for v in kept if v.service_date == date(2025, 12, 25)]


def test_the_cutoff_date_itself_is_kept(all_visits):
    """Inclusive: a visit served exactly on the cutoff stays in the run."""
    kept = apply_service_date_cutoff(all_visits, date(2026, 6, 2))
    assert [v for v in kept if v.service_date == date(2026, 6, 2)]


def test_the_cutoff_drops_the_visit_from_the_schedule_too(schedule_bytes, all_visits):
    """Scope is the whole run, so the two consumers cannot disagree."""
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))

    kept = apply_service_date_cutoff(all_visits, date(2026, 4, 1))
    plan = build_plan(kept, rows, today=STAMP)
    planned = {c.visit.service_date for c in plan}
    assert planned
    assert min(planned) >= date(2026, 4, 1)

    # `Castellano` 03/26 is a Fill without the cutoff, and absent with it.
    without = build_plan(all_visits, rows, today=STAMP)
    assert [c for c in without if c.visit.service_date == date(2026, 3, 26)]
    assert not [c for c in plan if c.visit.service_date == date(2026, 3, 26)]


def test_the_cutoff_drops_the_visit_from_the_employees_file_too(
        employee_sheets, all_visits):
    kept = apply_service_date_cutoff(all_visits, date(2026, 6, 1))
    changes = plan_employee_changes(employee_sheets, eob_visits(kept), today=STAMP)

    # Castellano 03/26 fills Ana's tab without a cutoff; with one it is gone.
    assert not [c for c in changes
                if c.session_date == "03/26/2026" and c.action == EMP_ACTION_FILL]
    # ...and the June rows still work.
    assert change_for(changes, "Ravensworth", "06/16/2026",
                      sheet="Ana").action == EMP_ACTION_APPEND


def test_a_cutoff_row_with_no_eob_is_reported_not_filled(employee_sheets, all_visits):
    """A pre-cutoff tab row simply has no match; nothing is written to it."""
    from remit.config import EMP_ACTION_NO_MATCH

    kept = apply_service_date_cutoff(all_visits, date(2026, 6, 1))
    changes = plan_employee_changes(employee_sheets, eob_visits(kept), today=STAMP)
    change = change_for(changes, "Castellano", "03/26/2026", sheet="Ana")
    assert change.action == EMP_ACTION_NO_MATCH
    assert not change.accepted


def test_the_cutoff_never_writes_what_it_dropped(employees_bytes, employee_sheets,
                                                 all_visits):
    kept = apply_service_date_cutoff(all_visits, date(2026, 6, 1))
    changes = plan_employee_changes(employee_sheets, eob_visits(kept), today=STAMP)
    updated, _ = build_updated_employees(employees_bytes, changes)
    row = next(r for r in tab_rows(load_employees_workbook(updated.getvalue()), "Ana")
               if r["Date of Session"] == "03/26/2026")
    assert row["Paid by Ins toAna"] is None


def test_cutoff_label_reads_as_the_preview_shows_it():
    assert cutoff_label(None) == "no cutoff - all service dates processed"
    assert cutoff_label(date(2026, 7, 1)) == "cutoff: on/after 07/01/2026"


def test_an_empty_cutoff_is_the_default_everywhere(all_visits, employee_sheets):
    """Data is never dropped unless the user asks for it."""
    unfiltered = apply_service_date_cutoff(all_visits, None)
    assert len(unfiltered) == len(all_visits)
    changes = plan_employee_changes(employee_sheets, eob_visits(unfiltered),
                                    today=STAMP)
    assert [c for c in changes if c.session_date == "12/25/2025"]


# --- Idempotency and never-overwrite ----------------------------------------

def test_rerun_appends_nothing_new(employees_bytes, changes, visits_for_tabs):
    once, _ = build_updated_employees(employees_bytes, changes)
    payload = once.getvalue()

    reread = read_sheets(load_employees_workbook(payload))
    second = plan_employee_changes(reread, visits_for_tabs, today=STAMP)
    _, stats = build_updated_employees(payload, second)

    assert stats["appended_rows"] == 0
    assert stats["filled_cells"] == 0


def test_existing_values_are_never_overwritten(employees_bytes, changes):
    """`Castellano` 04/09 already records 999.99 in Ana's tab."""
    updated, _ = build_updated_employees(employees_bytes, changes)
    row = next(r for r in tab_rows(load_employees_workbook(updated.getvalue()), "Ana")
               if r["Date of Session"] == "04/09/2026")
    assert row["Paid by Ins toAna"] == 999.99


def test_every_sheet_survives(employees_bytes, changes):
    updated, _ = build_updated_employees(employees_bytes, changes)
    workbook = load_employees_workbook(updated.getvalue())
    assert workbook.sheetnames == ["Ana", "Marcia", "Oxana", "Notes"]
    assert workbook["Notes"]["A1"].value == "Not a provider tab"


def test_declined_changes_write_nothing(employees_bytes, changes):
    for change in changes:
        change.accepted = False
    _, stats = build_updated_employees(employees_bytes, changes)
    assert stats["filled_cells"] == 0
    assert stats["appended_rows"] == 0


# --- The schedule side is unaffected ----------------------------------------

def test_the_schedule_plan_still_works_without_the_employees_workbook(
        schedule_bytes, all_visits):
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))
    assert build_plan(all_visits, rows, today=STAMP)


def test_the_schedule_still_records_every_provider(schedule_bytes, all_visits):
    """Appends being NPI-gated in the tabs changes nothing in the schedule."""
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))
    plan = build_plan(all_visits, rows, today=STAMP)
    updated, _ = build_updated_workbook(schedule_bytes, [c for c in plan if c.accepted])

    written = sheet_rows(get_schedule_sheet(load_schedule_workbook(updated.getvalue())))
    ravensworth = [r for r in written if str(r["Patient"]).startswith("Ravensworth")]
    assert ravensworth
    assert {r["Comment"] for r in ravensworth} >= {"Ana", "Dr. A"}


@pytest.mark.parametrize("name", EMPLOYEE_SHEETS)
def test_every_tab_is_still_read(employee_sheets, name):
    assert name in employee_sheets


def test_eob_visit_is_immutable(visits_for_tabs):
    """A frozen record: nothing downstream can edit a visit in place."""
    with pytest.raises(Exception):
        visits_for_tabs[0].payment = 1.0
    assert isinstance(visits_for_tabs[0], EobVisit)
