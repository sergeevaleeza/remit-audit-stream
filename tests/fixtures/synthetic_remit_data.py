"""Source of truth for the synthetic Noridian-style remittance fixture.

Every identifier here is fictional: patient names, MBIs, account numbers,
claim control numbers, and the check/EFT number are all invented and follow no
real person. Dollar amounts and CPT/adjustment codes are real Medicare fee
schedule values for these codes (public, non-identifying) reused across
fictional patients and dates, per the project's de-identification policy.

This module is imported by `make_remit_fixture.py` (renders the PDF) and by
the test suite indirectly via the generated ground-truth numbers printed by
that script -- it is not imported by the app itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- Synthetic provider identity -------------------------------------------

SYNTHETIC_PROVIDER_NPI = "1000000001"
SYNTHETIC_GROUP_NPI = "1000000000"
SYNTHETIC_CLINIC_NAME = "EXAMPLE MULTI-SPECIALTY CLINIC"
SYNTHETIC_CLINIC_ADDRESS = ("PO BOX 0000", "SAMPLETOWN, CA 90000-0000")

# Header "DATE:" -- the batch/run date, shifted well away from any real date.
SYNTHETIC_HEADER_DATE = "08/14/26"  # -> 08/14/2026
SYNTHETIC_CHECK_EFT = "900000001"

# --- Real, public Medicare fee-schedule amounts for these CPT codes --------
# (Not identifiers -- the same figures apply to any patient billed this way.)

CPT_RECIPE: dict[str, dict[str, float]] = {
    "99213": dict(billed=200.00, allowed=117.58, deduct=0.00, coins=23.52, rc=82.42, provpd=92.18),
    "99214": dict(billed=200.00, allowed=166.40, deduct=0.00, coins=33.28, rc=33.60, provpd=130.46),
    "90833": dict(billed=200.00, allowed=94.62, deduct=0.00, coins=18.92, rc=105.38, provpd=74.19),
    "90836": dict(billed=200.00, allowed=120.06, deduct=0.00, coins=24.01, rc=79.94, provpd=94.13),
}


@dataclass
class ServiceLineSpec:
    cpt: str
    service_date: str  # YYYY-MM-DD
    telehealth: bool = False  # adds the "95" modifier


@dataclass
class ClaimBlock:
    """One `NAME ... MID ... ACNT ... ICN ...` block (one page section)."""

    printed_name: str  # exactly as it would appear on the remit (may be truncated)
    mbi: str
    acct: str
    icn: str
    lines: list[ServiceLineSpec]
    forwarded_to: str = "EXAMPLE SUPPLEMENTAL PLAN"


@dataclass
class Patient:
    """One fictional patient, with how they should appear in each fixture."""

    key: str
    schedule_name: str  # Title Case, as billing staff would type it
    printed_name: str  # as Medicare prints it (may be truncated/have a middle initial)
    mbi_seed: str
    acct: str
    claims: list[ClaimBlock] = field(default_factory=list)


def _mbi(seed: str) -> str:
    """An 11-character, MBI-shaped placeholder that spells out 'FAKE'."""
    return f"9FAKE{seed:0>4}A1"[:11]


# --- The synthetic roster ---------------------------------------------------
# Covers every parser/matching edge case the original acceptance tests needed,
# with zero connection to any real patient, date, or transaction.

ROSTER: list[Patient] = [
    # Already fully paid in the schedule for both dates -> both should Skip.
    # Also exercises "same patient across two separate claim blocks".
    Patient(
        key="marlowe",
        schedule_name="Marlowe, Diane",
        printed_name="MARLOWE, DIANE",
        mbi_seed="0001",
        acct="MARLOD",
        claims=[
            ClaimBlock(
                "MARLOWE, DIANE", _mbi("0001"), "MARLOD", "9999900000001",
                [ServiceLineSpec("99213", "2026-03-09", telehealth=True),
                 ServiceLineSpec("90833", "2026-03-09", telehealth=True)],
            ),
            ClaimBlock(
                "MARLOWE, DIANE", _mbi("0001"), "MARLOD", "9999900000002",
                [ServiceLineSpec("99214", "2026-04-21"),
                 ServiceLineSpec("90836", "2026-04-21")],
            ),
        ],
    ),
    # Already paid for three dates inside ONE claim block -> all three Skip.
    Patient(
        key="thackeray",
        schedule_name="Thackeray, Renata",
        printed_name="THACKERAY, RENATA",
        mbi_seed="0002",
        acct="THACKR",
        claims=[
            ClaimBlock(
                "THACKERAY, RENATA", _mbi("0002"), "THACKR", "9999900000003",
                [ServiceLineSpec("99213", "2026-03-20"), ServiceLineSpec("90833", "2026-03-20"),
                 ServiceLineSpec("99213", "2026-04-07"), ServiceLineSpec("90836", "2026-04-07"),
                 ServiceLineSpec("99213", "2026-04-24"), ServiceLineSpec("90833", "2026-04-24")],
            ),
        ],
    ),
    # Schedule already has Payment = 0.00 for this date -> Skip, not Fill.
    # Also the single-CPT-line case (no add-on).
    Patient(
        key="whitfield",
        schedule_name="Whitfield, Harold",
        printed_name="WHITFIELD, HAROLD",
        mbi_seed="0003",
        acct="WHITFH",
        claims=[
            ClaimBlock(
                "WHITFIELD, HAROLD", _mbi("0003"), "WHITFH", "9999900000004",
                [ServiceLineSpec("99214", "2026-03-04")],
            ),
        ],
    ),
    # Empty Payment in the schedule for 03/26 -> Fill path. Spans two claim
    # blocks / four dates, one with a different E/M code.
    Patient(
        key="castellano",
        schedule_name="Castellano, Miguel",
        printed_name="CASTELLANO, MIGUEL",
        mbi_seed="0004",
        acct="CASTEM",
        claims=[
            ClaimBlock(
                "CASTELLANO, MIGUEL", _mbi("0004"), "CASTEM", "9999900000005",
                [ServiceLineSpec("99213", "2026-03-26"), ServiceLineSpec("90836", "2026-03-26")],
            ),
            ClaimBlock(
                "CASTELLANO, MIGUEL", _mbi("0004"), "CASTEM", "9999900000006",
                [ServiceLineSpec("99213", "2026-04-09"), ServiceLineSpec("90836", "2026-04-09"),
                 ServiceLineSpec("99213", "2026-04-21"), ServiceLineSpec("90836", "2026-04-21"),
                 ServiceLineSpec("99214", "2026-05-07"), ServiceLineSpec("90836", "2026-05-07")],
            ),
        ],
    ),
    # Long surname Medicare truncates to 13 chars, first name to 8 -- exercises
    # the prefix/truncation matching path. Also has the "95" modifier.
    Patient(
        key="featherstonehaugh",
        schedule_name="Featherstonehaugh, Wilhelmina",
        printed_name="FEATHERSTONEH, WILHELMI",  # 17->13, 10->8 chars, as Medicare would print
        mbi_seed="0005",
        acct="FEATHW",
        claims=[
            ClaimBlock(
                "FEATHERSTONEH, WILHELMI", _mbi("0005"), "FEATHW", "9999900000007",
                [ServiceLineSpec("99213", "2026-03-02", telehealth=True),
                 ServiceLineSpec("90833", "2026-03-02", telehealth=True)],
            ),
        ],
    ),
    # Trailing middle initial in the remit -- schedule holds no initial.
    # Second date has no matching schedule row at all -> New row.
    Patient(
        key="sorensen",
        schedule_name="Sorensen, Marcus",
        printed_name="SORENSEN, MARCUS R",
        mbi_seed="0006",
        acct="SORENM",
        claims=[
            ClaimBlock(
                "SORENSEN, MARCUS R", _mbi("0006"), "SORENM", "9999900000008",
                [ServiceLineSpec("99213", "2026-05-14"), ServiceLineSpec("90836", "2026-05-14")],
            ),
            ClaimBlock(
                "SORENSEN, MARCUS R", _mbi("0006"), "SORENM", "9999900000009",
                [ServiceLineSpec("99213", "2026-05-26"), ServiceLineSpec("90836", "2026-05-26")],
            ),
        ],
    ),
    # Single CPT line, no middle initial or truncation -- a plain new-row case.
    Patient(
        key="okafor",
        schedule_name="Okafor, Chidinma",
        printed_name="OKAFOR, CHIDINMA",
        mbi_seed="0007",
        acct="OKAFOC",
        claims=[
            ClaimBlock(
                "OKAFOR, CHIDINMA", _mbi("0007"), "OKAFOC", "9999900000010",
                [ServiceLineSpec("99214", "2026-04-28")],
            ),
        ],
    ),
    # Two-digit year spanning 2025/2026, across two claim blocks.
    Patient(
        key="petrossian",
        schedule_name="Petrossian, Knarik",
        printed_name="PETROSSIAN, KNARIK S",
        mbi_seed="0008",
        acct="PETROK",
        claims=[
            ClaimBlock(
                "PETROSSIAN, KNARIK S", _mbi("0008"), "PETROK", "9999900000011",
                [ServiceLineSpec("99213", "2025-12-25", telehealth=True),
                 ServiceLineSpec("90833", "2025-12-25", telehealth=True)],
            ),
            ClaimBlock(
                "PETROSSIAN, KNARIK S", _mbi("0008"), "PETROK", "9999900000012",
                [ServiceLineSpec("99213", "2026-03-03", telehealth=True),
                 ServiceLineSpec("90833", "2026-03-03", telehealth=True)],
            ),
        ],
    ),
    # Schedule row carries a generational suffix the remit does not print.
    # The surname also has a Slavic feminine ending, so the suffix must be
    # stripped *before* harmonisation or the two sides will not agree.
    Patient(
        key="bystritskaya",
        schedule_name="Bystritskaya Jr, Anna",
        printed_name="BYSTRITSKAYA, ANNA",
        mbi_seed="0010",
        acct="BYSTRA",
        claims=[
            ClaimBlock(
                "BYSTRITSKAYA, ANNA", _mbi("0010"), "BYSTRA", "9999900000014",
                [ServiceLineSpec("99213", "2026-04-14"),
                 ServiceLineSpec("90833", "2026-04-14")],
            ),
        ],
    ),
    # In the remit only -- absent from the schedule entirely -> New row.
    Patient(
        key="whitcombe",
        schedule_name="Whitcombe, Rosalind",
        printed_name="WHITCOMBE, ROSALIND",
        mbi_seed="0009",
        acct="WHITCR",
        claims=[
            ClaimBlock(
                "WHITCOMBE, ROSALIND", _mbi("0009"), "WHITCR", "9999900000013",
                [ServiceLineSpec("99213", "2026-05-06"), ServiceLineSpec("90833", "2026-05-06")],
            ),
        ],
    ),
]

# In the schedule only -- never appears in the remit at all.
SCHEDULE_ONLY_PATIENT = dict(key="delacroix", schedule_name="Delacroix, Owen")

# --- Mutual (DX reference) fixture ------------------------------------------
# Fictional diagnoses for the fictional roster above. The `Active` sheet has
# no header row; column A is the patient, B the DX, E the attending doctor
# (present for realism, deliberately never read by the app).
#
# (patient, dx, attending)
MUTUAL_ROWS: list[tuple[str, str, str]] = [
    ("Marlowe, Diane", "F41.1, F32.9", "Dr. A"),
    ("Thackeray, Renata", "F33.1, G47.00", "Dr. A"),
    ("Whitfield, Harold", "F43.21", "Dr. B"),
    ("Castellano, Miguel", "F41.9, F51.01", "Dr. A"),
    ("Featherstonehaugh, Wilhelmina", "F31.81", "Dr. A"),
    ("Sorensen, Marcus", "F40.10", "Dr. B"),
    # Suffix in the Mutual file too -- must still match the remit's plain name.
    ("Bystritskaya Jr, Anna", "F42.2", "Dr. A"),
    # Present in the remit but with a DX conflict: two rows, different codes.
    ("Whitcombe, Rosalind", "F34.1", "Dr. A"),
    ("Whitcombe, Rosalind", "F33.2", "Dr. C"),
    # Someone who never appears in the remit at all.
    ("Delacroix, Owen", "F90.0", "Dr. B"),
    # OKAFOR and PETROSSIAN are deliberately absent -> DX left blank.
]


# --- Second fixture: deductibles and bare continuation lines ----------------
#
# Reproduces the structural shapes that make a service line easy to misparse:
#   (a) two lines where ALLOWED is fully consumed by DEDUCT, so PROV-PD is a
#       genuine 0.00 -- not a parse failure;
#   (b) a line with a partial deductible;
#   (c) several fully-paid lines;
#   (d) bare `CO-253 <amt>` continuation lines with NO `REM:` prefix, which is
#       why continuations are skipped on "does not start with a 10-digit NPI"
#       rather than on the `REM:` prefix;
#   (e) the same patient billed under two different performing-provider NPIs.
#
# All names/MBIs/ICNs are fictional. The dollar figures are public Medicare
# fee-schedule amounts for these codes, reused for a fictional person.

DEDUCT_HEADER_DATE = "07/20/26"  # -> 07/20/2026
DEDUCT_CHECK_EFT = "900000002"

#: The therapist's own NPI, distinct from the physician's.
SYNTHETIC_THERAPIST_NPI = "1000000003"

#: 90834 (45-minute psychotherapy): billed/allowed/RC are constant; the
#: deductible split varies line to line.
CPT_90834_BILLED = 200.00
CPT_90834_ALLOWED = 133.00
CPT_90834_RC = 67.00

#: (service date, DEDUCT, COINS, PROV-PD, sequestration on the continuation)
#: ALLOWED - DEDUCT - COINS - sequestration = PROV-PD on every row.
DEDUCTIBLE_LINES: list[tuple[str, float, float, float, float | None]] = [
    ("2026-01-15", 133.00, 0.00, 0.00, None),    # allowed fully deducted
    ("2026-02-10", 133.00, 0.00, 0.00, None),    # allowed fully deducted
    ("2026-03-11", 17.00, 23.20, 90.94, 1.86),   # partial deductible
    ("2026-03-31", 0.00, 26.60, 104.27, 2.13),   # fully paid
    ("2026-04-15", 0.00, 26.60, 104.27, 2.13),
    ("2026-05-04", 0.00, 26.60, 104.27, 2.13),
    ("2026-05-20", 0.00, 26.60, 104.27, 2.13),
    ("2026-06-02", 0.00, 26.60, 104.27, 2.13),
    ("2026-06-16", 0.00, 26.60, 104.27, 2.13),
]

#: The same fictional patient, billed under the physician's NPI on other dates.
PHYSICIAN_CLAIM_LINES: list[tuple[str, tuple[str, ...]]] = [
    ("2026-04-28", ("99214", "90836")),
    ("2026-05-05", ("99214", "90836")),
    ("2026-06-07", ("99213", "90833")),
]

DEDUCT_PATIENT_NAME = "QUIMBY, THEODORA"
DEDUCT_PATIENT_MBI = _mbi("0201")
DEDUCT_PATIENT_ACCT = "QUIMBT"
