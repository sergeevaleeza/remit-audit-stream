"""Render the OA-18 duplicate fixture: `RemitDoc-0000000003.PDF`.

A later, fully synthetic remittance that re-adjudicates the visits of
`RemitDoc-0000000002.PDF` as **exact duplicates** -- Medicare reason code 18,
printed `OA-18` -- each at `$0.00`, because the original remittance already
paid them.

Uploading this alongside the paying remit is what used to zero out real
payments under plain `replace_with_latest`. It also contains:

* one genuinely **new, paid, non-duplicate** visit on a date the paying remit
  did not cover, so a later remit still contributes real work; and
* one visit present **only** as a duplicate, standing in for the case where the
  remittance that actually paid was never uploaded.

Run with:  python -m tests.fixtures.make_duplicate_fixture
"""

from __future__ import annotations

from pathlib import Path

from .make_remit_fixture import _mmdd, _mmddyy, render
from .synthetic_remit_data import (
    CPT_90834_ALLOWED,
    CPT_90834_BILLED,
    CPT_90834_RC,
    DUPLICATE_CHECK_EFT,
    DUPLICATE_FIXTURE_NEW_VISIT,
    DUPLICATE_HEADER_DATE,
    DUPLICATE_OF_DATES,
    DUPLICATE_ONLY_DATE,
    DEDUCT_PATIENT_ACCT,
    DEDUCT_PATIENT_MBI,
    DEDUCT_PATIENT_NAME,
    SYNTHETIC_CLINIC_ADDRESS,
    SYNTHETIC_CLINIC_NAME,
    SYNTHETIC_GROUP_NPI,
    SYNTHETIC_THERAPIST_NPI,
)

OUTPUT = Path(__file__).parent / "RemitDoc-0000000003.PDF"


def _line(iso_date: str, allowed: float, deduct: float, coins: float,
          group_reason: str, rc: float, provpd: float) -> str:
    return (
        f"{SYNTHETIC_THERAPIST_NPI} {_mmdd(iso_date)} {_mmddyy(iso_date)} 11 1.0 90834 "
        f"{CPT_90834_BILLED:.2f} {allowed:.2f} {deduct:.2f} {coins:.2f} "
        f"{group_reason} {rc:.2f} {provpd:.2f}"
    )


def _duplicate_line(iso_date: str) -> str:
    """An exact-duplicate adjudication: whole charge to OA-18, $0.00 paid."""
    return _line(iso_date, 0.00, 0.00, 0.00, "OA-18", CPT_90834_BILLED, 0.00)


def build_lines() -> tuple[list[str], int, float]:
    lines: list[str] = [
        "NORIDIAN HEALTHCARE SOLUTIONS, LLC",
        "MEDICARE",
        "P.O. BOX 0000",
        "REMITTANCE",
        "EXAMPLE ND 00000-0000",
        "000-000-0000",
        f"NPI: {SYNTHETIC_GROUP_NPI}",
        f"{SYNTHETIC_CLINIC_NAME}",
        "ADVICE",
        f"DATE: {DUPLICATE_HEADER_DATE}",
        f"{SYNTHETIC_CLINIC_ADDRESS[0]}",
        f"{SYNTHETIC_CLINIC_ADDRESS[1]}",
        f"CHECK/EFT #: {DUPLICATE_CHECK_EFT}",
        "PERF PROV SERV DATE POS NOS PROC MODS BILLED ALLOWED DEDUCT COINS GRP/RC-AMT PROV PD",
        "_________ _________ ___ ___ ____ ____ ______ _______ ______ _____ __________ _______",
        "_" * 118,
    ]

    claim_count = 0
    grand_total = 0.0
    totals = dict(billed=0.0, allowed=0.0, deduct=0.0, coins=0.0, rc=0.0)

    def close_claim(billed, allowed, deduct, coins, rc, prov):
        nonlocal grand_total
        grand_total += prov
        totals["billed"] += billed
        totals["allowed"] += allowed
        totals["deduct"] += deduct
        totals["coins"] += coins
        totals["rc"] += rc
        lines.append(
            f"PT RESP {coins:.2f} CLAIM TOTALS {billed:.2f} {allowed:.2f} "
            f"{deduct:.2f} {coins:.2f} {rc:.2f} {prov:.2f}"
        )
        lines.append("CLAIM INFORMATION FORWARDED TO: EXAMPLE SUPPLEMENTAL PLAN")
        lines.append(f"NET {prov:.2f}")
        lines.append("_" * 118)

    def header(icn: str) -> None:
        lines.append(
            f"NAME {DEDUCT_PATIENT_NAME} MID {DEDUCT_PATIENT_MBI} "
            f"ACNT {DEDUCT_PATIENT_ACCT} ICN {icn} ASG Y MOA MA01"
        )

    # --- Claim 1: the duplicates of the paying remit's visits --------------
    claim_count += 1
    header("9999900000401")
    b = a = d = c = r = p = 0.0
    for iso_date in DUPLICATE_OF_DATES:
        lines.append(_duplicate_line(iso_date))
        # The duplicate's own continuation line, with no `REM:` prefix.
        lines.append("OA-18 0.00")
        b += CPT_90834_BILLED
        r += CPT_90834_BILLED
    close_claim(b, a, d, c, r, p)

    # --- Claim 2: a genuinely new, genuinely paid visit --------------------
    claim_count += 1
    header("9999900000402")
    iso_date, deduct, coins, provpd, sequestration = DUPLICATE_FIXTURE_NEW_VISIT
    lines.append(_line(iso_date, CPT_90834_ALLOWED, deduct, coins,
                       "CO-45", CPT_90834_RC, provpd))
    lines.append(f"CO-253 {sequestration:.2f}")
    close_claim(CPT_90834_BILLED, CPT_90834_ALLOWED, deduct, coins,
                CPT_90834_RC, provpd)

    # --- Claim 3: a visit seen ONLY as a duplicate -------------------------
    claim_count += 1
    header("9999900000403")
    lines.append(_duplicate_line(DUPLICATE_ONLY_DATE))
    lines.append("OA-18 0.00")
    close_claim(CPT_90834_BILLED, 0.00, 0.00, 0.00, CPT_90834_BILLED, 0.00)

    lines.append("TOTALS: # OF BILLED ALLOWED DEDUCT COINS TOTAL PROV PD PROV CHECK")
    lines.append("CLAIMS AMT AMT AMT AMT RC-AMT AMT ADJ AMT AMT")
    lines.append(
        f"{claim_count} {totals['billed']:.2f} {totals['allowed']:.2f} "
        f"{totals['deduct']:.2f} {totals['coins']:.2f} {totals['rc']:.2f} "
        f"{grand_total:.2f} 0.00 {grand_total:.2f}"
    )
    lines.append("GLOSSARY: Group, Reason, MOA, Remark and Adjustment Codes")
    lines.append("18 Exact duplicate claim/service (example text).")
    lines.append("253 Sequestration - reduction in federal payment (example text).")

    return lines, claim_count, round(grand_total, 2)


def main() -> None:
    lines, claim_count, grand_total = build_lines()
    render(lines, OUTPUT)
    print(f"wrote {OUTPUT}")
    print(f"claim_count={claim_count} grand_total_prov_pd={grand_total}")


if __name__ == "__main__":
    main()
