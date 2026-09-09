"""Match aggregated remittance visits to rows of the `2026 Medicare` sheet.

Two things make this harder than a dict lookup:

* Patient names arrive uppercased from Medicare, sometimes truncated
  (e.g. a 17-letter surname clipped to 13 characters) or carrying a trailing
  middle initial (``SMITH, JANE R``), against Title Case names in the sheet.
* The ``Data``/``Billed`` columns are hand-entered free text and hold a mix of
  real datetimes, ``MM/DD/YYYY`` strings and junk placeholders (``2/32/26``,
  ``No Billing``), so dates are only ever compared as parsed calendar values.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

from rapidfuzz import fuzz

from .config import (
    ACTION_FILL,
    ACTION_NEW,
    ACTION_REVIEW,
    ACTION_SKIP,
    COL_BILLED,
    COL_COMMENT,
    COL_COPAY,
    COL_PAYMENT,
    FILLABLE_COLUMNS,
    NAME_AUTO_MATCH_SCORE,
    NAME_REVIEW_MIN_SCORE,
)
from .pdf_parser import Visit, order_cpt_codes

_WS_RE = re.compile(r"\s+")

# Date strings seen in the wild, most specific first.
_DATE_FORMATS = (
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%Y-%m-%d",
    "%m.%d.%Y",
    "%m.%d.%y",
)

_DATE_LIKE_RE = re.compile(r"^\s*(\d{1,4})[/\-.](\d{1,2})[/\-.](\d{2,4})\s*$")

# Weighting for the combined name score: the surname carries more signal.
_LAST_WEIGHT = 0.6
_FIRST_WEIGHT = 0.4

# Shortest prefix that may stand in for a Medicare-truncated name part.
_MIN_LAST_PREFIX = 4
_MIN_FIRST_PREFIX = 3


# --- Name normalisation ----------------------------------------------------

def _normalize_token(value: str) -> str:
    """Lowercase, strip zero-width junk and collapse whitespace."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace(" ", " ")  # non-breaking space
    for zero_width in ("​", "‌", "‍", "﻿"):
        text = text.replace(zero_width, "")
    return _WS_RE.sub(" ", text.strip().lower())


def surname_key(value: str) -> str:
    """Harmonise Slavic/Armenian surname endings so gendered forms agree.

    Mirrors the harmonisation used by the clinic's calendar tooling, so
    ``Ivanova``/``Ivanov`` and ``-ielyan``/``-ilyan`` collapse to one key.
    """
    key = _normalize_token(value)

    if key.endswith("ielyan"):
        key = key[:-6] + "ilyan"
    elif key.endswith("yelyan"):
        key = key[:-6] + "lyan"
    elif key.endswith("elyan"):
        key = key[:-5] + "lyan"

    replacements = (
        ("skaya", "sky"), ("tskaya", "tsky"), ("vskaya", "vsky"), ("zkaya", "zky"),
        ("ckaya", "cky"), ("shaya", "shay"), ("chaya", "chay"), ("zhaya", "zhay"),
        ("aya", "y"), ("ova", "ov"), ("eva", "ev"), ("ina", "in"),
        ("kina", "kin"), ("yina", "yin"),
    )
    for suffix, replacement in replacements:
        if key.endswith(suffix):
            return key[: -len(suffix)] + replacement
    return key


def split_name(value: str) -> tuple[str, str]:
    """``"SMITH, JANE R"`` -> ``("smith", "jane")``.

    Splits on the comma, drops a trailing single-letter middle initial and
    harmonises the surname. Names without a comma are treated as surname-only.
    """
    text = _normalize_token(value)
    if not text:
        return "", ""

    if "," in text:
        last, _, first = text.partition(",")
    else:
        last, first = text, ""

    first_parts = first.split()
    if len(first_parts) > 1 and len(first_parts[-1].rstrip(".")) == 1:
        first_parts = first_parts[:-1]

    return surname_key(last.strip()), " ".join(first_parts).strip()


def _part_score(left: str, right: str, min_prefix: int) -> float:
    """Score one name part, treating Medicare truncation as a full match."""
    if not left or not right:
        return 100.0 if left == right else 0.0
    if left == right:
        return 100.0
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= min_prefix and longer.startswith(shorter):
        return 100.0
    return float(fuzz.ratio(left, right))


def name_score(pdf_name: str, sheet_name: str) -> float:
    """0-100 similarity between a remittance name and a schedule name."""
    pdf_last, pdf_first = split_name(pdf_name)
    sheet_last, sheet_first = split_name(sheet_name)

    last = _part_score(pdf_last, sheet_last, _MIN_LAST_PREFIX)
    first = _part_score(pdf_first, sheet_first, _MIN_FIRST_PREFIX)
    return round(last * _LAST_WEIGHT + first * _FIRST_WEIGHT, 2)


# --- Free-text date handling ----------------------------------------------

def parse_loose_date(value: Any) -> date | None:
    """Parse a schedule cell into a calendar date, or ``None`` if it isn't one.

    Handles real ``datetime`` cells, the several hand-typed string formats in
    use, and returns ``None`` for placeholders such as ``2/32/26`` (day 32) or
    ``No Billing`` so they never compare equal to a real date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    # Fall back to a loose numeric split so 1/5/2026 style values still parse
    # even with stray spacing, while impossible days still fail cleanly.
    match = _DATE_LIKE_RE.match(text)
    if match:
        first, second, third = (int(g) for g in match.groups())
        year = third if third > 99 else 2000 + third
        try:
            return date(year, first, second)
        except ValueError:
            return None
    return None


def is_blank(value: Any) -> bool:
    """True only for a genuinely empty cell.

    ``0``, ``0.00`` and formula strings such as ``=23.52+18.92`` are recorded
    values, not blanks, and must never be overwritten.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


# --- Schedule rows ---------------------------------------------------------

@dataclass
class ScheduleRow:
    """One data row of the `2026 Medicare` sheet, as read from the workbook."""

    row_num: int
    patient: Any = None
    ins: Any = None
    data: Any = None
    billed: Any = None
    payment: Any = None
    copay: Any = None
    comment: Any = None
    dx: Any = None
    cpt: Any = None

    @property
    def data_date(self) -> date | None:
        return parse_loose_date(self.data)

    @property
    def payment_recorded(self) -> bool:
        """Any value at all in Payment means this visit is already recorded."""
        return not is_blank(self.payment)

    @property
    def cpt_codes(self) -> set[str]:
        if is_blank(self.cpt):
            return set()
        return {c.strip() for c in re.split(r"[/,;+]", str(self.cpt)) if c.strip()}


# --- Planned changes -------------------------------------------------------

@dataclass
class Change:
    """A single proposed edit, pending the user's confirmation."""

    visit: Visit
    action: str
    row_num: int | None = None
    fills: dict[str, Any] = field(default_factory=dict)
    existing_billed: Any = None
    score: float | None = None
    review_reasons: list[str] = field(default_factory=list)
    accepted: bool = True

    @property
    def needs_review(self) -> bool:
        return self.action == ACTION_REVIEW

    @property
    def effective_action(self) -> str:
        """What accepting this change would actually do.

        A reviewed item keeps its ``Needs review`` label in the UI; if the user
        ticks it anyway, its shape decides whether it fills a row or appends
        one. Deriving this avoids mutating the plan on confirmation.
        """
        if self.action != ACTION_REVIEW:
            return self.action
        return ACTION_FILL if self.row_num and self.fills else ACTION_NEW

    @property
    def action_label(self) -> str:
        if self.action == ACTION_FILL and self.row_num:
            return f"{ACTION_FILL} {self.row_num}"
        return self.action


def _candidate_rows(visit: Visit, rows: Sequence[ScheduleRow]) -> list[tuple[ScheduleRow, float]]:
    """Rows on the visit's calendar date whose name is at least review-worthy."""
    scored = []
    for row in rows:
        if is_blank(row.patient) or row.data_date != visit.service_date:
            continue
        score = name_score(visit.patient, str(row.patient))
        if score >= NAME_REVIEW_MIN_SCORE:
            scored.append((row, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def _disambiguate(visit: Visit,
                  candidates: Sequence[tuple[ScheduleRow, float]]) -> tuple[ScheduleRow, float] | None:
    """Pick between rows that share a patient and date (e.g. two providers).

    Prefers CPT overlap, then a matching provider Comment. Returns ``None``
    when the tie survives both, so the caller can flag it for review.
    """
    best_score = candidates[0][1]
    top = [pair for pair in candidates if pair[1] == best_score]
    if len(top) == 1:
        return top[0]

    visit_cpts = set(visit.cpt_codes)
    by_cpt = [pair for pair in top if pair[0].cpt_codes & visit_cpts]
    if len(by_cpt) == 1:
        return by_cpt[0]
    if by_cpt:
        top = by_cpt

    if visit.doctor:
        by_doctor = [
            pair for pair in top
            if not is_blank(pair[0].comment)
            and _normalize_token(pair[0].comment) == _normalize_token(visit.doctor)
        ]
        if len(by_doctor) == 1:
            return by_doctor[0]

    # A single unpaid row among otherwise-identical candidates is unambiguous.
    unpaid = [pair for pair in top if not pair[0].payment_recorded]
    if len(unpaid) == 1:
        return unpaid[0]

    return None


def _fill_values(visit: Visit, row: ScheduleRow) -> dict[str, Any]:
    """Target cells that are currently blank, and what to put in them.

    Anything already holding a value -- including a junk ``Billed``
    placeholder -- is left out of the result and so never overwritten.
    """
    proposed = {
        COL_BILLED: visit.billed_str,
        COL_PAYMENT: visit.payment,
        COL_COPAY: visit.copay,
        COL_COMMENT: visit.doctor,
    }
    current = {
        COL_BILLED: row.billed,
        COL_PAYMENT: row.payment,
        COL_COPAY: row.copay,
        COL_COMMENT: row.comment,
    }
    return {
        column: proposed[column]
        for column in FILLABLE_COLUMNS
        if proposed[column] not in (None, "") and is_blank(current[column])
    }


def plan_change(visit: Visit, rows: Sequence[ScheduleRow]) -> Change:
    """Decide what, if anything, this visit should do to the schedule."""
    review_reasons: list[str] = []
    if not visit.known_provider:
        review_reasons.append(f"Unknown provider NPI {visit.npi} - Comment left blank")
    if len(visit.check_efts) > 1:
        review_reasons.append(
            "Service lines came from more than one check/EFT "
            f"({', '.join(sorted(visit.check_efts))}) - verify before applying"
        )

    candidates = _candidate_rows(visit, rows)

    if not candidates:
        return Change(
            visit=visit,
            action=ACTION_REVIEW if review_reasons else ACTION_NEW,
            fills={},
            score=None,
            review_reasons=review_reasons,
            accepted=not review_reasons,
        )

    chosen = _disambiguate(visit, candidates)
    if chosen is None:
        rows_listed = ", ".join(str(row.row_num) for row, _ in candidates)
        review_reasons.append(
            f"Ambiguous match - rows {rows_listed} share this patient and date; "
            f"accepting applies to row {candidates[0][0].row_num}"
        )
        # Fills are computed against the row shown in the preview, so ticking an
        # ambiguous item has a visible effect rather than silently doing nothing.
        top_row = candidates[0][0]
        return Change(
            visit=visit,
            action=ACTION_REVIEW,
            row_num=top_row.row_num,
            fills={} if top_row.payment_recorded else _fill_values(visit, top_row),
            existing_billed=top_row.billed,
            score=candidates[0][1],
            review_reasons=review_reasons,
            accepted=False,
        )

    row, score = chosen

    if score < NAME_AUTO_MATCH_SCORE:
        review_reasons.append(
            f"Low-confidence name match ({score:.0f}%): "
            f"remittance '{visit.patient}' vs sheet '{row.patient}'"
        )

    if row.payment_recorded:
        return Change(
            visit=visit,
            action=ACTION_SKIP,
            row_num=row.row_num,
            existing_billed=row.billed,
            score=score,
            review_reasons=[],
            accepted=False,
        )

    if review_reasons:
        return Change(
            visit=visit,
            action=ACTION_REVIEW,
            row_num=row.row_num,
            fills=_fill_values(visit, row),
            existing_billed=row.billed,
            score=score,
            review_reasons=review_reasons,
            accepted=False,
        )

    return Change(
        visit=visit,
        action=ACTION_FILL,
        row_num=row.row_num,
        fills=_fill_values(visit, row),
        existing_billed=row.billed,
        score=score,
        review_reasons=[],
        accepted=True,
    )


def build_plan(visits: Iterable[Visit], rows: Sequence[ScheduleRow]) -> list[Change]:
    """Plan every visit against the schedule, newest decisions last."""
    return [plan_change(visit, rows) for visit in visits]


def summarize(changes: Sequence[Change]) -> dict[str, int]:
    """Counts behind the preview summary line."""
    return {
        "visits": len(changes),
        "fill": sum(1 for c in changes if c.action == ACTION_FILL),
        "new": sum(1 for c in changes if c.action == ACTION_NEW),
        "skip": sum(1 for c in changes if c.action == ACTION_SKIP),
        "review": sum(1 for c in changes if c.action == ACTION_REVIEW),
    }


def new_row_values(visit: Visit) -> dict[str, Any]:
    """Column values for an appended row (DX is deliberately left blank)."""
    from .config import COL_CPT, COL_DATA, COL_INS, COL_PATIENT, INSURANCE_VALUE

    return {
        COL_PATIENT: visit.patient,
        COL_INS: INSURANCE_VALUE,
        COL_DATA: visit.data_str,
        COL_BILLED: visit.billed_str,
        COL_PAYMENT: visit.payment,
        COL_COPAY: visit.copay,
        COL_COMMENT: visit.doctor,
        COL_CPT: "/".join(order_cpt_codes(visit.cpt_codes)),
    }
