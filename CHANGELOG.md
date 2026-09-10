# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.5.0] — 2026-09-10

Adds the `AMSMC_employees.xlsx` workbook as a fourth upload and a second
download, derives `Ins` from the remit's place of service, and hardens the
running app against PHI leakage. 281 tests pass (was 241).

### Added — place of service -> `Ins`

- The parser now captures each service line's **POS** (the 2-digit token right
  after the 6-digit service date) and its **modifiers** (anything between PROC
  and the money columns), aggregated to the visit.
- `pdf_parser.insurance_label(visit)` replaces the old "Ins is always
  Medicare" constant: **POS `10` + modifier `95` -> `POS 10(95)`**, everything
  else -> `Medicare`. New schedule rows take this value.
- Existing rows already say `Medicare`; per never-overwrite they are left alone
  and **flagged in the preview** when the EOB says the visit was telehealth.
  `OVERWRITE_INS_FOR_TELEHEALTH = False` in `remit/config.py` flips that to an
  actual cell update.

### Added — the employees workbook

- **Fourth upload** (`AMSMC_employees.xlsx`, optional) and a **second
  download**, `AMSMC_employees_updated_YYYY-MM-DD.xlsx` — complete and
  standalone, every sheet and format preserved.
- New `remit/employees.py`. **The header row is not in the same place on every
  sheet** (Ana and Marcia row 1, Oxana row 2), so it is located by scanning the
  first rows for the expected labels; labels are matched case-insensitively
  with internal whitespace collapsed, because the real file contains
  `Co-payment   Old` and `Paid by Ins toAna`. Oxana's patient column is headed
  `Patient`, the others `Patient Name`.
- Per-provider payment column, as confirmed:
  `{"Ana": "Paid by Ins toAna", "Oxana": "Paid by Insurance", "Marcia": "Paid by Insurance"}`.
- Mapped columns: `Patient Name`/`Patient`, `Date of Session`, `Insurance`,
  `Co-pay by EOB`, and the provider payment column. Everything else is left
  blank, and a non-blank cell is never overwritten.
- **Routing is by patient name + Date of Session, never by `Comment`** — the
  tabs are staff-curated and are the source of truth. Existing rows are filled;
  further sessions of a patient already in a tab are appended (**Ana and Oxana
  only — Marcia's tab is fill-only**); a patient in no tab is listed as
  *unassigned — needs manual placement* rather than guessed
  (`FALLBACK_TO_COMMENT_FOR_NEW = False`).
- Appended rows copy the last data row's font, fill, border, alignment and
  number format. Dedup is by patient + Date of Session, so re-running adds
  nothing.
- Per-provider preview section, plus panels for telehealth/`Ins` mismatches and
  unassigned visits.
- `tests/test_employees.py` (40 tests) and a **synthetic**
  `tests/fixtures/AMSMC_employees_sample.xlsx` reproducing all three layouts.

### Changed — `Comment` cross-check is narrower than first specified

The request called for flagging a matched row whenever the schedule `Comment`
"names a different practitioner". Implemented literally that flags **every**
row: `Comment` is derived from the remit's performing-provider NPI, so it holds
a *physician's* name (`Dr. Levinson`, `Dr. A`) and almost never one of the three
employee tab names.

So a `Comment` is treated as a usable cross-check **only when it actually names
a provider tab**. `Oxana` on a row in Ana's tab is a mismatch; `Dr. A` is not a
conflict at all, because it carries no signal about which tab is correct. Blank
`Comment` fills normally, as specified.

### Added — app-wide HIPAA safeguards

- All uploads and downloads stay in `BytesIO`; nothing is written to the server
  filesystem or `/tmp`.
- `safe_error()` shows the exception **type** and a generic instruction, never
  the message — openpyxl and pdfplumber quote cell values in their errors.
- `.streamlit/config.toml`: `[client] showErrorDetails = false` so tracebacks
  are not rendered into the page, `[logger] level = "error"`, and
  `[browser] gatherUsageStats = false`.
- No PHI in module globals or `st.cache_resource`; no `persist="disk"` caching.
  State lives in per-session `st.session_state`.
- A **Clear all data** button wipes the session, and a banner states that files
  are processed in-session and not stored server-side.
- Download filenames contain only the workbook name and the date.
- `.gitignore` now excludes `AMSMC_employees*.xlsx` (PHI), with an explicit
  exception for the synthetic fixture.
- New [`HIPAA.md`](HIPAA.md) documenting all of the above, the required
  viewer-allowlist step, and the caveat that **Streamlit Community Cloud and
  GitHub are not BAA-covered**, so real PHI on them is a gap that code cannot
  close.

---

## [1.4.0] — 2026-09-10

Reconciles a second (or later) EOB for a visit that is already recorded.
Previously such a visit was classified *Skip (already paid)* and a real payment
was lost — the classic case being a first remittance paying `$0.00` and a later
one that actually pays. 241 tests pass (was 197).

### Added

- **`Updated (adjusted EOB)`**, a fourth action alongside *Fill*, *Skip* and
  *New row*. When a remit visit matches a recorded row on patient + service
  date but reports different amounts, it is now reconciled instead of skipped:
  - `Payment` and `Co-pay` are set to the later remit's values.
  - `Billed` is set to the later remit's header `DATE:`.
  - `Remit Check/EFT #` **appends** the new number to the existing value
    (joined with `; `), so the payment history stays visible on the row.
  - `Processed On` is stamped with today.
  - Applied only on confirm, and only for a change the user left accepted.
- **`REPROCESS_POLICY`** in `remit/config.py`, defaulting to
  `replace_with_latest`: a later Medicare remit *restates* the claim, so the
  recorded amount is replaced, **never summed**. `sum` and `flag_only` ship as
  alternatives but are not the default.
- Preview columns showing **old → new** for `Payment`, `Co-pay`, `Billed` and
  `Remit Check/EFT #`, plus a `Why` column carrying a plain-language note such
  as `was $0.00, now $130.46 (payment received)`. The summary line and metric
  row gained an *Updated* count.
- `remit.matching.reconcile_recorded_row()`, the single place that decides
  skip vs. update vs. review for a row that already has a `Payment`, plus
  `as_number()` / `amounts_differ()` helpers and `Change.updates`,
  `Change.previous`, `Change.update_notes`, `Change.change_display()`.
- `apply_changes` returns `updated_rows` and `updated_cells`.
- `tests/test_adjusted_eob.py` (44 tests) covering the headline `0.00 → paid`
  case, identical re-reports, out-of-order remits, unreadable recorded values,
  CPT changes, multi-remit reconciliation and the no-duplicate-row guarantee.

### Changed

- **Amounts are no longer summed across remittances.** `aggregate_visits` now
  aggregates service lines *within* each remittance, then reconciles across
  remittances by taking the newest (by header `DATE:`). Previously two remits
  covering one visit had their amounts added together, which under
  `replace_with_latest` would have been wrong. Summing within a single
  remittance — the E/M line plus its psychotherapy add-on — is unchanged.
- `Visit` gained `remit_count`, `superseded_payments` and `restated`; a visit
  fed by several remits notes in the preview how many contributed.
- Several contributing remits no longer force *Needs review* on their own.
  That flag existed as a safety net for the old summing behaviour; the case now
  has defined semantics and is reported as a note instead.
- `ScheduleRow` reads back the `Processed On` / `Remit Check/EFT #` columns so
  a restatement can append to the check history rather than replace it.
- Sidebar and duplicate-remit warning text updated: the never-overwrite rule
  now has one stated, always-confirmed exception.
- `test_zero_payment_counts_as_recorded` became
  `test_zero_payment_is_never_treated_as_fillable`, and
  `test_zero_payment_survives_the_write` became
  `test_zero_payment_survives_when_the_remit_agrees`. Both encoded the old
  behaviour where a `0.00` row was skipped outright. The underlying guarantee
  is unchanged and still tested: `0.00` is never a *blank*, so it is never
  *filled* — it is now *restated* when a later remit disagrees.

### Safety rules

- **Order matters.** An update happens only when the incoming remit is newer
  than the recorded `Billed`. An older remit arriving out of order never
  downgrades a newer value — *Needs review*, nothing changed. When the recorded
  `Billed` is an unparseable placeholder (`2/32/26`) the order cannot be
  verified, so the update is proposed with that stated in the preview.
- **Never a second row.** A restatement always targets the matched row. A
  changed CPT set is still the same visit: flagged *Needs review*, and if
  accepted it updates that row rather than appending a duplicate.
- **Unreadable values are never clobbered.** A recorded `Payment` that is a
  formula or free text cannot be compared, so the row is flagged for review
  rather than overwritten — which also keeps formula cells intact.
- **Idempotent.** Once applied, the row agrees with the remit and reclassifies
  as *Skip — already recorded*; re-running writes nothing.

### Not implemented — employees-file propagation

The request also asked to propagate confirmed updates to an employees workbook
(the Ana/Oxana/Marcia tabs, matching on patient + `Date of Session` and writing
`Paid by Insurance` / `Paid by Ins toAna`). **That feature does not exist in
this repository** — there is no employees upload, sheet handling, or any
reference to those tabs or columns anywhere in the code or git history, so
there was nothing to extend. It was left unbuilt rather than invented against
guessed sheet names and a guessed column layout.

The groundwork is in place: every confirmed restatement carries its old and new
`Payment` / `Co-pay` on `Change.updates` and `Change.previous`, so wiring a
second workbook to those values is additive once its real format is known.

---

## [1.3.0] — 2026-09-08

New rows are grouped under their patient instead of always being appended.
Only *where* new rows are written changed — fills, dedup/idempotency,
never-overwrite, the `DX` / `Processed On` / `Remit Check/EFT #` logic and
whole-workbook preservation are all unchanged. 197 tests pass (was 174).

### Changed

- **A new visit for a patient already in the schedule is inserted directly
  below that patient's last existing row**, so each person's rows stay
  together. Patients absent from the schedule are still appended at the bottom.
  *Fill existing row* cases are unaffected — only genuinely new rows move.
- Placement uses the same suffix-aware, normalised name matching as everything
  else, so a new `MARCHETTI, DEAN` visit groups under `Marchetti Jr, Dean`.
  Non-contiguous patient rows anchor on the **last** occurrence, and several
  new visits for one patient insert as a single block in `Data` order.
- `apply_changes` now returns `inserted_rows` and `new_rows` alongside the
  existing `appended_rows`, which now counts only bottom appends.

### Added

- `find_patient_anchor()` in `remit/matching.py`, plus `anchor_row`,
  `anchor_patient` and `placement_fallback` on `Change`, with a
  `placement_display` property for the preview
  (`under existing "Name"` vs `appended (patient not in schedule)`).
- `insertion_blocked_reason()` and `annotate_placement()` in
  `remit/excel_updater.py` — structural safety checks described below.
- A **Placement** column in the preview table, and a confirm line that splits
  the count into rows grouped under a patient vs appended at the bottom.
- `tests/test_row_placement.py` (23 tests) covering anchor selection, grouped
  insertion, date-ordered blocks, the suffix case, bottom appends, styling
  inheritance, merged-cell and totals-row fallbacks, row-count integrity, and
  idempotency over an already-grouped sheet.
- `sheet_rows()` and `find_row()` test helpers in `tests/conftest.py`: rows
  move now, so tests locate them by identity rather than a fixed index.

### Safety notes on mid-sheet insertion

`openpyxl.insert_rows` is unforgiving, so the implementation is deliberate:

- **All placements are computed against the original layout first**, then
  applied. Fills are written before any insertion, using original row numbers.
- **Inserts are applied bottom-up** (highest anchor first) via a single
  `insert_rows(anchor + 1, count)` per anchor, so inserting lower down cannot
  shift an anchor still to be processed. Bottom appends run last, against the
  final layout.
- `insert_rows` leaves new cells **unstyled**, so each inserted cell copies the
  anchor row's font, fill, border, alignment and number format.
- `insert_rows` does **not** adjust merged ranges, formulas, conditional
  formatting, data validations or charts. Rather than risk corrupting a sheet,
  the app refuses to insert when doing so would **split a merged region** or
  place data **above a totals/summary row**; that patient's new rows are
  appended at the bottom instead and the preview says why. The check runs
  inside `apply_changes`, so correctness never depends on the preview
  annotation.

---

## [1.2.0] — 2026-09-08

Four incremental changes to matching and output. All existing behaviour is
preserved: parse → preview → confirm → download, never-overwrite, dedup /
idempotency, and whole-workbook preservation. Test count went from 90 to 174.

### Added

- **Generational suffixes are stripped for name comparison.** `Marchetti Jr, Dean`
  in the schedule now matches `MARCHETTI, DEAN` in the remit and is *filled*
  instead of duplicated. `JR`, `SR`, `II`, `III`, `IV` and `V` are removed
  (case-insensitive, with or without a trailing period) from the surname
  before comparison — including before the Slavic/Armenian harmonisation, so a
  suffix can no longer stop those rules firing. The stored name is never
  rewritten, and an exact parsed-date match is still required, so stripping a
  suffix cannot cause a cross-person match. Applies everywhere names are
  compared, including the new Mutual lookup.
- **`to_title_name()`** — names on *newly appended* rows are written in the
  sheet's style (`Marchetti, Dean`, not `MARCHETTI, DEAN`). Handles hyphens
  (`SMITH-JONES` → `Smith-Jones`), apostrophes (`O'BRIEN` → `O'Brien`), `Mc`
  (`MCDONALD` → `McDonald`), single-letter middle initials
  (`CURRAN, THOMAS M` → `Curran, Thomas M`) and suffixes (`JR` → `Jr`, roman
  numerals left uppercase). Existing names are never touched.
- **Optional third upload: `List_of_Patients_Mutual.xlsx`** (`remit/mutual.py`).
  Reads only the `Active` sheet, which has **no header row** — column A is the
  patient and column B the DX, read positionally from row 1. Column E
  (attending doctor) is deliberately not read and no doctor aliasing is
  implemented. The DX fills blank `DX` cells on new rows and on existing rows
  the app is already filling (`FILL_DX_ON_EXISTING`, default `True`). A
  non-blank DX is never overwritten. A patient missing from the file leaves DX
  blank; a patient listed twice with different codes uses the first and flags
  the conflict; a low-confidence name match is flagged *Needs review* and
  leaves DX blank. Without the upload, behaviour is exactly as before.
- **Audit columns `Processed On` (L) and `Remit Check/EFT #` (M)**, with
  headers created in row 2 on first use, inheriting the header row's
  formatting. Every row the app creates or fills this run is stamped with the
  run date (`MM/DD/YYYY`, reusing `DATE_FMT`) and the originating
  `CHECK/EFT #` (joined with `; ` if several remits feed one row). These are
  the app's own columns, so they are refreshed rather than only filled when
  blank — rows the app skips or never touches are left unchanged.
- **Leftover-duplicate detection.** `find_legacy_duplicate_rows()` reports
  all-caps rows a previous run appended that now match a suffix-bearing row on
  the same date, surfaced as a preview warning. Nothing is deleted
  automatically.
- Synthetic `tests/fixtures/List_of_Patients_Mutual.xlsx` (fictional names and
  diagnoses), plus a suffix-bearing patient (`Bystritskaya Jr, Anna`) added to
  the synthetic roster to cover the suffix path end to end.
- 84 new tests across `tests/test_name_handling.py`, `tests/test_mutual_dx.py`
  and `tests/test_audit_columns.py`.

### Changed

- The preview table gained `DX (from Mutual)`, `Processed On` and
  `Remit Check/EFT #` columns; the summary line reports whether a DX reference
  was loaded, and DX conflicts in the reference file are listed separately.
- `build_plan()` / `plan_change()` take an optional `dx_lookup` and `today`;
  `new_row_values()` takes optional `dx`, `processed_on` and `check_eft`.
- `README.md` — new features, the Mutual upload and its column mapping, the DX
  fill rules, the audit columns, and limitations covering `Mac` surnames and
  leftover duplicates.

### Notes

- **DX is not filled on skipped rows.** The brief asked for blank `DX` on
  "existing rows" to be filled, but Change 4 also requires that rows the app
  skips (already paid) are left unchanged, and the idempotency guarantee
  depends on it. DX filling is therefore scoped to rows the app is already
  touching — fills and new rows.
- **The reported `Marchetti Jr` symptom had a second cause.** Suffix names already
  scored 100% via an incidental prefix match, so the duplicate seen in
  practice came from the *date* not being present on the matched row rather
  than from the name. Explicit suffix stripping is still the right fix: it
  makes the match intentional rather than accidental, and it repairs a real
  failure where a suffix blocked surname harmonisation (a Slavic feminine
  surname with `Jr` scored 88% — below the 92% auto-match threshold — and is
  now 100%). That case is covered by a regression test.

---

## [1.1.0] — 2026-09-08

De-identified the repository before it is pushed to GitHub. The initial build
(below) had used a real, unredacted clinic PDF as a test fixture and had
seeded real patient names, real provider NPIs, and one real diagnosis code
into code, docs, and tests. This release replaces all of that with fully
synthetic data and adds guardrails against it recurring.

### Removed

- **The real remittance PDF fixture.** The `tests/fixtures/` PDF had been an
  unredacted copy of an actual Medicare remittance: real patient names, MBIs,
  account numbers, claim control numbers, dates of service, and payment
  amounts. Deleted outright, including its filename (which encoded the real
  document's own check/EFT-adjacent reference number).
- **A real diagnosis code tied to a real, named patient**, which had been
  copied into the synthetic-looking schedule fixture (`tests/make_fixture.py`)
  along with that patient's real name, real dates of service, and real payment
  amounts. Replaced with a fictional DX string for a fictional patient.
- Every real patient surname, real provider NPI, real doctor name, and the
  real `CHECK/EFT #`/filename number, from `remit/*.py`, `tests/*.py`,
  `README.md`, and this changelog.

### Added

- **`tests/fixtures/synthetic_remit_data.py`** — the single source of truth
  for a fictional patient roster (10 patients covering every parser/matching
  edge case: the `95` telehealth modifier, single-CPT vs. E/M+add-on visits, a
  two-digit year spanning 2025/2026, one Medicare-truncated long surname, one
  trailing middle initial, and a patient split across two claim blocks), a
  synthetic provider NPI, and MBI/ACNT/ICN/check-EFT placeholders that spell
  out their own fictionality (`9FAKE...`, `9999900000001`, `900000001`).
- **`tests/fixtures/make_remit_fixture.py`** — renders that roster into an
  actual Noridian-shaped PDF (real selectable Courier text via `reportlab`, so
  pdfplumber extracts it exactly as it would a real remit) at
  `tests/fixtures/RemitDoc-0000000001.PDF`. Dollar amounts reuse the real,
  public Medicare fee-schedule figures for these CPT codes (not identifiers on
  their own) so parsing math is realistic, but every name/date/ID is invented.
- **`tests/fixtures/README.md`** stating that every fixture is synthetic.
- **`npi_map.example.json`** — a shipped, synthetic example of the NPI→doctor
  config format. `remit/config.py` now resolves `NPI_TO_DOCTOR` from (in
  order) a gitignored `npi_map.local.json`, a Streamlit secrets
  `[npi_to_doctor]` table, or this synthetic default — so a real clinic's
  provider mapping never has to live in the repo.
- **`SECURITY.md`** — PHI handling policy: never commit real patient files,
  keep real inputs outside the repo, GitHub is not a BAA-covered environment.
- **`scripts/check_for_phi.py`** + a sample pre-commit hook — blocks a commit
  that contains an MBI-shaped string, any of the retired real NPIs, or the
  retired real clinic name, as a backstop against this recurring.
- `.gitignore` additions: `*_updated.xlsx`, `.env`, `.streamlit/secrets.toml`,
  `.ipynb_checkpoints/`, `/data/`, `/private/`, `/local/`, and
  `npi_map.local.json`.

### Changed

- `tests/make_fixture.py` rebuilt against the synthetic roster: same
  structural quirks (title row, mixed datetime/string dates, a `2/32/26`
  placeholder, a `Co-pay` formula, a `0.00` payment, a second sheet that must
  survive) but with every name, date, and DX code now fictional.
- `tests/conftest.py`, `tests/test_pdf_parser.py`, `tests/test_matching.py`,
  and `tests/test_app_and_edges.py` rewritten against the synthetic fixtures.
  All 90 tests still pass; the per-visit ground-truth table is now derived
  from `synthetic_remit_data.py` instead of hand-copied real figures.
- `remit/pdf_parser.py`, `remit/matching.py`, and `README.md` — docstrings,
  comments, and worked examples that had quoted real patient names or NPIs now
  use generic placeholders (`SMITH, JANE`, `1000000001`).

### Git history

No purge was needed. `git log --all` showed exactly one commit on this repo
(`Initial commit`, containing only `LICENSE`) had ever been made, and it had
never been pushed with any of the changes above — everything removed here was
still uncommitted working-tree content. The real `List_of_Patients_Schedule.xlsx`
and the original `RemitDoc-*.PDF` at the repo root, used only for local manual
testing during the build below, were never staged and remain gitignored.

### Verification

- `git log --all --name-only` and `git grep --all` confirm zero commits, on
  any ref, ever contained a real patient name, real MBI, the real clinic name,
  or the real check/EFT number.
- `git grep` over the current working tree for every real surname, real NPI,
  the real check/EFT number, and the real clinic/address strings returns no
  matches outside of this changelog's own description of what was removed.
- Full test suite passes (90/90) against the synthetic fixtures.

---

## [1.0.0] — 2026-09-08

Initial build: a complete Streamlit app that turns Noridian Medicare
remittance PDFs into reviewed updates to the clinic's patient schedule
workbook. Built in one session from an empty repo containing only a `LICENSE`.

### Added

**App**

- `streamlit_app.py` — upload → parse → preview → confirm → download flow.
  - Uploaders for one schedule `.xlsx` and any number of remittance PDFs.
  - Summary line: *N visits parsed · X to fill · Y new rows · Z skipped
    (already paid) · W need review*, plus a metric row.
  - `st.data_editor` preview with a per-row **Accept** checkbox and columns for
    Patient, Data, provider Comment, CPT, Payment, Co-pay, proposed Billed,
    **existing Billed**, matched row number, match %, and the exact list of
    cells that would be written.
  - A prominent **Needs review** panel listing every fuzzy match, unknown NPI
    and ambiguous multi-match, with the reason spelled out. These start
    unticked and are never auto-applied.
  - **Confirm & generate file** button; the workbook is built into an in-memory
    `BytesIO` and offered via `st.download_button`, named
    `List_of_Patients_Schedule_updated_YYYY-MM-DD.xlsx`.
  - Parsed-files expander showing each PDF's header date, `CHECK/EFT #`, claim
    count, service-line count and total PROV-PD.
  - Warning when the same `CHECK/EFT #` appears in more than one uploaded file,
    instead of silently summing a re-uploaded remittance.
  - Results are cached against a hash of the uploads, so parsing only re-runs
    when the uploaded files actually change.

**`remit/config.py`**

- Single home for the sheet name, header/data row numbers, canonical column
  names, `DATE_FMT`, the NPI→doctor table, fuzzy-match thresholds and action
  labels. Includes the group billing NPI as an explicit non-doctor constant.

**`remit/pdf_parser.py`**

- Shape-based service-line extraction that is immune to optional CPT modifiers
  shifting the columns: first token must be a 10-digit NPI, the service date is
  the 6-digit `MMDDYY` token, and COINS/PROV-PD are the 4th and last of the
  tokens matching `\d+\.\d{2}`.
- Claim-block detection via `NAME … MID`; two-digit years expanded to 2025/2026.
- Aggregation of service lines by (patient, service date, provider NPI) across
  claim blocks *and* across uploaded files.
- CPT ordering with the E/M code first, then the psychotherapy add-on.
- `REM:`/reason-code continuation lines, page headers, underline rows and the
  `TOTALS` block are all excluded by the same first-token rule.

**`remit/matching.py`**

- Name normalisation reusing the clinic calendar tool's Slavic/Armenian
  surname harmonisation, plus comma splitting, trailing middle-initial removal
  and prefix handling for Medicare-truncated names.
- `parse_loose_date` for the free-text `Data`/`Billed` columns — accepts real
  datetimes and several hand-typed string formats, and returns `None` for
  placeholders like `2/32/26` and `No Billing` so they never compare equal to a
  real date.
- `is_blank`, which treats `0`, `0.00` and formula strings as recorded values.
- Fill / Skip / New row / Needs review decisions, with disambiguation of
  multi-row matches by CPT overlap, then provider comment, then unpaid status.
- `Change.effective_action`, so accepting a reviewed item resolves to a fill or
  an append without mutating the stored plan.

**`remit/excel_updater.py`**

- Column resolution by header text in row 2 — never by hardcoded column letter.
- Reads and writes with openpyxl directly (never `pandas.to_excel`), so all
  sheets, styles and formulas survive.
- Blank-only writes, with every target cell re-checked immediately before it is
  written, and a count of any cell skipped for being non-blank.
- New rows appended after the last data row, inheriting the previous row's
  styling; `DX` deliberately left blank.
- Date-stamped download filename.

**Tests — 90 passing**

- `tests/test_pdf_parser.py` (25) — ground-truth checks against the fixture
  remittance: header billed date, several named-visit payment/co-pay/CPT
  figures, single-CPT and two-digit-year cases, modifier-shift immunity,
  noise-line rejection, and a whole-file sanity check that the parsed claim
  count and the sum of every PROV-PD match the remit's own `TOTALS` line.
- `tests/test_matching.py` (43) — idempotency, the fill path, `0.00`-is-not-blank,
  the new-row path, case/whitespace matching, the `Billed` placeholder caveat,
  cross-format date matching, workbook/formula/other-sheet preservation, and
  that declined changes are not written.
- `tests/test_app_and_edges.py` (22) — unknown NPI, ambiguity and its three
  tie-breakers, low-confidence names, workbook repair, header/sheet validation
  errors, reordered columns, and a `streamlit.testing` smoke test of the app
  script.
- `tests/make_fixture.py` — regenerates the sample workbook, deliberately
  mirroring the real file's quirks (title row, mixed datetime/string dates,
  junk date placeholders, a `Co-pay` formula, a `0.00` payment, and a second
  sheet that must survive).
- `tests/fixtures/` — the sample remittance PDF and the generated sample
  workbook used as test fixtures.

**Project files**

- `requirements.txt` (pinned for Streamlit Community Cloud),
  `requirements-dev.txt`, `.streamlit/config.toml`, and a `.gitignore` that
  excludes real patient files from the repo root while keeping the test
  fixtures tracked.
- `README.md` covering features, the PDF field mapping and NPI table, local
  run, deployment, usage, the matching and update rules, a PHI/privacy note and
  the limitations.

### Notes on the source data

Three things about the real clinic files (used locally during development,
outside the repo) shaped the implementation:

- **The production workbook does not open in openpyxl as-is.** Excel had written
  `<family val="18"/>` and `<family val="34"/>` font attributes, which openpyxl's
  schema rejects (it caps the attribute at 14), so `load_workbook` raised
  `ValueError: Max value is 14`. The app now clamps just that attribute in an
  in-memory copy of the xlsx zip and retries; every other part is copied byte
  for byte. Without this the app could not open the real file at all.
- **`Data` and `Billed` are not purely text.** In the real sheet they are a mix
  of genuine `datetime` cells and hand-typed strings. All date comparison goes
  through `parse_loose_date`, which handles both.
- **`Payment` and `Co-pay` contain formulas.** A number of cells hold strings
  like `=23.52+18.92`. These are treated as recorded values, never as blanks,
  and are preserved on write.

### Verification

- Parser validated across several hundred service lines from multiple
  remittance PDFs supplied for local testing: every line yielded exactly six
  dollar amounts, and a second provider NPI was confirmed present.
- End-to-end run against a real, multi-sheet, ~1000-row production workbook
  (locally, outside the repo): the large majority of visits were correctly
  identified as already recorded, a handful of rows were filled, a handful of
  new rows were appended, all sheets were preserved, and pre-existing formulas
  and `Billed` placeholders were left intact.
- Each proposed new row was audited to confirm the patient/date combination is
  genuinely absent from the schedule rather than a missed match.

[1.5.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.5.0
[1.4.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.4.0
[1.3.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.3.0
[1.2.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.2.0
[1.1.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.1.0
[1.0.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.0.0
