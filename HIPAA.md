# HIPAA safeguards

This app processes **Protected Health Information** — patient names, dates of
service, diagnoses and payment amounts. What follows is a description of the
engineering safeguards built into it. It is **not a compliance certification**;
your practice's privacy officer signs off, not this document.

## Read this first: the hosting caveat

**Streamlit Community Cloud and GitHub are not BAA-covered.** Neither Snowflake
(which operates Streamlit Community Cloud) nor GitHub will sign a Business
Associate Agreement for a free-tier account, and under HIPAA a vendor that
stores or processes PHI on your behalf generally needs one.

So: running this app against **real** patient data on Streamlit Community Cloud
is itself a gap, no matter how carefully the code is written. The safeguards
below reduce risk; they do not close that gap. Genuinely compliant use needs
BAA-backed hosting (a HIPAA-eligible cloud account, or an internal machine) and
restricted access.

Treat the deployed URL as sensitive, and keep real files out of the repo.

## Safeguards in the code

### Data never touches disk

Uploads are read from Streamlit's in-memory buffers, every workbook is
manipulated as an `openpyxl` object, and every download is assembled into a
`BytesIO`. Nothing is written to the server filesystem or to `/tmp` — there is
no temp-file path anywhere in the request flow. When the session ends, the data
is gone.

### No PHI in logs, errors, or tracebacks

- The app never prints or logs patient names, MBIs, file contents or dataframes.
- Exception text from `openpyxl` and `pdfplumber` can quote cell values, so
  errors are surfaced through `safe_error()`, which shows the exception **type**
  and a generic instruction — never the message.
- `.streamlit/config.toml` sets `[client] showErrorDetails = false`, so an
  unhandled traceback is not rendered into the page, and `[logger] level =
  "error"` keeps request detail out of the server log.

### No telemetry, no outbound calls

`[browser] gatherUsageStats = false`. The app makes no network, API or analytics
calls at all. The only external configuration it reads is the NPI → doctor map,
which is *provider* data, not patient PHI.

### No PHI in caches or globals

Parsed visits, plans and generated workbooks live in `st.session_state`, which
is per-session and per-user. Nothing is stored in module-level globals or in
`st.cache_resource`, and no `st.cache_data(persist="disk")` is used anywhere, so
data cannot bleed between sessions or survive on the server.

### Session hygiene

A **Clear all data** button wipes every uploaded file and result from
`st.session_state` in one click, and a banner states that files are processed
in-session and not stored server-side.

### No PHI in filenames

Downloads are named from the workbook and the date only —
`List_of_Patients_Schedule_updated_YYYY-MM-DD.xlsx`,
`AMSMC_employees_updated_YYYY-MM-DD.xlsx`. A patient name never appears in a
filename.

### Repository hygiene

Real patient files are gitignored: `*.xlsx` and `*.pdf` at the repo root,
`AMSMC_employees*.xlsx` anywhere, plus `/data/`, `/private/`, `/local/` and
`*_updated.xlsx`. Everything under `tests/fixtures/` is **synthetic** — invented
patients, MBI-shaped placeholders, and a placeholder NPI map. `scripts/check_for_phi.py`
is a pre-commit heuristic that scans staged files for PHI-shaped strings.

## Required deployment steps

These are **not** handled by the code and must be done by whoever deploys it:

1. **Restrict viewers.** In Streamlit Community Cloud, set the app to private
   and add an allowlist of the specific Google accounts that need it. An app
   left public is an unauthenticated PHI disclosure.
2. **Do not share the URL** beyond authorized staff.
3. **Supply the real NPI map out of band** — `npi_map.local.json` (gitignored)
   or Streamlit secrets. Never commit it.
4. **Prefer BAA-backed hosting** for real data, per the caveat above.

HTTPS is enforced by the platform.

## What this app does not do

No audit logging of who accessed what, no user authentication of its own (it
relies entirely on the platform's viewer allowlist), no encryption at rest
beyond the platform's own, and no automatic session timeout. If your risk
assessment requires any of these, this app does not yet provide them.
