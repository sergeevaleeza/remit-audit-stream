"""Regression cover for non-zero DEDUCT and bare continuation lines.

The failure this guards against: a visit whose deductible is non-zero, or whose
sequestration continuation line carries an amount, being read as
`Payment = 0` / `Co-pay = 0`.

Two shapes make that easy to get wrong, and both are reproduced in the
synthetic `RemitDoc-0000000002.PDF`:

* **A non-zero `DEDUCT`.** `DEDUCT` is the 3rd of the six dollar amounts and is
  its own field. When it consumes all of `ALLOWED`, `PROV-PD` is a *genuine*
  `0.00`; when it is partial, `COINS` and `PROV-PD` still sit at positions 4
  and 6. It must never be conflated with either.
* **Bare `CO-253 <amt>` continuation lines with no `REM:` prefix.** These carry
  a dollar amount, so folding one into the preceding service line would push a
  seventh amount onto it. They are skipped because they do not start with a
  10-digit NPI -- *not* because they start with `REM:`.

Every patient here is fictional (see `tests/fixtures/README.md`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from remit.pdf_parser import (
    aggregate_visits,
    is_service_line,
    parse_document,
    parse_service_line,
)

from .fixtures.synthetic_remit_data import (
    CPT_90834_ALLOWED,
    DEDUCT_CHECK_EFT,
    DEDUCTIBLE_LINES,
    SYNTHETIC_PROVIDER_NPI,
    SYNTHETIC_THERAPIST_NPI,
)

FIXTURE = Path(__file__).parent / "fixtures" / "RemitDoc-0000000002.PDF"

#: The fixture's own `TOTALS ... PROV PD AMT` line.
EXPECTED_TOTAL_PROV_PD = 1332.11

#: Therapist claim totals: 0 + 0 + 90.94 + 6 x 104.27.
EXPECTED_THERAPIST_PAYMENT = 716.56
EXPECTED_THERAPIST_COINS = 182.80

PAID = 104.27
PAID_COINS = 26.60


@pytest.fixture(scope="module")
def parsed():
    document = parse_document(str(FIXTURE), FIXTURE.name)
    return document, aggregate_visits([document])


@pytest.fixture(scope="module")
def document(parsed):
    return parsed[0]


@pytest.fixture(scope="module")
def deduct_visits(parsed):
    return parsed[1]


def visit_on(visits, service_date: date, npi: str):
    for visit in visits:
        if visit.service_date == service_date and visit.npi == npi:
            return visit
    raise AssertionError(f"no visit on {service_date} for NPI {npi}")


# --- The nine therapist visits ---------------------------------------------

@pytest.mark.parametrize(
    "service_date, payment, copay",
    [
        (date(2026, 1, 15), 0.00, 0.00),      # ALLOWED fully consumed by DEDUCT
        (date(2026, 2, 10), 0.00, 0.00),      # ALLOWED fully consumed by DEDUCT
        (date(2026, 3, 11), 90.94, 23.20),    # partial deductible
        (date(2026, 3, 31), PAID, PAID_COINS),
        (date(2026, 4, 15), PAID, PAID_COINS),
        (date(2026, 5, 4), PAID, PAID_COINS),
        (date(2026, 5, 20), PAID, PAID_COINS),
        (date(2026, 6, 2), PAID, PAID_COINS),
        (date(2026, 6, 16), PAID, PAID_COINS),
    ],
)
def test_therapist_visit_values(deduct_visits, service_date, payment, copay):
    visit = visit_on(deduct_visits, service_date, SYNTHETIC_THERAPIST_NPI)
    assert visit.cpt_display == "90834"
    assert visit.payment == payment
    assert visit.copay == copay


def test_only_the_first_two_visits_are_zero(deduct_visits):
    """The regression was every visit reading 0; only two genuinely are."""
    therapist = [v for v in deduct_visits if v.npi == SYNTHETIC_THERAPIST_NPI]
    assert len(therapist) == 9
    zeros = [v for v in therapist if v.payment == 0.0]
    assert len(zeros) == 2
    assert sorted(v.data_str for v in zeros) == ["01/15/2026", "02/10/2026"]


def test_therapist_claim_totals(deduct_visits):
    therapist = [v for v in deduct_visits if v.npi == SYNTHETIC_THERAPIST_NPI]
    assert round(sum(v.payment for v in therapist), 2) == EXPECTED_THERAPIST_PAYMENT
    assert round(sum(v.copay for v in therapist), 2) == EXPECTED_THERAPIST_COINS


def test_therapist_visits_are_all_office_medicare(deduct_visits):
    for visit in (v for v in deduct_visits if v.npi == SYNTHETIC_THERAPIST_NPI):
        assert visit.pos == "11"
        assert visit.insurance == "Medicare"


# --- The physician visits, same patient ------------------------------------

@pytest.mark.parametrize(
    "service_date, cpt, payment, copay",
    [
        (date(2026, 4, 28), "99214/90836", 224.59, 57.29),
        (date(2026, 5, 5), "99214/90836", 224.59, 57.29),
        (date(2026, 6, 7), "99213/90833", 166.37, 42.44),
    ],
)
def test_physician_visit_values(deduct_visits, service_date, cpt, payment, copay):
    visit = visit_on(deduct_visits, service_date, SYNTHETIC_PROVIDER_NPI)
    assert visit.cpt_display == cpt
    assert visit.payment == payment
    assert visit.copay == copay


def test_one_patient_split_across_two_providers(deduct_visits):
    """Same person, two performing NPIs -> separate visits, never merged."""
    assert len({v.patient for v in deduct_visits}) == 1
    assert {v.npi for v in deduct_visits} == {
        SYNTHETIC_THERAPIST_NPI, SYNTHETIC_PROVIDER_NPI,
    }
    assert len(deduct_visits) == 12


# --- Whole-file sanity ------------------------------------------------------

def test_whole_file_total_matches_the_totals_line(document):
    assert document.total_prov_pd == EXPECTED_TOTAL_PROV_PD


def test_visit_payments_sum_to_the_totals_line(deduct_visits):
    assert round(sum(v.payment for v in deduct_visits), 2) == EXPECTED_TOTAL_PROV_PD


def test_service_line_count(document):
    """9 therapist lines + 6 physician lines; no continuation counted."""
    assert len(document.service_lines) == 15


def test_check_eft_and_header_date(document):
    assert document.check_eft == DEDUCT_CHECK_EFT
    assert document.billed_date == date(2026, 7, 20)


# --- DEDUCT is its own field ------------------------------------------------

def test_deduct_is_parsed_separately_from_coins_and_prov_pd():
    line = parse_service_line(
        f"{SYNTHETIC_THERAPIST_NPI} 0311 031126 11 1.0 90834 "
        "200.00 133.00 17.00 23.20 CO-45 67.00 90.94",
        "TEST, PATIENT", "f.pdf",
    )
    assert line.deduct == 17.00
    assert line.coins == 23.20
    assert line.prov_pd == 90.94


def test_full_deductible_yields_a_genuine_zero():
    line = parse_service_line(
        f"{SYNTHETIC_THERAPIST_NPI} 0115 011526 11 1.0 90834 "
        "200.00 133.00 133.00 0.00 CO-45 67.00 0.00",
        "TEST, PATIENT", "f.pdf",
    )
    assert line.deduct == CPT_90834_ALLOWED == 133.00
    assert line.coins == 0.00
    assert line.prov_pd == 0.00


def test_zero_deductible_line_is_unaffected():
    line = parse_service_line(
        f"{SYNTHETIC_THERAPIST_NPI} 0331 033126 11 1.0 90834 "
        "200.00 133.00 0.00 26.60 CO-45 67.00 104.27",
        "TEST, PATIENT", "f.pdf",
    )
    assert line.deduct == 0.00
    assert line.coins == 26.60
    assert line.prov_pd == 104.27


def test_deduct_total_is_non_zero_in_the_fixture(document):
    """Proves the fixture really exercises the deductible path."""
    assert sum(line.deduct for line in document.service_lines) == 283.00


# --- Continuation lines -----------------------------------------------------

def test_bare_continuation_lines_are_not_service_lines():
    """No `REM:` prefix, but still not a service line: no 10-digit NPI."""
    for text in ("CO-253 2.13", "CO-253 1.86", "CO-45 67.00", "CO-29 200.00"):
        assert not is_service_line(text.split()), text
        assert parse_service_line(text, "TEST", "f.pdf") is None


def test_rem_prefixed_continuations_are_also_skipped():
    for text in ("REM: N782 CO-253 1.00", "REM: N211"):
        assert not is_service_line(text.split()), text
        assert parse_service_line(text, "TEST", "f.pdf") is None


def test_the_skip_rule_is_the_npi_not_the_rem_prefix():
    """A line starting with a 10-digit NPI IS a service line, `REM:` or not."""
    service = (f"{SYNTHETIC_THERAPIST_NPI} 0331 033126 11 1.0 90834 "
               "200.00 133.00 0.00 26.60 CO-45 67.00 104.27")
    assert is_service_line(service.split())
    # ...and nothing else in this remit's vocabulary is.
    for text in ("CO-253 2.13", "PT RESP 0.00 CLAIM TOTALS 200.00 0.00 0.00 0.00 200.00 0.00",
                 "NET 372.62", "4 3000.00 1982.12 283.00 339.82 1045.07 1332.11 0.00 1332.11"):
        assert not is_service_line(text.split()), text


def test_continuation_amounts_never_reach_a_visit(deduct_visits):
    """1.86 / 2.13 are sequestration figures, never a Payment or Co-pay."""
    sequestrations = {
        amount for *_, amount in DEDUCTIBLE_LINES if amount is not None
    }
    assert sequestrations == {1.86, 2.13}
    for visit in deduct_visits:
        assert visit.payment not in sequestrations
        assert visit.copay not in sequestrations


def test_prov_pd_is_read_positionally_not_as_the_last_amount():
    """A trailing amount must not be mistaken for PROV-PD.

    This is the shape that would appear if a bare `CO-253` continuation were
    ever folded onto its service line by text extraction.
    """
    line = parse_service_line(
        f"{SYNTHETIC_THERAPIST_NPI} 0331 033126 11 1.0 90834 "
        "200.00 133.00 0.00 26.60 CO-45 67.00 104.27 CO-253 2.13",
        "TEST, PATIENT", "f.pdf",
    )
    assert line.prov_pd == 104.27
    assert line.coins == 26.60


def test_amounts_come_only_from_the_physical_line():
    """Parsing one line never consults the next one."""
    service = (f"{SYNTHETIC_THERAPIST_NPI} 0115 011526 11 1.0 90834 "
               "200.00 133.00 133.00 0.00 CO-45 67.00 0.00")
    alone = parse_service_line(service, "TEST", "f.pdf")
    assert alone.prov_pd == 0.00
    # The continuation that follows it in the file is parsed independently.
    assert parse_service_line("CO-253 1.86", "TEST", "f.pdf") is None


# --- The zero-deductible path must not regress -----------------------------

def test_original_fixture_total_is_unchanged(document):
    """The first fixture (all DEDUCT 0.00) still parses to its own total."""
    from .test_pdf_parser import EXPECTED_TOTAL_PROV_PD as ORIGINAL_TOTAL
    from .conftest import REMIT_PDF

    original = parse_document(str(REMIT_PDF), REMIT_PDF.name)
    assert original.total_prov_pd == ORIGINAL_TOTAL
    assert all(line.deduct == 0.00 for line in original.service_lines)
    # ...and the two fixtures are genuinely different files.
    assert original.total_prov_pd != document.total_prov_pd
