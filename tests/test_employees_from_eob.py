"""The employees file is populated from the EOBs, not from the schedule.

The employees tabs are filled directly from the reconciled EOB visits, rather
than by reading values back out of the updated schedule. The schedule and the
employees workbook are independent consumers of one visit set.

Appends are gated on the EOB's **PERF PROV NPI**: a visit reaches Ana's or
Oxana's tab only if she performed it. Marcia is fill-only, because her sessions
bill incident-to under the supervising physician and the remit can never prove
one is hers.

The EOB-date cutoff that scopes a whole run lives in `test_eob_cutoff.py`.

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
from remit.matching import build_plan

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


# --- Single-source NPI mapping (regression) ---------------------------------
#
# The tab gate once read a *second*, separately-configured map. Whoever filled
# in the real NPIs for the `Comment` column had no reason to know a parallel
# map also needed them, so the gate kept comparing against placeholders and
# every real associate visit was reported "not that tab's provider" and swept
# into the no-tab bucket. These pin the two paths to one map.

#: A deployment-shaped mapping: a supervising physician who names no tab, plus
#: two associates whose Comment text IS their employees-tab name. The NPIs are
#: fictional (the real ones are never committed -- see SECURITY.md).
REAL_SHAPED_MAP = {
    "1000009001": "Dr. Levinson",   # supervising physician -- names no tab
    "1000009002": "Oxana",
    "1000009003": "Ana",
}


def _remap(monkeypatch, mapping):
    """Point the one NPI map at `mapping`, everywhere that reads it."""
    from remit import config, employees

    monkeypatch.setattr(config, "NPI_TO_DOCTOR", mapping)
    employee_npi = config._resolve_employee_npi()
    for module in (config, employees):
        monkeypatch.setattr(module, "EMPLOYEE_NPI", employee_npi, raising=False)
    appends = tuple(n for n in EMPLOYEE_SHEETS if n in employee_npi)
    for module in (config, employees):
        monkeypatch.setattr(module, "EMPLOYEE_APPEND_SHEETS", appends, raising=False)
    return employee_npi


def test_the_tab_gate_is_derived_from_the_comment_map(monkeypatch):
    """One map, inverted -- not a second one that can be left unconfigured."""
    from remit.config import _resolve_employee_npi

    monkeypatch.setattr("remit.config.NPI_TO_DOCTOR", REAL_SHAPED_MAP)
    assert _resolve_employee_npi() == {"Ana": "1000009003", "Oxana": "1000009002"}


def test_a_physician_entry_names_no_tab(monkeypatch):
    """Levinson bills Marcia's sessions incident-to; that owns no tab."""
    from remit.config import _resolve_employee_npi

    monkeypatch.setattr("remit.config.NPI_TO_DOCTOR", REAL_SHAPED_MAP)
    resolved = _resolve_employee_npi()
    assert "Marcia" not in resolved
    assert "1000009001" not in resolved.values()


def test_changing_the_map_moves_both_paths_together(monkeypatch):
    """The regression: the two must never be able to disagree."""
    from remit.config import _resolve_employee_npi
    from remit.pdf_parser import Visit

    swapped = {"1000009003": "Oxana", "1000009002": "Ana"}
    monkeypatch.setattr("remit.config.NPI_TO_DOCTOR", swapped)
    monkeypatch.setattr("remit.pdf_parser.NPI_TO_DOCTOR", swapped)

    # The `Comment` path...
    visit = Visit(patient="X, Y", service_date=date(2026, 6, 1),
                  npi="1000009003", billed_date=date(2026, 6, 30))
    assert visit.doctor == "Oxana"
    # ...and the tab gate agree, because they are the same map.
    assert _resolve_employee_npi()["Oxana"] == "1000009003"


def test_tab_names_match_case_and_space_insensitively(monkeypatch):
    from remit.config import _resolve_employee_npi

    monkeypatch.setattr("remit.config.NPI_TO_DOCTOR",
                        {"1000009003": "  ana ", "1000009002": "OXANA"})
    assert _resolve_employee_npi() == {"Ana": "1000009003", "Oxana": "1000009002"}


def test_an_unconfigured_map_leaves_every_tab_fill_only(monkeypatch):
    """Safe failure: the app can fail to add a row, never add someone else's."""
    from remit.config import _resolve_employee_npi

    monkeypatch.setattr("remit.config.NPI_TO_DOCTOR", {"1000009001": "Dr. Levinson"})
    assert _resolve_employee_npi() == {}


def test_there_is_no_second_configurable_npi_source(monkeypatch):
    """The invariant. `EMPLOYEE_NPI` is always the inversion of the one map.

    Nothing may reintroduce a separately-configured tab->NPI map: an operator
    who sets the real NPIs once must not have to know about a second place.
    """
    from remit import config

    def boom(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the tab gate consulted a second config source")

    monkeypatch.setattr(config, "_load_local_map", boom)
    monkeypatch.setattr(config, "_load_secrets_map", boom)
    monkeypatch.setattr(config, "NPI_TO_DOCTOR", REAL_SHAPED_MAP)

    assert config._resolve_employee_npi() == {
        "Ana": "1000009003", "Oxana": "1000009002",
    }
    assert not hasattr(config, "_SYNTHETIC_EMPLOYEE_NPI")


def test_a_supervising_npi_fills_an_associate_row_without_flagging(changes):
    """`Castellano` 03/26 is Ana's, billed incident-to. Normal, not a conflict."""
    change = change_for(changes, "Castellano", "03/26/2026", sheet="Ana")
    assert change.npi == SUPERVISING_NPI
    assert change.action == EMP_ACTION_FILL
    assert not change.needs_review
    assert change.accepted


def test_an_already_filled_row_is_skipped_not_flagged(employee_sheets,
                                                      visits_for_tabs):
    """A row with nothing blank left needs no write and is not a problem."""
    from remit.config import EMP_ACTION_NOTHING

    first = plan_employee_changes(employee_sheets, visits_for_tabs, today=STAMP)
    filled = change_for(first, "Ravensworth", "06/02/2026", sheet="Ana")
    assert filled.action == EMP_ACTION_FILL

    # Apply, re-read, re-plan: the same row now has nothing left to write.
    from pathlib import Path
    payload = (Path(__file__).parent / "fixtures"
               / "AMSMC_employees_sample.xlsx").read_bytes()
    updated, _ = build_updated_employees(payload, first)
    second = plan_employee_changes(read_sheets(load_employees_workbook(
        updated.getvalue())), visits_for_tabs, today=STAMP)

    again = change_for(second, "Ravensworth", "06/02/2026", sheet="Ana")
    assert again.action == EMP_ACTION_NOTHING
    assert not again.needs_review
    assert not again.fills
