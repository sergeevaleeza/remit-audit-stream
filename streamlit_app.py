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
    ACTION_NEW,
    ACTION_REVIEW,
    ACTION_SKIP,
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
from remit.mutual import load_dx_lookup
from remit.pdf_parser import parse_remittances

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

st.set_page_config(page_title="Remittance → Schedule Updater", page_icon="🧾", layout="wide")


def upload_signature(schedule_bytes: bytes, pdf_payloads: list[tuple[bytes, str]],
                     mutual_bytes: bytes | None) -> str:
    """Stable id for one set of uploads, so parsing only reruns when they change."""
    digest = hashlib.sha256(schedule_bytes)
    for payload, name in pdf_payloads:
        digest.update(name.encode("utf-8"))
        digest.update(payload)
    if mutual_bytes:
        digest.update(b"mutual")
        digest.update(mutual_bytes)
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
        f"**{counts['skip']}** skipped (already paid) · "
        f"**{counts['review']}** need review"
    )
    columns = st.columns(5)
    for column, (label, key) in zip(
        columns,
        [("Visits parsed", "visits"), ("To fill", "fill"), ("New rows", "new"),
         ("Skipped", "skip"), ("Need review", "review")],
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


def warn_on_duplicate_checks(documents) -> None:
    """A check number appearing twice in one batch usually means a re-upload."""
    counts = Counter(doc.check_eft for doc in documents if doc.check_eft)
    for check, count in counts.items():
        if count > 1:
            names = ", ".join(d.filename for d in documents if d.check_eft == check)
            st.warning(
                f"CHECK/EFT #{check} appears in {count} uploaded files ({names}). "
                "These look like the same remittance — amounts would be summed. "
                "Remove the duplicate unless this is intentional."
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
ever written — an existing value is never overwritten. The app stamps its own
`Processed On` and `Remit Check/EFT #` columns on rows it creates or fills.
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
st.caption(
    "Parses Noridian remittance advice PDFs and proposes payment updates to the "
    f"`{SHEET_NAME}` sheet. Nothing is written until you confirm."
)

left, middle, right = st.columns(3)
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

if not schedule_upload or not pdf_uploads:
    st.info("Upload the schedule workbook and at least one remittance PDF to begin.")
    st.stop()

schedule_bytes = schedule_upload.getvalue()
pdf_payloads = [(upload.getvalue(), upload.name) for upload in pdf_uploads]
mutual_bytes = mutual_upload.getvalue() if mutual_upload else None
signature = upload_signature(schedule_bytes, pdf_payloads, mutual_bytes)


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
    except Exception as error:  # noqa: BLE001 - surfaced to the user verbatim
        st.error(f"Could not read the workbook: {error}")
        st.stop()

    with st.spinner("Parsing remittance PDFs…"):
        try:
            documents, visits = parse_remittances(
                [(io.BytesIO(payload), name) for payload, name in pdf_payloads]
            )
        except Exception as error:  # noqa: BLE001
            st.error(f"Could not parse the PDFs: {error}")
            st.stop()

    dx_lookup = None
    if mutual_bytes:
        try:
            dx_lookup = load_dx_lookup(mutual_bytes, mutual_upload.name)
        except ScheduleError as error:
            st.error(str(error))
            st.stop()
        except Exception as error:  # noqa: BLE001
            st.error(f"Could not read the DX reference workbook: {error}")
            st.stop()

    plan = build_plan(visits, rows, dx_lookup)
    # Flag any anchor that would be unsafe to insert under, so the preview can
    # say the row will be appended instead. apply_changes re-checks this
    # independently, so correctness never depends on this annotation.
    annotate_placement(worksheet, columns, plan)

    st.session_state.update(
        signature=signature,
        documents=documents,
        plan=plan,
        schedule_row_count=len(rows),
        schedule_rows=rows,
        dx_entry_count=len(dx_lookup) if dx_lookup else 0,
        dx_conflicts=dx_lookup.conflicts if dx_lookup else {},
        generated=None,
    )

documents = st.session_state["documents"]
plan = st.session_state["plan"]

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
    "the bottom when the patient is not in the schedule."
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
    f"Ready to fill **{accepted_fills}** existing row(s) and add "
    f"**{accepted_new}** new row(s) "
    f"({accepted_grouped} grouped under an existing patient, "
    f"{accepted_new - accepted_grouped} appended at the bottom)."
)

if st.button("✅ Confirm & generate file", type="primary", disabled=not (accepted_fills or accepted_new)):
    with st.spinner("Applying changes…"):
        # Reviewed items are applied only when explicitly ticked; their
        # effective action is derived, so the plan itself is never mutated.
        buffer, stats = build_updated_workbook(
            schedule_bytes, [c for c in plan if c.accepted]
        )
    st.session_state["generated"] = (buffer.getvalue(), stats)

generated = st.session_state.get("generated")
if generated:
    payload, stats = generated
    st.success(
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
        "⬇️ Download updated workbook",
        data=payload,
        file_name=download_filename(schedule_upload.name),
        mime=XLSX_MIME,
        type="primary",
    )
