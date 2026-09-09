"""Ground-truth tests against the synthetic RemitDoc-0000000001.PDF.

The fixture is entirely fictional (see `tests/fixtures/README.md`); every
expected figure below comes from `tests/fixtures/synthetic_remit_data.py`,
the single source of truth used to both generate the PDF and check it here.
"""

from __future__ import annotations

from datetime import date

import pytest

from remit.pdf_parser import (
    expand_two_digit_year,
    is_service_line,
    order_cpt_codes,
    parse_header_date,
    parse_serv_date,
    parse_service_line,
)

from .conftest import find_visit
from .fixtures.synthetic_remit_data import (
    CPT_RECIPE,
    ROSTER,
    SYNTHETIC_CHECK_EFT,
    SYNTHETIC_HEADER_DATE,
    SYNTHETIC_PROVIDER_NPI,
)

EXPECTED_CLAIM_COUNT = sum(len(patient.claims) for patient in ROSTER)
EXPECTED_SERVICE_LINES = sum(
    len(claim.lines) for patient in ROSTER for claim in patient.claims
)
EXPECTED_TOTAL_PROV_PD = round(
    sum(
        CPT_RECIPE[spec.cpt]["provpd"]
        for patient in ROSTER
        for claim in patient.claims
        for spec in claim.lines
    ),
    2,
)


# --- Header ----------------------------------------------------------------

def test_header_billed_date(document):
    """`DATE:` in the top-right header applies to every visit."""
    month, day, year = SYNTHETIC_HEADER_DATE.split("/")
    assert document.billed_date == date(2000 + int(year), int(month), int(day))


def test_check_eft_number(document):
    assert document.check_eft == SYNTHETIC_CHECK_EFT


# --- Whole-file sanity -----------------------------------------------------

def test_claim_count(document):
    assert document.claim_count == EXPECTED_CLAIM_COUNT


def test_service_line_count(document):
    assert len(document.service_lines) == EXPECTED_SERVICE_LINES


def test_total_prov_pd_matches_totals_line(document):
    """Sum of every PROV-PD equals the TOTALS PROV PD AMT on the remit."""
    assert document.total_prov_pd == EXPECTED_TOTAL_PROV_PD


def test_visit_payments_sum_to_totals_line(visits):
    """Aggregation must not lose or double-count a single cent."""
    assert round(sum(v.payment for v in visits), 2) == EXPECTED_TOTAL_PROV_PD


def test_every_service_line_uses_a_performing_provider_npi(document):
    """The group billing NPI from the page header must never leak in."""
    assert {line.npi for line in document.service_lines} == {SYNTHETIC_PROVIDER_NPI}


# --- Per-visit ground truth ------------------------------------------------

@pytest.mark.parametrize(
    "patient, service_date, cpt, payment, copay",
    [
        ("MARLOWE, DIANE", date(2026, 3, 9), "99213/90833", 166.37, 42.44),
        ("MARLOWE, DIANE", date(2026, 4, 21), "99214/90836", 224.59, 57.29),
        ("OKAFOR, CHIDINMA", date(2026, 4, 28), "99214", 130.46, 33.28),
        ("PETROSSIAN, KNARIK S", date(2025, 12, 25), "99213/90833", 166.37, 42.44),
        ("CASTELLANO, MIGUEL", date(2026, 3, 26), "99213/90836", 186.31, 47.53),
    ],
)
def test_visit_values(visits, patient, service_date, cpt, payment, copay):
    visit = find_visit(visits, patient, service_date)
    assert visit.cpt_display == cpt
    assert visit.payment == payment
    assert visit.copay == copay


def test_comment_comes_from_performing_provider_npi(visits):
    visit = find_visit(visits, "MARLOWE, DIANE", date(2026, 3, 9))
    assert visit.doctor == "Dr. A"
    assert visit.known_provider


def test_single_cpt_visit_has_no_separator(visits):
    """OKAFOR has one service line, so no add-on and no slash."""
    visit = find_visit(visits, "OKAFOR, CHIDINMA", date(2026, 4, 28))
    assert visit.cpt_display == "99214"
    assert len(visit.cpt_codes) == 1


def test_two_digit_year_spans_2025(visits):
    """`122525`-style tokens must land in 2025, not 2026."""
    visit = find_visit(visits, "PETROSSIAN, KNARIK S", date(2025, 12, 25))
    assert visit.service_date.year == 2025
    assert visit.data_str == "12/25/2025"


def test_billed_date_is_shared_by_every_visit(visits):
    month, day, year = SYNTHETIC_HEADER_DATE.split("/")
    expected = f"{month}/{day}/20{year}"
    assert {v.billed_str for v in visits} == {expected}


def test_same_patient_aggregated_across_claim_blocks(visits):
    """CASTELLANO appears under two claim blocks; dates must not be merged."""
    castellano = sorted(
        (v for v in visits if v.patient == "CASTELLANO, MIGUEL"),
        key=lambda v: v.service_date,
    )
    assert [v.data_str for v in castellano] == [
        "03/26/2026", "04/09/2026", "04/21/2026", "05/07/2026",
    ]


def test_truncated_and_middle_initial_names_are_preserved_verbatim(visits):
    """The parser reports the name as printed; harmonising is matching's job."""
    names = {v.patient for v in visits}
    assert "FEATHERSTONEH, WILHELMI" in names
    assert "SORENSEN, MARCUS R" in names


# --- Service-line field extraction ----------------------------------------

def test_modifier_does_not_shift_fields():
    """The optional `95` telehealth modifier must not move COINS or PROV-PD."""
    plain = parse_service_line(
        "1000000001 0309 030926 11 1.0 99213 200.00 117.58 0.00 23.52 CO-45 82.42 92.18",
        "TEST, PATIENT", "f.pdf",
    )
    with_modifier = parse_service_line(
        "1000000001 0313 031326 10 1.0 99213 95 200.00 117.58 0.00 23.52 CO-45 82.42 92.18",
        "TEST, PATIENT", "f.pdf",
    )
    for line in (plain, with_modifier):
        assert line is not None
        assert line.proc == "99213"
        assert line.coins == 23.52
        assert line.prov_pd == 92.18


def test_continuation_lines_are_ignored():
    """REM:/reason-code lines carry amounts but are not service lines."""
    assert parse_service_line("REM: N782 CO-253 1.88", "TEST", "f.pdf") is None
    assert parse_service_line("CO-253 1.88", "TEST", "f.pdf") is None


def test_totals_and_noise_lines_are_not_service_lines():
    noise = [
        "PT RESP 99.73 CLAIM TOTALS 800.00 498.66 0.00 99.73 309.31 390.96",
        "NET 390.96",
        "13 2992.55 1000.00 0.00 500.00 400.00 2992.55 0.00 2992.55",
        "CLAIM INFORMATION FORWARDED TO: EXAMPLE SUPPLEMENTAL PLAN",
        "NPI: 1000000000",
        "PERF PROV SERV DATE POS NOS PROC MODS BILLED ALLOWED DEDUCT COINS GRP/RC-AMT PROV PD",
    ]
    for line in noise:
        assert not is_service_line(line.split()), line


def test_service_date_uses_the_six_digit_token():
    assert parse_serv_date("030926") == date(2026, 3, 9)
    assert parse_serv_date("122525") == date(2025, 12, 25)
    assert parse_serv_date("0309") is None  # the 4-digit MMDD token
    assert parse_serv_date("023026") is None  # impossible calendar date


def test_expand_two_digit_year():
    assert expand_two_digit_year(25) == 2025
    assert expand_two_digit_year(26) == 2026
    assert expand_two_digit_year(2026) == 2026


def test_header_date_parsing():
    assert parse_header_date("DATE: 08/14/26") == date(2026, 8, 14)
    assert parse_header_date("no date here") is None


def test_cpt_ordering_puts_em_code_first():
    assert order_cpt_codes(["90833", "99213"]) == ["99213", "90833"]
    assert order_cpt_codes(["90836", "99214"]) == ["99214", "90836"]
    assert order_cpt_codes(["99214"]) == ["99214"]
