"""Shared fixtures: the synthetic remittance PDF and sample schedule workbook.

Every identifier in these fixtures is fictional -- see
`tests/fixtures/README.md` and `tests/fixtures/synthetic_remit_data.py`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from remit.pdf_parser import Visit, parse_remittances

FIXTURES = Path(__file__).parent / "fixtures"
REMIT_PDF = FIXTURES / "RemitDoc-0000000001.PDF"
SCHEDULE_XLSX = FIXTURES / "List_of_Patients_Schedule.xlsx"


@pytest.fixture(scope="session")
def remit_parse():
    """(documents, visits) parsed once from the sample remittance."""
    return parse_remittances([(str(REMIT_PDF), REMIT_PDF.name)])


@pytest.fixture(scope="session")
def documents(remit_parse):
    return remit_parse[0]


@pytest.fixture(scope="session")
def document(documents):
    return documents[0]


@pytest.fixture(scope="session")
def visits(remit_parse):
    return remit_parse[1]


@pytest.fixture(scope="session")
def visit_lookup(visits) -> dict[tuple[str, str], Visit]:
    """Visits keyed by (patient name, MM/DD/YYYY service date)."""
    return {(v.patient, v.data_str): v for v in visits}


@pytest.fixture()
def schedule_bytes() -> bytes:
    return SCHEDULE_XLSX.read_bytes()


@pytest.fixture()
def schedule_rows(schedule_bytes):
    """Parsed rows of the sample schedule."""
    from remit.excel_updater import (get_schedule_sheet, load_schedule_workbook,
                                     read_schedule_rows, resolve_columns)
    worksheet = get_schedule_sheet(load_schedule_workbook(schedule_bytes))
    return read_schedule_rows(worksheet, resolve_columns(worksheet))


def sheet_rows(worksheet) -> list[dict]:
    """Every data row of an output sheet, keyed by column name.

    Rows move now that new visits are inserted under their patient, so tests
    locate rows by identity rather than by a fixed index.
    """
    from remit.config import DATA_START_ROW, HEADER_ROW

    headers = {}
    for column in range(1, worksheet.max_column + 1):
        value = worksheet.cell(row=HEADER_ROW, column=column).value
        if value is not None:
            headers[str(value).strip()] = column

    rows = []
    for row in range(DATA_START_ROW, worksheet.max_row + 1):
        patient = worksheet.cell(row=row, column=headers["Patient"]).value
        if patient is None or str(patient).strip() == "":
            continue
        record = {name: worksheet.cell(row=row, column=col).value
                  for name, col in headers.items()}
        record["_row"] = row
        rows.append(record)
    return rows


def find_row(worksheet, patient: str, data=None) -> dict:
    """One data row, located by patient name and (optionally) its `Data` date.

    `data` may be a date, a datetime or any of the sheet's hand-typed string
    formats -- it is compared by parsed calendar value, like everywhere else.
    """
    from remit.matching import name_score, parse_loose_date

    wanted_date = parse_loose_date(data) if data is not None else None
    if data is not None and wanted_date is None:
        raise AssertionError(f"find_row got an unparseable date: {data!r}")

    matches = []
    for record in sheet_rows(worksheet):
        if name_score(patient, str(record["Patient"])) < 92.0:
            continue
        if wanted_date is not None and parse_loose_date(record["Data"]) != wanted_date:
            continue
        matches.append(record)

    if not matches:
        raise AssertionError(f"no row for {patient!r} (data={data!r})")
    if len(matches) > 1:
        raise AssertionError(
            f"{len(matches)} rows matched {patient!r} (data={data!r}): "
            f"{[m['_row'] for m in matches]}"
        )
    return matches[0]


def find_visit(visits, patient: str, service_date: date) -> Visit:
    """Locate one visit, failing loudly if the fixture ever drifts."""
    for visit in visits:
        if visit.patient == patient and visit.service_date == service_date:
            return visit
    raise AssertionError(f"no visit for {patient} on {service_date}")
