"""The 835 database as the single remittance source.

Payments come from `master_remittance_records` instead of parsed PDFs. What
changes is the *source*; everything the source feeds -- per-visit aggregation,
the OA-18 duplicate rule, never-overwrite, row placement, the audit columns,
the EOB-date cutoff -- is the same code and is asserted to still hold here.

What is genuinely new is two-payer routing:

* a Noridian visit is Medicare, and its money is `Payment` / `Co-pay` on
  `2026 Medicare`;
* an HPSM visit is normally primary and belongs on `2026 Medical`;
* but an HPSM visit carrying **CARC 23** is a crossover -- HPSM settling what
  Medicare left the patient -- so it is not a visit of its own at all. Its
  money is that Medicare visit's coinsurance being paid, and it goes into that
  row's `Co-pays Paid` and nothing else.

Every patient in the fixture is fictional and its NPIs are the repo's synthetic
placeholders (see `fixtures/synthetic_db_data.py`).
"""

from __future__ import annotations

from datetime import date

import pytest

from remit.config import (
    ACTION_CROSSOVER_ORPHAN,
    ACTION_FILL,
    ACTION_NEW,
    ACTION_SKIP,
    COL_COPAY,
    COL_COPAYS_PAID,
    COL_INS,
    COL_PAYMENT,
    DB_INGEST_ENABLED,
    DB_SOURCE_SCOPE,
    MEDICAL_SHEET_NAME,
    NEVER_TOUCH_COLUMNS,
    PDF_INGEST_ENABLED,
    SHEET_NAME,
)
from remit.db_source import (
    RemitDbError,
    aggregate_by_payer,
    hpsm_visits,
    load_visits,
    medicare_visits,
    open_readonly,
    read_service_rows,
)
from remit.excel_updater import (
    apply_changes,
    build_updated_workbook,
    load_schedule_workbook,
    read_all_schedule_rows,
)
from remit.matching import build_plan
from remit.pdf_parser import split_by_eob_cutoff

from .fixtures.synthetic_db_data import (
    NPI_ANA,
    NPI_OXANA,
    NPI_SUPERVISING,
    PAYER_HPSM,
    PAYER_MEDICARE,
    PAYER_OUT_OF_SCOPE,
    REMIT_DATE_HPSM,
    REMIT_DATE_MEDICARE,
)

STAMP = date(2026, 9, 10)


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    """The synthetic 835 database, generated rather than committed."""
    from .fixtures.make_db_fixture import build

    return build(tmp_path_factory.mktemp("db") / "remittance_data.sqlite")


@pytest.fixture(scope="module")
def loaded(db_path):
    return load_visits(db_path)


@pytest.fixture()
def visits(loaded):
    return loaded[1]


@pytest.fixture()
def plan(schedule_bytes, visits):
    workbook = load_schedule_workbook(schedule_bytes)
    rows, _ = read_all_schedule_rows(workbook)
    return build_plan(visits, rows, today=STAMP)


def change_for(plan, surname, service_date):
    found = [c for c in plan
             if c.visit.patient.startswith(surname.upper())
             and c.visit.service_date == service_date]
    if len(found) != 1:
        raise AssertionError(f"{len(found)} changes for {surname} {service_date}: "
                             f"{[(c.action, c.visit.payer) for c in found]}")
    return found[0]


def changes_for(plan, surname, service_date):
    return [c for c in plan
            if c.visit.patient.startswith(surname.upper())
            and c.visit.service_date == service_date]


# --- Reading the database ---------------------------------------------------

def test_reads_the_service_line_table(db_path):
    connection = open_readonly(db_path)
    try:
        rows = read_service_rows(connection)
    finally:
        connection.close()
    assert rows
    assert all(row["service_from_date"] for row in rows)


def test_out_of_scope_payers_are_filtered_out(db_path, loaded):
    """Carelon is in the file and must never reach the rest of the app."""
    connection = open_readonly(db_path)
    try:
        payers = {row["payer_name"] for row in read_service_rows(connection)}
    finally:
        connection.close()
    assert payers == {PAYER_MEDICARE, PAYER_HPSM}
    assert PAYER_OUT_OF_SCOPE not in payers

    _, visits, report = loaded
    assert PAYER_OUT_OF_SCOPE not in {v.payer for v in visits}
    assert PAYER_OUT_OF_SCOPE not in report.payer_rows


def test_text_amount_columns_are_cast_to_numbers(visits):
    ashgrove = next(v for v in medicare_visits(visits)
                    if v.patient.startswith("ASHGROVE"))
    assert isinstance(ashgrove.payment, float)
    assert ashgrove.payment == 166.37   # 92.18 + 74.19
    assert ashgrove.copay == 42.44      # 23.52 + 18.92


def test_an_unparseable_amount_is_absent_not_zero():
    from remit.db_source import _to_amount

    assert _to_amount("104.27") == 104.27
    assert _to_amount("$1,004.27") == 1004.27
    assert _to_amount("") is None
    assert _to_amount("n/a") is None
    assert _to_amount(None) is None
    assert _to_amount("0.00") == 0.0   # a real zero, not absence


def test_iso_dates_and_title_cased_names(visits):
    ashgrove = next(v for v in visits if v.patient.startswith("ASHGROVE"))
    assert ashgrove.service_date == date(2026, 7, 6)
    # The remit's own uppercase survives to the one place that writes names.
    assert ashgrove.patient == "ASHGROVE, PETRA"
    from remit.matching import to_title_name
    assert to_title_name(ashgrove.patient) == "Ashgrove, Petra"


def test_each_payer_is_aggregated_separately(visits):
    """Otherwise the crossover would be read as a restatement of the primary.

    Both share (patient, service date, NPI), which is the aggregation key, so
    aggregating them together would let the later HPSM `$42.44` replace the
    Medicare `$166.37`.
    """
    both = [v for v in visits
            if v.patient.startswith("ASHGROVE") and v.service_date == date(2026, 7, 6)]
    assert len(both) == 2
    assert {v.payer for v in both} == {PAYER_MEDICARE, PAYER_HPSM}
    assert next(v.payment for v in both if v.payer == PAYER_MEDICARE) == 166.37
    assert next(v.payment for v in both if v.payer == PAYER_HPSM) == 42.44


def test_a_file_without_the_table_is_rejected(tmp_path):
    import sqlite3

    path = tmp_path / "wrong.sqlite"
    sqlite3.connect(path).execute("CREATE TABLE something_else (x TEXT)")
    connection = open_readonly(path)
    try:
        with pytest.raises(RemitDbError, match="no `master_remittance_records`"):
            read_service_rows(connection)
    finally:
        connection.close()


def test_remits_are_grouped_by_trace_number(loaded):
    documents, _, report = loaded
    assert report.remits == len(documents) >= 3
    # A trace number is per payer, so the two plans cannot fuse into one remit.
    assert all(len({line.payer for line in d.service_lines}) == 1
               for d in documents)


# --- Payer routing ----------------------------------------------------------

def test_noridian_visit_targets_the_medicare_sheet(plan):
    change = change_for(plan, "CORDOVA", date(2026, 7, 14))
    assert change.visit.payer == PAYER_MEDICARE
    assert change.sheet == SHEET_NAME
    assert change.action == ACTION_NEW


def test_hpsm_primary_visit_targets_the_medical_sheet(plan):
    change = change_for(plan, "DRUMMOND", date(2026, 7, 15))
    assert change.visit.payer == PAYER_HPSM
    assert not change.visit.crossover
    assert change.sheet == MEDICAL_SHEET_NAME
    assert change.action == ACTION_NEW


def test_a_noridian_fill_writes_payment_and_copay(plan):
    change = next(c for c in changes_for(plan, "ASHGROVE", date(2026, 7, 6))
                  if c.visit.payer == PAYER_MEDICARE)
    assert change.action == ACTION_FILL
    assert change.sheet == SHEET_NAME
    assert change.fills[COL_PAYMENT] == 166.37
    assert change.fills[COL_COPAY] == 42.44
    assert COL_COPAYS_PAID not in change.fills


def test_an_hpsm_primary_fill_lands_on_the_medical_sheet(plan):
    change = next(c for c in changes_for(plan, "BELLWEATHER", date(2026, 7, 8))
                  if not c.visit.crossover)
    assert change.action == ACTION_FILL
    assert change.sheet == MEDICAL_SHEET_NAME
    assert change.fills[COL_PAYMENT] == 104.27


def test_an_existing_row_is_filled_on_whichever_sheet_holds_it(plan):
    """Staff decide which sheet a visit is filed on; a fill follows the row."""
    sheets = {c.sheet for c in plan if c.action == ACTION_FILL}
    assert sheets == {SHEET_NAME, MEDICAL_SHEET_NAME}


# --- The crossover ----------------------------------------------------------

def test_a_crossover_fills_only_co_pays_paid_on_the_medicare_row(plan):
    change = next(c for c in changes_for(plan, "ASHGROVE", date(2026, 7, 6))
                  if c.visit.crossover)
    assert change.visit.payer == PAYER_HPSM
    assert change.sheet == SHEET_NAME          # the Medicare row, not Medical
    assert change.action == ACTION_FILL
    assert change.fills == {COL_COPAYS_PAID: 42.44}
    # Explicitly NOT these.
    assert COL_PAYMENT not in change.fills
    assert COL_COPAY not in change.fills
    assert COL_INS not in change.fills


def test_a_crossover_never_creates_a_medical_row(plan):
    crossovers = [c for c in plan if c.visit.crossover]
    assert crossovers
    assert not [c for c in crossovers
                if c.sheet == MEDICAL_SHEET_NAME or c.action == ACTION_NEW]


def test_a_crossover_with_no_medicare_visit_is_flagged(plan):
    change = change_for(plan, "EASTMAN", date(2026, 7, 20))
    assert change.action == ACTION_CROSSOVER_ORPHAN
    assert change.needs_review
    assert not change.accepted
    assert not change.fills
    assert "no matching visit" in " ".join(change.review_reasons)


def test_an_orphan_crossover_writes_nothing(schedule_bytes, plan):
    workbook = load_schedule_workbook(schedule_bytes)
    orphan = change_for(plan, "EASTMAN", date(2026, 7, 20))
    orphan.accepted = True            # even if a user ticks it
    stats = apply_changes(workbook, [orphan])
    assert stats["filled_cells"] == 0
    assert stats["new_rows"] == 0


def test_co_pays_paid_is_no_longer_never_touch():
    """The crossover is the one thing that may write it."""
    assert COL_COPAYS_PAID not in NEVER_TOUCH_COLUMNS


def test_a_recorded_co_pays_paid_is_never_overwritten(schedule_bytes, visits):
    """Never-overwrite still governs the column the crossover writes."""
    workbook = load_schedule_workbook(schedule_bytes)
    worksheet = workbook[SHEET_NAME]
    headers = {worksheet.cell(row=2, column=c).value: c
               for c in range(1, worksheet.max_column + 1)}
    target = next(r for r in range(3, worksheet.max_row + 1)
                  if worksheet.cell(row=r, column=headers["Patient"]).value
                  == "Ashgrove, Petra")
    worksheet.cell(row=target, column=headers[COL_COPAYS_PAID]).value = 11.11

    import io
    buffer = io.BytesIO()
    workbook.save(buffer)

    rows, _ = read_all_schedule_rows(load_schedule_workbook(buffer.getvalue()))
    replanned = build_plan(visits, rows, today=STAMP)
    crossover = next(c for c in changes_for(replanned, "ASHGROVE", date(2026, 7, 6))
                     if c.visit.crossover)
    assert crossover.action == ACTION_SKIP
    assert not crossover.fills


# --- Writing to both sheets -------------------------------------------------

def test_both_sheets_are_written(schedule_bytes, plan):
    buffer, stats = build_updated_workbook(
        schedule_bytes, [c for c in plan if c.accepted])
    workbook = load_schedule_workbook(buffer.getvalue())

    medicare = workbook[SHEET_NAME]
    medical = workbook[MEDICAL_SHEET_NAME]
    med_headers = {medicare.cell(row=2, column=c).value: c
                   for c in range(1, medicare.max_column + 1)}
    dic_headers = {medical.cell(row=2, column=c).value: c
                   for c in range(1, medical.max_column + 1)}

    ashgrove = next(r for r in range(3, medicare.max_row + 1)
                    if medicare.cell(row=r, column=med_headers["Patient"]).value
                    == "Ashgrove, Petra")
    assert medicare.cell(row=ashgrove, column=med_headers[COL_PAYMENT]).value == 166.37
    assert medicare.cell(row=ashgrove, column=med_headers[COL_COPAYS_PAID]).value == 42.44

    bellweather = next(r for r in range(3, medical.max_row + 1)
                       if medical.cell(row=r, column=dic_headers["Patient"]).value
                       == "Bellweather, Colm"
                       and medical.cell(row=r, column=dic_headers["Data"]).value
                       is not None
                       and medical.cell(row=r, column=dic_headers[COL_PAYMENT]).value
                       == 104.27)
    assert bellweather

    assert stats["new_rows"] >= 2


def test_appended_rows_take_their_sheets_ins_label(schedule_bytes, plan):
    buffer, _ = build_updated_workbook(
        schedule_bytes, [c for c in plan if c.accepted])
    workbook = load_schedule_workbook(buffer.getvalue())

    def ins_for(sheet, patient):
        worksheet = workbook[sheet]
        headers = {worksheet.cell(row=2, column=c).value: c
                   for c in range(1, worksheet.max_column + 1)}
        for row in range(3, worksheet.max_row + 1):
            if worksheet.cell(row=row, column=headers["Patient"]).value == patient:
                return worksheet.cell(row=row, column=headers[COL_INS]).value
        raise AssertionError(f"{patient} not on {sheet}")

    assert ins_for(SHEET_NAME, "Cordova, Ines") == "Medicare"
    # Drummond is telehealth (modifier 95, no POS from HPSM).
    assert ins_for(MEDICAL_SHEET_NAME, "Drummond, Faye") == "POS 10(95)"


def test_the_non_schedule_sheet_survives(schedule_bytes, plan):
    buffer, _ = build_updated_workbook(
        schedule_bytes, [c for c in plan if c.accepted])
    workbook = load_schedule_workbook(buffer.getvalue())
    assert workbook.sheetnames == [SHEET_NAME, MEDICAL_SHEET_NAME, "2026 Dental"]
    assert workbook["2026 Dental"]["A1"].value == "Untouched sheet"


def test_rerun_is_a_no_op(schedule_bytes, visits):
    buffer, _ = build_updated_workbook(
        schedule_bytes,
        [c for c in build_plan(
            visits, read_all_schedule_rows(load_schedule_workbook(schedule_bytes))[0],
            today=STAMP) if c.accepted])

    once = buffer.getvalue()
    rows, _ = read_all_schedule_rows(load_schedule_workbook(once))
    again = build_plan(visits, rows, today=STAMP)
    _, stats = build_updated_workbook(once, [c for c in again if c.accepted])
    assert stats["filled_cells"] == 0
    assert stats["new_rows"] == 0


# --- Telehealth, NPI and reconciliation still apply -------------------------

def test_telehealth_needs_no_place_of_service_from_hpsm(visits):
    """HPSM leaves `location_number` empty, so the 95 modifier decides alone."""
    drummond = next(v for v in visits if v.patient.startswith("DRUMMOND"))
    assert drummond.pos is None
    assert drummond.telehealth
    assert drummond.insurance == "POS 10(95)"


def test_a_stated_office_place_of_service_still_rules_telehealth_out(visits):
    ashgrove = next(v for v in medicare_visits(visits)
                    if v.patient.startswith("ASHGROVE"))
    assert ashgrove.pos == "11"
    assert not ashgrove.telehealth


def test_rendering_npi_is_carried_through(visits):
    by_npi = {v.patient.split(",")[0]: v.npi for v in visits}
    assert by_npi["ASHGROVE"] == NPI_ANA
    assert by_npi["BELLWEATHER"] == NPI_OXANA
    assert by_npi["CORDOVA"] == NPI_SUPERVISING


def test_oa18_does_not_zero_a_real_payment(visits):
    """A later HPSM remit re-adjudicates Bellweather at $0.00 as a duplicate."""
    bellweather = next(v for v in hpsm_visits(visits)
                       if v.patient.startswith("BELLWEATHER")
                       and v.service_date == date(2026, 7, 8))
    assert bellweather.payment == 104.27
    assert not bellweather.duplicate_only
    assert bellweather.ignored_duplicate_efts


# --- The cutoff runs on payment_date ---------------------------------------

def test_the_cutoff_filters_by_payment_date(loaded):
    documents, _, _ = loaded
    processed, skipped = split_by_eob_cutoff(documents, date(2026, 8, 15))
    assert skipped, "the Medicare remit is dated 08/10 and should be skipped"
    assert all(d.billed_date >= date(2026, 8, 15) for d in processed)

    visits = aggregate_by_payer(processed)
    # Ashgrove's Medicare line came from the skipped remit; its HPSM crossover
    # (08/17) survives, which is exactly the orphan case the app flags.
    medicare_dates = {v.service_date for v in medicare_visits(visits)}
    assert date(2026, 7, 6) not in medicare_dates
    assert any(v.crossover for v in hpsm_visits(visits))


def test_a_remit_dated_on_the_cutoff_is_kept(loaded):
    documents, _, _ = loaded
    processed, _ = split_by_eob_cutoff(documents, date(2026, 8, 10))
    assert date.fromisoformat(REMIT_DATE_MEDICARE) in {
        d.billed_date for d in processed}


def test_no_cutoff_keeps_every_remit(loaded):
    documents, _, _ = loaded
    processed, skipped = split_by_eob_cutoff(documents, None)
    assert processed == documents and skipped == []
    assert date.fromisoformat(REMIT_DATE_HPSM) in {d.billed_date for d in documents}


# --- Option B: the PDF entry point is off, the parser is not deleted --------

def test_the_database_is_the_only_enabled_source():
    assert DB_SOURCE_SCOPE == "db"
    assert DB_INGEST_ENABLED
    assert not PDF_INGEST_ENABLED


def test_the_pdf_parser_is_kept_and_still_works():
    """Gated off, not deleted: the reconciliation it fed is shared code."""
    from pathlib import Path

    from remit.pdf_parser import parse_remittances

    fixture = Path(__file__).parent / "fixtures" / "RemitDoc-0000000001.PDF"
    _, visits = parse_remittances([(str(fixture), fixture.name)])
    assert visits


def test_switching_the_scope_re_enables_the_pdf_path(monkeypatch):
    """A one-off paper remit is a config change, not a code change."""
    monkeypatch.setenv("REMIT_SOURCE_SCOPE", "pdf")
    import importlib

    from remit import config as config_module

    reloaded = importlib.reload(config_module)
    try:
        assert reloaded.PDF_INGEST_ENABLED
        assert not reloaded.DB_INGEST_ENABLED
    finally:
        monkeypatch.delenv("REMIT_SOURCE_SCOPE", raising=False)
        importlib.reload(config_module)


# --- The employees workbook: HPSM copays go to the copay column -------------

def test_each_tabs_copay_column_resolves_by_header(employee_sheets):
    """Ana column I, Marcia G, Oxana G in the real workbook."""
    from remit.config import EMPLOYEE_COPAY_COLUMN

    assert EMPLOYEE_COPAY_COLUMN == {
        "Ana": "Co-pay to Ana",
        "Marcia": "Co-payment Paid",
        "Oxana": "Co-payment Paid",
    }
    assert employee_sheets["Ana"].copay_column == 9      # I
    assert employee_sheets["Marcia"].copay_column == 7   # G
    assert employee_sheets["Oxana"].copay_column == 7    # G


def test_a_crossover_copay_fills_the_tabs_copay_column(employee_sheets, visits):
    """Ashgrove is Ana's, so HPSM's $42.44 is a copay paid to Ana."""
    from remit.employees import eob_visits, plan_employee_changes

    changes = plan_employee_changes(employee_sheets, eob_visits(visits), today=STAMP)
    crossover = next(c for c in changes
                     if c.patient.startswith("Ashgrove") and c.sheet == "Ana"
                     and c.npi == NPI_ANA and c.payment == 42.44)
    assert crossover.fills == {employee_sheets["Ana"].copay_column: 42.44}


def test_a_crossover_never_touches_the_insurance_payment_column(
        employee_sheets, visits):
    from remit.employees import eob_visits, plan_employee_changes

    changes = plan_employee_changes(employee_sheets, eob_visits(visits), today=STAMP)
    payment_column = employee_sheets["Ana"].payment_column
    for change in changes:
        if change.sheet == "Ana" and change.payment == 42.44:
            assert payment_column not in change.fills


def test_the_primary_payment_still_uses_the_payment_column(employee_sheets, visits):
    """Medicare's $166.37 for the same session is an insurance payment."""
    from remit.employees import eob_visits, plan_employee_changes

    changes = plan_employee_changes(employee_sheets, eob_visits(visits), today=STAMP)
    primary = next(c for c in changes
                   if c.patient.startswith("Ashgrove") and c.sheet == "Ana"
                   and c.payment == 166.37)
    assert employee_sheets["Ana"].payment_column in primary.fills
    assert primary.fills[employee_sheets["Ana"].payment_column] == 166.37


def test_both_amounts_land_in_the_written_employees_workbook(
        employees_bytes, employee_sheets, visits):
    from remit.employees import (
        build_updated_employees, eob_visits, load_employees_workbook,
        plan_employee_changes,
    )

    changes = plan_employee_changes(employee_sheets, eob_visits(visits), today=STAMP)
    updated, _ = build_updated_employees(employees_bytes, changes)
    worksheet = load_employees_workbook(updated.getvalue())["Ana"]
    headers = {worksheet.cell(row=1, column=c).value: c
               for c in range(1, worksheet.max_column + 1)}
    row = next(r for r in range(2, worksheet.max_row + 1)
               if worksheet.cell(row=r, column=headers["Date of Session"]).value
               == "07/06/2026")
    assert worksheet.cell(row=row, column=headers["Paid by Ins toAna"]).value == 166.37
    assert worksheet.cell(row=row, column=headers["Co-pay to Ana"]).value == 42.44


def test_a_crossover_never_appends_an_employee_row(employee_sheets, visits):
    """`Eastman` is Ana's by NPI but has no row; a crossover may not start one."""
    from remit.employees import eob_visits, plan_employee_changes

    changes = plan_employee_changes(employee_sheets, eob_visits(visits), today=STAMP)
    assert not [c for c in changes if c.appends and c.patient.startswith("Eastman")]


def test_a_levinson_rendered_visit_reaches_no_associate_tab(employee_sheets, visits):
    """`Cordova` is the supervising physician's, so no associate tab claims it."""
    from remit.config import EMP_ACTION_UNASSIGNED
    from remit.employees import eob_visits, plan_employee_changes

    changes = plan_employee_changes(employee_sheets, eob_visits(visits), today=STAMP)
    cordova = next(c for c in changes if c.patient.startswith("Cordova"))
    assert cordova.npi == NPI_SUPERVISING
    assert cordova.action == EMP_ACTION_UNASSIGNED
    assert cordova.sheet == ""
