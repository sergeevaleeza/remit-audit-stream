"""Reconciling a second (or later) EOB for a visit that is already recorded.

The headline case: the first remit paid `$0.00` and a later one actually pays.
That must not be skipped, must not be silently overwritten, and must never
produce a second row for the same patient + service date.
"""

from __future__ import annotations

import copy as copy_module
from datetime import date

import pytest

from remit.config import (
    ACTION_FILL,
    ACTION_REVIEW,
    ACTION_SKIP,
    ACTION_UPDATE,
    COL_BILLED,
    COL_CHECK_EFT,
    COL_COPAY,
    COL_PAYMENT,
    COL_PROCESSED_ON,
    SHEET_NAME,
)
from remit.excel_updater import (
    build_updated_workbook,
    get_schedule_sheet,
    load_schedule_workbook,
    read_schedule_rows,
    resolve_columns,
)
from remit.matching import (
    ScheduleRow,
    amounts_differ,
    as_number,
    build_plan,
    parse_loose_date,
    plan_change,
    summarize,
)
from remit.pdf_parser import RemitDocument, ServiceLine, Visit, aggregate_visits

from .conftest import find_row, find_visit, sheet_rows

# `Whitfield, Harold` sits on row 8 of the fixture with Payment 0.00 for
# 03/04/2026, and the remit reports a real payment for that visit.
ZERO_ROW = 8
ZERO_PATIENT = "WHITFIELD, HAROLD"
ZERO_DATE = date(2026, 3, 4)
STAMP = date(2026, 9, 10)


def make_visit(patient=ZERO_PATIENT, service_date=ZERO_DATE, payment=130.46,
               copay=33.28, billed=date(2026, 8, 14), cpts=("99214",),
               check="900000002", remit_count=1) -> Visit:
    return Visit(
        patient=patient,
        service_date=service_date,
        npi="1000000001",
        billed_date=billed,
        cpt_codes=list(cpts),
        payment=payment,
        copay=copay,
        check_efts={check},
        remit_count=remit_count,
    )


def row_for(rows, row_num) -> ScheduleRow:
    return next(r for r in rows if r.row_num == row_num)


@pytest.fixture()
def rows(schedule_rows):
    """A mutable copy, so a test can rewrite a cell without leaking."""
    return copy_module.deepcopy(schedule_rows)


# --- The headline case: 0.00 then a real payment ---------------------------

def test_zero_then_payment_is_an_update_not_a_skip(visits, schedule_rows):
    change = plan_change(
        find_visit(visits, ZERO_PATIENT, ZERO_DATE), schedule_rows, today=STAMP
    )
    assert change.action == ACTION_UPDATE
    assert change.action != ACTION_SKIP
    assert change.row_num == ZERO_ROW


def test_zero_then_payment_is_labelled_for_a_human(visits, schedule_rows):
    change = plan_change(
        find_visit(visits, ZERO_PATIENT, ZERO_DATE), schedule_rows, today=STAMP
    )
    assert "was $0.00, now $130.46 (payment received)" in change.update_summary


def test_update_proposes_the_later_remits_values(visits, schedule_rows):
    change = plan_change(
        find_visit(visits, ZERO_PATIENT, ZERO_DATE), schedule_rows, today=STAMP
    )
    assert change.updates[COL_PAYMENT] == 130.46
    assert change.updates[COL_COPAY] == 33.28
    assert change.updates[COL_BILLED] == "08/14/2026"
    assert change.updates[COL_PROCESSED_ON] == "09/10/2026"


def test_update_records_the_previous_values_for_the_preview(visits, schedule_rows):
    change = plan_change(
        find_visit(visits, ZERO_PATIENT, ZERO_DATE), schedule_rows, today=STAMP
    )
    assert change.previous[COL_PAYMENT] == 0
    assert change.previous[COL_BILLED] == "2/32/26"
    assert change.change_display(COL_PAYMENT) == "0 -> 130.46"


def test_applying_the_update_writes_the_new_amount(schedule_bytes, visits, schedule_rows):
    plan = build_plan([find_visit(visits, ZERO_PATIENT, ZERO_DATE)],
                      schedule_rows, today=STAMP)
    updated, stats = build_updated_workbook(schedule_bytes, plan)
    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]

    assert stats["updated_rows"] == 1
    assert worksheet.cell(row=ZERO_ROW, column=5).value == 130.46   # Payment
    assert worksheet.cell(row=ZERO_ROW, column=6).value == 33.28    # Co-pay
    assert worksheet.cell(row=ZERO_ROW, column=4).value == "08/14/2026"  # Billed
    assert worksheet.cell(row=ZERO_ROW, column=12).value == "09/10/2026"  # Processed On


def test_applying_the_update_creates_no_duplicate_row(schedule_bytes, visits, schedule_rows):
    plan = build_plan([find_visit(visits, ZERO_PATIENT, ZERO_DATE)],
                      schedule_rows, today=STAMP)
    updated, stats = build_updated_workbook(schedule_bytes, plan)
    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]

    assert stats["new_rows"] == 0
    assert len(sheet_rows(worksheet)) == len(schedule_rows)
    # find_row raises if more than one row matches this patient + date.
    assert find_row(worksheet, "Whitfield, Harold", ZERO_DATE)["Payment"] == 130.46


def test_check_eft_history_is_appended_not_replaced(rows, schedule_bytes):
    """An earlier remit number stays visible alongside the later one."""
    row = row_for(rows, ZERO_ROW)
    row.check_eft = "900000001"

    change = plan_change(make_visit(check="900000002"), rows, today=STAMP)
    assert change.updates[COL_CHECK_EFT] == "900000001; 900000002"


def test_check_eft_is_not_duplicated_when_already_present(rows):
    row = row_for(rows, ZERO_ROW)
    row.check_eft = "900000002"
    change = plan_change(make_visit(check="900000002"), rows, today=STAMP)
    assert change.updates.get(COL_CHECK_EFT) is None


# --- Identical re-report ----------------------------------------------------

def test_identical_amounts_are_skipped(rows):
    """Same patient, date, Payment and Co-pay -> a true duplicate."""
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay = 130.46, 33.28

    change = plan_change(make_visit(), rows, today=STAMP)
    assert change.action == ACTION_SKIP
    assert change.updates == {}
    assert not change.accepted


def test_reuploading_the_same_remit_is_a_skip(rows):
    """Identical values and the same CHECK/EFT number change nothing."""
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay, row.check_eft = 130.46, 33.28, "900000002"

    change = plan_change(make_visit(check="900000002"), rows, today=STAMP)
    assert change.action == ACTION_SKIP
    assert change.updates == {}


def test_a_formula_copay_matching_on_payment_still_skips(rows):
    """Idempotency must survive a Co-pay that openpyxl returns as a formula."""
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay = 130.46, "=23.52+9.76"

    change = plan_change(make_visit(), rows, today=STAMP)
    assert change.action == ACTION_SKIP


def test_cent_level_differences_are_real_differences(rows):
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay = 130.45, 33.28
    change = plan_change(make_visit(), rows, today=STAMP)
    assert change.action == ACTION_UPDATE


def test_sub_cent_noise_is_not_a_difference(rows):
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay = 130.460001, 33.28
    change = plan_change(make_visit(), rows, today=STAMP)
    assert change.action == ACTION_SKIP


# --- Out-of-order remits ----------------------------------------------------

def test_older_remit_does_not_downgrade_a_newer_value(rows):
    """The recorded value came from a newer remit, so nothing is changed."""
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay, row.billed = 200.00, 40.00, "08/14/2026"

    change = plan_change(make_visit(billed=date(2026, 7, 1)), rows, today=STAMP)
    assert change.action == ACTION_REVIEW
    assert change.updates == {}
    assert not change.accepted
    assert any("Out-of-order" in r for r in change.review_reasons)


def test_out_of_order_remit_writes_nothing_on_apply(schedule_bytes, rows):
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay, row.billed = 200.00, 40.00, "08/14/2026"

    change = plan_change(make_visit(billed=date(2026, 7, 1)), rows, today=STAMP)
    change.accepted = True  # even if ticked, there is nothing to write
    _, stats = build_updated_workbook(schedule_bytes, [change])
    assert stats["updated_cells"] == 0


def test_newer_remit_on_a_dated_row_is_an_update(rows):
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay, row.billed = 100.00, 20.00, "07/01/2026"

    change = plan_change(make_visit(billed=date(2026, 8, 14)), rows, today=STAMP)
    assert change.action == ACTION_UPDATE


def test_same_dated_remit_with_different_amounts_updates(rows):
    row = row_for(rows, ZERO_ROW)
    row.payment, row.copay, row.billed = 100.00, 20.00, "08/14/2026"

    change = plan_change(make_visit(billed=date(2026, 8, 14)), rows, today=STAMP)
    assert change.action == ACTION_UPDATE


def test_unusable_billed_date_updates_but_says_so(rows):
    """`2/32/26` cannot order the remits, so the update is annotated."""
    row = row_for(rows, ZERO_ROW)
    assert parse_loose_date(row.billed) is None

    change = plan_change(make_visit(), rows, today=STAMP)
    assert change.action == ACTION_UPDATE
    assert any("not a usable date" in note for note in change.update_notes)


# --- Unreadable recorded values --------------------------------------------

def test_formula_payment_is_never_silently_overwritten(rows):
    row = row_for(rows, ZERO_ROW)
    row.payment = "=100+30.46"

    change = plan_change(make_visit(), rows, today=STAMP)
    assert change.action == ACTION_REVIEW
    assert change.updates == {}
    assert any("not a plain number" in r for r in change.review_reasons)


# --- CPT changes ------------------------------------------------------------

def test_changed_cpt_set_needs_review_but_still_targets_one_row(rows):
    row = row_for(rows, ZERO_ROW)
    row.cpt = "99214"

    change = plan_change(make_visit(cpts=("99213", "90833")), rows, today=STAMP)
    assert change.action == ACTION_REVIEW
    assert change.row_num == ZERO_ROW
    assert any("CPT set changed" in r for r in change.review_reasons)


def test_accepting_a_cpt_changed_review_updates_that_row(schedule_bytes, rows):
    """No duplicate row: ticking it restates the row it matched."""
    row = row_for(rows, ZERO_ROW)
    row.cpt = "99214"

    change = plan_change(make_visit(cpts=("99213", "90833")), rows, today=STAMP)
    change.accepted = True
    assert change.effective_action == ACTION_UPDATE

    updated, stats = build_updated_workbook(schedule_bytes, [change])
    assert stats["new_rows"] == 0
    assert stats["updated_rows"] == 1
    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]
    assert worksheet.cell(row=ZERO_ROW, column=5).value == 130.46


# --- Several remits in one upload ------------------------------------------

def _document(name, billed, check, payment_lines):
    return RemitDocument(
        filename=name,
        billed_date=billed,
        check_eft=check,
        claim_count=1,
        service_lines=[
            ServiceLine(patient=ZERO_PATIENT, npi="1000000001",
                        service_date=ZERO_DATE, proc=proc, coins=coins,
                        prov_pd=paid, source_file=name, check_eft=check)
            for proc, coins, paid in payment_lines
        ],
    )


def test_two_remits_in_one_run_reconcile_to_the_latest():
    """Amounts are replaced by the newest remit, never summed."""
    older = _document("older.pdf", date(2026, 7, 1), "900000001",
                      [("99214", 0.00, 0.00)])
    newer = _document("newer.pdf", date(2026, 8, 14), "900000002",
                      [("99214", 33.28, 130.46)])

    visit = aggregate_visits([older, newer])[0]
    assert visit.payment == 130.46      # not 130.46 + 0.00 by luck -- replaced
    assert visit.copay == 33.28
    assert visit.billed_date == date(2026, 8, 14)
    assert visit.remit_count == 2
    assert visit.restated


def test_latest_wins_regardless_of_upload_order():
    older = _document("older.pdf", date(2026, 7, 1), "900000001",
                      [("99214", 10.00, 50.00)])
    newer = _document("newer.pdf", date(2026, 8, 14), "900000002",
                      [("99214", 33.28, 130.46)])

    forward = aggregate_visits([older, newer])[0]
    backward = aggregate_visits([newer, older])[0]
    assert forward.payment == backward.payment == 130.46
    assert forward.billed_date == backward.billed_date == date(2026, 8, 14)


def test_amounts_are_never_summed_across_remits():
    older = _document("older.pdf", date(2026, 7, 1), "900000001",
                      [("99214", 10.00, 50.00)])
    newer = _document("newer.pdf", date(2026, 8, 14), "900000002",
                      [("99214", 33.28, 130.46)])
    visit = aggregate_visits([older, newer])[0]
    assert visit.payment != 180.46
    assert visit.superseded_payments == [50.00]


def test_lines_within_one_remit_are_still_summed():
    """Within a single remittance the E/M and add-on lines add up."""
    single = _document("one.pdf", date(2026, 8, 14), "900000002",
                       [("99213", 23.52, 92.18), ("90833", 18.92, 74.19)])
    visit = aggregate_visits([single])[0]
    assert visit.payment == 166.37
    assert visit.copay == 42.44
    assert visit.remit_count == 1


def test_every_contributing_check_is_kept_for_the_audit_trail():
    older = _document("older.pdf", date(2026, 7, 1), "900000001",
                      [("99214", 0.00, 0.00)])
    newer = _document("newer.pdf", date(2026, 8, 14), "900000002",
                      [("99214", 33.28, 130.46)])
    visit = aggregate_visits([older, newer])[0]
    assert visit.check_efts == {"900000001", "900000002"}


def test_multi_remit_visit_notes_how_many_contributed(rows):
    change = plan_change(make_visit(remit_count=2), rows, today=STAMP)
    assert change.action == ACTION_UPDATE
    assert any("2 remits in this upload" in note for note in change.update_notes)


# --- No duplicate rows, ever ------------------------------------------------

def test_no_plan_ever_produces_two_rows_for_one_patient_and_date(
        schedule_bytes, visits, schedule_rows):
    plan = build_plan(visits, schedule_rows, today=STAMP)
    updated, _ = build_updated_workbook(schedule_bytes, plan)
    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]

    seen = set()
    for record in sheet_rows(worksheet):
        from remit.matching import split_name
        key = (split_name(str(record["Patient"])), parse_loose_date(record["Data"]))
        assert key not in seen, f"duplicate row for {record['Patient']} {record['Data']}"
        seen.add(key)


def test_an_updated_visit_is_not_also_appended(schedule_bytes, visits, schedule_rows):
    plan = build_plan(visits, schedule_rows, today=STAMP)
    updates = [c for c in plan if c.action == ACTION_UPDATE]
    assert updates
    for change in updates:
        assert change.row_num is not None
        assert change.anchor_row is not None  # the patient exists
        assert change.effective_action == ACTION_UPDATE


# --- Guardrails -------------------------------------------------------------

def test_updates_are_not_written_unless_accepted(schedule_bytes, visits, schedule_rows):
    plan = build_plan([find_visit(visits, ZERO_PATIENT, ZERO_DATE)],
                      schedule_rows, today=STAMP)
    for change in plan:
        change.accepted = False

    updated, stats = build_updated_workbook(schedule_bytes, plan)
    assert stats["updated_rows"] == 0
    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]
    assert worksheet.cell(row=ZERO_ROW, column=5).value == 0


def test_a_fill_is_still_a_fill_not_an_update(visits, schedule_rows):
    """A blank Payment stays on the never-overwrite fill path."""
    change = plan_change(
        find_visit(visits, "CASTELLANO, MIGUEL", date(2026, 3, 26)),
        schedule_rows, today=STAMP,
    )
    assert change.action == ACTION_FILL
    assert change.updates == {}


def test_applying_an_update_twice_is_idempotent(schedule_bytes, visits, schedule_rows):
    """Once restated, the same remit agrees with the row and is skipped."""
    first = build_plan([find_visit(visits, ZERO_PATIENT, ZERO_DATE)],
                       schedule_rows, today=STAMP)
    once, _ = build_updated_workbook(schedule_bytes, first)

    worksheet = get_schedule_sheet(load_schedule_workbook(once.getvalue()))
    rows_after = read_schedule_rows(worksheet, resolve_columns(worksheet))

    second = build_plan([find_visit(visits, ZERO_PATIENT, ZERO_DATE)],
                        rows_after, today=STAMP)
    assert second[0].action == ACTION_SKIP

    _, stats = build_updated_workbook(once.getvalue(), second)
    assert stats["updated_cells"] == 0
    assert stats["filled_cells"] == 0


def test_summary_counts_updates_separately(visits, schedule_rows):
    counts = summarize(build_plan(visits, schedule_rows, today=STAMP))
    assert counts["update"] == 1
    assert (counts["fill"] + counts["new"] + counts["skip"]
            + counts["update"] + counts["review"]) == counts["visits"]


# --- Numeric helpers --------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [(0, 0.0), (0.0, 0.0), (130.46, 130.46), ("130.46", 130.46),
     ("$130.46", 130.46), ("1,130.46", 1130.46),
     ("=23.52+18.92", None), ("No Billing", None), (None, None), ("", None)],
)
def test_as_number(value, expected):
    assert as_number(value) == expected


def test_amounts_differ_ignores_unreadable_cells():
    """An unreadable cell is handled explicitly, never counted as a diff."""
    assert not amounts_differ("=1+2", 130.46)
    assert amounts_differ(0, 130.46)
    assert not amounts_differ(130.46, 130.46)
