"""Read `List_of_Patients_Mutual.xlsx` and look up a patient's DX code.

The `Active` sheet has **no header row** -- data starts on row 1 and columns
are read positionally: A = Patient, B = DX. Other columns (including E,
attending doctor) exist but are deliberately not read.

Name lookup reuses the same normalisation the schedule matcher uses, so
generational suffixes, casing and Medicare truncation resolve identically
across all three files.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import (
    DX_AMBIGUOUS,
    DX_CONFLICT,
    DX_FOUND,
    DX_NOT_FOUND,
    MUTUAL_COL_DX,
    MUTUAL_COL_PATIENT,
    MUTUAL_DATA_START_ROW,
    MUTUAL_SHEET_NAME,
    NAME_AUTO_MATCH_SCORE,
    NAME_REVIEW_MIN_SCORE,
)
from .matching import is_blank, name_score, split_name


def _name_key(name: str) -> str:
    """The exact-match key: harmonised surname + first name."""
    last, first = split_name(name)
    return f"{last}|{first}"


@dataclass(frozen=True)
class DxEntry:
    """One row of the Mutual sheet that carries a usable DX."""

    name: str
    dx: str
    row_num: int


@dataclass(frozen=True)
class DxMatch:
    """The outcome of looking one patient up in the Mutual file."""

    dx: str | None
    status: str
    matched_name: str | None = None
    score: float | None = None
    conflicting: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return self.status in (DX_FOUND, DX_CONFLICT)

    @property
    def needs_review(self) -> bool:
        return self.status == DX_AMBIGUOUS


@dataclass
class DxLookup:
    """Normalised patient name -> DX, with conflict and ambiguity reporting."""

    entries: list[DxEntry] = field(default_factory=list)
    by_key: dict[str, list[DxEntry]] = field(default_factory=dict)
    source_name: str = "List_of_Patients_Mutual.xlsx"

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def conflicts(self) -> dict[str, tuple[str, ...]]:
        """Keys whose rows disagree about the DX, with the differing values."""
        out: dict[str, tuple[str, ...]] = {}
        for key, entries in self.by_key.items():
            distinct = tuple(dict.fromkeys(e.dx for e in entries))
            if len(distinct) > 1:
                out[key] = distinct
        return out

    def _resolve(self, entries: list[DxEntry], score: float | None) -> DxMatch:
        """First entry wins; disagreement is reported, not silently dropped."""
        distinct = tuple(dict.fromkeys(entry.dx for entry in entries))
        first = entries[0]
        if len(distinct) > 1:
            return DxMatch(
                dx=first.dx,
                status=DX_CONFLICT,
                matched_name=first.name,
                score=score,
                conflicting=distinct,
            )
        return DxMatch(dx=first.dx, status=DX_FOUND, matched_name=first.name, score=score)

    def find(self, name: str) -> DxMatch:
        """Look one patient up, exactly first and then fuzzily."""
        if not name or not self.entries:
            return DxMatch(dx=None, status=DX_NOT_FOUND)

        exact = self.by_key.get(_name_key(name))
        if exact:
            return self._resolve(exact, score=100.0)

        scored = [(entry, name_score(name, entry.name)) for entry in self.entries]
        best = max(scored, key=lambda pair: pair[1])[1] if scored else 0.0
        if best < NAME_REVIEW_MIN_SCORE:
            return DxMatch(dx=None, status=DX_NOT_FOUND)

        top = [entry for entry, score in scored if score == best]
        if best < NAME_AUTO_MATCH_SCORE:
            return DxMatch(
                dx=None,
                status=DX_AMBIGUOUS,
                matched_name=top[0].name,
                score=best,
            )
        return self._resolve(top, score=best)


def build_dx_lookup(rows: list[tuple[str, str, int]],
                    source_name: str = "List_of_Patients_Mutual.xlsx") -> DxLookup:
    """Build a lookup from `(patient, dx, row_num)` triples."""
    lookup = DxLookup(source_name=source_name)
    for patient, dx, row_num in rows:
        if is_blank(patient) or is_blank(dx):
            continue
        entry = DxEntry(name=str(patient).strip(), dx=str(dx).strip(), row_num=row_num)
        lookup.entries.append(entry)
        lookup.by_key.setdefault(_name_key(entry.name), []).append(entry)
    return lookup


def load_dx_lookup(data: bytes,
                   source_name: str = "List_of_Patients_Mutual.xlsx") -> DxLookup:
    """Read the `Active` sheet of an uploaded Mutual workbook."""
    # Imported lazily so this module does not depend on excel_updater at import
    # time (excel_updater already depends on matching).
    from .excel_updater import ScheduleError, load_schedule_workbook

    workbook = load_schedule_workbook(data)
    if MUTUAL_SHEET_NAME not in workbook.sheetnames:
        raise ScheduleError(
            f"Mutual workbook has no sheet named '{MUTUAL_SHEET_NAME}'. "
            f"Found: {', '.join(workbook.sheetnames)}"
        )

    worksheet = workbook[MUTUAL_SHEET_NAME]
    rows: list[tuple[str, str, int]] = []
    for row in range(MUTUAL_DATA_START_ROW, worksheet.max_row + 1):
        patient = worksheet.cell(row=row, column=MUTUAL_COL_PATIENT).value
        dx = worksheet.cell(row=row, column=MUTUAL_COL_DX).value
        if is_blank(patient) or is_blank(dx):
            continue
        rows.append((str(patient).strip(), str(dx).strip(), row))

    return build_dx_lookup(rows, source_name=source_name)
