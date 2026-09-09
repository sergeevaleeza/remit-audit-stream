"""Noridian Medicare Remittance Advice PDF -> aggregated visits.

The layout is fixed-width-ish but optional CPT modifiers shift the columns, so
fields are located by *shape* (a 10-digit NPI first token, the 6-digit MMDDYY
token, the ordered list of dollar amounts) rather than by character offset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Sequence

import pdfplumber

from .config import CENTURY_PREFIX, DATE_FMT, NPI_TO_DOCTOR

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

# Evaluation & management codes sort before psychotherapy add-ons.
_EM_PREFIX = "99"

# Expected number of dollar amounts on a well-formed service line.
_EXPECTED_AMOUNTS = 6

# Index of COINS within the ordered dollar amounts.
_COINS_INDEX = 3


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

    service_date = next(
        (d for d in (parse_serv_date(t) for t in tokens[1:]) if d is not None), None
    )
    if service_date is None:
        return None

    # PROC sits after the two date tokens, POS and NOS, and always before the
    # money columns. Stopping at the first dollar amount keeps a modifier or a
    # trailing code from being mistaken for the procedure.
    proc = None
    for token in tokens[1:]:
        if _AMOUNT_RE.match(token):
            break
        if _SERV_DATE_RE.match(token):
            continue
        if _PROC_RE.match(token):
            proc = token
            break
    if proc is None:
        return None

    amounts = [float(t) for t in tokens if _AMOUNT_RE.match(t)]
    if len(amounts) < _EXPECTED_AMOUNTS:
        return None

    return ServiceLine(
        patient=patient,
        npi=npi,
        service_date=service_date,
        proc=proc,
        coins=amounts[_COINS_INDEX],
        prov_pd=amounts[-1],
        source_file=source_file,
        check_eft=check_eft,
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

    return RemitDocument(
        filename=filename,
        billed_date=billed_date,
        check_eft=check_eft,
        claim_count=claim_count,
        service_lines=service_lines,
    )


def aggregate_visits(documents: Sequence[RemitDocument]) -> list[Visit]:
    """Group every service line by (patient, service date, provider NPI).

    Aggregation spans claim blocks and uploaded files, so a patient appearing
    under several ICNs still yields one visit per date and provider.
    """
    billed_by_file = {doc.filename: doc.billed_date for doc in documents}
    fallback_billed = next(
        (doc.billed_date for doc in documents if doc.billed_date is not None), None
    )

    visits: dict[tuple[str, date, str], Visit] = {}
    for doc in documents:
        for line in doc.service_lines:
            key = (line.patient, line.service_date, line.npi)
            visit = visits.get(key)
            if visit is None:
                visit = Visit(
                    patient=line.patient,
                    service_date=line.service_date,
                    npi=line.npi,
                    billed_date=billed_by_file.get(line.source_file) or fallback_billed,
                )
                visits[key] = visit
            visit.cpt_codes.append(line.proc)
            visit.payment = round(visit.payment + line.prov_pd, 2)
            visit.copay = round(visit.copay + line.coins, 2)
            visit.source_files.add(line.source_file)
            if line.check_eft:
                visit.check_efts.add(line.check_eft)

    return sorted(visits.values(), key=lambda v: (v.patient, v.service_date, v.npi))


def parse_remittances(files: Sequence[tuple[object, str]]) -> tuple[list[RemitDocument], list[Visit]]:
    """Parse several (file_obj, filename) pairs and aggregate across all of them."""
    documents = [parse_document(obj, name) for obj, name in files]
    return documents, aggregate_visits(documents)
