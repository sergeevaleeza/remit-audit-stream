# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.6.0] — 2026-09-10

Marcia's tab now appends as well as fills, and every provider tab gains
`Processed On` / `Remit Check/EFT #` audit columns. 381 tests pass (was 344).
The Mutual workbook remains a read-only DX source and is never modified.

### Changed — Marcia fills *and* appends

Reverses the earlier "Marcia never appends" rule. All three tabs now behave
identically:

- **Fill** existing rows by patient name (suffix-aware) + `Date of Session`.
- **Append** a patient's further sessions — those whose `Date of Session` is
  not yet in the tab — to the **end** of that tab, filling the mapped columns
  and copying styling from the last existing data row.
- The `Comment` cross-check is unchanged: a schedule `Comment` naming a
  *different provider tab* still flags *practitioner mismatch — needs review*
  rather than placing the row silently. A `Comment` naming a physician
  (`Dr. …`) is still not a conflict.
- Dedup is still by patient + `Date of Session`, so re-runs add nothing.

`EMPLOYEE_APPEND_SHEETS` is now all three tabs.

### Added — optional catch-all for no-tab patients

`MARCIA_CATCH_ALL_UNASSIGNED`, **default `False`**. Off, a visit for a patient
in no provider tab is listed *Unassigned — needs manual placement* exactly as
before. On, it is appended to the end of Marcia's tab as
*Append (auto-placed, unassigned) — review*, which is never accepted by
default.

> **Warning, also carried as a comment in `remit/employees.py`.** Most visits
> bill under the supervising physician's NPI (incident-to), so a large share of
> "no tab" patients are that physician's **own direct patients**, who
> legitimately belong in no associate's tab. Turning this on sweeps all of them
> into Marcia's tab, turning it into an overflow bucket. Keep it `False` unless
> that behaviour is explicitly wanted.

### Added — `Processed On` and `Remit Check/EFT #` on every provider tab

- Created at the first empty columns after that tab's existing headers, on
  **that tab's own header row** — Ana and Marcia row 1, Oxana row 2 — inheriting
  the header row's formatting. Resolved by header text, case-insensitively and
  with internal whitespace collapsed, like every other employee-sheet column.
- `Processed On` is the run date in `DATE_FMT` (`MM/DD/YYYY`).
- `Remit Check/EFT #` is the **paying** remit's number. Per the OA-18 rule an
  exact-duplicate occurrence supplies no amounts, so it supplies no audit
  number either; several *authoritative* remits are joined with `; `.
- Written only for rows the app fills or appends in that run. Rows it does not
  touch are left completely alone, and the headers are created only on tabs the
  run actually writes to.
- Both values appear in the per-provider preview table.

### Fixed

- `Visit.authoritative_eft` previously took the first of the visit's *merged*
  check numbers, which could be a discarded duplicate's. The authoritative EFTs
  are now captured before the audit-trail union, so a duplicate's number can
  never be reported as the payer of record. It is now a derived property over
  the new `Visit.authoritative_efts` set.

### Tests

- `tests/test_employee_audit.py` (37 tests): Marcia filling and appending,
  bottom placement, inherited styling, no double-add on re-run, the `Comment`
  cross-check still applying; the catch-all off and on, always review-flagged
  and never written unless accepted; audit headers on all three tabs at the
  right header row with the right formatting, reused not duplicated, created
  only on written tabs, stamped on filled/appended rows and blank elsewhere;
  and the EFT being the paying remit's, never an `OA-18` duplicate's.
- The synthetic roster gained a second `Whitfield, Harold` session (04/02) so
  Marcia has a genuine append to make. Several tests that hardcoded row numbers
  or counts now derive them, so the fixture can grow without churn.

---

## [1.5.2] — 2026-09-10

An `OA-18` exact-duplicate remittance could overwrite a real payment with
`$0.00`. Reason-18 occurrences are now non-authoritative. 344 tests pass
(was 312). **This is the mechanism behind the all-zero visits.**

### Fixed — a duplicate EOB no longer zeroes a real payment

Medicare reason code **18** — group `OA` or `CO` — means *"exact duplicate
claim/service"*. A duplicate is adjudicated at `$0` because the original
already paid. Under plain `replace_with_latest`, a duplicate remittance
arriving later therefore replaced the genuine payment with zero.

Reproduced on the real files before changing anything: the paying remit
(`DATE: 07/30`) and the duplicate (`DATE: 08/27`, nine `OA-18` lines for the
same dates) processed together collapsed nine visits from `716.56` to `104.27`,
eight of them zeroed. After the fix those nine visits reconcile to
`0, 0, 90.94, 104.27 × 6` — **`716.56`** / COINS **`182.80`** — in either
upload order.

**Rule:** an occurrence carrying reason 18 is *not authoritative*. It can never
contribute, overwrite, zero or downgrade a value.

- Amounts come from the **newest non-duplicate occurrence** by remit `DATE:`.
  This is order-independent — a later duplicate never beats an earlier real
  payment.
- The audit trail still lists **every** EFT that mentioned the visit; only the
  duplicate's *amounts* are discarded, not the fact of it.
- The preview explains it — *"OA-18 duplicate from EFT x ignored; kept payment
  from EFT y"* — and does **not** mark the row *Needs review*, because a clean
  authoritative payment exists. It is simply applied.
- If a visit is seen **only** as a duplicate (the paying remit was never
  uploaded), it is flagged *Duplicate only (OA-18) — original paying remit not
  uploaded; verify* rather than written as a real `$0.00`.
- For non-duplicate occurrences, `replace_with_latest` is unchanged.

**Discrimination is by reason code, never by amount.** A `$0.00` line carrying
`CO-45` because the deductible consumed the whole allowed amount has no code 18
and behaves exactly as before — still a genuine `$0.00`, still not a duplicate.

### Added — the parser captures adjustment reason codes

- `ServiceLine.codes` collects every `XX-nnn` group/reason token from the
  service line **and** from its continuation lines (the indented `REM:` /
  `CO-###` rows beneath it). Codes are folded upward onto the service line;
  continuation lines are still never treated as service lines, so `COINS` and
  `PROV-PD` are unaffected — they remain the 4th and 6th of the six dollar
  amounts on the service line itself.
- `ServiceLine.is_duplicate` / `Visit.is_duplicate`, matching on the numeric
  reason `18` regardless of group prefix, so `OA-18` and `CO-18` both count
  while `CO-118` and `OA-181` do not.
- `Visit.duplicate_only`, `Visit.ignored_duplicate_efts`,
  `Visit.authoritative_eft` and `Visit.duplicate_note`, plus
  `Change.duplicate_note` surfaced in the preview's *Why* column.
- `aggregate_visits` now gathers all occurrences of a visit and reconciles them
  in one pass (`_reconcile`) rather than merging pairwise, which is what makes
  the outcome independent of upload order.

### Changed

- The multi-EFT *Needs review* flag no longer fires when the extra EFT belongs
  to a discarded duplicate — that case is now explained by `duplicate_note`
  instead of stopping the user.

### Added — fixtures and tests

- `tests/fixtures/RemitDoc-0000000003.PDF`, a **synthetic** later remittance
  built by `make_duplicate_fixture.py`: nine `OA-18` duplicates of the paying
  fixture's visits, one genuinely new paid visit, and one visit present only as
  a duplicate. Real remittances stay gitignored.
- `tests/test_duplicate_reason_18.py` (32 tests): the real payment surviving a
  later duplicate, upload-order independence, both EFTs on the audit trail, the
  duplicate-only flag and its *Needs review*, a `CO-45` deductible `$0.00`
  staying a genuine zero, `CO-18` treated the same as `OA-18`, near-miss codes
  (`CO-118`, `OA-181`) rejected, continuation-line code folding, and
  `replace_with_latest` still applying between two non-duplicate occurrences.

---

## [1.5.1] — 2026-09-10

Hardens service-line amount extraction against non-zero deductibles and
amount-carrying continuation lines, and locks both with regression cover.
312 tests pass (was 281).

### Investigated — the reported all-zero symptom did not reproduce

The report was that a patient's nine `90834` visits under one provider were all
written with `Payment = 0` and `Co-pay = 0`, with a non-zero `DEDUCT` and/or
bare `CO-253` continuation lines as the suspected trigger.

Running the current parser over the real remittance that contains those visits
returns the correct figures:

| Service date | Payment | Co-pay | Why |
|---|---|---|---|
| 01/15, 02/10 | `0.00` | `0.00` | genuine — `ALLOWED` fully consumed by `DEDUCT` |
| 03/11 | `90.94` | `23.20` | partial deductible |
| 03/31 … 06/16 (×6) | `104.27` | `26.60` | no deductible remaining |

Claim totals `716.56` / `182.80`, whole-file `PROV-PD` `1332.11` — matching that
remit's own `TOTALS … PROV PD AMT` line exactly, with a `DEDUCT` total of
`283.00` confirming the deductible path really was exercised.

Two further checks:

- A scan of every remittance on hand found **1156 service lines, all with
  exactly six dollar amounts** — no continuation line is ever being folded into
  a service line by text extraction — alongside 521 bare `CO-### <amt>`
  continuations, all correctly skipped.
- Every version of `pdf_parser.py` in this repository's history has used the
  10-digit-NPI skip rule and `_COINS_INDEX = 3`. The `REM:` prefix appears only
  in a docstring, never in logic, so the described defect has never been present
  here. The most likely explanation for what was observed is a **stale
  deployment** (Streamlit keeps imported modules in `sys.modules`, so pushing
  changes under `remit/` requires rebooting the Cloud app).

### Fixed — `PROV-PD` was read as "the last amount", not the sixth

While confirming the above, one genuine latent defect surfaced, and it produces
exactly the reported symptom if it is ever reached: `prov_pd` was taken as
`amounts[-1]` rather than by position. The canonical layout has six amounts —
`BILLED, ALLOWED, DEDUCT, COINS, RC-AMT, PROV-PD` — so if a seventh ever trailed
the line (the shape that would occur if a bare `CO-253 2.13` continuation were
merged into it by a future pdfplumber/layout change), `PROV-PD` would silently
become the sequestration figure:

```
… 200.00 133.00 0.00 26.60 CO-45 67.00 104.27 CO-253 2.13
before -> prov_pd = 2.13      after -> prov_pd = 104.27
```

`PROV-PD` is now read at index 5 and `COINS` at index 3, both positional. No
current input changes behaviour — all 1156 real service lines have exactly six
amounts — but the failure mode is now unreachable.

### Added

- `ServiceLine.deduct`, so the deductible is carried as its own field and can
  never be conflated with `COINS` or `PROV-PD`. Named index constants
  (`_BILLED_INDEX` … `_PROV_PD_INDEX`) replace the bare literals.
- `tests/fixtures/RemitDoc-0000000002.PDF`, a **synthetic** second remittance
  built by `make_deductible_fixture.py` reproducing the reported shapes: two
  lines where `ALLOWED` is fully consumed by `DEDUCT` (`PROV-PD` a genuine
  `0.00`), one partial deductible, six fully-paid lines, **bare `CO-253 <amt>`
  continuations with no `REM:` prefix**, and one patient billed under two
  performing-provider NPIs. Its `TOTALS` line reads `1332.11`, and the per-visit
  figures match the real file's exactly. The real PDF is PHI and is not
  committed.
- `tests/test_deductible_regression.py` (31 tests): the nine per-visit values,
  the `716.56` / `182.80` claim totals, the `1332.11` whole-file total, the
  physician visits for the same patient, `DEDUCT` never conflated with `COINS`
  or `PROV-PD`, bare and `REM:`-prefixed continuations both skipped, the skip
  rule being keyed on "does not start with a 10-digit NPI" rather than on the
  `REM:` prefix, continuation amounts never reaching a visit, and a guard that
  the first fixture's zero-deductible path still totals unchanged.

### Note on already-written zeros

If an earlier run did write `0` into schedule rows for visits that were in fact
paid, the current app corrects them rather than skipping: a recorded amount that
disagrees with the remit is classified **`Updated (adjusted EOB)`** (added in
1.4.0) and shown as `was $0.00, now $104.27 (payment received)` for confirmation.

---

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

[1.6.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.6.0
[1.5.2]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.5.2
[1.5.1]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.5.1
[1.5.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.5.0
[1.4.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.4.0
[1.3.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.3.0
[1.2.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.2.0
[1.1.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.1.0
[1.0.0]: https://github.com/your-org/remit-audit-stream/releases/tag/v1.0.0
