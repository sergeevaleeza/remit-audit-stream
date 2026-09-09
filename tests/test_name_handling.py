"""Generational suffixes (Change 1) and title casing (Change 2)."""

from __future__ import annotations

from datetime import date

import pytest

from remit.config import ACTION_FILL, ACTION_NEW, SHEET_NAME
from remit.excel_updater import build_updated_workbook, load_schedule_workbook
from remit.matching import (
    build_plan,
    find_legacy_duplicate_rows,
    name_score,
    new_row_values,
    plan_change,
    split_name,
    strip_generational_suffix,
    to_title_name,
)

from .conftest import find_visit
from .test_app_and_edges import make_row, make_visit


# --- Change 1: generational suffixes ---------------------------------------

@pytest.mark.parametrize(
    "surname, expected",
    [
        ("marchetti jr", "marchetti"),
        ("marchetti jr.", "marchetti"),
        ("MARCHETTI JR", "marchetti"),
        ("smith sr", "smith"),
        ("smith ii", "smith"),
        ("smith iii", "smith"),
        ("smith iv", "smith"),
        ("smith v", "smith"),
        ("van der berg jr", "van der berg"),
        ("marchetti", "marchetti"),
    ],
)
def test_strip_generational_suffix(surname, expected):
    assert strip_generational_suffix(surname) == expected


def test_a_bare_suffix_is_not_stripped_away():
    """A surname that is only `V` must survive -- never strip the last token."""
    assert strip_generational_suffix("v") == "v"
    assert split_name("V, John")[0] == "v"


@pytest.mark.parametrize(
    "remit_name, sheet_name",
    [
        ("MARCHETTI, DEAN", "Marchetti Jr, Dean"),
        ("MARCHETTI, DEAN", "Marchetti Jr., Dean"),
        ("SMITH, JOHN", "Smith III, John"),
        ("SMITH, JOHN", "Smith Sr, John"),
        # The suffix must not block Slavic harmonisation: without stripping
        # first, `bystritskaya jr` never reduces to `bystritsky`.
        ("BYSTRITSKAYA, ANNA", "Bystritskaya Jr, Anna"),
        ("IVANOV, MARIA", "Ivanova Jr, Maria"),
    ],
)
def test_suffix_names_match_the_plain_remit_name(remit_name, sheet_name):
    assert name_score(remit_name, sheet_name) == 100.0


def test_suffix_stripping_does_not_match_different_people():
    """Stripping a suffix must not collapse two genuinely different names."""
    assert name_score("MARCHETTI, DEAN", "Castellano Jr, Miguel") < 80.0
    assert name_score("SMITH, JOHN", "Smith Jr, Michael") < 92.0


def test_suffix_row_is_filled_not_duplicated():
    """The reported bug: `Marchetti Jr, Dean` must Fill, never create a New row."""
    rows = [make_row(3, "Marchetti Jr, Dean", date(2026, 3, 9))]
    change = plan_change(make_visit(patient="MARCHETTI, DEAN"), rows)
    assert change.action == ACTION_FILL
    assert change.row_num == 3
    assert change.action != ACTION_NEW


def test_suffix_match_still_requires_an_exact_date(visits):
    """Suffix stripping must not let a different date match."""
    rows = [make_row(3, "Marchetti Jr, Dean", date(2026, 1, 1))]
    change = plan_change(make_visit(patient="MARCHETTI, DEAN",
                                    service_date=date(2026, 3, 9)), rows)
    assert change.action == ACTION_NEW


def test_suffix_row_fills_in_the_real_fixture(schedule_bytes, visits, schedule_rows):
    """End to end: the fixture's `Bystritskaya Jr, Anna` row gets filled."""
    visit = find_visit(visits, "BYSTRITSKAYA, ANNA", date(2026, 4, 14))
    change = plan_change(visit, schedule_rows)
    assert change.action == ACTION_FILL

    row = next(r for r in schedule_rows if str(r.patient).startswith("Bystritskaya"))
    updated, stats = build_updated_workbook(schedule_bytes, [change])
    assert stats["appended_rows"] == 0

    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]
    assert worksheet.cell(row=row.row_num, column=5).value == visit.payment
    # The stored name is never rewritten.
    assert worksheet.cell(row=row.row_num, column=1).value == "Bystritskaya Jr, Anna"


def test_legacy_duplicate_rows_are_reported():
    """An old all-caps append next to a suffix row is flagged, not deleted."""
    rows = [
        make_row(3, "Marchetti Jr, Dean", date(2026, 3, 9), payment=100.0),
        make_row(4, "MARCHETTI, DEAN", date(2026, 3, 9), payment=100.0),
    ]
    pairs = find_legacy_duplicate_rows(rows)
    assert len(pairs) == 1
    duplicate, original = pairs[0]
    assert duplicate.row_num == 4
    assert original.row_num == 3


def test_no_false_duplicates_in_a_clean_sheet(schedule_rows):
    assert find_legacy_duplicate_rows(schedule_rows) == []


# --- Change 2: title casing ------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("MARCHETTI, DEAN", "Marchetti, Dean"),
        ("CURRAN, THOMAS M", "Curran, Thomas M"),
        ("MARCHETTI JR, DEAN", "Marchetti Jr, Dean"),
        ("MARCHETTI SR, DEAN", "Marchetti Sr, Dean"),
        ("SMITH III, JOHN", "Smith III, John"),
        ("SMITH II, JOHN", "Smith II, John"),
        ("SMITH IV, JOHN", "Smith IV, John"),
        ("SMITH-JONES, ANNA", "Smith-Jones, Anna"),
        ("O'BRIEN, SEAN", "O'Brien, Sean"),
        ("MCDONALD, IAN", "McDonald, Ian"),
        ("D'ANGELO, MARIA", "D'Angelo, Maria"),
        ("VAN DER BERG, PIET", "Van Der Berg, Piet"),
        ("PETROSSIAN, KNARIK S", "Petrossian, Knarik S"),
        ("already, titled", "Already, Titled"),
        ("SMITH,  JOHN ", "Smith, John"),
        ("MADONNA", "Madonna"),
    ],
)
def test_to_title_name(raw, expected):
    assert to_title_name(raw) == expected


def test_to_title_name_handles_empty_values():
    assert to_title_name("") == ""
    assert to_title_name("   ") == ""
    assert to_title_name(None) == ""


def test_title_casing_is_idempotent():
    once = to_title_name("MARCHETTI JR, DEAN")
    assert to_title_name(once) == once


def test_new_rows_use_title_case(visits):
    visit = find_visit(visits, "WHITCOMBE, ROSALIND", date(2026, 5, 6))
    assert new_row_values(visit)["Patient"] == "Whitcombe, Rosalind"


def test_existing_names_are_never_rewritten(schedule_bytes, visits, schedule_rows):
    """Only new rows are title-cased; the sheet's own spellings are untouched."""
    plan = build_plan(visits, schedule_rows)
    updated, _ = build_updated_workbook(schedule_bytes, plan)
    worksheet = load_schedule_workbook(updated.getvalue())[SHEET_NAME]

    for row in schedule_rows:
        assert worksheet.cell(row=row.row_num, column=1).value == row.patient
