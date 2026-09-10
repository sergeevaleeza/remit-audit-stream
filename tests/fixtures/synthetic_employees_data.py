"""Source of truth for the synthetic `AMSMC_employees.xlsx` fixture.

Every patient name and amount here is fictional and matches the fictional
roster in `synthetic_remit_data.py`. The real employees workbook is PHI and is
gitignored -- never commit it.

The three sheets deliberately reproduce the real file's quirks:

* Ana and Marcia carry their header on **row 1**; Oxana on **row 2**.
* Oxana's patient column is headed `Patient`, the others `Patient Name`.
* Several labels contain doubled internal spaces (`Co-payment   Old`) or odd
  spacing (`Paid by Ins toAna`), which is why headers are matched with
  whitespace collapsed.
"""

from __future__ import annotations

ANA_HEADERS = [
    "Patient Name", "Insurance", "Co-pay by EOB", "Date of Session", "Billed",
    "Paid by Insurance", "Co-Pay", "Paid by Ins toAna", "Co-pay to Ana",
    "Co-payment   Old", "Memo", "Paid to Ana", "Invoice Date",
]

MARCIA_HEADERS = [
    "Patient Name", "Insurance", "Co-pay by EOB", "Date of Session", "Billed",
    "Paid by Insurance", "Co-payment Paid", "Co-payment   Old", "Memo",
    "Paid to Marcia", "invoice sent",
]

OXANA_HEADERS = [
    "Patient", "Insurance", "Co-pay by EOB", "Date of Session", "Billed",
    "Paid by Insurance", "Co-payment Paid", "Co-payment   Old", "Memo",
]

#: Title above Oxana's header row, which is why hers sits on row 2.
OXANA_TITLE = "Oxana - 2026 sessions"

# --- Existing rows ----------------------------------------------------------
# (patient, date of session, prefilled values by header)
#
# `Castellano, Miguel` is Ana's; the schedule's Comment for him is "Dr. A",
# which is NOT a practitioner tab name, so it never conflicts.
# `Marlowe, Diane` sits in Ana's tab; the schedule fixture sets her Comment to
# "Oxana", which IS a provider tab -> practitioner mismatch, flagged not filled.
# (A Comment naming a physician such as "Dr. A" is not a conflict: it says
# nothing about which tab a session belongs to.)
# `Thackeray, Renata` is Oxana's, with a blank Comment on the schedule.
# `Whitfield, Harold` is Marcia's (fill-only tab).
# `Delacroix, Owen` has a session that is not in the remit at all.

ANA_ROWS = [
    ("Castellano, Miguel", "03/26/2026", {}),
    # Already has a payment recorded -> never overwritten.
    ("Castellano, Miguel", "04/09/2026", {"Paid by Ins toAna": 999.99}),
    ("Marlowe, Diane", "03/09/2026", {}),
    ("Delacroix, Owen", "01/05/2026", {}),
]

MARCIA_ROWS = [
    ("Whitfield, Harold", "03/04/2026", {}),
]

OXANA_ROWS = [
    ("Thackeray, Renata", "03/20/2026", {}),
    ("Thackeray, Renata", "04/07/2026", {}),
]
