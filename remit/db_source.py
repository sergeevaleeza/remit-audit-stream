"""Read the structured 835 database (`remittance_data.sqlite`) into visits.

This replaces PDF parsing as the payment source. The extractor
(`remits-extractor`) has already flattened every 835 into one row per service
line in `master_remittance_records`, so the fragile part of the old pipeline --
locating fields by shape in fixed-width text -- is gone.

Everything *downstream* is unchanged and deliberately reused: rows are turned
into the same `ServiceLine` / `RemitDocument` objects the parser produced, then
handed to `aggregate_visits`, so per-visit aggregation, the OA-18 duplicate
rule, multi-EFT reconciliation and the EOB-date cutoff all apply exactly as
before.

Two things about the real database shape drive the code below:

* **Amount columns are TEXT.** They are cast, and an unparseable value is
  treated as absent rather than silently becoming zero.
* **Each payer is aggregated separately.** `aggregate_visits` keys a visit on
  (patient, service date, NPI) with no payer in the key, so a Medicare line
  and the HPSM crossover line that settles it would otherwise merge into one
  visit and the later HPSM payment would replace the Medicare payment. Keeping
  the payers apart is what lets the crossover be recognised as a *second*
  payer's contribution to the same visit.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import (
    DB_ROW_LEVEL,
    DB_TABLE,
    PAYER_HPSM,
    PAYER_MEDICARE,
    PAYERS_IN_SCOPE,
)
from .pdf_parser import RemitDocument, ServiceLine, Visit, aggregate_visits


class RemitDbError(RuntimeError):
    """The uploaded file cannot be used as the 835 database."""


#: Columns read from `master_remittance_records`. Named explicitly so a schema
#: change surfaces as a clear error instead of a silently missing value.
_COLUMNS = (
    "payment_trace_number",
    "check_number",
    "payment_date",
    "payer_name",
    "claim_filing_indicator_code",
    "rendering_provider_npi",
    "patient_first_name",
    "patient_last_name",
    "service_from_date",
    "procedure_code",
    "modifier_1",
    "modifier_2",
    "modifier_3",
    "modifier_4",
    "location_number",
    "service_payment_amount",
    "PR_adjustment_amount",
    "patient_responsibility_amount",
    "CARC_codes",
    "patient_control_number",
    "payer_claim_control_number",
)

#: Columns the app needs but which some extractor versions do not emit. Missing
#: ones are read as NULL rather than failing the whole run.
_OPTIONAL_COLUMNS = frozenset({"patient_responsibility_amount"})


def _to_amount(value: Any) -> float | None:
    """A TEXT money column as a number, or None when it is not one.

    None means "the 835 did not state this", which is not the same as `0.00`
    and must never be quietly turned into it.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    """An ISO `YYYY-MM-DD` column as a date. The extractor normalises these."""
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _patient_name(last: Any, first: Any) -> str:
    """`Last, First` in the remit's own uppercase, as the parser yielded it.

    Title-casing happens later, in the one place that writes names, so the DB
    path and the PDF path present identically to everything downstream.
    """
    last = " ".join(str(last or "").split())
    first = " ".join(str(first or "").split())
    if last and first:
        return f"{last}, {first}"
    return last or first


def _codes(value: Any) -> tuple[str, ...]:
    """`"45,23"` -> `("45", "23")`. Bare reason numbers, no group prefix."""
    return tuple(
        part.strip() for part in str(value or "").replace(";", ",").split(",")
        if part.strip()
    )


def _modifiers(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row[key]).strip()
        for key in ("modifier_1", "modifier_2", "modifier_3", "modifier_4")
        if str(row.get(key) or "").strip()
    )


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open the database read-only, so a run can never alter the source."""
    try:
        connection = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro",
                                     uri=True)
    except sqlite3.Error as error:  # pragma: no cover - platform dependent
        raise RemitDbError(str(type(error).__name__)) from error
    connection.row_factory = sqlite3.Row
    return connection


def available_columns(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({DB_TABLE})").fetchall()
    return {row[1] for row in rows}


def read_service_rows(connection: sqlite3.Connection,
                      payers: Sequence[str] = PAYERS_IN_SCOPE
                      ) -> list[dict[str, Any]]:
    """Every in-scope service line, as plain dicts.

    Payers outside `payers` -- Carelon today -- are filtered out in SQL rather
    than read and discarded, so an out-of-scope plan cannot reach the rest of
    the app by accident.
    """
    present = available_columns(connection)
    if not present:
        raise RemitDbError(
            f"The database has no `{DB_TABLE}` table. Is this the file "
            "`remits-extractor` produced?"
        )

    missing = [c for c in _COLUMNS
               if c not in present and c not in _OPTIONAL_COLUMNS]
    if missing:
        raise RemitDbError(
            f"`{DB_TABLE}` is missing expected column(s): {', '.join(missing)}."
        )

    selected = [c if c in present else f"NULL AS {c}" for c in _COLUMNS]
    placeholders = ", ".join("?" for _ in payers)
    sql = (
        f"SELECT {', '.join(selected)} FROM {DB_TABLE} "
        f"WHERE row_level = ? AND payer_name IN ({placeholders})"
    )
    try:
        rows = connection.execute(sql, (DB_ROW_LEVEL, *payers)).fetchall()
    except sqlite3.Error as error:
        raise RemitDbError(f"Could not query `{DB_TABLE}` "
                           f"({type(error).__name__}).") from error
    return [dict(row) for row in rows]


@dataclass
class DbLoadReport:
    """What a load did, for the preview. Carries no patient information."""

    total_rows: int = 0
    used_rows: int = 0
    skipped_no_date: int = 0
    skipped_no_npi: int = 0
    payer_rows: dict[str, int] = None  # type: ignore[assignment]
    remits: int = 0

    def __post_init__(self) -> None:
        if self.payer_rows is None:
            self.payer_rows = {}


def _service_line(row: dict[str, Any]) -> ServiceLine | None:
    """One database row as a `ServiceLine`, or None when unusable."""
    service_date = _to_date(row["service_from_date"])
    if service_date is None:
        return None

    npi = str(row["rendering_provider_npi"] or "").strip()
    if not npi:
        return None

    trace = str(row["payment_trace_number"] or "").strip()
    check = str(row["check_number"] or "").strip()

    # HPSM leaves `location_number` empty; Noridian fills it with the real POS
    # (02/10/11). An empty value stays None so `_is_telehealth` falls back to
    # the modifier rather than assuming an office visit.
    pos = str(row["location_number"] or "").strip() or None

    return ServiceLine(
        patient=_patient_name(row["patient_last_name"], row["patient_first_name"]),
        npi=npi,
        service_date=service_date,
        proc=str(row["procedure_code"] or "").strip(),
        coins=_to_amount(row["PR_adjustment_amount"]) or 0.0,
        prov_pd=_to_amount(row["service_payment_amount"]) or 0.0,
        source_file=str(row["payer_name"] or ""),
        check_eft=trace or check or None,
        pos=pos,
        modifiers=_modifiers(row),
        codes=_codes(row["CARC_codes"]),
        payer=str(row["payer_name"] or "").strip(),
    )


def build_documents(rows: Iterable[dict[str, Any]]
                    ) -> tuple[list[RemitDocument], DbLoadReport]:
    """Group service lines into one `RemitDocument` per remittance.

    The remittance is identified by `payment_trace_number` -- the real DB has
    150 distinct trace numbers against only 9 distinct `check_number`s, so the
    trace number is what actually separates one remit from another.
    """
    report = DbLoadReport()
    buckets: dict[tuple[str, str], list[ServiceLine]] = {}
    dates: dict[tuple[str, str], date | None] = {}
    checks: dict[tuple[str, str], str | None] = {}

    for row in rows:
        report.total_rows += 1
        payer = str(row.get("payer_name") or "").strip()
        report.payer_rows[payer] = report.payer_rows.get(payer, 0) + 1

        line = _service_line(row)
        if line is None:
            if _to_date(row.get("service_from_date")) is None:
                report.skipped_no_date += 1
            else:
                report.skipped_no_npi += 1
            continue

        report.used_rows += 1
        # Payer is part of the remit key as well as the trace number: the two
        # plans' numbering is independent, so a shared trace number must not
        # fuse a Medicare remit and an HPSM one into a single document.
        key = (payer, str(line.check_eft or ""))
        buckets.setdefault(key, []).append(line)
        dates.setdefault(key, _to_date(row.get("payment_date")))
        checks.setdefault(key, str(row.get("check_number") or "").strip() or None)

    documents: list[RemitDocument] = []
    for (payer, trace), lines in sorted(buckets.items()):
        documents.append(RemitDocument(
            filename=f"{payer} · {trace or 'no trace number'}",
            billed_date=dates[(payer, trace)],
            check_eft=trace or checks[(payer, trace)],
            claim_count=len({line.patient for line in lines}),
            service_lines=lines,
        ))
    report.remits = len(documents)
    return documents, report


def aggregate_by_payer(documents: Sequence[RemitDocument]) -> list[Visit]:
    """Reconcile each payer's remits separately, then combine.

    Separately, because `aggregate_visits` keys a visit on (patient, service
    date, NPI). A Medicare line and the HPSM crossover line settling the same
    session share all three, so aggregating them together would treat the
    second payer as a restatement of the first and lose the Medicare payment.
    """
    by_payer: dict[str, list[RemitDocument]] = {}
    for document in documents:
        payer = next((line.payer for line in document.service_lines), "")
        by_payer.setdefault(payer, []).append(document)

    visits: list[Visit] = []
    for payer in sorted(by_payer):
        visits.extend(aggregate_visits(by_payer[payer]))
    return sorted(visits, key=lambda v: (v.payer, v.patient, v.service_date, v.npi))


def load_visits(path: str | Path,
                payers: Sequence[str] = PAYERS_IN_SCOPE,
                ) -> tuple[list[RemitDocument], list[Visit], DbLoadReport]:
    """`(documents, visits, report)` for one 835 database. Read-only."""
    connection = open_readonly(path)
    try:
        rows = read_service_rows(connection, payers)
    finally:
        connection.close()

    documents, report = build_documents(rows)
    return documents, aggregate_by_payer(documents), report


def load_visits_from_bytes(data: bytes, payers: Sequence[str] = PAYERS_IN_SCOPE
                           ) -> tuple[list[RemitDocument], list[Visit], DbLoadReport]:
    """Same, for an uploaded file.

    sqlite needs a real path, so the upload is written to a private temporary
    file and deleted immediately afterwards -- it is never stored alongside the
    app, and nothing is written back into it.
    """
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    try:
        handle.write(data)
        handle.close()
        return load_visits(handle.name, payers)
    finally:
        try:
            Path(handle.name).unlink()
        except OSError:  # pragma: no cover - best effort cleanup
            pass


def medicare_visits(visits: Iterable[Visit]) -> list[Visit]:
    return [v for v in visits if v.payer == PAYER_MEDICARE]


def hpsm_visits(visits: Iterable[Visit]) -> list[Visit]:
    return [v for v in visits if v.payer == PAYER_HPSM]
