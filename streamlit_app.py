"""Medicare remittance -> patient schedule updater.

Upload the schedule workbook and one or more Noridian remittance PDFs, review
every proposed change, then download an updated copy of the workbook. Nothing
is ever written without an explicit confirmation in this UI.
"""

from __future__ import annotations

import hashlib
import io
from collections import Counter

import pandas as pd
import streamlit as st

from remit.config import (
    ACTION_FILL,
    EMPLOYEE_APPEND_SHEETS,
    EMPLOYEE_SHEETS,
    ACTION_NEW,
    ACTION_REVIEW,
    ACTION_SKIP,
    ACTION_UPDATE,
    COL_BILLED,
    COL_CHECK_EFT,
    COL_COPAY,
    COL_PAYMENT,
    NPI_TO_DOCTOR,
    SHEET_NAME,
)
from remit.excel_updater import (
    ScheduleError,
    annotate_placement,
    build_updated_workbook,
    download_filename,
    get_schedule_sheet,
    load_schedule_workbook,
    read_schedule_rows,
    resolve_columns,
)
from remit.matching import build_plan, find_legacy_duplicate_rows, summarize
from remit.employees import (
    EmployeesError,
    build_updated_employees,
    employees_download_filename,
    load_employees_workbook,
    plan_employee_changes,
    read_sheets,
    schedule_visits_from_plan,
    summarize_employees,
)
from remit.mutual import load_dx_lookup
from remit.pdf_parser import parse_remittances

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

st.set_page_config(page_title="Remittance → Schedule Updater", page_icon="🧾", layout="wide")


def safe_error(message: str, error: Exception) -> None:
    """Show a generic reason without echoing row data back into the page.

    Exception text from openpyxl/pdfplumber can carry cell values, so only the
    exception *type* is surfaced -- never its message.
    """
    st.error(f"{message} ({type(error).__name__}). "
             "Check the file is the expected workbook/PDF and try again.")


def clear_session() -> None:
    """Wipe every uploaded file and result from this session."""
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def upload_signature(schedule_bytes: bytes, pdf_payloads: list[tuple[bytes, str]],
                     mutual_bytes: bytes | None,
                     employees_bytes: bytes | None = None) -> str:
    """Stable id for one set of uploads, so parsing only reruns when they change."""
    digest = hashlib.sha256(schedule_bytes)
    for payload, name in pdf_payloads:
        digest.update(name.encode("utf-8"))
        digest.update(payload)
    if mutual_bytes:
        digest.update(b"mutual")
        digest.update(mutual_bytes)
    if employees_bytes:
        digest.update(b"employees")
        digest.update(employees_bytes)
    return digest.hexdigest()


def plan_to_frame(changes) -> pd.DataFrame:
    """The preview table: one row per proposed change, accept flag first."""
    return pd.DataFrame(
        [
            {
                "Accept": change.accepted,
                "Action": change.action_label,
                "Patient": change.visit.patient,
                "Data": change.visit.data_str,
                "Comment (provider)": change.visit.doctor or "—",
                "CPT": change.visit.cpt_display,
                "Payment": change.visit.payment,
                "Co-pay": change.visit.copay,
                "Proposed Billed": change.visit.billed_str,
                "Existing Billed": "" if change.existing_billed is None else str(change.existing_billed),
                "DX (from Mutual)": change.dx_display,
                "Payment old → new": change.change_display(COL_PAYMENT) or "—",
                "Co-pay old → new": change.change_display(COL_COPAY) or "—",
                "Billed old → new": change.change_display(COL_BILLED) or "—",
                "Check/EFT old → new": change.change_display(COL_CHECK_EFT) or "—",
                "Why": change.update_summary or change.duplicate_note or "—",
                "Placement": change.placement_display or "—",
                "Matched row": change.row_num or "—",
                "Match %": "—" if change.score is None else f"{change.score:.0f}",
                "Will write": ", ".join(change.fills) if change.fills else "—",
                "Processed On": change.processed_on,
                "Remit Check/EFT #": change.check_eft_display,
            }
            for change in changes
        ]
    )


def render_summary(counts: dict[str, int]) -> None:
    st.markdown(
        f"**{counts['visits']} visits parsed** · "
        f"**{counts['fill']}** to fill · "
        f"**{counts['new']}** new rows · "
        f"**{counts['update']}** updated (adjusted EOB) · "
        f"**{counts['skip']}** skipped (already recorded) · "
        f"**{counts['review']}** need review"
    )
    columns = st.columns(6)
    for column, (label, key) in zip(
        columns,
        [("Visits parsed", "visits"), ("To fill", "fill"), ("New rows", "new"),
         ("Updated", "update"), ("Skipped", "skip"), ("Need review", "review")],
    ):
        column.metric(label, counts[key])


def render_review_items(changes) -> None:
    review = [c for c in changes if c.needs_review]
    if not review:
        return
    st.warning(f"{len(review)} item(s) need review — these are unchecked by default.")
    with st.container(border=True):
        for change in review:
            st.markdown(
                f"**{change.visit.patient}** · {change.visit.data_str} · "
                f"{change.visit.cpt_display} · ${change.visit.payment:,.2f}"
            )
            for reason in change.review_reasons:
                st.markdown(f"- {reason}")


def render_legacy_duplicates(rows) -> None:
    """Flag old all-caps appends that now match a suffix name in the sheet."""
    pairs = find_legacy_duplicate_rows(rows)
    if not pairs:
        return
    st.warning(
        f"{len(pairs)} row(s) look like duplicates a previous run appended "
        "before generational suffixes were matched. Nothing is deleted "
        "automatically — review and remove them by hand."
    )
    with st.expander("Possible leftover duplicate rows", expanded=False):
        for duplicate, original in pairs:
            st.markdown(
                f"- Row **{duplicate.row_num}** `{duplicate.patient}` duplicates "
                f"row **{original.row_num}** `{original.patient}` "
                f"on {duplicate.data}"
            )


def render_employee_preview(changes) -> None:
    """Per-provider table of what the employees workbook would receive."""
    if not changes:
        return

    counts = summarize_employees(changes)
    st.subheader("Employees file (AMSMC_employees.xlsx)")
    st.markdown(
        f"**{counts['fill']}** row(s) to fill · **{counts['append']}** to append · "
        f"**{counts['mismatch']}** practitioner mismatch · "
        f"**{counts['unassigned']}** unassigned · "
        f"**{counts['no_match']}** with no Schedule match"
    )

    for sheet in EMPLOYEE_SHEETS:
        rows = [c for c in changes if c.sheet == sheet]
        if not rows:
            continue
        note = " — fill-only, never appended to" if sheet not in EMPLOYEE_APPEND_SHEETS else ""
        with st.expander(f"{sheet}{note} ({len(rows)} row(s))", expanded=False):
            st.dataframe(
                pd.DataFrame([
                    {
                        "Action": c.action,
                        "Patient": c.patient,
                        "Date of Session": c.session_date,
                        "Insurance": c.insurance or "—",
                        "Co-pay by EOB": c.copay if c.copay is not None else "—",
                        "Payment": c.payment if c.payment is not None else "—",
                        "→ column": c.payment_column or "—",
                        "Row": c.row_num or "(new)",
                        "Note": c.note or "—",
                    }
                    for c in rows
                ]),
                hide_index=True, use_container_width=True,
            )

    unplaced = [c for c in changes if not c.sheet]
    if unplaced:
        st.warning(
            f"{len(unplaced)} visit(s) are for patients who appear in no provider "
            "tab. They are listed rather than guessed at — place them by hand."
        )
        with st.expander("Unassigned visits", expanded=False):
            st.dataframe(
                pd.DataFrame([
                    {"Patient": c.patient, "Date of Session": c.session_date,
                     "Insurance": c.insurance, "Payment": c.payment}
                    for c in unplaced
                ]),
                hide_index=True, use_container_width=True,
            )


def render_telehealth_flags(changes) -> None:
    """Existing rows whose `Ins` disagrees with the EOB's place of service."""
    flagged = [c for c in changes if c.telehealth_mismatch]
    if not flagged:
        return
    st.info(
        f"{len(flagged)} matched row(s) look like telehealth but the sheet says "
        "otherwise. Per never-overwrite the `Ins` cell is left alone."
    )
    with st.expander("Telehealth / Ins mismatches", expanded=False):
        for change in flagged:
            st.markdown(
                f"- **{change.visit.patient}** · {change.visit.data_str} — "
                f"{change.telehealth_mismatch}"
            )


def warn_on_duplicate_checks(documents) -> None:
    """A check number appearing twice in one batch usually means a re-upload."""
    counts = Counter(doc.check_eft for doc in documents if doc.check_eft)
    for check, count in counts.items():
        if count > 1:
            names = ", ".join(d.filename for d in documents if d.check_eft == check)
            st.warning(
                f"CHECK/EFT #{check} appears in {count} uploaded files ({names}). "
                "These look like the same remittance re-uploaded. Amounts are "
                "not summed — the newest remit wins — but remove the duplicate "
                "unless this is intentional."
            )


# --- Sidebar ---------------------------------------------------------------

with st.sidebar:
    st.header("How this works")
    st.markdown(
        f"""
1. Upload `List_of_Patients_Schedule.xlsx`
2. Upload one or more remittance PDFs
3. Optionally upload `List_of_Patients_Mutual.xlsx` for DX codes
4. Review every proposed change
5. Confirm and download the updated copy

Only the **{SHEET_NAME}** sheet is touched. Blank cells are the only cells
ever written — with one exception you always confirm first: an
**Updated (adjusted EOB)** row, where a later remit restates a payment that is
already recorded. The app stamps its own `Processed On` and
`Remit Check/EFT #` columns on rows it creates, fills or restates.
"""
    )
    st.subheader("Provider NPI → Comment")
    st.table(pd.DataFrame(
        [{"NPI": npi, "Comment": doctor} for npi, doctor in NPI_TO_DOCTOR.items()]
    ))
    st.caption(
        "Files are processed in memory for this session only and are not "
        "stored server-side. This is PHI — close the tab when you are done."
    )


# --- Uploads ---------------------------------------------------------------

st.title("🧾 Medicare Remittance → Schedule Updater")

banner, reset = st.columns([5, 1])
with banner:
    st.caption(
        "🔒 Files are processed **in memory for this session only** — nothing is "
        "written to the server, logged, or shared. Close the tab or use "
        "*Clear all data* when you are done. This handles PHI."
    )
with reset:
    if st.button("🧹 Clear all data", help="Wipe uploads and results from this session"):
        clear_session()
        st.rerun()
st.caption(
    "Parses Noridian remittance advice PDFs and proposes payment updates to the "
    f"`{SHEET_NAME}` sheet. Nothing is written until you confirm."
)

left, middle, right, far_right = st.columns(4)
with left:
    schedule_upload = st.file_uploader(
        "1 · Patient schedule (.xlsx)", type=["xlsx"], accept_multiple_files=False
    )
with middle:
    pdf_uploads = st.file_uploader(
        "2 · Medicare remittance PDFs", type=["pdf"], accept_multiple_files=True
    )
with right:
    mutual_upload = st.file_uploader(
        "3 · DX reference (optional)", type=["xlsx"], accept_multiple_files=False,
        help="List_of_Patients_Mutual.xlsx — sheet 'Active'. Supplies the DX "
             "column. Without it, DX is left blank as before.",
    )
with far_right:
    employees_upload = st.file_uploader(
        "4 · Employees file (optional)", type=["xlsx"], accept_multiple_files=False,
        help="AMSMC_employees.xlsx — the Ana / Marcia / Oxana tabs. Filled from "
             "the schedule and offered as a second download.",
    )

if not schedule_upload or not pdf_uploads:
    st.info("Upload the schedule workbook and at least one remittance PDF to begin.")
    st.stop()

schedule_bytes = schedule_upload.getvalue()
pdf_payloads = [(upload.getvalue(), upload.name) for upload in pdf_uploads]
mutual_bytes = mutual_upload.getvalue() if mutual_upload else None
employees_bytes = employees_upload.getvalue() if employees_upload else None
signature = upload_signature(schedule_bytes, pdf_payloads, mutual_bytes, employees_bytes)


# --- Parse (only when the uploads change) ----------------------------------

if st.session_state.get("signature") != signature:
    try:
        workbook = load_schedule_workbook(schedule_bytes)
        worksheet = get_schedule_sheet(workbook)
        columns = resolve_columns(worksheet)
        rows = read_schedule_rows(worksheet, columns)
    except ScheduleError as error:
        st.error(str(error))
        st.stop()
    except Exception as error:  # noqa: BLE001
        safe_error("Could not read the workbook", error)
        st.stop()

    with st.spinner("Parsing remittance PDFs…"):
        try:
            documents, visits = parse_remittances(
                [(io.BytesIO(payload), name) for payload, name in pdf_payloads]
            )
        except Exception as error:  # noqa: BLE001
            safe_error("Could not parse the PDFs", error)
            st.stop()

    dx_lookup = None
    if mutual_bytes:
        try:
            dx_lookup = load_dx_lookup(mutual_bytes, mutual_upload.name)
        except ScheduleError as error:
            st.error(str(error))
            st.stop()
        except Exception as error:  # noqa: BLE001
            safe_error("Could not read the DX reference workbook", error)
            st.stop()

    plan = build_plan(visits, rows, dx_lookup)
    # Flag any anchor that would be unsafe to insert under, so the preview can
    # say the row will be appended instead. apply_changes re-checks this
    # independently, so correctness never depends on this annotation.
    annotate_placement(worksheet, columns, plan)

    employee_sheets = None
    employee_changes: list = []
    if employees_bytes:
        try:
            employees_workbook = load_employees_workbook(employees_bytes)
            employee_sheets = read_sheets(employees_workbook)
            employee_changes = plan_employee_changes(
                employee_sheets, schedule_visits_from_plan(rows, plan)
            )
        except EmployeesError as error:
            st.error(str(error))
            st.stop()
        except Exception as error:  # noqa: BLE001
            safe_error("Could not read the employees workbook", error)
            st.stop()

    st.session_state.update(
        signature=signature,
        documents=documents,
        plan=plan,
        employee_changes=employee_changes,
        schedule_row_count=len(rows),
        schedule_rows=rows,
        dx_entry_count=len(dx_lookup) if dx_lookup else 0,
        dx_conflicts=dx_lookup.conflicts if dx_lookup else {},
        generated=None,
        employees_generated=None,
    )

documents = st.session_state["documents"]
plan = st.session_state["plan"]
employee_changes = st.session_state.get("employee_changes") or []

dx_entry_count = st.session_state.get("dx_entry_count", 0)
st.success(
    f"Read {st.session_state['schedule_row_count']} schedule rows from "
    f"`{SHEET_NAME}` and {sum(d.claim_count for d in documents)} claims "
    f"from {len(documents)} PDF(s)."
    + (f" DX reference loaded: {dx_entry_count} patient(s)." if dx_entry_count
       else " No DX reference uploaded — DX will be left blank.")
)

dx_conflicts = st.session_state.get("dx_conflicts") or {}
if dx_conflicts:
    st.warning(
        f"{len(dx_conflicts)} patient(s) appear more than once in the DX "
        "reference with different codes. The first is used; check these:"
    )
    with st.expander("DX conflicts in the reference file", expanded=False):
        for key, values in dx_conflicts.items():
            st.markdown(f"- `{key.replace('|', ', ')}` → {', '.join(values)}")

with st.expander("Parsed remittance files", expanded=False):
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "File": doc.filename,
                    "Billed date (header DATE:)": doc.billed_date.strftime("%m/%d/%Y")
                    if doc.billed_date else "—",
                    "CHECK/EFT #": doc.check_eft or "—",
                    "Claims": doc.claim_count,
                    "Service lines": len(doc.service_lines),
                    "Total PROV-PD": f"${doc.total_prov_pd:,.2f}",
                }
                for doc in documents
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )

warn_on_duplicate_checks(documents)


# --- Preview ---------------------------------------------------------------

st.header("Preview proposed changes")
render_summary(summarize(plan))
render_review_items(plan)
render_telehealth_flags(plan)
render_employee_preview(employee_changes)
render_legacy_duplicates(st.session_state.get("schedule_rows") or [])

actionable = [c for c in plan if c.action != ACTION_SKIP]
show_skipped = st.checkbox(
    f"Also show the {sum(1 for c in plan if c.action == ACTION_SKIP)} skipped "
    "(already-paid) visits",
    value=False,
)
visible = plan if show_skipped else actionable

if not visible:
    st.info("Nothing to apply — every parsed visit is already recorded in the schedule.")
    st.stop()

st.caption(
    "Untick any row you do not want applied. **Existing Billed** is shown next to "
    "the proposed value: where a placeholder like `2/32/26` is already present it "
    "is left in place, so fix those by hand if needed. **DX (from Mutual)** shows "
    "what would be written into a blank DX cell; `(not found)` means the patient "
    "is not in the reference file and DX stays blank. **Placement** shows where a "
    "new row will go: grouped under that patient's existing rows, or appended at "
    "the bottom when the patient is not in the schedule. The **old → new** "
    "columns appear on `Updated (adjusted EOB)` rows, where a later remit "
    "restates an amount that is already recorded — the only case where "
    "confirming overwrites a populated cell."
)

preview = plan_to_frame(visible)
edited = st.data_editor(
    preview,
    hide_index=True,
    use_container_width=True,
    disabled=[column for column in preview.columns if column != "Accept"],
    column_config={
        "Accept": st.column_config.CheckboxColumn("Accept", help="Apply this change"),
        "Payment": st.column_config.NumberColumn("Payment", format="$%.2f"),
        "Co-pay": st.column_config.NumberColumn("Co-pay", format="$%.2f"),
    },
    key=f"editor_{signature}",
)

for change, accepted in zip(visible, edited["Accept"]):
    change.accepted = bool(accepted) and change.action != ACTION_SKIP

accepted_updates = sum(
    1 for c in plan if c.accepted and c.effective_action == ACTION_UPDATE
)
accepted_fills = sum(
    1 for c in plan if c.accepted and c.effective_action == ACTION_FILL
)
accepted_new = sum(
    1 for c in plan if c.accepted and c.effective_action == ACTION_NEW
)
accepted_grouped = sum(
    1 for c in plan
    if c.accepted and c.effective_action == ACTION_NEW and c.inserts_under_patient
)


# --- Confirm ---------------------------------------------------------------

st.header("Confirm")
st.markdown(
    f"Ready to fill **{accepted_fills}** existing row(s), restate "
    f"**{accepted_updates}** adjusted-EOB row(s), and add "
    f"**{accepted_new}** new row(s) "
    f"({accepted_grouped} grouped under an existing patient, "
    f"{accepted_new - accepted_grouped} appended at the bottom)."
)

if st.button("✅ Confirm & generate file", type="primary", disabled=not (accepted_fills or accepted_new or accepted_updates)):
    with st.spinner("Applying changes…"):
        # Reviewed items are applied only when explicitly ticked; their
        # effective action is derived, so the plan itself is never mutated.
        buffer, stats = build_updated_workbook(
            schedule_bytes, [c for c in plan if c.accepted]
        )
    st.session_state["generated"] = (buffer.getvalue(), stats)

    if employees_bytes and employee_changes:
        with st.spinner("Updating the employees workbook…"):
            try:
                emp_buffer, emp_stats = build_updated_employees(
                    employees_bytes, employee_changes
                )
                st.session_state["employees_generated"] = (
                    emp_buffer.getvalue(), emp_stats
                )
            except Exception as error:  # noqa: BLE001
                safe_error("Could not update the employees workbook", error)

generated = st.session_state.get("generated")
if generated:
    payload, stats = generated
    st.success(
        f"Restated {stats['updated_cells']} cell(s) on {stats['updated_rows']} "
        f"adjusted-EOB row(s). "
        f"Wrote {stats['filled_cells']} cell(s) across {stats['filled_rows']} existing "
        f"row(s), inserted {stats['inserted_rows']} new row(s) under their patient "
        f"and appended {stats['appended_rows']} at the bottom. "
        "Every other sheet, formula and format is preserved."
    )
    if stats["skipped_non_blank"]:
        st.info(
            f"{stats['skipped_non_blank']} cell(s) were left alone because they "
            "already held a value."
        )
    st.download_button(
        "⬇️ Download updated schedule",
        data=payload,
        file_name=download_filename(schedule_upload.name),
        mime=XLSX_MIME,
        type="primary",
    )

employees_generated = st.session_state.get("employees_generated")
if employees_generated:
    emp_payload, emp_stats = employees_generated
    st.success(
        f"Employees workbook: filled {emp_stats['filled_cells']} cell(s) across "
        f"{emp_stats['filled_rows']} row(s) and appended "
        f"{emp_stats['appended_rows']} row(s). Every sheet and format preserved."
    )
    if emp_stats["skipped_non_blank"]:
        st.info(
            f"{emp_stats['skipped_non_blank']} employees cell(s) were left alone "
            "because they already held a value."
        )
    st.download_button(
        "⬇️ Download updated employees file",
        data=emp_payload,
        file_name=employees_download_filename(employees_upload.name),
        mime=XLSX_MIME,
    )
