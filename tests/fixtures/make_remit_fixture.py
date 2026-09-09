"""Render `synthetic_remit_data.ROSTER` into a Noridian-shaped, fully synthetic
remittance PDF for `tests/fixtures/`.

Renders real selectable text (Courier, so pdfplumber's extraction matches the
real remit's layout) -- not an image -- so the fixture exercises the actual
parser, not a mock of it.

Run with:  python -m tests.fixtures.make_remit_fixture
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from .synthetic_remit_data import (
    CPT_RECIPE,
    ROSTER,
    SYNTHETIC_CHECK_EFT,
    SYNTHETIC_CLINIC_ADDRESS,
    SYNTHETIC_CLINIC_NAME,
    SYNTHETIC_GROUP_NPI,
    SYNTHETIC_HEADER_DATE,
    SYNTHETIC_PROVIDER_NPI,
)

OUTPUT = Path(__file__).parent / "RemitDoc-0000000001.PDF"

FONT = "Courier"
FONT_SIZE = 8
LEFT_MARGIN = 36
TOP_MARGIN = 760
LINE_HEIGHT = 10.2
LINES_PER_PAGE = 68


def _mmdd(iso_date: str) -> str:
    y, m, d = iso_date.split("-")
    return f"{m}{d}"


def _mmddyy(iso_date: str) -> str:
    y, m, d = iso_date.split("-")
    return f"{m}{d}{y[2:]}"


def service_line_text(cpt: str, iso_date: str, telehealth: bool) -> tuple[str, dict]:
    recipe = CPT_RECIPE[cpt]
    pos = "10" if telehealth else "11"
    modifier = " 95" if telehealth else ""
    text = (
        f"{SYNTHETIC_PROVIDER_NPI} {_mmdd(iso_date)} {_mmddyy(iso_date)} {pos} 1.0 {cpt}{modifier} "
        f"{recipe['billed']:.2f} {recipe['allowed']:.2f} {recipe['deduct']:.2f} {recipe['coins']:.2f} "
        f"CO-45 {recipe['rc']:.2f} {recipe['provpd']:.2f}"
    )
    return text, recipe


def build_lines() -> tuple[list[str], int, float]:
    """The full page text as a flat list of lines, plus claim count and the
    grand total PROV-PD (so the fixture's own TOTALS line is self-consistent).
    """
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
        f"DATE: {SYNTHETIC_HEADER_DATE}",
        f"{SYNTHETIC_CLINIC_ADDRESS[0]}",
        f"{SYNTHETIC_CLINIC_ADDRESS[1]}",
        f"CHECK/EFT #: {SYNTHETIC_CHECK_EFT}",
        "PERF PROV SERV DATE POS NOS PROC MODS BILLED ALLOWED DEDUCT COINS GRP/RC-AMT PROV PD",
        "_________ _________ ___ ___ ____ ____ ______ _______ ______ _____ __________ _______",
        "_" * 118,
    ]

    claim_count = 0
    grand_total = 0.0

    for patient in ROSTER:
        for claim in patient.claims:
            claim_count += 1
            lines.append(
                f"NAME {claim.printed_name} MID {claim.mbi} ACNT {claim.acct} "
                f"ICN {claim.icn} ASG Y MOA MA01 MA07"
            )
            billed_sum = allowed_sum = deduct_sum = coins_sum = rc_sum = prov_sum = 0.0
            for spec in claim.lines:
                text, recipe = service_line_text(spec.cpt, spec.service_date, spec.telehealth)
                lines.append(text)
                lines.append("REM: N782 CO-253 1.00")  # continuation noise; must be ignored
                billed_sum += recipe["billed"]
                allowed_sum += recipe["allowed"]
                deduct_sum += recipe["deduct"]
                coins_sum += recipe["coins"]
                rc_sum += recipe["rc"]
                prov_sum += recipe["provpd"]

            grand_total += prov_sum
            lines.append(
                f"PT RESP {coins_sum:.2f} CLAIM TOTALS {billed_sum:.2f} {allowed_sum:.2f} "
                f"{deduct_sum:.2f} {coins_sum:.2f} {rc_sum:.2f} {prov_sum:.2f}"
            )
            lines.append(f"CLAIM INFORMATION FORWARDED TO: {claim.forwarded_to}")
            lines.append(f"NET {prov_sum:.2f}")
            lines.append("_" * 118)

    lines.append(
        "TOTALS: # OF BILLED ALLOWED DEDUCT COINS TOTAL PROV PD PROV CHECK"
    )
    lines.append("CLAIMS AMT AMT AMT AMT RC-AMT AMT ADJ AMT AMT")

    total_billed = sum(CPT_RECIPE[s.cpt]["billed"] for p in ROSTER for c in p.claims for s in c.lines)
    total_allowed = sum(CPT_RECIPE[s.cpt]["allowed"] for p in ROSTER for c in p.claims for s in c.lines)
    total_deduct = sum(CPT_RECIPE[s.cpt]["deduct"] for p in ROSTER for c in p.claims for s in c.lines)
    total_coins = sum(CPT_RECIPE[s.cpt]["coins"] for p in ROSTER for c in p.claims for s in c.lines)
    total_rc = sum(CPT_RECIPE[s.cpt]["rc"] for p in ROSTER for c in p.claims for s in c.lines)

    lines.append(
        f"{claim_count} {total_billed:.2f} {total_allowed:.2f} {total_deduct:.2f} "
        f"{total_coins:.2f} {total_rc:.2f} {grand_total:.2f} 0.00 {grand_total:.2f}"
    )
    lines.append("GLOSSARY: Group, Reason, MOA, Remark and Adjustment Codes")
    lines.append(
        "CO Contractual Obligation. Amount for which the provider is financially liable."
    )
    lines.append("253 Sequestration - reduction in federal payment (example text).")

    return lines, claim_count, round(grand_total, 2)


def render(lines: list[str], out_path: Path) -> None:
    pdf = canvas.Canvas(str(out_path), pagesize=letter)
    y = TOP_MARGIN
    count_on_page = 0
    for line in lines:
        if count_on_page >= LINES_PER_PAGE:
            pdf.showPage()
            y = TOP_MARGIN
            count_on_page = 0
        pdf.setFont(FONT, FONT_SIZE)
        pdf.drawString(LEFT_MARGIN, y, line)
        y -= LINE_HEIGHT
        count_on_page += 1
    pdf.showPage()
    pdf.save()


def main() -> None:
    lines, claim_count, grand_total = build_lines()
    render(lines, OUTPUT)
    print(f"wrote {OUTPUT}")
    print(f"claim_count={claim_count} grand_total_prov_pd={grand_total}")


if __name__ == "__main__":
    main()
