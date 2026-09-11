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
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from rapidfuzz import fuzz

from .config import (
    ACTION_FILL,
    ACTION_NEW,
    ACTION_REVIEW,
    ACTION_SKIP,
    ACTION_UPDATE,
    AMOUNT_TOLERANCE,
    CHECK_EFT_JOINER,
    COL_BILLED,
    COL_CHECK_EFT,
    COL_COMMENT,
    COL_COPAY,
    COL_DX,
    COL_PAYMENT,
    COL_PROCESSED_ON,
    DATE_FMT,
    DX_AMBIGUOUS,
    DX_CONFLICT,
    DX_NO_FILE,
    FILL_DX_ON_EXISTING,
    FILLABLE_COLUMNS,
    GENERATIONAL_SUFFIX_TITLES,
    GENERATIONAL_SUFFIXES,
    NAME_AUTO_MATCH_SCORE,
    NAME_REVIEW_MIN_SCORE,
    OVERWRITE_INS_FOR_TELEHEALTH,
    REPROCESS_FLAG_ONLY,
    REPROCESS_POLICY,
    REPROCESS_SUM,
)
from .pdf_parser import Visit, order_cpt_codes

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from .mutual import DxLookup

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


def strip_generational_suffix(last_name: str) -> str:
    """``"marchetti jr"`` -> ``"marchetti"``; ``"smith iii"`` -> ``"smith"``.

    Applied to the surname *before* harmonisation, so a suffix cannot block
    the Slavic/Armenian ending rules from firing (``Bystritskaya Jr`` has to
    reduce the same way ``Bystritskaya`` does). Never strips the only token,
    so a genuine surname of ``V`` survives.
    """
    parts = _normalize_token(last_name).split()
    while len(parts) > 1 and parts[-1].rstrip(".") in GENERATIONAL_SUFFIXES:
        parts.pop()
    return " ".join(parts)


def split_name(value: str) -> tuple[str, str]:
    """``"SMITH, JANE R"`` -> ``("smith", "jane")``.

    Splits on the comma, drops a trailing single-letter middle initial, strips
    any generational suffix and harmonises the surname. Names without a comma
    are treated as surname-only.
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

    return surname_key(strip_generational_suffix(last)), " ".join(first_parts).strip()


# --- Title casing for names the app writes ---------------------------------

def _title_word(word: str) -> str:
    """Capitalise one hyphen-free, space-free word, keeping Mc/apostrophes."""
    if not word:
        return word
    if "'" in word:
        return "'".join(_title_word(part) for part in word.split("'"))
    titled = word[0].upper() + word[1:].lower()
    # McDonald, but not "Mc" alone. Mac- is deliberately left alone: MacDonald
    # and Machado/Macy are indistinguishable without a name list.
    if len(titled) > 3 and titled[:2] == "Mc":
        titled = titled[:2] + titled[2].upper() + titled[3:]
    return titled


def _title_part(part: str) -> str:
    """Title-case one side of the comma, token by token."""
    tokens = []
    for token in part.split():
        stripped = token.rstrip(".")
        trailing = token[len(stripped):]
        suffix = GENERATIONAL_SUFFIX_TITLES.get(stripped.lower())
        if suffix is not None:
            tokens.append(suffix + trailing)
        else:
            tokens.append("-".join(_title_word(w) for w in token.split("-")))
    return " ".join(tokens)


def to_title_name(value: str) -> str:
    """``"MARCHETTI, DEAN"`` -> ``"Marchetti, Dean"``, matching the sheet's style.

    Preserves the ``Last, First`` shape, keeps single-letter middle initials
    capitalised, renders generational suffixes the way the sheet writes them
    (``Jr``/``Sr``, roman numerals uppercase), and handles hyphens,
    apostrophes and ``Mc``. Used only for names the app *creates* -- an
    existing name in the sheet is never rewritten.
    """
    if value is None:
        return ""
    text = _WS_RE.sub(" ", str(value).strip())
    if not text:
        return ""

    if "," in text:
        last, _, first = text.partition(",")
        last_titled = _title_part(last.strip())
        first_titled = _title_part(first.strip())
        return f"{last_titled}, {first_titled}" if first_titled else last_titled
    return _title_part(text)


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
    #: The app's own audit columns, read back so a restatement can append to
    #: the check/EFT history rather than replacing it.
    processed_on: Any = None
    check_eft: Any = None

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

    #: DX sourced from the optional Mutual workbook, and how it was resolved.
    dx_value: str | None = None
    dx_status: str = DX_NO_FILE
    dx_note: str = ""

    #: Audit stamps written to the app's own columns for any row it touches.
    processed_on: str = ""
    check_eft_display: str = ""

    #: Where a *new* row goes: directly under this patient's last existing row,
    #: or at the bottom of the sheet when the patient is not in the schedule.
    #: Row numbers refer to the original layout, before any insertion.
    anchor_row: int | None = None
    anchor_patient: str | None = None
    #: Set when a structurally unsafe anchor forced a bottom append instead.
    placement_fallback: str = ""

    #: Cells an accepted `Updated (adjusted EOB)` change would OVERWRITE, and
    #: the values they hold today. Unlike `fills`, these target populated
    #: cells, so they are only ever written for an accepted update.
    updates: dict[str, Any] = field(default_factory=dict)
    previous: dict[str, Any] = field(default_factory=dict)
    #: Plain-language notes about the restatement, shown in the preview.
    update_notes: list[str] = field(default_factory=list)

    #: Explains a discarded OA-18 duplicate, or a duplicate-only visit.
    duplicate_note: str = ""

    #: Set when the EOB says telehealth but the matched row's `Ins` does not.
    #: Surfaced in the preview; the cell itself is only rewritten when
    #: OVERWRITE_INS_FOR_TELEHEALTH is on, via `ins_update`.
    telehealth_mismatch: str = ""
    ins_update: str = ""

    @property
    def needs_review(self) -> bool:
        return self.action == ACTION_REVIEW

    @property
    def inserts_under_patient(self) -> bool:
        """True when this new row is grouped under an existing patient."""
        return self.anchor_row is not None and not self.placement_fallback

    @property
    def placement_display(self) -> str:
        """The preview's placement column, for new rows."""
        if self.effective_action != ACTION_NEW:
            return ""
        if self.placement_fallback:
            return f"appended ({self.placement_fallback})"
        if self.anchor_row is not None:
            return f'under existing "{self.anchor_patient}"'
        return "appended (patient not in schedule)"

    @property
    def dx_display(self) -> str:
        """The DX column of the preview table."""
        if self.dx_status == DX_NO_FILE:
            return ""
        if self.dx_value and self.dx_note:
            return f"{self.dx_value} ({self.dx_note})"
        if self.dx_value:
            return self.dx_value
        return f"({self.dx_status})"

    @property
    def effective_action(self) -> str:
        """What accepting this change would actually do.

        A reviewed item keeps its ``Needs review`` label in the UI; if the user
        ticks it anyway, its shape decides whether it fills a row or appends
        one. Deriving this avoids mutating the plan on confirmation.
        """
        if self.action != ACTION_REVIEW:
            return self.action
        if self.row_num and self.updates:
            return ACTION_UPDATE
        return ACTION_FILL if self.row_num and self.fills else ACTION_NEW

    @property
    def action_label(self) -> str:
        if self.action == ACTION_FILL and self.row_num:
            return f"{ACTION_FILL} {self.row_num}"
        if self.action == ACTION_UPDATE and self.row_num:
            return f"{ACTION_UPDATE} - row {self.row_num}"
        return self.action

    @property
    def is_update(self) -> bool:
        """True when applying this would overwrite a recorded amount."""
        return bool(self.updates)

    def change_display(self, column: str) -> str:
        """`old -> new` for one column, or blank when it is not changing."""
        if column not in self.updates:
            return ""
        before = self.previous.get(column)
        before_text = "(blank)" if is_blank(before) else str(before)
        return f"{before_text} -> {self.updates[column]}"

    @property
    def update_summary(self) -> str:
        """The preview's one-line explanation of a restatement."""
        return "; ".join(self.update_notes)


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


def _fill_values(visit: Visit, row: ScheduleRow,
                 dx_value: str | None = None) -> dict[str, Any]:
    """Target cells that are currently blank, and what to put in them.

    Anything already holding a value -- including a junk ``Billed``
    placeholder or an existing ``DX`` -- is left out of the result and so
    never overwritten.
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
    fills = {
        column: proposed[column]
        for column in FILLABLE_COLUMNS
        if proposed[column] not in (None, "") and is_blank(current[column])
    }

    if FILL_DX_ON_EXISTING and dx_value and is_blank(row.dx):
        fills[COL_DX] = dx_value

    return fills


def as_number(value: Any) -> float | None:
    """A cell's numeric value, or None when it cannot be read as one.

    Formula strings such as ``=23.52+18.92`` are stored, not evaluated, by
    openpyxl, so they come back as text and cannot be compared numerically.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text or text.startswith("="):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def amounts_differ(recorded: Any, incoming: float) -> bool:
    """True when a recorded cell disagrees with the remit's amount."""
    number = as_number(recorded)
    if number is None:
        return False  # unreadable: handled separately, never treated as a diff
    return abs(number - incoming) > AMOUNT_TOLERANCE


def _money(value: Any) -> str:
    number = as_number(value)
    return f"${number:,.2f}" if number is not None else f"{value}"


def _append_check_eft(existing: Any, incoming: str) -> str:
    """Append this remit's check number, keeping the earlier ones visible."""
    seen: list[str] = []
    for part in str(existing or "").split(CHECK_EFT_JOINER.strip()):
        cleaned = part.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    for part in incoming.split(CHECK_EFT_JOINER.strip()):
        cleaned = part.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return CHECK_EFT_JOINER.join(seen)


def reconcile_recorded_row(visit: Visit, row: ScheduleRow, processed_on: str,
                           check_eft_display: str) -> tuple[str, dict, dict, list[str], list[str]]:
    """Compare a remit visit against a row that already has a Payment.

    Returns ``(action, updates, previous, notes, review_reasons)``. The visit
    and the row are already known to be the same patient and service date, so
    this only decides whether the amounts agree, whether the remit is newer,
    and what an accepted update would overwrite.
    """
    notes: list[str] = []
    reasons: list[str] = []

    recorded_payment = as_number(row.payment)
    if recorded_payment is None:
        # A formula or free text: we cannot tell whether it agrees, and
        # overwriting it would destroy the formula. Always ask.
        reasons.append(
            f"Recorded Payment {row.payment!r} is not a plain number, so it "
            f"cannot be compared with the remit's {_money(visit.payment)} - "
            "check this row by hand"
        )
        return ACTION_REVIEW, {}, {}, notes, reasons

    payment_differs = amounts_differ(row.payment, visit.payment)
    copay_differs = amounts_differ(row.copay, visit.copay)

    if not payment_differs and not copay_differs:
        return ACTION_SKIP, {}, {}, notes, reasons

    # --- The amounts disagree, so this is a restatement --------------------
    recorded_billed = parse_loose_date(row.billed)
    incoming_billed = visit.billed_date

    if (recorded_billed is not None and incoming_billed is not None
            and incoming_billed < recorded_billed):
        reasons.append(
            f"Out-of-order remit: this remittance is dated "
            f"{incoming_billed.strftime(DATE_FMT)} but the row already records "
            f"{recorded_billed.strftime(DATE_FMT)}. The newer value "
            f"({_money(row.payment)}) is kept; nothing is changed."
        )
        return ACTION_REVIEW, {}, {}, notes, reasons

    if REPROCESS_POLICY == REPROCESS_FLAG_ONLY:
        reasons.append(
            f"Adjusted EOB: recorded {_money(row.payment)}, remit reports "
            f"{_money(visit.payment)}. Policy is flag-only, so nothing is changed."
        )
        return ACTION_REVIEW, {}, {}, notes, reasons

    if REPROCESS_POLICY == REPROCESS_SUM:
        new_payment = round((recorded_payment or 0.0) + visit.payment, 2)
        new_copay = round((as_number(row.copay) or 0.0) + visit.copay, 2)
        notes.append(
            f"Policy '{REPROCESS_SUM}': added to the recorded amount "
            f"({_money(row.payment)} + {_money(visit.payment)})"
        )
    else:
        new_payment = visit.payment
        new_copay = visit.copay

    updates: dict[str, Any] = {
        COL_PAYMENT: new_payment,
        COL_COPAY: new_copay,
        COL_PROCESSED_ON: processed_on,
    }
    previous: dict[str, Any] = {
        COL_PAYMENT: row.payment,
        COL_COPAY: row.copay,
        COL_PROCESSED_ON: None,
    }

    if visit.billed_str:
        updates[COL_BILLED] = visit.billed_str
        previous[COL_BILLED] = row.billed

    if check_eft_display:
        merged_eft = _append_check_eft(row.check_eft, check_eft_display)
        if merged_eft != str(row.check_eft or ""):
            updates[COL_CHECK_EFT] = merged_eft
            previous[COL_CHECK_EFT] = row.check_eft

    # The headline case: a zero-dollar first EOB later actually pays.
    if recorded_payment == 0 and visit.payment > 0:
        notes.append(
            f"was $0.00, now {_money(visit.payment)} (payment received)"
        )
    elif payment_differs:
        notes.append(
            f"Payment {_money(row.payment)} -> {_money(visit.payment)}"
        )
    if copay_differs:
        notes.append(f"Co-pay {_money(row.copay)} -> {_money(visit.copay)}")

    if recorded_billed is None and not is_blank(row.billed):
        notes.append(
            f"recorded Billed {row.billed!r} is not a usable date, so the "
            "remit order could not be verified"
        )

    # A changed CPT set still means the same visit -- never a second row --
    # but it is worth a human look before the amounts are restated.
    row_cpts = row.cpt_codes
    visit_cpts = set(visit.cpt_codes)
    if row_cpts and visit_cpts and row_cpts != visit_cpts:
        reasons.append(
            f"CPT set changed: sheet has {'/'.join(order_cpt_codes(row_cpts))}, "
            f"remit reports {visit.cpt_display}. The row is still treated as "
            "the same visit (no duplicate row is created)."
        )
        return ACTION_REVIEW, updates, previous, notes, reasons

    if visit.restated:
        notes.append(
            f"{visit.remit_count} remits in this upload reported this visit; "
            "the newest supplies the amounts"
        )

    return ACTION_UPDATE, updates, previous, notes, reasons


def telehealth_flag(visit: Visit, row: ScheduleRow) -> tuple[str, str]:
    """(preview message, `Ins` value to write) when the row disagrees.

    Existing rows almost always say `Medicare`; if the EOB shows the visit was
    telehealth the row is out of date. Per never-overwrite the cell is left
    alone and merely flagged, unless OVERWRITE_INS_FOR_TELEHEALTH is on.
    """
    if not visit.telehealth or is_blank(row.ins):
        return "", ""
    if _normalize_token(row.ins) == _normalize_token(visit.insurance):
        return "", ""
    message = (
        f"EOB says telehealth (POS 10 + 95) but row {row.row_num} has "
        f"Ins = {row.ins!r}. Expected {visit.insurance!r}"
    )
    if OVERWRITE_INS_FOR_TELEHEALTH:
        return message + " - will be updated", visit.insurance
    return message + " - left as-is, fix by hand", ""


def find_patient_anchor(visit: Visit,
                        rows: Sequence[ScheduleRow]) -> tuple[int | None, str | None]:
    """The last existing row belonging to this visit's patient, if any.

    A new row for a patient who is already in the schedule is inserted
    directly beneath this anchor so each person's rows stay together. Uses the
    same suffix-aware, normalised matching as everything else, so
    ``MARCHETTI, DEAN`` anchors under ``Marchetti Jr, Dean``. Rows are not
    required to be contiguous -- the *last* occurrence wins.
    """
    anchor_row: int | None = None
    anchor_patient: str | None = None
    for row in rows:
        if is_blank(row.patient):
            continue
        if name_score(visit.patient, str(row.patient)) < NAME_AUTO_MATCH_SCORE:
            continue
        if anchor_row is None or row.row_num > anchor_row:
            anchor_row = row.row_num
            anchor_patient = str(row.patient)
    return anchor_row, anchor_patient


def _dx_for(visit: Visit, dx_lookup: "DxLookup | None") -> tuple[str | None, str, str, str | None]:
    """(dx value, status, preview note, review reason) for one visit."""
    if dx_lookup is None:
        return None, DX_NO_FILE, "", None

    match = dx_lookup.find(visit.patient)

    if match.status == DX_CONFLICT:
        note = f"conflict: {', '.join(match.conflicting)}"
        return match.dx, match.status, note, None

    if match.status == DX_AMBIGUOUS:
        reason = (
            f"Ambiguous DX name match ({match.score:.0f}%): "
            f"'{visit.patient}' vs Mutual '{match.matched_name}' - DX left blank"
        )
        return None, match.status, "", reason

    return match.dx, match.status, "", None


def plan_change(visit: Visit, rows: Sequence[ScheduleRow],
                dx_lookup: "DxLookup | None" = None,
                today: date | None = None) -> Change:
    """Decide what, if anything, this visit should do to the schedule."""
    review_reasons: list[str] = []
    if not visit.known_provider:
        review_reasons.append(f"Unknown provider NPI {visit.npi} - Comment left blank")
    # An extra EFT that belongs to a discarded OA-18 duplicate is already
    # explained by `duplicate_note`, so it is not a reason to stop and ask.
    unexplained_efts = visit.check_efts - visit.ignored_duplicate_efts
    if len(unexplained_efts) > 1:
        review_reasons.append(
            "Service lines came from more than one check/EFT "
            f"({', '.join(sorted(unexplained_efts))}) - verify before applying"
        )

    dx_value, dx_status, dx_note, dx_reason = _dx_for(visit, dx_lookup)
    if dx_reason:
        review_reasons.append(dx_reason)

    # An OA-18 exact duplicate is adjudicated at $0 because the original
    # already paid. If every occurrence was a duplicate, the paying remit was
    # never uploaded, so there is no real amount to record.
    if visit.duplicate_only:
        review_reasons.append(visit.duplicate_note)



    anchor_row, anchor_patient = find_patient_anchor(visit, rows)

    audit = dict(
        dx_value=dx_value,
        dx_status=dx_status,
        dx_note=dx_note,
        processed_on=(today or date.today()).strftime(DATE_FMT),
        check_eft_display=CHECK_EFT_JOINER.join(sorted(visit.check_efts)),
        duplicate_note=visit.duplicate_note,
        anchor_row=anchor_row,
        anchor_patient=anchor_patient,
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
            **audit,
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
            fills={} if top_row.payment_recorded else _fill_values(visit, top_row, dx_value),
            existing_billed=top_row.billed,
            score=candidates[0][1],
            review_reasons=review_reasons,
            accepted=False,
            **audit,
        )

    row, score = chosen

    telehealth_message, ins_update = telehealth_flag(visit, row)
    audit["telehealth_mismatch"] = telehealth_message
    audit["ins_update"] = ins_update

    if score < NAME_AUTO_MATCH_SCORE:
        review_reasons.append(
            f"Low-confidence name match ({score:.0f}%): "
            f"remittance '{visit.patient}' vs sheet '{row.patient}'"
        )

    if row.payment_recorded:
        # A Payment is already on the row. Either the remit agrees (a true
        # duplicate -> skip, untouched) or it restates the claim, which is an
        # update the user has to confirm. Either way this is the same visit,
        # so a second row is never created.
        recorded_action, updates, previous, notes, recorded_reasons = reconcile_recorded_row(
            visit, row, audit["processed_on"], audit["check_eft_display"],
        )
        review_reasons.extend(recorded_reasons)

        if recorded_action == ACTION_SKIP and not review_reasons:
            return Change(
                visit=visit,
                action=ACTION_SKIP,
                row_num=row.row_num,
                existing_billed=row.billed,
                score=score,
                review_reasons=[],
                accepted=False,
                **audit,
            )

        needs_review = recorded_action == ACTION_REVIEW or bool(review_reasons)
        return Change(
            visit=visit,
            action=ACTION_REVIEW if needs_review else ACTION_UPDATE,
            row_num=row.row_num,
            existing_billed=row.billed,
            score=score,
            review_reasons=review_reasons,
            accepted=not needs_review,
            updates=updates,
            previous=previous,
            update_notes=notes,
            **audit,
        )

    if review_reasons:
        return Change(
            visit=visit,
            action=ACTION_REVIEW,
            row_num=row.row_num,
            fills=_fill_values(visit, row, dx_value),
            existing_billed=row.billed,
            score=score,
            review_reasons=review_reasons,
            accepted=False,
            **audit,
        )

    return Change(
        visit=visit,
        action=ACTION_FILL,
        row_num=row.row_num,
        fills=_fill_values(visit, row, dx_value),
        existing_billed=row.billed,
        score=score,
        review_reasons=[],
        accepted=True,
        **audit,
    )


def build_plan(visits: Iterable[Visit], rows: Sequence[ScheduleRow],
               dx_lookup: "DxLookup | None" = None,
               today: date | None = None) -> list[Change]:
    """Plan every visit against the schedule, newest decisions last."""
    return [plan_change(visit, rows, dx_lookup, today) for visit in visits]


def find_legacy_duplicate_rows(rows: Sequence[ScheduleRow]) -> list[tuple[ScheduleRow, ScheduleRow]]:
    """Rows a previous run appended that now duplicate a suffix-name row.

    Before generational suffixes were stripped for matching, a sheet row like
    ``Marchetti Jr, Dean`` could fail to match ``MARCHETTI, DEAN`` and get a duplicate
    appended in the app's old all-caps style. Those pairs are reported so they
    can be cleaned up by hand -- nothing is deleted automatically.
    """
    pairs: list[tuple[ScheduleRow, ScheduleRow]] = []
    for row in rows:
        if is_blank(row.patient):
            continue
        name = str(row.patient)
        # The app now writes Title Case, so an all-caps name is a legacy append.
        if name != name.upper() or not any(ch.isalpha() for ch in name):
            continue
        for other in rows:
            if other.row_num == row.row_num or is_blank(other.patient):
                continue
            other_name = str(other.patient)
            if other_name == other_name.upper():
                continue
            if (row.data_date is not None
                    and row.data_date == other.data_date
                    and name_score(name, other_name) >= NAME_AUTO_MATCH_SCORE):
                pairs.append((row, other))
                break
    return pairs


def summarize(changes: Sequence[Change]) -> dict[str, int]:
    """Counts behind the preview summary line."""
    return {
        "visits": len(changes),
        "fill": sum(1 for c in changes if c.action == ACTION_FILL),
        "new": sum(1 for c in changes if c.action == ACTION_NEW),
        "skip": sum(1 for c in changes if c.action == ACTION_SKIP),
        "update": sum(1 for c in changes if c.action == ACTION_UPDATE),
        "review": sum(1 for c in changes if c.action == ACTION_REVIEW),
    }


def new_row_values(visit: Visit, dx: str | None = None,
                   processed_on: str = "", check_eft: str = "") -> dict[str, Any]:
    """Column values for an appended row.

    The patient name is written in the sheet's Title Case style rather than
    Medicare's uppercase. ``DX`` stays blank unless the optional Mutual
    workbook supplied one.
    """
    from .config import (
        COL_CHECK_EFT,
        COL_CPT,
        COL_DATA,
        COL_INS,
        COL_PATIENT,
        COL_PROCESSED_ON,
    )

    values = {
        COL_PATIENT: to_title_name(visit.patient),
        # `Medicare`, or `POS 10(95)` when the EOB says telehealth.
        COL_INS: visit.insurance,
        COL_DATA: visit.data_str,
        COL_BILLED: visit.billed_str,
        COL_PAYMENT: visit.payment,
        COL_COPAY: visit.copay,
        COL_COMMENT: visit.doctor,
        COL_CPT: "/".join(order_cpt_codes(visit.cpt_codes)),
    }
    if dx:
        values[COL_DX] = dx
    if processed_on:
        values[COL_PROCESSED_ON] = processed_on
    if check_eft:
        values[COL_CHECK_EFT] = check_eft
    return values
