"""Central configuration for the remittance -> schedule updater.

Every tunable constant lives here so the parsing, matching and writing layers
never disagree about a format or a threshold.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# --- Workbook layout -------------------------------------------------------

#: The only sheet the app is ever allowed to touch.
SHEET_NAME = "2026 Medicare"

#: Row 1 is a title row (contains "2026"); row 2 holds the headers.
HEADER_ROW = 2
DATA_START_ROW = 3

#: Canonical header text, resolved by matching row 2 -- never by column letter.
COL_PATIENT = "Patient"
COL_INS = "Ins"
COL_DATA = "Data"
COL_BILLED = "Billed"
COL_PAYMENT = "Payment"
COL_COPAY = "Co-pay"
COL_COPAYS_PAID = "Co-pays Paid"
COL_OFFICE = "Office"
COL_COMMENT = "Comment"
COL_DX = "DX"
COL_CPT = "CPT Code"

#: Audit columns the app maintains itself, appended after CPT Code (L and M in
#: the standard layout). Created on demand if the sheet does not have them yet.
COL_PROCESSED_ON = "Processed On"
COL_CHECK_EFT = "Remit Check/EFT #"
AUDIT_COLUMNS = (COL_PROCESSED_ON, COL_CHECK_EFT)

#: Joins several check/EFT numbers when more than one remit feeds one row.
CHECK_EFT_JOINER = "; "

REQUIRED_COLUMNS = (
    COL_PATIENT,
    COL_INS,
    COL_DATA,
    COL_BILLED,
    COL_PAYMENT,
    COL_COPAY,
    COL_COMMENT,
    COL_CPT,
)

ALL_COLUMNS = REQUIRED_COLUMNS + (COL_COPAYS_PAID, COL_OFFICE, COL_DX) + AUDIT_COLUMNS

#: Cells this app is ever permitted to write on an existing row.
FILLABLE_COLUMNS = (COL_BILLED, COL_PAYMENT, COL_COPAY, COL_COMMENT)

#: Columns that are never touched under any circumstance.
NEVER_TOUCH_COLUMNS = (COL_COPAYS_PAID, COL_OFFICE)

# --- Value formats ---------------------------------------------------------

#: Data/Billed are free-text columns, so dates are written back as strings in
#: this format rather than coerced to Excel date serials.
DATE_FMT = "%m/%d/%Y"

#: Default `Ins` value: an ordinary in-office Medicare encounter.
INSURANCE_VALUE = "Medicare"

#: `Ins` value for a telehealth encounter -- POS 10 (patient's home) billed
#: with the 95 modifier. Derived per visit by `pdf_parser.insurance_label`.
TELEHEALTH_INSURANCE_LABEL = "POS 10(95)"

#: Existing schedule rows already say `Medicare`. Per never-overwrite they are
#: left alone and merely flagged when the EOB says telehealth. Set True to
#: rewrite those specific `Ins` cells to the telehealth label instead.
OVERWRITE_INS_FOR_TELEHEALTH = False

# --- Provider mapping -------------------------------------------------------
#
# A clinic's real provider names and NPIs are never committed to this repo.
# The mapping below is a synthetic placeholder shipped for local development
# and the test suite. To use real values, either:
#
#   1. Copy `npi_map.example.json` to `npi_map.local.json` (repo root) and
#      fill in real NPI -> doctor-name pairs. That file is gitignored.
#   2. On Streamlit Community Cloud, add an `[npi_to_doctor]` table to the
#      app's Secrets instead of shipping a file.
#
# Either source, if present, overrides the synthetic default below.

#: PERF PROV NPI (the one on the service line) -> Comment text.
#: NOT the "NPI:" in the page header, which is the group billing NPI.
_SYNTHETIC_NPI_TO_DOCTOR = {
    "1000000001": "Dr. A",
    "1000000002": "Dr. B",
    "1000000003": "Dr. C",
}

#: Group billing NPI that appears in the page header and must never be used
#: to resolve a doctor name. Override with REMIT_GROUP_NPI if needed.
GROUP_BILLING_NPI = os.environ.get("REMIT_GROUP_NPI", "1000000000")


def _load_local_npi_map() -> dict[str, str] | None:
    """A gitignored JSON file with the clinic's real NPI -> doctor mapping."""
    env_path = os.environ.get("REMIT_NPI_MAP_PATH")
    repo_root = Path(__file__).resolve().parent.parent
    for candidate in filter(None, (env_path, repo_root / "npi_map.local.json")):
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data:
            return {str(npi): str(name) for npi, name in data.items()}
    return None


def _load_secrets_npi_map() -> dict[str, str] | None:
    """An `[npi_to_doctor]` table in Streamlit secrets, when deployed."""
    try:
        import streamlit as st

        table = st.secrets.get("npi_to_doctor")
    except Exception:
        return None
    if table:
        return {str(npi): str(name) for npi, name in dict(table).items()}
    return None


def _resolve_npi_to_doctor() -> dict[str, str]:
    return _load_local_npi_map() or _load_secrets_npi_map() or dict(_SYNTHETIC_NPI_TO_DOCTOR)


NPI_TO_DOCTOR = _resolve_npi_to_doctor()

# --- Matching thresholds ---------------------------------------------------

#: rapidfuzz score at or above which a name match is applied automatically.
NAME_AUTO_MATCH_SCORE = 92.0

#: Scores in [NAME_REVIEW_MIN_SCORE, NAME_AUTO_MATCH_SCORE) are surfaced as
#: "Needs review" rather than applied or ignored. Below the floor the visit is
#: treated as a genuinely new patient/date and becomes a new row.
NAME_REVIEW_MIN_SCORE = 80.0

# --- Action labels ---------------------------------------------------------

ACTION_FILL = "Fill existing row"
ACTION_SKIP = "Skip (already paid)"
ACTION_NEW = "New row"
ACTION_REVIEW = "Needs review"

#: A later remit restated a visit that is already recorded with different
#: amounts. Distinct from a fill (the cell was blank) and from a skip (the
#: values agree), because applying it overwrites a recorded value.
ACTION_UPDATE = "Updated (adjusted EOB)"

# --- Reprocessing policy ----------------------------------------------------

#: What to do when a later remittance reports different amounts for a visit
#: that is already recorded in the schedule.
#:
#: ``replace_with_latest`` -- a later Medicare remit *restates* the claim, so
#: the recorded amount is replaced by the later remit's amount. This is the
#: default and the only policy that writes.
#: ``sum`` -- add the later remit's amount to what is recorded. Only correct
#: if the payer issues supplemental payments rather than restatements.
#: ``flag_only`` -- never propose a write; surface every difference for review.
REPROCESS_REPLACE = "replace_with_latest"
REPROCESS_SUM = "sum"
REPROCESS_FLAG_ONLY = "flag_only"

REPROCESS_POLICY = REPROCESS_REPLACE

#: Money is compared at cent precision; anything closer is the same value.
AMOUNT_TOLERANCE = 0.005

#: Two-digit years are expanded into this century.
CENTURY_PREFIX = 2000

# --- Employees workbook (AMSMC_employees.xlsx) ------------------------------
#
# An optional fourth upload. One sheet per practitioner; the header row is not
# in the same place on every sheet, so it is located by scanning the first two
# rows for the expected labels rather than assumed.

EMPLOYEE_SHEETS = ("Ana", "Marcia", "Oxana")

#: How far down to look for the header row (1-based, inclusive).
EMPLOYEE_HEADER_SEARCH_ROWS = 2

#: Where each provider's insurance payment goes. Ana's tab keeps the payer's
#: payment in a separate column from the practice's own `Paid by Insurance`.
EMPLOYEE_PAYMENT_COLUMN = {
    "Ana": "Paid by Ins toAna",
    "Oxana": "Paid by Insurance",
    "Marcia": "Paid by Insurance",
}

#: The patient-name header differs on Oxana's sheet.
EMPLOYEE_PATIENT_HEADERS = ("Patient Name", "Patient")

EMPLOYEE_COL_INSURANCE = "Insurance"
EMPLOYEE_COL_COPAY_EOB = "Co-pay by EOB"
EMPLOYEE_COL_DATE = "Date of Session"

#: Tabs the app may append brand-new visits to. Marcia's tab is fill-only.
EMPLOYEE_APPEND_SHEETS = ("Ana", "Oxana")

#: `all_matching` syncs every matching schedule visit into the tabs, deduped
#: by patient + Date of Session so nothing is added twice.
EMPLOYEES_SCOPE = "all_matching"

#: A patient in no provider tab cannot be assigned by name. With False they
#: are listed as unassigned; with True a brand-new patient is appended to the
#: tab named by their schedule `Comment`.
FALLBACK_TO_COMMENT_FOR_NEW = False

# --- Employees actions ------------------------------------------------------

EMP_ACTION_FILL = "Fill existing row"
EMP_ACTION_APPEND = "Append new row"
EMP_ACTION_NO_MATCH = "No Schedule match"
EMP_ACTION_MISMATCH = "Practitioner mismatch - needs review"
EMP_ACTION_UNASSIGNED = "Unassigned - needs manual placement"
EMP_ACTION_NOTHING = "Nothing to fill"

# --- Name normalisation -----------------------------------------------------

#: Generational suffixes stripped from the surname before names are compared,
#: so `Marchetti Jr` and `MARCHETTI` resolve to the same person. Matched
#: case-insensitively, with or without a trailing period. The stored name in
#: the sheet is never rewritten -- this only affects comparison.
GENERATIONAL_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

#: How each suffix is rendered when the app writes a *new* name, matching the
#: sheet's existing convention (`Marchetti Jr, Dean` -- no period).
GENERATIONAL_SUFFIX_TITLES = {
    "jr": "Jr",
    "sr": "Sr",
    "ii": "II",
    "iii": "III",
    "iv": "IV",
    "v": "V",
}

# --- Mutual (DX reference) workbook -----------------------------------------

#: The optional `List_of_Patients_Mutual.xlsx` upload used to source DX codes.
MUTUAL_SHEET_NAME = "Active"

#: That sheet has NO header row -- data starts on row 1 and is read
#: positionally. Column E (attending doctor) exists but is deliberately unused.
MUTUAL_DATA_START_ROW = 1
MUTUAL_COL_PATIENT = 1  # column A
MUTUAL_COL_DX = 2       # column B

#: When True, a blank `DX` on an existing row the app is already filling is
#: populated from the Mutual file too. Set False to restrict DX to new rows.
#: A non-blank DX is never overwritten either way, and rows the app skips
#: (already paid) are never touched.
FILL_DX_ON_EXISTING = True

# --- DX lookup outcomes -----------------------------------------------------

DX_FOUND = "found"
DX_NOT_FOUND = "not found"
DX_CONFLICT = "conflict"
DX_AMBIGUOUS = "ambiguous"
DX_NO_FILE = "no file"
