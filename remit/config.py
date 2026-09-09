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

#: Literal written into the Ins column for every new row.
INSURANCE_VALUE = "Medicare"

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

#: Two-digit years are expanded into this century.
CENTURY_PREFIX = 2000

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
