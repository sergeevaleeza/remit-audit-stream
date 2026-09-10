"""Render the deductible regression fixture: `RemitDoc-0000000002.PDF`.

A second, fully synthetic remittance built to reproduce the structural shapes
that make a service line easy to misparse:

* two lines where ALLOWED is entirely consumed by DEDUCT, so `PROV-PD` is a
  **genuine** `0.00` rather than a parse failure;
* a line with a *partial* deductible;
* six fully-paid lines;
* **bare `CO-253 <amt>` continuation lines with no `REM:` prefix** -- the
  reason continuations are skipped on "does not start with a 10-digit NPI"
  rather than on the `REM:` prefix;
* the same patient billed under two different performing-provider NPIs.

Run with:  python -m tests.fixtures.make_deductible_fixture
"""

from __future__ import annotations

from pathlib import Path

from .make_remit_fixture import _mmdd, _mmddyy, render
from .synthetic_remit_data import (
    CPT_90834_ALLOWED,
    CPT_90834_BILLED,
    CPT_90834_RC,
    CPT_RECIPE,
    DEDUCT_CHECK_EFT,
    DEDUCT_HEADER_DATE,
    DEDUCT_PATIENT_ACCT,
    DEDUCT_PATIENT_MBI,
    DEDUCT_PATIENT_NAME,
    DEDUCTIBLE_LINES,
    PHYSICIAN_CLAIM_LINES,
    SYNTHETIC_CLINIC_ADDRESS,
    SYNTHETIC_CLINIC_NAME,
    SYNTHETIC_GROUP_NPI,
    SYNTHETIC_PROVIDER_NPI,
    SYNTHETIC_THERAPIST_NPI,
)

OUTPUT = Path(__file__).parent / "RemitDoc-0000000002.PDF"


def _service_line(npi: str, iso_date: str, cpt: str, billed: float, allowed: float,
                  deduct: float, coins: float, rc: float, provpd: float) -> str:
    """One canonical service line: six amounts in the documented order."""
    return (
        f"{npi} {_mmdd(iso_date)} {_mmddyy(iso_date)} 11 1.0 {cpt} "
        f"{billed:.2f} {allowed:.2f} {deduct:.2f} {coins:.2f} "
        f"CO-45 {rc:.2f} {provpd:.2f}"
    )


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
        f"DATE: {DEDUCT_HEADER_DATE}",
        f"{SYNTHETIC_CLINIC_ADDRESS[0]}",
        f"{SYNTHETIC_CLINIC_ADDRESS[1]}",
        f"CHECK/EFT #: {DEDUCT_CHECK_EFT}",
        "PERF PROV SERV DATE POS NOS PROC MODS BILLED ALLOWED DEDUCT COINS GRP/RC-AMT PROV PD",
        "_________ _________ ___ ___ ____ ____ ______ _______ ______ _____ __________ _______",
        "_" * 118,
    ]

    claim_count = 0
    grand_total = 0.0
    totals = dict(billed=0.0, allowed=0.0, deduct=0.0, coins=0.0, rc=0.0)

    def close_claim(billed, allowed, deduct, coins, rc, prov, forwarded):
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
        lines.append(f"CLAIM INFORMATION FORWARDED TO: {forwarded}")
        lines.append(f"NET {prov:.2f}")
        lines.append("_" * 118)

    # --- Claim 1: the therapist's 90834 series, with deductibles ------------
    claim_count += 1
    lines.append(
        f"NAME {DEDUCT_PATIENT_NAME} MID {DEDUCT_PATIENT_MBI} "
        f"ACNT {DEDUCT_PATIENT_ACCT} ICN 9999900000201 ASG Y MOA MA01"
    )
    b = a = d = c = r = p = 0.0
    for iso_date, deduct, coins, provpd, sequestration in DEDUCTIBLE_LINES:
        lines.append(_service_line(
            SYNTHETIC_THERAPIST_NPI, iso_date, "90834",
            CPT_90834_BILLED, CPT_90834_ALLOWED, deduct, coins, CPT_90834_RC, provpd,
        ))
        if sequestration is not None:
            # BARE continuation: no `REM:` prefix, and it carries an amount.
            # It must be skipped because it does not start with a 10-digit NPI.
            lines.append(f"CO-253 {sequestration:.2f}")
        else:
            lines.append("REM: N211")
        b += CPT_90834_BILLED
        a += CPT_90834_ALLOWED
        d += deduct
        c += coins
        r += CPT_90834_RC
        p += provpd
    close_claim(b, a, d, c, r, round(p, 2), "EXAMPLE SUPPLEMENTAL PLAN")

    # --- Claims 2 & 3: the same patient under the physician's NPI -----------
    for index, (iso_date, cpts) in enumerate(PHYSICIAN_CLAIM_LINES):
        claim_count += 1
        lines.append(
            f"NAME {DEDUCT_PATIENT_NAME} MID {DEDUCT_PATIENT_MBI} "
            f"ACNT {DEDUCT_PATIENT_ACCT} ICN 999990000030{index} ASG Y MOA MA01"
        )
        b = a = d = c = r = p = 0.0
        for cpt in cpts:
            recipe = CPT_RECIPE[cpt]
            lines.append(_service_line(
                SYNTHETIC_PROVIDER_NPI, iso_date, cpt,
                recipe["billed"], recipe["allowed"], recipe["deduct"],
                recipe["coins"], recipe["rc"], recipe["provpd"],
            ))
            lines.append("CO-253 1.00")  # bare continuation again
            b += recipe["billed"]
            a += recipe["allowed"]
            d += recipe["deduct"]
            c += recipe["coins"]
            r += recipe["rc"]
            p += recipe["provpd"]
        close_claim(b, a, d, c, r, round(p, 2), "EXAMPLE SUPPLEMENTAL PLAN")

    lines.append("TOTALS: # OF BILLED ALLOWED DEDUCT COINS TOTAL PROV PD PROV CHECK")
    lines.append("CLAIMS AMT AMT AMT AMT RC-AMT AMT ADJ AMT AMT")
    lines.append(
        f"{claim_count} {totals['billed']:.2f} {totals['allowed']:.2f} "
        f"{totals['deduct']:.2f} {totals['coins']:.2f} {totals['rc']:.2f} "
        f"{grand_total:.2f} 0.00 {grand_total:.2f}"
    )
    lines.append("GLOSSARY: Group, Reason, MOA, Remark and Adjustment Codes")
    lines.append("253 Sequestration - reduction in federal payment (example text).")
    lines.append("N211 Alert: You may not appeal this decision (example text).")

    return lines, claim_count, round(grand_total, 2)


def main() -> None:
    lines, claim_count, grand_total = build_lines()
    render(lines, OUTPUT)
    print(f"wrote {OUTPUT}")
    print(f"claim_count={claim_count} grand_total_prov_pd={grand_total}")


if __name__ == "__main__":
    main()
