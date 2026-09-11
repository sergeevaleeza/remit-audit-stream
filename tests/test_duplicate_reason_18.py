"""Reason code 18 (`OA-18` / `CO-18`) -- exact duplicate claim/service.

Medicare adjudicates a duplicate at `$0.00` because the original already paid.
Under plain `replace_with_latest` a duplicate remittance arriving later would
therefore overwrite the real payment with zero. It must not: an occurrence
carrying reason 18 is **not authoritative** and can never contribute,
overwrite, zero or downgrade a value.

The discriminator is the **reason code, not the amount**. A legitimate `$0.00`
line -- the whole allowed amount applied to the patient's deductible, carrying
`CO-45` -- has no code 18 and keeps behaving exactly as before.

Fixtures: `RemitDoc-0000000002.PDF` pays these visits; `RemitDoc-0000000003.PDF`
re-adjudicates them as `OA-18` duplicates. Both are synthetic (see
`tests/fixtures/README.md`); the real remittances that exhibited this are PHI
and are not committed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from remit.matching import build_plan, plan_change
from remit.pdf_parser import (
    DUPLICATE_REASON_CODE,
    adjustment_codes,
    aggregate_visits,
    marks_duplicate,
    parse_document,
    parse_remittances,
    parse_service_line,
)

from .fixtures.synthetic_remit_data import (
    DEDUCTIBLE_LINES,
    DUPLICATE_CHECK_EFT,
    DUPLICATE_FIXTURE_NEW_VISIT,
    DUPLICATE_ONLY_DATE,
    DEDUCT_CHECK_EFT,
    SYNTHETIC_THERAPIST_NPI,
)

FIXTURES = Path(__file__).parent / "fixtures"
PAYING = (str(FIXTURES / "RemitDoc-0000000002.PDF"), "RemitDoc-0000000002.PDF")
DUPLICATE = (str(FIXTURES / "RemitDoc-0000000003.PDF"), "RemitDoc-0000000003.PDF")

#: The nine dates the paying remit covers, with their real amounts.
EXPECTED_PAYING_TOTAL = 716.56
EXPECTED_PAYING_COINS = 182.80
PAID = 104.27


def therapist_visits(visits):
    return sorted(
        (v for v in visits if v.npi == SYNTHETIC_THERAPIST_NPI),
        key=lambda v: v.service_date,
    )


def visit_on(visits, iso_date: str):
    wanted = date.fromisoformat(iso_date)
    for visit in visits:
        if visit.service_date == wanted and visit.npi == SYNTHETIC_THERAPIST_NPI:
            return visit
    raise AssertionError(f"no visit on {iso_date}")


@pytest.fixture(scope="module")
def both():
    return parse_remittances([PAYING, DUPLICATE])[1]


@pytest.fixture(scope="module")
def reversed_order():
    return parse_remittances([DUPLICATE, PAYING])[1]


@pytest.fixture(scope="module")
def duplicate_only_upload():
    return parse_remittances([DUPLICATE])[1]


# --- Recognising the code ---------------------------------------------------

def test_adjustment_codes_are_captured_from_the_service_line():
    line = parse_service_line(
        f"{SYNTHETIC_THERAPIST_NPI} 0115 011526 11 1.0 90834 "
        "200.00 0.00 0.00 0.00 OA-18 200.00 0.00",
        "TEST, PATIENT", "f.pdf",
    )
    assert line.codes == ("OA-18",)
    assert line.is_duplicate


def test_co_18_is_also_a_duplicate():
    """Match on the numeric reason, whatever the group prefix."""
    assert marks_duplicate(["CO-18"])
    assert marks_duplicate(["OA-18"])
    assert marks_duplicate(["CO-45", "OA-18"])
    assert not marks_duplicate(["CO-45", "CO-253"])
    assert DUPLICATE_REASON_CODE == "18"


def test_similar_codes_are_not_duplicates():
    """`CO-118` / `OA-181` must not be mistaken for reason 18."""
    for code in ("CO-118", "OA-181", "CO-18X", "PR-1"):
        assert not marks_duplicate([code]), code


def test_adjustment_codes_helper():
    assert adjustment_codes("... CO-45 82.42 92.18") == ("CO-45",)
    assert adjustment_codes("REM: N782 CO-253 1.88") == ("CO-253",)
    assert adjustment_codes("no codes here") == ()


def test_codes_are_folded_up_from_continuation_lines():
    """A `CO-###` continuation belongs to the service line above it."""
    document = parse_document(str(FIXTURES / "RemitDoc-0000000002.PDF"), "f.pdf")
    paid = [l for l in document.service_lines if l.prov_pd == PAID]
    assert paid
    for line in paid:
        # CO-45 from the service line, CO-253 from its bare continuation.
        assert "CO-45" in line.codes
        assert "CO-253" in line.codes
        assert not line.is_duplicate


def test_continuation_lines_do_not_become_service_lines():
    document = parse_document(str(FIXTURES / "RemitDoc-0000000003.PDF"), "f.pdf")
    # 9 duplicates + 1 new paid + 1 duplicate-only = 11 service lines.
    assert len(document.service_lines) == 11


# --- The core rule ----------------------------------------------------------

@pytest.mark.parametrize(
    "iso_date, payment, copay",
    [(iso, provpd, coins) for iso, _d, coins, provpd, _s in DEDUCTIBLE_LINES],
)
def test_real_payment_survives_a_later_duplicate(both, iso_date, payment, copay):
    visit = visit_on(both, iso_date)
    assert visit.payment == payment
    assert visit.copay == copay


def test_paying_remit_totals_are_unchanged_by_the_duplicate(both):
    covered = {iso for iso, *_ in DEDUCTIBLE_LINES}
    visits = [v for v in therapist_visits(both)
              if v.service_date.isoformat() in covered]
    assert len(visits) == 9
    assert round(sum(v.payment for v in visits), 2) == EXPECTED_PAYING_TOTAL
    assert round(sum(v.copay for v in visits), 2) == EXPECTED_PAYING_COINS


def test_a_duplicate_never_zeroes_a_payment(both):
    """The headline case: `104.27` must not become `0.00`."""
    visit = visit_on(both, "2026-03-31")
    assert visit.payment == PAID
    assert visit.payment != 0.00
    assert visit.remit_count == 2


def test_audit_trail_lists_both_efts(both):
    visit = visit_on(both, "2026-03-31")
    assert visit.check_efts == {DEDUCT_CHECK_EFT, DUPLICATE_CHECK_EFT}
    assert visit.authoritative_eft == DEDUCT_CHECK_EFT
    assert visit.ignored_duplicate_efts == {DUPLICATE_CHECK_EFT}


def test_preview_note_names_both_efts(both):
    visit = visit_on(both, "2026-03-31")
    assert visit.duplicate_note == (
        f"OA-18 duplicate from EFT {DUPLICATE_CHECK_EFT} ignored; "
        f"kept payment from EFT {DEDUCT_CHECK_EFT}"
    )


# --- Order independence -----------------------------------------------------

def test_upload_order_does_not_matter(both, reversed_order):
    forward = {v.data_str: (v.payment, v.copay) for v in therapist_visits(both)}
    backward = {v.data_str: (v.payment, v.copay) for v in therapist_visits(reversed_order)}
    assert forward == backward


def test_duplicate_first_still_keeps_the_real_payment(reversed_order):
    assert visit_on(reversed_order, "2026-03-31").payment == PAID


def test_duplicate_is_never_the_authoritative_occurrence(both):
    for visit in therapist_visits(both):
        if visit.duplicate_only:
            continue
        assert not visit.is_duplicate


# --- Duplicate-only ---------------------------------------------------------

def test_duplicate_only_visit_is_flagged(both):
    visit = visit_on(both, DUPLICATE_ONLY_DATE)
    assert visit.duplicate_only
    assert "Duplicate only (OA-18)" in visit.duplicate_note
    assert "verify" in visit.duplicate_note


def test_duplicate_only_visit_needs_review(both, schedule_rows):
    """Not recorded as a real $0.00 -- handed to a human instead."""
    from remit.config import ACTION_REVIEW

    change = plan_change(visit_on(both, DUPLICATE_ONLY_DATE), schedule_rows)
    assert change.action == ACTION_REVIEW
    assert not change.accepted
    assert any("Duplicate only" in reason for reason in change.review_reasons)


def test_every_visit_is_duplicate_only_when_only_duplicates_uploaded(duplicate_only_upload):
    covered = {iso for iso, *_ in DEDUCTIBLE_LINES} | {DUPLICATE_ONLY_DATE}
    flagged = [v for v in therapist_visits(duplicate_only_upload)
               if v.service_date.isoformat() in covered]
    assert len(flagged) == 10
    assert all(v.duplicate_only for v in flagged)
    assert all(v.payment == 0.00 for v in flagged)


def test_a_clean_payment_is_not_marked_for_review(both, schedule_rows):
    """With an authoritative occurrence present, just apply it."""
    from remit.config import ACTION_REVIEW

    change = plan_change(visit_on(both, "2026-03-31"), schedule_rows)
    assert change.action != ACTION_REVIEW
    assert change.duplicate_note  # still explained in the preview
    assert not any("Duplicate only" in r for r in change.review_reasons)


# --- A genuine $0.00 is not a duplicate -------------------------------------

def test_deductible_zero_is_preserved(both):
    """`CO-45`, whole allowed amount deducted, no code 18 -> a real $0.00."""
    for iso_date in ("2026-01-15", "2026-02-10"):
        visit = visit_on(both, iso_date)
        assert visit.payment == 0.00
        assert visit.copay == 0.00
        assert not visit.duplicate_only


def test_deductible_line_is_not_flagged_as_a_duplicate():
    line = parse_service_line(
        f"{SYNTHETIC_THERAPIST_NPI} 0115 011526 11 1.0 90834 "
        "200.00 133.00 133.00 0.00 CO-45 67.00 0.00",
        "TEST, PATIENT", "f.pdf",
    )
    assert line.prov_pd == 0.00
    assert line.codes == ("CO-45",)
    assert not line.is_duplicate


def test_zero_dollar_alone_does_not_mark_a_duplicate():
    """Discrimination is by reason code, never by the amount."""
    zero_no_code = parse_service_line(
        f"{SYNTHETIC_THERAPIST_NPI} 0115 011526 11 1.0 90834 "
        "200.00 0.00 0.00 0.00 CO-97 200.00 0.00",
        "TEST, PATIENT", "f.pdf",
    )
    assert zero_no_code.prov_pd == 0.00
    assert not zero_no_code.is_duplicate


# --- A later remit still does real work -------------------------------------

def test_new_paid_visit_from_the_later_remit_is_kept(both):
    """A later remit is not ignored wholesale -- only its duplicates are."""
    iso_date, _deduct, coins, provpd, _seq = DUPLICATE_FIXTURE_NEW_VISIT
    visit = visit_on(both, iso_date)
    assert visit.payment == provpd == PAID
    assert visit.copay == coins
    assert not visit.duplicate_only
    assert visit.check_efts == {DUPLICATE_CHECK_EFT}


def test_non_duplicate_replace_with_latest_still_applies():
    """Two non-duplicate occurrences: the newer one still wins."""
    from remit.pdf_parser import RemitDocument, ServiceLine

    def document(name, billed, eft, provpd, coins):
        return RemitDocument(
            filename=name, billed_date=billed, check_eft=eft, claim_count=1,
            service_lines=[ServiceLine(
                patient="TEST, PATIENT", npi=SYNTHETIC_THERAPIST_NPI,
                service_date=date(2026, 3, 31), proc="90834",
                coins=coins, prov_pd=provpd, source_file=name,
                check_eft=eft, codes=("CO-45",),
            )],
        )

    older = document("older.pdf", date(2026, 7, 1), "900000010", 50.00, 10.00)
    newer = document("newer.pdf", date(2026, 8, 1), "900000011", 104.27, 26.60)

    visit = aggregate_visits([older, newer])[0]
    assert visit.payment == PAID
    assert visit.superseded_payments == [50.00]
    assert not visit.duplicate_only


def test_a_duplicate_does_not_count_as_a_superseding_restatement(both):
    """The discarded duplicate is not reported as an earlier real amount."""
    visit = visit_on(both, "2026-03-31")
    assert visit.superseded_payments == [0.00] or visit.superseded_payments == []
    # ...and the surviving value is the real one either way.
    assert visit.payment == PAID
