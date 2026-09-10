"""Populate `AMSMC_employees.xlsx` from the reconciled `2026 Medicare` sheet.

One sheet per practitioner (Ana, Marcia, Oxana). Two things make this less
mechanical than it looks:

* **The header row is not in the same place on every sheet** -- Ana and Marcia
  put it on row 1, Oxana on row 2 -- so it is located by scanning the first
  rows for the expected labels rather than assumed. Labels also carry odd
  internal spacing (`Co-payment   Old`, `Paid by Ins toAna`), so they are
  matched case-insensitively with whitespace collapsed.
* **Who belongs to whom is decided by the tabs, not by the remit.** Staff
  curate these sheets, so a patient's presence in a tab is the source of
  truth. The schedule's `Comment` (derived from the remit NPI) is only a
  secondary cross-check, because an older visit may have been seen by a
  different practitioner.
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
    EMP_ACTION_APPEND,
    EMP_ACTION_FILL,
    EMP_ACTION_MISMATCH,
    EMP_ACTION_NO_MATCH,
    EMP_ACTION_NOTHING,
    EMP_ACTION_UNASSIGNED,
    EMPLOYEE_APPEND_SHEETS,
    EMPLOYEE_COL_COPAY_EOB,
    EMPLOYEE_COL_DATE,
    EMPLOYEE_COL_INSURANCE,
    EMPLOYEE_HEADER_SEARCH_ROWS,
    EMPLOYEE_PATIENT_HEADERS,
    EMPLOYEE_PAYMENT_COLUMN,
    EMPLOYEE_SHEETS,
    FALLBACK_TO_COMMENT_FOR_NEW,
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

    @property
    def needs_review(self) -> bool:
        return self.action in (EMP_ACTION_MISMATCH, EMP_ACTION_UNASSIGNED)

    @property
    def writes(self) -> bool:
        return self.action in (EMP_ACTION_FILL, EMP_ACTION_APPEND)


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


# --- Schedule-side view -----------------------------------------------------

@dataclass(frozen=True)
class ScheduleVisit:
    """The schedule facts the employees file needs, after the run's changes."""

    patient: str
    session_date: date
    insurance: str
    copay: Any
    payment: Any
    comment: str = ""

    @property
    def date_str(self) -> str:
        return self.session_date.strftime(DATE_FMT)


def schedule_visits_from_plan(rows, changes) -> list[ScheduleVisit]:
    """The schedule as it will look *after* this run's accepted changes.

    Existing rows contribute their current values, overlaid with anything the
    run fills or restates, so the employees file is populated from the same
    numbers the user is about to download.
    """
    from .config import COL_COMMENT, COL_COPAY, COL_PAYMENT

    pending: dict[int, dict[str, Any]] = {}
    for change in changes:
        if not change.accepted or change.row_num is None:
            continue
        merged = dict(change.fills)
        merged.update(change.updates)
        if merged:
            pending.setdefault(change.row_num, {}).update(merged)

    visits: list[ScheduleVisit] = []
    seen: set[tuple[tuple[str, str], date]] = set()

    def add(patient, session_date, insurance, copay, payment, comment):
        if is_blank(patient) or session_date is None:
            return
        from .matching import split_name

        key = (split_name(str(patient)), session_date)
        if key in seen:
            return
        seen.add(key)
        visits.append(
            ScheduleVisit(
                patient=str(patient),
                session_date=session_date,
                insurance=str(insurance or "").strip(),
                copay=copay,
                payment=payment,
                comment="" if is_blank(comment) else str(comment).strip(),
            )
        )

    for row in rows:
        overlay = pending.get(row.row_num, {})
        add(
            row.patient,
            row.data_date,
            overlay.get("Ins", row.ins),
            overlay.get(COL_COPAY, row.copay),
            overlay.get(COL_PAYMENT, row.payment),
            overlay.get(COL_COMMENT, row.comment),
        )

    # Rows this run appends do not exist in `rows` yet.
    for change in changes:
        if not change.accepted or change.row_num is not None:
            continue
        if not getattr(change, "visit", None):
            continue
        add(
            to_title_name(change.visit.patient),
            change.visit.service_date,
            change.visit.insurance,
            change.visit.copay,
            change.visit.payment,
            change.visit.doctor,
        )

    return visits


# --- Planning ---------------------------------------------------------------

def _same_person(left: str, right: str) -> bool:
    return name_score(left, right) >= NAME_AUTO_MATCH_SCORE


def names_a_provider_tab(comment: str) -> str | None:
    """The provider tab a `Comment` names, if it names one at all.

    `Comment` comes from the remit's performing-provider NPI, so it usually
    holds a *physician's* name (`Dr. Levinson`) rather than one of the three
    employee tabs. A physician name says nothing about which tab a session
    belongs to, so only a comment that actually matches a tab is a usable
    cross-check.
    """
    if not comment:
        return None
    key = _normalize_token(comment)
    for name in EMPLOYEE_SHEETS:
        if key == _normalize_token(name):
            return name
    return None


def _comment_conflict(visit: ScheduleVisit, sheet_name: str) -> bool:
    """True when the schedule `Comment` names a *different provider tab*.

    A blank `Comment`, or one naming a physician who is not one of the tabs,
    is never a conflict: it carries no signal about which tab is correct, and
    treating it as one would flag essentially every row.
    """
    named = names_a_provider_tab(visit.comment)
    if named is None:
        return False
    return _normalize_token(named) != _normalize_token(sheet_name)


def _mapped_fills(sheet: EmployeeSheet, row_values: dict[str, Any],
                  visit: ScheduleVisit) -> dict[int, Any]:
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


def plan_employee_changes(sheets: dict[str, EmployeeSheet],
                          visits: Sequence[ScheduleVisit]) -> list[EmployeeChange]:
    """Decide what each provider sheet should get from the schedule."""
    changes: list[EmployeeChange] = []
    matched_visits: set[tuple[str, date]] = set()

    # 1. Fill rows the tabs already have, matched by patient + Date of Session.
    for sheet in sheets.values():
        payment_label = EMPLOYEE_PAYMENT_COLUMN.get(sheet.name, "")
        for row in sheet.rows:
            row_date = row.parsed_date
            match = next(
                (
                    visit for visit in visits
                    if visit.session_date == row_date
                    and _same_person(str(row.patient), visit.patient)
                ),
                None,
            ) if row_date else None

            if match is None:
                changes.append(EmployeeChange(
                    sheet=sheet.name,
                    action=EMP_ACTION_NO_MATCH,
                    patient=str(row.patient),
                    session_date="" if row_date is None else row_date.strftime(DATE_FMT),
                    row_num=row.row_num,
                    accepted=False,
                ))
                continue

            matched_visits.add((_key_of(match.patient), match.session_date))

            if _comment_conflict(match, sheet.name):
                changes.append(EmployeeChange(
                    sheet=sheet.name,
                    action=EMP_ACTION_MISMATCH,
                    patient=str(row.patient),
                    session_date=match.date_str,
                    row_num=row.row_num,
                    insurance=match.insurance,
                    copay=match.copay,
                    payment=match.payment,
                    payment_column=payment_label,
                    fills=_mapped_fills(sheet, row.values, match),
                    note=(
                        f"Schedule Comment says '{match.comment}', but this is "
                        f"the {sheet.name} tab"
                    ),
                    accepted=False,
                ))
                continue

            fills = _mapped_fills(sheet, row.values, match)
            changes.append(EmployeeChange(
                sheet=sheet.name,
                action=EMP_ACTION_FILL if fills else EMP_ACTION_NOTHING,
                patient=str(row.patient),
                session_date=match.date_str,
                row_num=row.row_num,
                insurance=match.insurance,
                copay=match.copay,
                payment=match.payment,
                payment_column=payment_label,
                fills=fills,
                accepted=bool(fills),
            ))

    # 2. Append the remaining visits of patients already established in a tab.
    for visit in visits:
        if (_key_of(visit.patient), visit.session_date) in matched_visits:
            continue

        homes = [name for name, sheet in sheets.items() if sheet.has_patient(visit.patient)]

        if not homes:
            changes.append(_unassigned(visit, sheets))
            continue

        target = homes[0]
        note = ""
        if len(homes) > 1:
            # Present in several tabs: the Comment is the only signal left.
            by_comment = [name for name in homes if not _comment_conflict(visit, name)]
            if visit.comment and len(by_comment) == 1:
                target = by_comment[0]
                note = f"routed by Comment '{visit.comment}' ({', '.join(homes)})"
            else:
                changes.append(EmployeeChange(
                    sheet=" / ".join(homes),
                    action=EMP_ACTION_MISMATCH,
                    patient=visit.patient,
                    session_date=visit.date_str,
                    insurance=visit.insurance,
                    copay=visit.copay,
                    payment=visit.payment,
                    note=(
                        f"Patient appears in {len(homes)} tabs ({', '.join(homes)}) "
                        "and the Comment does not resolve it"
                    ),
                    accepted=False,
                ))
                continue

        if target not in EMPLOYEE_APPEND_SHEETS:
            # Marcia's tab is fill-only by policy.
            changes.append(EmployeeChange(
                sheet=target,
                action=EMP_ACTION_UNASSIGNED,
                patient=visit.patient,
                session_date=visit.date_str,
                insurance=visit.insurance,
                copay=visit.copay,
                payment=visit.payment,
                note=f"{target} is fill-only; add this session by hand if it belongs here",
                accepted=False,
            ))
            continue

        sheet = sheets[target]
        if _comment_conflict(visit, target):
            note = f"Schedule Comment says '{visit.comment}', but appending to {target}"
            changes.append(EmployeeChange(
                sheet=target,
                action=EMP_ACTION_MISMATCH,
                patient=visit.patient,
                session_date=visit.date_str,
                insurance=visit.insurance,
                copay=visit.copay,
                payment=visit.payment,
                payment_column=EMPLOYEE_PAYMENT_COLUMN.get(target, ""),
                note=note,
                accepted=False,
            ))
            continue

        changes.append(EmployeeChange(
            sheet=target,
            action=EMP_ACTION_APPEND,
            patient=visit.patient,
            session_date=visit.date_str,
            insurance=visit.insurance,
            copay=visit.copay,
            payment=visit.payment,
            payment_column=EMPLOYEE_PAYMENT_COLUMN.get(target, ""),
            fills=_append_values(sheet, visit),
            note=note,
            accepted=True,
        ))

    return changes


def _key_of(patient: str):
    from .matching import split_name

    return split_name(patient)


def _unassigned(visit: ScheduleVisit, sheets: dict[str, EmployeeSheet]) -> EmployeeChange:
    """A patient in no tab: listed, never guessed at."""
    if FALLBACK_TO_COMMENT_FOR_NEW and visit.comment:
        target = next(
            (name for name in sheets if _normalize_token(name) == _normalize_token(visit.comment)),
            None,
        )
        if target and target in EMPLOYEE_APPEND_SHEETS:
            sheet = sheets[target]
            return EmployeeChange(
                sheet=target,
                action=EMP_ACTION_APPEND,
                patient=visit.patient,
                session_date=visit.date_str,
                insurance=visit.insurance,
                copay=visit.copay,
                payment=visit.payment,
                payment_column=EMPLOYEE_PAYMENT_COLUMN.get(target, ""),
                fills=_append_values(sheet, visit),
                note=f"new patient routed by Comment '{visit.comment}'",
                accepted=True,
            )

    return EmployeeChange(
        sheet="",
        action=EMP_ACTION_UNASSIGNED,
        patient=visit.patient,
        session_date=visit.date_str,
        insurance=visit.insurance,
        copay=visit.copay,
        payment=visit.payment,
        note="Patient is not in any provider tab - place this session by hand",
        accepted=False,
    )


def _append_values(sheet: EmployeeSheet, visit: ScheduleVisit) -> dict[int, Any]:
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

    for name in EMPLOYEE_SHEETS:
        appends = [
            c for c in accepted
            if c.action == EMP_ACTION_APPEND and c.sheet == name
        ]
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
        "unassigned": sum(1 for c in changes if c.action == EMP_ACTION_UNASSIGNED),
    }
