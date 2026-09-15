"""Populate `AMSMC_employees.xlsx` directly from the reconciled EOB visits.

One sheet per practitioner (Ana, Marcia, Oxana). The tabs are fed from the
same reconciled visit set that feeds the schedule, taken *after* OA-18 /
multi-EFT reconciliation -- the two are independent consumers of it, and this
module never reads a schedule cell. That matters because the schedule's own
values can lag or be hand-edited, whereas the EOB is what Medicare actually
adjudicated.

Three things make this less mechanical than it looks:

* **The header row is not in the same place on every sheet** -- Ana and Marcia
  put it on row 1, Oxana on row 2 -- so it is located by scanning the first
  rows for the expected labels rather than assumed. Labels also carry odd
  internal spacing (`Co-payment   Old`, `Paid by Ins toAna`), so they are
  matched case-insensitively with whitespace collapsed.
* **Appends are gated on the EOB's PERF PROV NPI**, not on who a tab already
  contains. A visit is appended to Ana's or Oxana's tab only when it was
  billed under that associate's own NPI, so a patient being "known" to a tab
  can never pull in a session somebody else performed.
* **Marcia is fill-only.** Her sessions are billed incident-to under the
  supervising physician's NPI, so the remit can never prove a visit is hers.
  Her tab roster defines ownership: existing rows are filled, nothing is ever
  added.
"""

from __future__ import annotations

import io
from copy import copy
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

from .config import (
    DATE_FMT,
    EMP_ACTION_AMBIGUOUS,
    EMP_ACTION_APPEND,
    EMP_ACTION_FILL,
    EMP_ACTION_MISMATCH,
    EMP_ACTION_NO_MATCH,
    EMP_ACTION_NOTHING,
    EMP_ACTION_UNASSIGNED,
    EMPLOYEE_APPEND_SHEETS,
    EMPLOYEE_AUDIT_COLUMNS,
    EMPLOYEE_COL_COPAY_EOB,
    COL_CHECK_EFT,
    COL_PROCESSED_ON,
    EMPLOYEE_COL_DATE,
    EMPLOYEE_COL_INSURANCE,
    EMPLOYEE_HEADER_SEARCH_ROWS,
    EMPLOYEE_NPI,
    EMPLOYEE_PATIENT_HEADERS,
    EMPLOYEE_PAYMENT_COLUMN,
    EMPLOYEE_SHEETS,
    NAME_AUTO_MATCH_SCORE,
)
from .matching import (
    _normalize_token,
    is_blank,
    name_score,
    parse_loose_date,
    to_title_name,
)

#: Every header the app needs to find on at least one sheet.
_WANTED_HEADERS = (
    set(EMPLOYEE_PATIENT_HEADERS)
    | {EMPLOYEE_COL_INSURANCE, EMPLOYEE_COL_COPAY_EOB, EMPLOYEE_COL_DATE}
    | set(EMPLOYEE_PAYMENT_COLUMN.values())
)


class EmployeesError(RuntimeError):
    """The uploaded workbook cannot be used as the employees file."""


def header_key(value: Any) -> str:
    """Normalise a header label: lowercase, whitespace collapsed."""
    return _normalize_token(value)


def find_header_row(worksheet: Worksheet,
                    search_rows: int = EMPLOYEE_HEADER_SEARCH_ROWS) -> int:
    """The row holding the column labels, found by looking for them.

    Ana and Marcia use row 1, Oxana row 2. Whichever of the first rows carries
    the most recognised labels wins, so a new sheet with yet another layout
    still resolves.
    """
    wanted = {header_key(h) for h in _WANTED_HEADERS}
    best_row, best_hits = 1, 0
    for row in range(1, search_rows + 1):
        hits = sum(
            1
            for column in range(1, worksheet.max_column + 1)
            if header_key(worksheet.cell(row=row, column=column).value) in wanted
        )
        if hits > best_hits:
            best_row, best_hits = row, hits
    if best_hits == 0:
        raise EmployeesError(
            f"Sheet '{worksheet.title}' has no recognisable header row in the "
            f"first {search_rows} rows."
        )
    return best_row


def resolve_employee_columns(worksheet: Worksheet, header_row: int) -> dict[str, int]:
    """Map normalised header text -> column index for one sheet."""
    found: dict[str, int] = {}
    for column in range(1, worksheet.max_column + 1):
        key = header_key(worksheet.cell(row=header_row, column=column).value)
        if key and key not in found:
            found[key] = column
    return found


def column_for(columns: dict[str, int], *labels: str) -> int | None:
    """The first of several candidate labels that this sheet actually has."""
    for label in labels:
        index = columns.get(header_key(label))
        if index is not None:
            return index
    return None


@dataclass
class EmployeeRow:
    """One data row of a provider sheet."""

    sheet: str
    row_num: int
    patient: Any = None
    session_date: Any = None
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def parsed_date(self) -> date | None:
        return parse_loose_date(self.session_date)


@dataclass
class EmployeeSheet:
    """One provider tab, with its resolved layout and rows."""

    name: str
    header_row: int
    columns: dict[str, int]
    rows: list[EmployeeRow] = field(default_factory=list)

    @property
    def data_start_row(self) -> int:
        return self.header_row + 1

    @property
    def patient_column(self) -> int | None:
        return column_for(self.columns, *EMPLOYEE_PATIENT_HEADERS)

    @property
    def payment_column(self) -> int | None:
        label = EMPLOYEE_PAYMENT_COLUMN.get(self.name)
        return column_for(self.columns, label) if label else None

    def has_patient(self, patient: str) -> bool:
        return any(
            not is_blank(row.patient)
            and name_score(patient, str(row.patient)) >= NAME_AUTO_MATCH_SCORE
            for row in self.rows
        )

    def has_session(self, patient: str, session_date: date) -> bool:
        """True when this tab already carries that patient on that date.

        The dedup key for appends, so re-running a remit adds nothing.
        """
        return any(
            not is_blank(row.patient)
            and row.parsed_date == session_date
            and name_score(patient, str(row.patient)) >= NAME_AUTO_MATCH_SCORE
            for row in self.rows
        )


@dataclass
class EmployeeChange:
    """A proposed edit to one provider sheet, pending confirmation."""

    sheet: str
    action: str
    patient: str
    session_date: str
    row_num: int | None = None
    insurance: str = ""
    copay: Any = None
    payment: Any = None
    payment_column: str = ""
    fills: dict[int, Any] = field(default_factory=dict)
    note: str = ""
    accepted: bool = True
    #: The EOB's PERF PROV NPI, shown in the preview so the reason an append
    #: did or did not happen is visible without opening the remit.
    npi: str = ""
    #: Audit stamps written to this tab's own columns for rows touched now.
    processed_on: str = ""
    check_eft: str = ""

    @property
    def needs_review(self) -> bool:
        return self.action in (
            EMP_ACTION_MISMATCH, EMP_ACTION_AMBIGUOUS, EMP_ACTION_UNASSIGNED,
        )

    @property
    def writes(self) -> bool:
        return self.action in (EMP_ACTION_FILL, EMP_ACTION_APPEND)

    @property
    def appends(self) -> bool:
        return self.action == EMP_ACTION_APPEND


def load_employees_workbook(data: bytes) -> openpyxl.Workbook:
    """Load the employees workbook, reusing the schedule's repair fallback."""
    from .excel_updater import load_schedule_workbook

    return load_schedule_workbook(data)


def read_sheets(workbook: openpyxl.Workbook) -> dict[str, EmployeeSheet]:
    """Read every provider tab that is present in the workbook."""
    sheets: dict[str, EmployeeSheet] = {}
    for name in EMPLOYEE_SHEETS:
        if name not in workbook.sheetnames:
            continue
        worksheet = workbook[name]
        header_row = find_header_row(worksheet)
        columns = resolve_employee_columns(worksheet, header_row)
        sheet = EmployeeSheet(name=name, header_row=header_row, columns=columns)

        patient_column = sheet.patient_column
        date_column = column_for(columns, EMPLOYEE_COL_DATE)
        if patient_column is None:
            raise EmployeesError(
                f"Sheet '{name}' row {header_row} has no patient-name column "
                f"(looked for {' / '.join(EMPLOYEE_PATIENT_HEADERS)})."
            )

        for row in range(sheet.data_start_row, worksheet.max_row + 1):
            patient = worksheet.cell(row=row, column=patient_column).value
            if is_blank(patient):
                continue
            sheet.rows.append(
                EmployeeRow(
                    sheet=name,
                    row_num=row,
                    patient=patient,
                    session_date=(
                        worksheet.cell(row=row, column=date_column).value
                        if date_column else None
                    ),
                    values={
                        key: worksheet.cell(row=row, column=index).value
                        for key, index in columns.items()
                    },
                )
            )
        sheets[name] = sheet

    if not sheets:
        raise EmployeesError(
            "Workbook has none of the expected provider sheets "
            f"({', '.join(EMPLOYEE_SHEETS)}). "
            f"Found: {', '.join(workbook.sheetnames)}"
        )
    return sheets


# --- EOB-side view ----------------------------------------------------------

@dataclass(frozen=True)
class EobVisit:
    """One reconciled EOB visit, in the shape the employees tabs need.

    Built from the same `pdf_parser.Visit` objects the schedule is planned
    from, *after* reconciliation, so an OA-18 duplicate can never supply the
    amounts or the check/EFT number.
    """

    patient: str
    session_date: date
    npi: str
    insurance: str
    copay: Any
    payment: Any
    #: The paying remit's check/EFT number -- per the OA-18 rule this is never
    #: a duplicate's.
    check_eft: str = ""
    #: Set when the visit itself is unsafe to record (currently: every
    #: occurrence was an OA-18 duplicate, so no remit actually paid it). Any
    #: change built from it is surfaced for review and never auto-accepted.
    review_note: str = ""

    @property
    def date_str(self) -> str:
        return self.session_date.strftime(DATE_FMT)

    @property
    def provider_tab(self) -> str | None:
        """The provider tab that owns this visit's NPI, if any."""
        return tab_for_npi(self.npi)

    @property
    def key(self) -> tuple[str, date, str]:
        """Identity: the same (patient, date, NPI) key reconciliation used."""
        return (_normalize_token(self.patient), self.session_date, self.npi)


def tab_for_npi(npi: str) -> str | None:
    """The provider tab whose associate bills under this PERF PROV NPI.

    Returns ``None`` for a supervising physician's NPI or one the app does not
    know -- neither says anything about which associate performed the session,
    so neither is ever treated as a conflict.
    """
    for name, owner in EMPLOYEE_NPI.items():
        if owner and str(owner) == str(npi):
            return name
    return None


def eob_visits(visits: Sequence[Any]) -> list[EobVisit]:
    """The reconciled remittance visits, as the employees workbook sees them.

    Takes `pdf_parser.Visit` objects straight from aggregation -- the same
    list `build_plan` receives -- so the tabs and the schedule are populated
    from one source and cannot drift apart. Names are title-cased to the
    tabs' own style; nothing is read back out of the schedule.
    """
    built: list[EobVisit] = []
    for visit in visits:
        built.append(
            EobVisit(
                patient=to_title_name(visit.patient),
                session_date=visit.service_date,
                npi=str(visit.npi),
                insurance=visit.insurance,
                copay=visit.copay,
                payment=visit.payment,
                check_eft=visit.authoritative_eft or "",
                # A visit seen only as an exact duplicate was adjudicated at
                # $0 because the paying remit was never uploaded. Recording
                # that $0 in a provider's pay sheet would be wrong, so it is
                # shown and left for a human instead.
                review_note=visit.duplicate_note if visit.duplicate_only else "",
            )
        )
    return built


# --- Planning ---------------------------------------------------------------

def _same_person(left: str, right: str) -> bool:
    return name_score(left, right) >= NAME_AUTO_MATCH_SCORE


def _practitioner_conflict(visit: EobVisit, sheet_name: str) -> str:
    """The *other* associate this visit was billed under, if it was one.

    Only meaningful on a tab whose associate has an NPI of her own. A
    supervising physician's NPI (the incident-to case) is never a conflict:
    it is the ordinary way an associate's session is billed and says nothing
    about who performed it. Marcia's tab is exempt entirely -- her roster,
    not the remit, decides what is hers.
    """
    if sheet_name not in EMPLOYEE_APPEND_SHEETS:
        return ""
    owner = visit.provider_tab
    if owner is None or _normalize_token(owner) == _normalize_token(sheet_name):
        return ""
    return owner


def _matches_for(visits: Sequence[EobVisit], patient: Any,
                 session_date: date | None) -> list[EobVisit]:
    """Every reconciled EOB visit for this patient on this date."""
    if session_date is None or is_blank(patient):
        return []
    return [
        visit for visit in visits
        if visit.session_date == session_date
        and _same_person(str(patient), visit.patient)
    ]


def _pick_match(matches: Sequence[EobVisit],
                sheet_name: str) -> tuple[EobVisit, str]:
    """(the visit to use, why it is ambiguous -- blank when it is not).

    Several reconciled visits can share a patient and date when two providers
    both billed that day. The tab's own NPI settles it; on Marcia's tab, or
    when the tie survives, nothing is chosen silently.
    """
    if len(matches) == 1:
        return matches[0], ""

    own = EMPLOYEE_NPI.get(sheet_name)
    if own:
        preferred = [visit for visit in matches if visit.npi == str(own)]
        if len(preferred) == 1:
            return preferred[0], ""

    npis = ", ".join(sorted({visit.npi for visit in matches}))
    return matches[0], (
        f"{len(matches)} EOB visits match this patient and date (NPIs {npis})"
        + ("" if own else f"; {sheet_name} has no NPI of her own to resolve it")
    )


def _mapped_fills(sheet: EmployeeSheet, row_values: dict[str, Any],
                  visit: EobVisit) -> dict[int, Any]:
    """Blank mapped cells and what to put in them, as {column index: value}."""
    proposals = [
        (column_for(sheet.columns, EMPLOYEE_COL_INSURANCE), visit.insurance),
        (column_for(sheet.columns, EMPLOYEE_COL_COPAY_EOB), visit.copay),
        (sheet.payment_column, visit.payment),
    ]
    fills: dict[int, Any] = {}
    reverse = {index: key for key, index in sheet.columns.items()}
    for column, value in proposals:
        if column is None or value in (None, ""):
            continue
        if not is_blank(row_values.get(reverse.get(column, ""))):
            continue  # never overwrite
        fills[column] = value
    return fills


def _change_from(visit: EobVisit, sheet: EmployeeSheet, action: str,
                 **kwargs: Any) -> EmployeeChange:
    """An EmployeeChange carrying this visit's mapped values."""
    return EmployeeChange(
        sheet=sheet.name,
        action=action,
        patient=visit.patient,
        session_date=visit.date_str,
        insurance=visit.insurance,
        copay=visit.copay,
        payment=visit.payment,
        payment_column=EMPLOYEE_PAYMENT_COLUMN.get(sheet.name, ""),
        npi=visit.npi,
        check_eft=visit.check_eft,
        **kwargs,
    )


def plan_employee_changes(sheets: dict[str, EmployeeSheet],
                          visits: Sequence[EobVisit],
                          today: date | None = None) -> list[EmployeeChange]:
    """Decide what each provider sheet should get from the reconciled EOBs.

    Two independent passes. Every tab has its existing rows *filled* from the
    matching EOB visit; only the tabs whose associate has an NPI of her own
    additionally have NPI-matching visits *appended*. A visit that does
    neither is listed rather than guessed at.
    """
    stamp = (today or date.today()).strftime(DATE_FMT)
    changes: list[EmployeeChange] = []
    #: Visits that filled a row or were appended somewhere, by their
    #: (patient, date, NPI) key -- the one reconciliation already made unique.
    placed: set[tuple[str, date, str]] = set()

    # 1. Fill rows the tabs already have, matched by patient + Date of Session.
    for sheet in sheets.values():
        for row in sheet.rows:
            row_date = row.parsed_date
            matches = _matches_for(visits, row.patient, row_date)

            if not matches:
                changes.append(EmployeeChange(
                    sheet=sheet.name,
                    action=EMP_ACTION_NO_MATCH,
                    patient=str(row.patient),
                    session_date="" if row_date is None else row_date.strftime(DATE_FMT),
                    row_num=row.row_num,
                    accepted=False,
                ))
                continue

            match, ambiguity = _pick_match(matches, sheet.name)
            placed.add(match.key)
            fills = _mapped_fills(sheet, row.values, match)

            if ambiguity:
                changes.append(_change_from(
                    match, sheet, EMP_ACTION_AMBIGUOUS,
                    row_num=row.row_num, fills=fills,
                    note=f"{ambiguity} - confirm which one belongs here",
                    accepted=False,
                ))
                continue

            conflict = _practitioner_conflict(match, sheet.name)
            if conflict:
                changes.append(_change_from(
                    match, sheet, EMP_ACTION_MISMATCH,
                    row_num=row.row_num, fills=fills,
                    note=(
                        f"EOB was billed under {conflict}'s NPI {match.npi}, "
                        f"but this is the {sheet.name} tab"
                    ),
                    accepted=False,
                ))
                continue

            if match.review_note:
                changes.append(_change_from(
                    match, sheet, EMP_ACTION_AMBIGUOUS,
                    row_num=row.row_num, fills=fills,
                    note=match.review_note, accepted=False,
                ))
                continue

            changes.append(_change_from(
                match, sheet, EMP_ACTION_FILL if fills else EMP_ACTION_NOTHING,
                row_num=row.row_num, fills=fills, accepted=bool(fills),
            ))

    # 2. Append NPI-matched visits the tab does not have yet. Only tabs whose
    #    associate bills under her own NPI take appends; Marcia never does.
    for name in EMPLOYEE_SHEETS:
        sheet = sheets.get(name)
        if sheet is None or name not in EMPLOYEE_APPEND_SHEETS:
            continue
        own = str(EMPLOYEE_NPI[name])
        for visit in visits:
            if visit.npi != own:
                continue
            placed.add(visit.key)
            if sheet.has_session(visit.patient, visit.session_date):
                continue  # already in the tab -- dedup, so re-runs add nothing
            changes.append(_change_from(
                visit, sheet, EMP_ACTION_APPEND,
                fills=_append_values(sheet, visit),
                note=visit.review_note,
                accepted=not visit.review_note,
            ))

    # 3. Everything else: shown, never guessed at.
    for visit in visits:
        if visit.key not in placed:
            changes.append(_unassigned(visit, sheets))

    for change in changes:
        change.processed_on = stamp

    return changes


def _unassigned(visit: EobVisit, sheets: dict[str, EmployeeSheet]) -> EmployeeChange:
    """A visit no tab can claim: listed for manual placement, never guessed.

    Most visits bill under the supervising physician's NPI (incident-to), so
    this is the *ordinary* outcome for that physician's own direct patients,
    and for every Marcia session with no row already waiting for it. Nothing
    is written either way.
    """
    homes = [name for name, sheet in sheets.items() if sheet.has_patient(visit.patient)]
    if homes:
        fill_only = all(name not in EMPLOYEE_APPEND_SHEETS for name in homes)
        note = (
            f"{' / '.join(homes)} already has this patient, but the EOB's NPI "
            f"{visit.npi} is not that tab's provider"
            + (" (fill-only tab)" if fill_only else "")
            + " - add this session by hand"
        )
    else:
        note = "Patient is not in any provider tab - place this session by hand"

    if visit.review_note:
        note = f"{visit.review_note}; {note}"

    return EmployeeChange(
        sheet="",
        action=EMP_ACTION_UNASSIGNED,
        patient=visit.patient,
        session_date=visit.date_str,
        insurance=visit.insurance,
        copay=visit.copay,
        payment=visit.payment,
        npi=visit.npi,
        check_eft=visit.check_eft,
        note=note,
        accepted=False,
    )


def _append_values(sheet: EmployeeSheet, visit: EobVisit) -> dict[int, Any]:
    """Every mapped column for a brand-new row, as {column index: value}."""
    values: dict[int, Any] = {}
    for column, value in (
        (sheet.patient_column, visit.patient),
        (column_for(sheet.columns, EMPLOYEE_COL_DATE), visit.date_str),
        (column_for(sheet.columns, EMPLOYEE_COL_INSURANCE), visit.insurance),
        (column_for(sheet.columns, EMPLOYEE_COL_COPAY_EOB), visit.copay),
        (sheet.payment_column, visit.payment),
    ):
        if column is not None and value not in (None, ""):
            values[column] = value
    return values


# --- Applying ---------------------------------------------------------------

def _copy_style(source_cell, target_cell) -> None:
    if source_cell.has_style:
        target_cell.font = copy(source_cell.font)
        target_cell.border = copy(source_cell.border)
        target_cell.fill = copy(source_cell.fill)
        target_cell.alignment = copy(source_cell.alignment)
        target_cell.number_format = source_cell.number_format
        target_cell.protection = copy(source_cell.protection)


def _first_empty_header_column(worksheet: Worksheet, header_row: int) -> int:
    """First column whose header cell on this tab's header row is empty."""
    column = 1
    while not is_blank(worksheet.cell(row=header_row, column=column).value):
        column += 1
    return column


def ensure_audit_columns(worksheet: Worksheet, sheet: EmployeeSheet) -> dict[str, int]:
    """Find or create this tab's audit columns, honouring its header row.

    Ana and Marcia carry their headers on row 1, Oxana on row 2, so the new
    headers land on that tab's own header row and inherit its formatting.
    """
    resolved: dict[str, int] = {}
    template = worksheet.cell(row=sheet.header_row, column=max(sheet.columns.values()))

    for header in EMPLOYEE_AUDIT_COLUMNS:
        existing = column_for(sheet.columns, header)
        if existing is not None:
            resolved[header] = existing
            continue
        column = _first_empty_header_column(worksheet, sheet.header_row)
        cell = worksheet.cell(row=sheet.header_row, column=column, value=header)
        _copy_style(template, cell)
        sheet.columns[header_key(header)] = column
        resolved[header] = column

    return resolved


def _stamp_audit(worksheet: Worksheet, columns: dict[str, int],
                 row_num: int, change: EmployeeChange) -> None:
    """Write this run's audit stamps onto a row the app filled or appended.

    These are the app's own columns, so a row touched this run is (re)stamped.
    Rows the app did not touch are never written to.
    """
    for header, value in (
        (COL_PROCESSED_ON, change.processed_on),
        (COL_CHECK_EFT, change.check_eft),
    ):
        column = columns.get(header)
        if column and value:
            worksheet.cell(row=row_num, column=column).value = value


def _last_data_row(worksheet: Worksheet, sheet: EmployeeSheet) -> int:
    patient_column = sheet.patient_column
    last = sheet.header_row
    for row in range(sheet.data_start_row, worksheet.max_row + 1):
        if not is_blank(worksheet.cell(row=row, column=patient_column).value):
            last = row
    return last


def apply_employee_changes(workbook: openpyxl.Workbook,
                           sheets: dict[str, EmployeeSheet],
                           changes: Sequence[EmployeeChange]) -> dict[str, int]:
    """Write accepted fills and appends. Never overwrites a populated cell."""
    filled_cells = 0
    filled_rows = 0
    appended_rows = 0
    skipped_non_blank = 0

    accepted = [c for c in changes if c.accepted and c.writes and c.fills]

    # The audit headers are created only on tabs this run actually writes to.
    audit: dict[str, dict[str, int]] = {}
    for name in {c.sheet for c in accepted}:
        if name in sheets:
            audit[name] = ensure_audit_columns(workbook[name], sheets[name])

    for change in accepted:
        if change.action != EMP_ACTION_FILL or change.row_num is None:
            continue
        worksheet = workbook[change.sheet]
        wrote_any = False
        for column, value in change.fills.items():
            cell = worksheet.cell(row=change.row_num, column=column)
            if not is_blank(cell.value):
                skipped_non_blank += 1
                continue
            cell.value = value
            filled_cells += 1
            wrote_any = True
        if wrote_any:
            filled_rows += 1
            _stamp_audit(worksheet, audit.get(change.sheet, {}),
                         change.row_num, change)

    for name in EMPLOYEE_SHEETS:
        appends = [c for c in accepted if c.appends and c.sheet == name]
        if not appends or name not in sheets:
            continue
        worksheet = workbook[name]
        sheet = sheets[name]
        template_row = _last_data_row(worksheet, sheet)
        next_row = template_row + 1
        for change in sorted(appends, key=lambda c: (c.patient, c.session_date)):
            for column in sorted(sheet.columns.values()):
                target = worksheet.cell(row=next_row, column=column)
                if template_row > sheet.header_row:
                    _copy_style(worksheet.cell(row=template_row, column=column), target)
                if column in change.fills:
                    target.value = change.fills[column]
            _stamp_audit(worksheet, audit.get(name, {}), next_row, change)
            appended_rows += 1
            next_row += 1

    return {
        "filled_rows": filled_rows,
        "filled_cells": filled_cells,
        "appended_rows": appended_rows,
        "skipped_non_blank": skipped_non_blank,
    }


def workbook_to_bytes(workbook: openpyxl.Workbook) -> io.BytesIO:
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def build_updated_employees(data: bytes,
                            changes: Sequence[EmployeeChange]) -> tuple[io.BytesIO, dict[str, int]]:
    """Apply changes to a fresh copy of the upload and return it for download."""
    workbook = load_employees_workbook(data)
    sheets = read_sheets(workbook)
    stats = apply_employee_changes(workbook, sheets, changes)
    return workbook_to_bytes(workbook), stats


def employees_download_filename(original: str = "AMSMC_employees.xlsx",
                                today: date | None = None) -> str:
    """Date-stamped name. Never contains a patient name."""
    import re

    stem = re.sub(r"\.xlsx$", "", original, flags=re.IGNORECASE) or "AMSMC_employees"
    return f"{stem}_updated_{(today or date.today()).isoformat()}.xlsx"


def summarize_employees(changes: Sequence[EmployeeChange]) -> dict[str, int]:
    return {
        "rows": len(changes),
        "fill": sum(1 for c in changes if c.action == EMP_ACTION_FILL),
        "append": sum(1 for c in changes if c.action == EMP_ACTION_APPEND),
        "no_match": sum(1 for c in changes if c.action == EMP_ACTION_NO_MATCH),
        "mismatch": sum(1 for c in changes if c.action == EMP_ACTION_MISMATCH),
        "ambiguous": sum(1 for c in changes if c.action == EMP_ACTION_AMBIGUOUS),
        "unassigned": sum(1 for c in changes if c.action == EMP_ACTION_UNASSIGNED),
    }
