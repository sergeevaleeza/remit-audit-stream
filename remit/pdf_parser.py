"""Noridian Medicare Remittance Advice PDF -> aggregated visits.

The layout is fixed-width-ish but optional CPT modifiers shift the columns, so
fields are located by *shape* (a 10-digit NPI first token, the 6-digit MMDDYY
token, the ordered list of dollar amounts) rather than by character offset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable, Sequence

import pdfplumber

from .config import (
    CENTURY_PREFIX,
    DATE_FMT,
    INSURANCE_VALUE,
    NPI_TO_DOCTOR,
    TELEHEALTH_INSURANCE_LABEL,
)

# --- Line shapes -----------------------------------------------------------

# A service line always starts with the 10-digit performing-provider NPI.
_NPI_RE = re.compile(r"^\d{10}$")

# "NAME SMITH, JANE MID 9FAKE0001A1 ACNT ..." -> name between NAME and MID.
_NAME_RE = re.compile(r"^NAME\s+(?P<name>.+?)\s+MID\b")

# Header "DATE: 07/07/26" in the top-right block.
_HEADER_DATE_RE = re.compile(r"\bDATE:\s*(\d{2})/(\d{2})/(\d{2,4})\b")

# "CHECK/EFT #: 900000001" (the colon is sometimes tight against the number).
_CHECK_RE = re.compile(r"CHECK/EFT\s*#:\s*(\S+)")

# Dollar amounts. This deliberately excludes NOS "1.0", the NPI and the date
# integers, leaving exactly [BILLED, ALLOWED, DEDUCT, COINS, RC-AMT, PROV-PD].
_AMOUNT_RE = re.compile(r"^\d+\.\d{2}$")

# Six-digit service date token (MMDDYY). The 4-digit MMDD token is ignored.
_SERV_DATE_RE = re.compile(r"^\d{6}$")

# Procedure codes seen on these remits: 5 alphanumerics, digit-led.
_PROC_RE = re.compile(r"^\d[0-9A-Z]{4}$")

# Place of service: the 2-digit token straight after the 6-digit service date.
_POS_RE = re.compile(r"^\d{2}$")

#: POS code for the patient's home, which with modifier 95 means telehealth.
POS_TELEHEALTH = "10"

#: The telehealth modifier printed after PROC.
MODIFIER_TELEHEALTH = "95"

# Group/reason adjustment codes: `CO-45`, `OA-18`, `CO-253`, `PR-1` ...
_ADJ_CODE_RE = re.compile(r"\b([A-Z]{2})-(\d+)\b")

# A continuation line: the indented `REM:` / `CO-###` rows that belong to the
# service line above them. They carry reason codes (and sometimes an amount).
_CONTINUATION_RE = re.compile(r"^(?:REM:|[A-Z]{2}-\d+\b)")

#: Medicare reason code 18 -- "Exact duplicate claim/service". Printed as
#: `OA-18` or `CO-18`. The duplicate is adjudicated at $0 because the original
#: already paid, so such an occurrence is NOT authoritative: it must never
#: contribute, overwrite, zero or downgrade a real payment.
DUPLICATE_REASON_CODE = "18"


def adjustment_codes(text: str) -> tuple[str, ...]:
    """Every `XX-nnn` group/reason token in a line, in order of appearance."""
    return tuple(f"{group}-{reason}" for group, reason in _ADJ_CODE_RE.findall(text))


def marks_duplicate(codes: Iterable[str]) -> bool:
    """True when any code is reason 18, whatever its group prefix."""
    return any(code.split("-", 1)[-1] == DUPLICATE_REASON_CODE for code in codes)

# Evaluation & management codes sort before psychotherapy add-ons.
_EM_PREFIX = "99"

# Expected number of dollar amounts on a well-formed service line.
_EXPECTED_AMOUNTS = 6

# Positions within the ordered dollar amounts of a service line:
#   [BILLED, ALLOWED, DEDUCT, COINS, RC-AMT, PROV-PD]
# DEDUCT is its own field and is never conflated with COINS or PROV-PD; a
# non-zero deductible simply means ALLOWED was partly consumed before COINS.
_BILLED_INDEX = 0
_ALLOWED_INDEX = 1
_DEDUCT_INDEX = 2
_COINS_INDEX = 3
_RC_AMT_INDEX = 4
_PROV_PD_INDEX = 5


@dataclass(frozen=True)
class ServiceLine:
    """One PERF PROV ... PROV PD row inside a claim block."""

    patient: str
    npi: str
    service_date: date
    proc: str
    coins: float
    prov_pd: float
    source_file: str
    check_eft: str | None = None
    #: The deductible applied to this line. Kept distinct from COINS and
    #: PROV-PD: a non-zero deductible consumes part of ALLOWED and legitimately
    #: drives PROV-PD to 0.00 without meaning the line failed to parse.
    deduct: float = 0.0
    #: Place of service (`11` office, `10` the patient's home) and any CPT
    #: modifiers, used to tell a telehealth encounter from an in-office one.
    pos: str | None = None
    modifiers: tuple[str, ...] = ()
    #: Group/reason codes from this line **and** its continuation lines.
    codes: tuple[str, ...] = ()

    @property
    def telehealth(self) -> bool:
        return self.pos == POS_TELEHEALTH and MODIFIER_TELEHEALTH in self.modifiers

    @property
    def is_duplicate(self) -> bool:
        """Reason 18: an exact-duplicate claim, adjudicated at $0."""
        return marks_duplicate(self.codes)


@dataclass
class Visit:
    """All service lines sharing (patient, service date, provider NPI)."""

    patient: str
    service_date: date
    npi: str
    billed_date: date | None
    cpt_codes: list[str] = field(default_factory=list)
    payment: float = 0.0
    copay: float = 0.0
    source_files: set[str] = field(default_factory=set)
    check_efts: set[str] = field(default_factory=set)

    #: How many remittances in this run reported this visit. More than one
    #: means later remits restated it; only the newest supplies the amounts.
    remit_count: int = 1
    #: Payments from the superseded (older) remits, oldest first.
    superseded_payments: list[float] = field(default_factory=list)

    #: Place of service and modifiers, carried up from the service lines.
    #: These are consistent across a visit's CPT lines on these remits.
    pos: str | None = None
    modifiers: set[str] = field(default_factory=set)

    #: Group/reason codes seen on this occurrence's service lines.
    codes: set[str] = field(default_factory=set)
    #: True when this occurrence is an exact-duplicate adjudication (reason
    #: 18). Such an occurrence is adjudicated at $0 because the original
    #: already paid, so it is never allowed to supply the visit's amounts.
    is_duplicate: bool = False
    #: True when *every* occurrence of this visit was a duplicate, i.e. the
    #: remittance that actually paid was not uploaded.
    duplicate_only: bool = False
    #: EFT numbers of duplicate occurrences whose amounts were discarded, and
    #: the EFT that did supply the amounts.
    ignored_duplicate_efts: set[str] = field(default_factory=set)
    authoritative_eft: str | None = None

    @property
    def duplicate_note(self) -> str:
        """Preview text explaining a discarded duplicate, if there was one."""
        if self.duplicate_only:
            return (
                "Duplicate only (OA-18) - original paying remit not uploaded; "
                "verify before recording"
            )
        if not self.ignored_duplicate_efts:
            return ""
        ignored = ", ".join(sorted(self.ignored_duplicate_efts))
        kept = self.authoritative_eft or "the paying remit"
        return f"OA-18 duplicate from EFT {ignored} ignored; kept payment from EFT {kept}"

    @property
    def telehealth(self) -> bool:
        """POS 10 (patient's home) plus the 95 modifier."""
        return self.pos == POS_TELEHEALTH and MODIFIER_TELEHEALTH in self.modifiers

    @property
    def insurance(self) -> str:
        """The `Ins` value this visit implies."""
        return insurance_label(self)

    @property
    def restated(self) -> bool:
        """True when more than one uploaded remit reported this visit."""
        return self.remit_count > 1

    @property
    def doctor(self) -> str:
        """Comment text for this visit's provider; blank when the NPI is new."""
        return NPI_TO_DOCTOR.get(self.npi, "")

    @property
    def known_provider(self) -> bool:
        return self.npi in NPI_TO_DOCTOR

    @property
    def cpt_display(self) -> str:
        """E/M code first, then the psychotherapy add-on, joined with '/'."""
        return "/".join(order_cpt_codes(self.cpt_codes))

    @property
    def data_str(self) -> str:
        return self.service_date.strftime(DATE_FMT)

    @property
    def billed_str(self) -> str:
        return self.billed_date.strftime(DATE_FMT) if self.billed_date else ""

    @property
    def key(self) -> tuple[str, date, str]:
        return (self.patient, self.service_date, self.npi)


@dataclass
class RemitDocument:
    """Everything parsed out of a single remittance PDF."""

    filename: str
    billed_date: date | None
    check_eft: str | None
    claim_count: int
    service_lines: list[ServiceLine]

    @property
    def total_prov_pd(self) -> float:
        return round(sum(line.prov_pd for line in self.service_lines), 2)


def insurance_label(visit: "Visit") -> str:
    """The `Ins` value for a visit: telehealth is called out, else Medicare.

    Replaces the old "Ins is always Medicare" constant. A visit billed from
    the patient's home (POS 10) with the 95 modifier is a telehealth
    encounter and is labelled `POS 10(95)` so it is visible in the schedule
    and in the employees workbook.
    """
    if visit.telehealth:
        return TELEHEALTH_INSURANCE_LABEL
    return INSURANCE_VALUE


def order_cpt_codes(codes: Iterable[str]) -> list[str]:
    """E/M (99xxx) first, then add-ons, each group in ascending order."""
    unique = sorted(set(codes))
    em = [c for c in unique if c.startswith(_EM_PREFIX)]
    rest = [c for c in unique if not c.startswith(_EM_PREFIX)]
    return em + rest


def expand_two_digit_year(yy: int) -> int:
    """25 -> 2025, 26 -> 2026. Four-digit years pass through untouched."""
    return yy if yy > 99 else CENTURY_PREFIX + yy


def parse_serv_date(token: str) -> date | None:
    """030926 -> 2026-03-09; 122525 -> 2025-12-25."""
    if not _SERV_DATE_RE.match(token):
        return None
    try:
        return date(expand_two_digit_year(int(token[4:6])), int(token[:2]), int(token[2:4]))
    except ValueError:
        return None


def parse_header_date(text: str) -> date | None:
    """Pull the DATE: MM/DD/YY value that applies to every visit in the file."""
    match = _HEADER_DATE_RE.search(text)
    if not match:
        return None
    month, day, year = (int(g) for g in match.groups())
    try:
        return date(expand_two_digit_year(year), month, day)
    except ValueError:
        return None


def parse_check_eft(text: str) -> str | None:
    match = _CHECK_RE.search(text)
    return match.group(1) if match else None


def is_service_line(tokens: Sequence[str]) -> bool:
    """A line is a service line only if its first token is a 10-digit number.

    This alone discards every REM:/reason-code continuation line, the page
    headers, the underline rows and the TOTALS block.
    """
    return bool(tokens) and bool(_NPI_RE.match(tokens[0]))


def parse_service_line(line: str, patient: str, source_file: str,
                       check_eft: str | None = None) -> ServiceLine | None:
    """Extract the five fields we need, tolerating optional CPT modifiers."""
    tokens = line.split()
    if not is_service_line(tokens):
        return None

    npi = tokens[0]

    service_date = None
    date_index = None
    for index, token in enumerate(tokens[1:], start=1):
        parsed = parse_serv_date(token)
        if parsed is not None:
            service_date, date_index = parsed, index
            break
    if service_date is None:
        return None

    # POS is the 2-digit token immediately after the 6-digit service date:
    # `... MMDD MMDDYY POS NOS PROC ...`. 11 = office, 10 = patient's home
    # (telehealth).
    pos = None
    if date_index + 1 < len(tokens) and _POS_RE.match(tokens[date_index + 1]):
        pos = tokens[date_index + 1]

    # PROC sits after POS and NOS, and always before the money columns.
    # Stopping at the first dollar amount keeps a modifier or a trailing code
    # from being mistaken for the procedure.
    proc = None
    proc_index = None
    for index, token in enumerate(tokens[1:], start=1):
        if _AMOUNT_RE.match(token):
            break
        if _SERV_DATE_RE.match(token):
            continue
        if _PROC_RE.match(token):
            proc, proc_index = token, index
            break
    if proc is None:
        return None

    # Anything between PROC and the money columns is a modifier; `95` marks a
    # telehealth encounter.
    modifiers = []
    for token in tokens[proc_index + 1:]:
        if _AMOUNT_RE.match(token):
            break
        modifiers.append(token)

    # Amounts come from THIS physical line only -- a continuation line is
    # never merged in, because it does not start with a 10-digit NPI and so
    # never reaches this function. The canonical layout has exactly six
    # amounts, so they are read positionally rather than relative to the end:
    # were a stray amount ever to trail the line, `amounts[-1]` would silently
    # return that instead of PROV-PD.
    amounts = [float(t) for t in tokens if _AMOUNT_RE.match(t)]
    if len(amounts) < _EXPECTED_AMOUNTS:
        return None

    return ServiceLine(
        patient=patient,
        npi=npi,
        service_date=service_date,
        proc=proc,
        deduct=amounts[_DEDUCT_INDEX],
        coins=amounts[_COINS_INDEX],
        prov_pd=amounts[_PROV_PD_INDEX],
        codes=adjustment_codes(line),
        source_file=source_file,
        check_eft=check_eft,
        pos=pos,
        modifiers=tuple(modifiers),
    )


def extract_text(file_obj) -> str:
    """Full text of a PDF, pages joined by newlines."""
    with pdfplumber.open(file_obj) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def parse_document(file_obj, filename: str = "remit.pdf") -> RemitDocument:
    """Parse one remittance PDF into its claim/service-line structure."""
    text = extract_text(file_obj)
    billed_date = parse_header_date(text)
    check_eft = parse_check_eft(text)

    service_lines: list[ServiceLine] = []
    claim_count = 0
    current_patient: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        name_match = _NAME_RE.match(line)
        if name_match:
            current_patient = " ".join(name_match.group("name").split())
            claim_count += 1
            continue

        if current_patient is None:
            continue

        parsed = parse_service_line(line, current_patient, filename, check_eft)
        if parsed is not None:
            service_lines.append(parsed)
            continue

        # A continuation line (`REM: …` or a bare `CO-### …`) carries reason
        # codes that belong to the service line above it. It is still never
        # treated as a service line -- only its codes are folded upward, so
        # amounts are unaffected.
        if service_lines and _CONTINUATION_RE.match(line):
            previous = service_lines[-1]
            extra = tuple(
                code for code in adjustment_codes(line)
                if code not in previous.codes
            )
            if extra:
                service_lines[-1] = replace(
                    previous, codes=previous.codes + extra
                )

    return RemitDocument(
        filename=filename,
        billed_date=billed_date,
        check_eft=check_eft,
        claim_count=claim_count,
        service_lines=service_lines,
    )


def aggregate_visits(documents: Sequence[RemitDocument]) -> list[Visit]:
    """Group service lines into one visit per (patient, service date, NPI).

    Within a single remittance the lines of a visit are summed, so a patient
    appearing under several ICNs still yields one visit per date and provider.

    Across remittances the amounts are **not** summed: a later Medicare remit
    restates the claim rather than topping it up, so the newest remit (by its
    header ``DATE:``) supplies the visit's amounts. Superseded values are kept
    on the visit so the preview can show what changed.
    """
    fallback_billed = next(
        (doc.billed_date for doc in documents if doc.billed_date is not None), None
    )

    # Aggregate within each document first, keyed by document index so two
    # uploads sharing a filename cannot collide.
    per_document: list[tuple[RemitDocument, dict[tuple[str, date, str], Visit]]] = []
    for doc in documents:
        bucket: dict[tuple[str, date, str], Visit] = {}
        for line in doc.service_lines:
            key = (line.patient, line.service_date, line.npi)
            visit = bucket.get(key)
            if visit is None:
                visit = Visit(
                    patient=line.patient,
                    service_date=line.service_date,
                    npi=line.npi,
                    billed_date=doc.billed_date or fallback_billed,
                )
                bucket[key] = visit
            visit.cpt_codes.append(line.proc)
            visit.payment = round(visit.payment + line.prov_pd, 2)
            visit.copay = round(visit.copay + line.coins, 2)
            visit.source_files.add(line.source_file)
            if line.check_eft:
                visit.check_efts.add(line.check_eft)
            if line.pos and visit.pos is None:
                visit.pos = line.pos
            visit.modifiers.update(line.modifiers)
            visit.codes.update(line.codes)
            if line.is_duplicate:
                visit.is_duplicate = True
        per_document.append((doc, bucket))

    # Collect every occurrence of each visit, then reconcile them together.
    occurrences: dict[tuple[str, date, str], list[Visit]] = {}
    for _, bucket in per_document:
        for key, candidate in bucket.items():
            occurrences.setdefault(key, []).append(candidate)

    merged = {key: _reconcile(group) for key, group in occurrences.items()}
    return sorted(merged.values(), key=lambda v: (v.patient, v.service_date, v.npi))


def _remit_sort_key(visit: Visit) -> date:
    """Undated remits sort oldest, so any dated one outranks them."""
    return visit.billed_date or date.min


def _reconcile(group: list[Visit]) -> Visit:
    """Pick the occurrence that supplies a visit's amounts.

    Reason-18 occurrences are *exact duplicates*, adjudicated at $0 because the
    original already paid. They are therefore not authoritative: the amounts
    come from the newest **non-duplicate** occurrence, whatever order the
    remittances were uploaded in. Only when every occurrence is a duplicate --
    the paying remittance was never uploaded -- is the visit flagged for a human
    rather than recorded as a real $0.00.

    Among non-duplicate occurrences the existing `replace_with_latest`
    behaviour is unchanged: the newest remit restates the claim.
    """
    authoritative = [visit for visit in group if not visit.is_duplicate]
    duplicates = [visit for visit in group if visit.is_duplicate]

    pool = authoritative or duplicates
    ordered = sorted(pool, key=_remit_sort_key)
    winner = ordered[-1]

    winner.remit_count = len(group)
    winner.superseded_payments = [visit.payment for visit in ordered[:-1]]
    winner.duplicate_only = not authoritative
    winner.authoritative_eft = (
        sorted(winner.check_efts)[0] if winner.check_efts and authoritative else None
    )

    # The audit trail lists every EFT that mentioned this visit, including the
    # duplicates -- only their *amounts* are discarded, not the fact of them.
    for visit in group:
        if visit is winner:
            continue
        winner.check_efts |= visit.check_efts
        winner.source_files |= visit.source_files
        winner.codes |= visit.codes

    if authoritative:
        winner.ignored_duplicate_efts = {
            eft for visit in duplicates for eft in visit.check_efts
        }

    return winner


def parse_remittances(files: Sequence[tuple[object, str]]) -> tuple[list[RemitDocument], list[Visit]]:
    """Parse several (file_obj, filename) pairs and aggregate across all of them."""
    documents = [parse_document(obj, name) for obj, name in files]
    return documents, aggregate_visits(documents)
