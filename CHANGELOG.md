# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

[1.1.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.1.0
[1.0.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.0.0
