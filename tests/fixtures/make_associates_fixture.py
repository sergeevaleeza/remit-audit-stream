"""Render the associate-NPI fixture: `RemitDoc-0000000004.PDF`.

A fully synthetic remittance whose service lines carry **three different
performing-provider NPIs** -- two associates who bill under their own, and the
supervising physician everyone else bills under.

Every other fixture bills under a single NPI, which cannot exercise the
employees workbook's NPI-gated append rule: a visit is added to Ana's or
Oxana's tab only when the EOB says that associate performed it. This fixture
is what gives that rule something to gate on, plus the two cases that must be
surfaced rather than guessed at -- a patient whose tab and whose EOB disagree
about the practitioner, and two providers billing the same patient on one day.

Run with:  python -m tests.fixtures.make_associates_fixture
"""

from __future__ import annotations

from pathlib import Path

from .make_remit_fixture import _mmdd, _mmddyy, render
from .synthetic_remit_data import (
    ASSOCIATES_CHECK_EFT,
    ASSOCIATES_CLAIMS,
    ASSOCIATES_HEADER_DATE,
    CPT_RECIPE,
    SYNTHETIC_CLINIC_ADDRESS,
    SYNTHETIC_CLINIC_NAME,
    SYNTHETIC_GROUP_NPI,
    associates_mbi,
)

OUTPUT = Path(__file__).parent / "RemitDoc-0000000004.PDF"


def service_line_text(npi: str, iso_date: str, cpt: str) -> tuple[str, dict]:
    """One `PERF PROV ... PROV PD` row, billed under `npi`.

    All in-office (POS 11) and unmodified: this fixture varies the performing
    provider and nothing else, so a failure here can only be about the NPI.
    """
    recipe = CPT_RECIPE[cpt]
    text = (
        f"{npi} {_mmdd(iso_date)} {_mmddyy(iso_date)} 11 1.0 {cpt} "
        f"{recipe['billed']:.2f} {recipe['allowed']:.2f} {recipe['deduct']:.2f} "
        f"{recipe['coins']:.2f} CO-45 {recipe['rc']:.2f} {recipe['provpd']:.2f}"
    )
    return text, recipe


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
        f"DATE: {ASSOCIATES_HEADER_DATE}",
        f"{SYNTHETIC_CLINIC_ADDRESS[0]}",
        f"{SYNTHETIC_CLINIC_ADDRESS[1]}",
        f"CHECK/EFT #: {ASSOCIATES_CHECK_EFT}",
        "PERF PROV SERV DATE POS NOS PROC MODS BILLED ALLOWED DEDUCT COINS GRP/RC-AMT PROV PD",
        "_________ _________ ___ ___ ____ ____ ______ _______ ______ _____ __________ _______",
        "_" * 118,
    ]

    grand_total = 0.0
    totals = dict(billed=0.0, allowed=0.0, deduct=0.0, coins=0.0, rc=0.0)

    for index, (printed_name, seed, acct, specs) in enumerate(ASSOCIATES_CLAIMS, start=1):
        lines.append(
            f"NAME {printed_name} MID {associates_mbi(seed)} ACNT {acct} "
            f"ICN 99999000{index:05d} ASG Y MOA MA01 MA07"
        )
        block = dict(billed=0.0, allowed=0.0, deduct=0.0, coins=0.0, rc=0.0, provpd=0.0)
        for iso_date, npi, cpt in specs:
            text, recipe = service_line_text(npi, iso_date, cpt)
            lines.append(text)
            lines.append("REM: N782 CO-253 1.00")  # continuation noise; ignored
            for key in ("billed", "allowed", "deduct", "coins", "rc"):
                block[key] += recipe[key]
                totals[key] += recipe[key]
            block["provpd"] += recipe["provpd"]

        grand_total += block["provpd"]
        lines.append(
            f"PT RESP {block['coins']:.2f} CLAIM TOTALS {block['billed']:.2f} "
            f"{block['allowed']:.2f} {block['deduct']:.2f} {block['coins']:.2f} "
            f"{block['rc']:.2f} {block['provpd']:.2f}"
        )
        lines.append("CLAIM INFORMATION FORWARDED TO: EXAMPLE SUPPLEMENTAL PLAN")
        lines.append(f"NET {block['provpd']:.2f}")
        lines.append("_" * 118)

    claim_count = len(ASSOCIATES_CLAIMS)
    lines.append("TOTALS: # OF BILLED ALLOWED DEDUCT COINS TOTAL PROV PD PROV CHECK")
    lines.append("CLAIMS AMT AMT AMT AMT RC-AMT AMT ADJ AMT AMT")
    lines.append(
        f"{claim_count} {totals['billed']:.2f} {totals['allowed']:.2f} "
        f"{totals['deduct']:.2f} {totals['coins']:.2f} {totals['rc']:.2f} "
        f"{grand_total:.2f} 0.00 {grand_total:.2f}"
    )
    lines.append("GLOSSARY: Group, Reason, MOA, Remark and Adjustment Codes")
    lines.append(
        "CO Contractual Obligation. Amount for which the provider is financially liable."
    )

    return lines, claim_count, round(grand_total, 2)


def main() -> None:
    lines, claim_count, grand_total = build_lines()
    render(lines, OUTPUT)
    print(f"wrote {OUTPUT}")
    print(f"claim_count={claim_count} grand_total_prov_pd={grand_total}")


if __name__ == "__main__":
    main()
