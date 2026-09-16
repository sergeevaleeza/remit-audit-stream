"""The optional EOB-date cutoff: skip whole remits dated before a date.

The unit is the **remittance**, not the visit, and the date compared is the
remit's own header `DATE:` -- the EOB/check date -- never the service date.
That distinction is the whole point of these tests. A remit settles claims
long after the fact: the synthetic paying remit is dated 07/20/2026 and pays
sessions going back to 01/15/2026. Filtering by service date would tear such a
remit in half and drop payments the user is actively reconciling; filtering by
the EOB's own date lets a clinic archive by batch instead -- "everything up to
the June check is reconciled, start from July".

The four synthetic remittances carry four distinct header dates, and their
service dates deliberately straddle them:

| Fixture | header `DATE:` | service dates |
|---|---|---|
| `RemitDoc-0000000002.PDF` | 07/20/2026 | 01/15 – 06/16/2026 |
| `RemitDoc-0000000001.PDF` | 08/14/2026 | 12/25/2025 – 05/26/2026 |
| `RemitDoc-0000000003.PDF` | 08/27/2026 | 01/15 – 07/07/2026 (OA-18 duplicates) |
| `RemitDoc-0000000004.PDF` | 09/04/2026 | 06/02 – 06/24/2026 |

Every patient here is fictional (see `tests/fixtures/README.md`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from remit.employees import (
    build_updated_employees,
    eob_visits,
    load_employees_workbook,
    plan_employee_changes,
)
from remit.excel_updater import (
    get_schedule_sheet,
    load_schedule_workbook,
    read_schedule_rows,
    resolve_columns,
)
from remit.matching import build_plan
from remit.pdf_parser import (
    RemitDocument,
    cutoff_label,
    parse_document,
    parse_remittances,
    split_by_eob_cutoff,
)

FIXTURES = Path(__file__).parent / "fixtures"

PAYING = FIXTURES / "RemitDoc-0000000002.PDF"       # DATE: 07/20/2026
ROSTER = FIXTURES / "RemitDoc-0000000001.PDF"       # DATE: 08/14/2026
DUPLICATES = FIXTURES / "RemitDoc-0000000003.PDF"   # DATE: 08/27/2026
ASSOCIATES = FIXTURES / "RemitDoc-0000000004.PDF"   # DATE: 09/04/2026

ALL_FIXTURES = (PAYING, ROSTER, DUPLICATES, ASSOCIATES)

STAMP = date(2026, 9, 10)


def load(*paths, cutoff=None):
    """(documents, visits) for these fixtures under an optional cutoff."""
    return parse_remittances([(str(p), p.name) for p in paths], eob_cutoff=cutoff)


@pytest.fixture(scope="module")
def documents():
    return [parse_document(str(p), p.name) for p in ALL_FIXTURES]


def service_dates(visits):
    return {v.service_date for v in visits}


def sources(visits):
    return {name for v in visits for name in v.source_files}


# --- The header DATE: is what the cutoff reads ------------------------------

def test_the_fixtures_have_the_header_dates_these_tests_assume(documents):
    assert {d.filename: d.billed_date for d in documents} == {
        PAYING.name: date(2026, 7, 20),
        ROSTER.name: date(2026, 8, 14),
        DUPLICATES.name: date(2026, 8, 27),
        ASSOCIATES.name: date(2026, 9, 4),
    }


def test_a_remit_can_pay_sessions_far_older_than_itself(documents):
    """The premise: an EOB date and a service date are not interchangeable."""
    paying = next(d for d in documents if d.filename == PAYING.name)
    oldest = min(line.service_date for line in paying.service_lines)
    assert paying.billed_date == date(2026, 7, 20)
    assert oldest == date(2026, 1, 15)
    assert (paying.billed_date - oldest).days > 180


# --- split_by_eob_cutoff ----------------------------------------------------

def test_no_cutoff_processes_every_uploaded_remit(documents):
    processed, skipped = split_by_eob_cutoff(documents, None)
    assert processed == documents
    assert skipped == []


def test_a_remit_dated_before_the_cutoff_is_skipped(documents):
    processed, skipped = split_by_eob_cutoff(documents, date(2026, 8, 14))
    assert [d.filename for d in skipped] == [PAYING.name]
    assert [d.filename for d in processed] == [
        ROSTER.name, DUPLICATES.name, ASSOCIATES.name
    ]


def test_a_remit_dated_on_the_cutoff_is_processed(documents):
    """Inclusive, so the boundary day is never silently lost."""
    processed, skipped = split_by_eob_cutoff(documents, date(2026, 7, 20))
    assert skipped == []
    assert PAYING.name in {d.filename for d in processed}


def test_a_cutoff_after_everything_skips_everything(documents):
    processed, skipped = split_by_eob_cutoff(documents, date(2027, 1, 1))
    assert processed == []
    assert len(skipped) == len(documents)


def test_a_remit_with_no_readable_header_date_is_kept():
    """It cannot be placed, and dropping payments is the worse failure."""
    undated = RemitDocument(filename="mystery.pdf", billed_date=None,
                            check_eft="900000099", claim_count=0,
                            service_lines=[])
    processed, skipped = split_by_eob_cutoff([undated], date(2026, 8, 1))
    assert processed == [undated]
    assert skipped == []


# --- A skipped remit contributes nothing anywhere ---------------------------

def test_a_skipped_remit_contributes_no_visits():
    _, visits = load(PAYING, ASSOCIATES, cutoff=date(2026, 8, 1))
    assert visits
    assert sources(visits) == {ASSOCIATES.name}


def test_a_skipped_remits_recent_service_dates_go_too():
    """`RemitDoc-...02` reaches 06/16/2026 — newer than sessions that survive.

    Proof the filter is on the EOB date: the skipped remit's *newest* service
    date is more recent than plenty of visits that are kept.
    """
    _, visits = load(PAYING, ROSTER, cutoff=date(2026, 8, 1))
    kept = service_dates(visits)
    assert date(2026, 6, 16) not in kept          # dropped, though recent
    assert date(2025, 12, 25) in kept             # kept, though ancient


def test_an_included_remit_keeps_its_oldest_service_dates():
    """A remit on the cutoff is processed whole, however old its sessions."""
    _, visits = load(PAYING, cutoff=date(2026, 7, 20))
    kept = service_dates(visits)
    assert date(2026, 1, 15) in kept
    assert date(2026, 2, 10) in kept


def test_an_empty_cutoff_processes_everything():
    _, with_cutoff = load(*ALL_FIXTURES, cutoff=None)
    _, without = load(*ALL_FIXTURES)
    assert service_dates(with_cutoff) == service_dates(without)
    assert sources(without) == {p.name for p in ALL_FIXTURES}


def test_documents_still_lists_every_upload_so_the_ui_can_say_what_it_skipped():
    documents, visits = load(PAYING, ASSOCIATES, cutoff=date(2026, 8, 1))
    assert [d.filename for d in documents] == [PAYING.name, ASSOCIATES.name]
    assert sources(visits) == {ASSOCIATES.name}


# --- The drop happens before reconciliation ---------------------------------

def test_a_skipped_remit_supplies_no_reconciliation_occurrence():
    """The strongest form of "before reconciliation".

    `RemitDoc-...03` re-adjudicates `RemitDoc-...02`'s visits as OA-18
    duplicates. With both in the run the paying remit wins and the amounts
    survive. Skip the paying remit by date and only the duplicate remains, so
    the visits must come back flagged *duplicate only* -- which can only happen
    if the excluded remit contributed no occurrence at all.
    """
    _, both = load(PAYING, DUPLICATES)
    restated = next(v for v in both if v.service_date == date(2026, 3, 31))
    assert restated.payment == 104.27
    assert not restated.duplicate_only

    _, duplicate_only = load(PAYING, DUPLICATES, cutoff=date(2026, 8, 1))
    orphaned = next(v for v in duplicate_only if v.service_date == date(2026, 3, 31))
    assert orphaned.duplicate_only
    assert orphaned.payment == 0.00
    assert orphaned.authoritative_eft is None


def test_a_skipped_remits_eft_never_reaches_the_audit_trail():
    _, visits = load(PAYING, DUPLICATES, cutoff=date(2026, 8, 1))
    for visit in visits:
        assert "900000002" not in visit.check_efts


def test_a_cutoff_that_keeps_both_remits_reconciles_as_normal():
    """A cutoff only ever excludes *older* remits, never a newer one.

    Set early enough to keep both, reconciliation is untouched: the OA-18
    duplicate is still recognised and the paying remit still wins.
    """
    _, visits = load(PAYING, DUPLICATES, cutoff=date(2026, 7, 1))
    restated = next(v for v in visits if v.service_date == date(2026, 3, 31))
    assert restated.remit_count == 2
    assert restated.payment == 104.27
    assert restated.authoritative_eft == "900000002"
    assert restated.ignored_duplicate_efts == {"900000003"}


# --- Scope is the whole run: schedule and employees -------------------------

def test_the_cutoff_drops_the_visit_from_the_schedule(schedule_bytes):
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))

    _, everything = load(ROSTER, ASSOCIATES)
    _, after_cutoff = load(ROSTER, ASSOCIATES, cutoff=date(2026, 9, 1))

    full = build_plan(everything, rows, today=STAMP)
    trimmed = build_plan(after_cutoff, rows, today=STAMP)

    # `Castellano` 03/26 is a Fill from the roster remit, which is now skipped.
    assert [c for c in full if c.visit.service_date == date(2026, 3, 26)]
    assert not [c for c in trimmed if c.visit.service_date == date(2026, 3, 26)]
    assert trimmed  # the associates remit still plans normally


def test_the_cutoff_drops_the_visit_from_the_employees_file(employee_sheets):
    from remit.config import EMP_ACTION_FILL

    _, after_cutoff = load(ROSTER, ASSOCIATES, cutoff=date(2026, 9, 1))
    changes = plan_employee_changes(employee_sheets, eob_visits(after_cutoff),
                                    today=STAMP)

    assert not [c for c in changes
                if c.session_date == "03/26/2026" and c.action == EMP_ACTION_FILL]
    # ...and the surviving remit still fills and appends.
    assert [c for c in changes
            if c.session_date == "06/16/2026" and c.appends]


def test_both_consumers_see_exactly_the_same_visit_set(schedule_bytes,
                                                       employee_sheets):
    """One filter, applied once, ahead of both."""
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    rows = read_schedule_rows(worksheet, resolve_columns(worksheet))

    _, visits = load(*ALL_FIXTURES, cutoff=date(2026, 8, 20))
    plan = build_plan(visits, rows, today=STAMP)
    changes = plan_employee_changes(employee_sheets, eob_visits(visits),
                                    today=STAMP)

    scheduled = {(c.visit.patient.lower(), c.visit.service_date) for c in plan}
    from remit.matching import parse_loose_date
    employed = {
        (c.patient.lower(), parse_loose_date(c.session_date))
        for c in changes if c.npi
    }
    assert employed <= scheduled


def test_nothing_from_a_skipped_remit_is_written_to_the_employees_file(
        employees_bytes, employee_sheets):
    _, visits = load(ROSTER, ASSOCIATES, cutoff=date(2026, 9, 1))
    changes = plan_employee_changes(employee_sheets, eob_visits(visits),
                                    today=STAMP)
    updated, _ = build_updated_employees(employees_bytes, changes)
    workbook = load_employees_workbook(updated.getvalue())

    worksheet = workbook["Ana"]
    headers = {worksheet.cell(row=1, column=c).value: c
               for c in range(1, worksheet.max_column + 1)}
    for row in range(2, worksheet.max_row + 1):
        if worksheet.cell(row=row, column=headers["Date of Session"]).value == "03/26/2026":
            assert worksheet.cell(row=row, column=headers["Paid by Ins toAna"]).value is None
            break
    else:
        raise AssertionError("Castellano 03/26 row missing from Ana's tab")


# --- The label --------------------------------------------------------------

def test_cutoff_label_names_the_eob_date_not_the_service_date():
    assert cutoff_label(None) == "no cutoff - every uploaded remit processed"
    assert cutoff_label(date(2026, 7, 1)) == "cutoff: EOBs on/after 07/01/2026"
