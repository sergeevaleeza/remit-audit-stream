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


def find_visit(visits, patient: str, service_date: date) -> Visit:
    """Locate one visit, failing loudly if the fixture ever drifts."""
    for visit in visits:
        if visit.patient == patient and visit.service_date == service_date:
            return visit
    raise AssertionError(f"no visit for {patient} on {service_date}")
