# Medicare Remittance → Patient Schedule Updater

A Streamlit app for the clinic's billing workflow. Upload the patient schedule
workbook and one or more Noridian Medicare Remittance Advice PDFs; the app
parses every payment out of the PDFs, matches each visit to the right row of the
`2026 Medicare` sheet, and shows you exactly what it proposes to change. Nothing
is written until you confirm — and when you do, you get a complete standalone
copy of the workbook to download, with every other sheet, formula and format
untouched. It is built for the person who reconciles remittances by hand and
needs the result to be auditable rather than fast.

---

## Features

- **Parse → preview → download.** Every proposed change is shown in an editable
  table before anything happens. There is no path that writes a file without an
  explicit confirmation click.
- **Multiple PDFs at once.** Visits are aggregated across every claim block and
  across every uploaded file, so a patient appearing under several ICNs still
  produces one row per visit date and provider.
- **Fuzzy patient matching.** Handles the uppercase Medicare spelling, names
  truncated by Medicare (a long surname clipped to its first 13 characters),
  trailing middle initials (`SMITH, JANE R` → `Smith, Jane`), generational
  suffixes (`Marchetti Jr, Dean` matches `MARCHETTI, DEAN`) and Slavic/Armenian
  gendered surname endings (`Ivanova` / `Ivanov`).
- **New names written in the sheet's style.** A row the app appends stores
  `Marchetti, Dean`, not Medicare's `MARCHETTI, DEAN`. Existing names are never
  rewritten.
- **New rows group under their patient.** A new visit for someone already in
  the schedule is inserted directly beneath that person's existing rows, so
  each patient stays together. Patients not in the schedule are appended at the
  bottom.
- **Optional DX reference.** Upload `List_of_Patients_Mutual.xlsx` and the app
  fills blank `DX` cells from it. Without it, `DX` is left blank as before.
- **Optional employees workbook.** Upload `AMSMC_employees.xlsx` and the app
  fills the Ana / Marcia / Oxana tabs **directly from the reconciled EOBs** —
  not from the updated schedule — and hands back a second download. Sessions
  are only *added* to a tab when the EOB's performing-provider NPI says that
  associate did the work; Marcia's tab is fill-only. Also optional — without it
  nothing changes.
- **Optional EOB-date cutoff.** Set *Ignore EOBs dated before* to skip whole
  uploaded remittances dated earlier than a date, so re-processing an
  already-archived batch cannot re-apply it. It reads the remit's header
  `DATE:`, **not** the service date — a July EOB paying a January session is
  kept or skipped as one unit. Empty by default, and inclusive of the date
  itself.
- **Telehealth is called out.** `Ins` is derived per visit from the remit's
  place of service: `POS 10(95)` for a telehealth encounter, `Medicare`
  otherwise.
- **Audit trail.** Rows the app creates or fills are stamped with
  `Processed On` and the `Remit Check/EFT #` that produced them.
- **Adjusted EOBs are reconciled, not lost.** When a later remittance restates
  a visit that is already recorded — classically a first EOB paying `$0.00` and
  a later one that actually pays — it is surfaced as
  **Updated (adjusted EOB)** with old → new values, instead of being skipped.
  Amounts are replaced by the newest remit, never summed.
- **Only fills blank cells, never overwrites.** This is the core safety
  guarantee. A cell that already holds *any* value is left exactly as it was.
  Every write is re-checked against the live cell immediately before it happens.
  The single exception is a confirmed adjusted-EOB restatement, which always
  shows you old → new first.
- **Dedup / idempotency.** Re-running the same remittance against an
  already-updated schedule is a no-op.
- **Messy-date tolerant.** `Data` and `Billed` are hand-typed free text holding
  a mix of real dates, `MM/DD/YYYY` strings and junk placeholders. Dates are
  only ever compared by parsed calendar value, never as strings.
- **Preserves the whole workbook.** openpyxl is used directly for both read and
  write, so all 24 sheets, styling and formulas survive into the download.
- **Duplicate-remit warning.** If the same `CHECK/EFT #` appears in two uploaded
  files, you get a warning. An identical re-report of a visit is recognised and
  skipped rather than re-applied.

---

## How it works

### PDF field mapping

Each service line in the remittance looks like this, with an optional `95`
telehealth modifier that shifts the columns:

```
1000000001 0309 030926 11 1.0 99213    200.00 117.58 0.00 23.52 CO-45  82.42 92.18
1000000001 0313 031326 10 1.0 99213 95 200.00 117.58 0.00 23.52 CO-45  82.42 92.18
```

Because modifiers move things around, fields are located by shape rather than by
column position: a line is a service line only if its **first token is a 10-digit
NPI**, the service date is the **6-digit `MMDDYY` token**, and the dollar amounts
are every token matching `\d+\.\d{2}` — which in order are
`[BILLED, ALLOWED, DEDUCT, COINS, RC-AMT, PROV-PD]`. `REM:` and reason-code
continuation lines, page headers and the `TOTALS` block never match that shape
and so are skipped automatically.

| Excel column | Source |
|---|---|
| `Patient` | the name between `NAME` and `MID` on the claim header |
| *(POS / modifiers)* | the 2-digit token after the service date, and any token between PROC and the money columns |
| `Ins` | `Medicare`, or `POS 10(95)` when POS is 10 **and** modifier 95 is present |
| `Data` | SERV DATE — the `MMDDYY` token → `MM/DD/YYYY` |
| `Billed` | the header `DATE:` value → `MM/DD/YYYY` |
| `Payment` | **sum of PROV-PD** across the visit's service lines *within one remit* |
| `Co-pay` | **sum of COINS** (the 4th dollar amount) across those lines |
| `Comment` | doctor resolved from the PERF-PROV NPI |
| `CPT Code` | E/M code first, then the add-on, joined with `/` (new rows only) |
| `DX` | from the optional Mutual workbook, else left blank |
| `Processed On` | today's date, on any row the app creates or fills |
| `Remit Check/EFT #` | the `CHECK/EFT #` of the remit that produced the row |
| `Co-pays Paid`, `Office` | never touched |

`PROV-PD` is already net of the CO-45 write-off and CO-253 sequestration, so it
is taken as-is. Visits aggregate all service lines sharing
**(patient, service date, provider NPI)**. That summing happens *within* a
single remittance; across remittances the newest one replaces the older values
rather than adding to them — see *Adjusted EOBs* below.

### NPI → doctor

The **PERF PROV** NPI on the service line is used — never the `NPI:` in the page
header, which is the group billing NPI.

The mapping itself is **not shipped in this repo**, since it identifies a real
clinic's providers. By default the app uses a synthetic placeholder mapping
(`remit/config.py`); to use real values, copy `npi_map.example.json` to
`npi_map.local.json` (repo root, gitignored) and fill in your real NPIs, or set
an `[npi_to_doctor]` table in Streamlit secrets when deployed. See
`npi_map.example.json` for the exact format:

| NPI | Comment |
|---|---|
| `1000000001` | Dr. A |
| `1000000002` | Dr. B |
| `1000000003` | Dr. C |
| `1000000011` | Ana |
| `1000000012` | Oxana |

An NPI outside this table leaves `Comment` blank and flags the visit for review.

The last two are associates who bill under their own NPI rather than
incident-to. That is a separate mapping from this one — see
*[The tab → NPI mapping](#the-tab--npi-mapping)* — because it decides
something different: not what to write in `Comment`, but which employee tab a
session may be **added** to.

### DX reference (`List_of_Patients_Mutual.xlsx`)

An **optional** third upload. Only the **`Active`** sheet is read, and it has
**no header row** — data starts on row 1 and columns are read positionally:

| Column | Meaning |
|---|---|
| A | Patient (`Lastname, Firstname`) |
| B | DX |
| E | attending doctor — **deliberately not read** |

Names are matched with exactly the same normalisation used against the
schedule, so suffixes, casing and Medicare truncation resolve consistently
across all three files. The DX is then written:

- **New rows** — `DX` is set from the lookup (previously always blank).
- **Existing rows the app is already filling** — a **blank** `DX` is filled
  too. Controlled by `FILL_DX_ON_EXISTING` in `remit/config.py`; set it to
  `False` to restrict DX to new rows only.
- A non-blank `DX` is never overwritten, and rows the app **skips** (already
  paid) are never touched at all.

Edge cases: a patient absent from the Mutual file leaves `DX` blank rather
than guessing; a patient listed twice with different codes uses the first and
flags the conflict in the preview; a low-confidence name match is flagged
*Needs review* and leaves `DX` blank.

### Place of service -> `Ins`

Each service line carries a **POS** code (the 2-digit token right after the
6-digit service date) and, for telehealth, a **`95`** modifier after the
procedure code. Aggregated to the visit, they decide the `Ins` value:

| Condition | `Ins` |
|---|---|
| POS `10` (patient's home) **and** modifier `95` | `POS 10(95)` |
| anything else | `Medicare` |

- **New schedule rows** get this value.
- **Existing rows** already say `Medicare`. Per never-overwrite they are left
  alone, but any row the EOB says was telehealth is **flagged in the preview**
  so it can be corrected by hand. Set `OVERWRITE_INS_FOR_TELEHEALTH = True` in
  `remit/config.py` to rewrite those specific cells instead.
- The same value is written into the employees workbook's `Insurance` column.

### Employees workbook (`AMSMC_employees.xlsx`)

An **optional** fourth upload, producing a **second download**. One tab per
practitioner, and the layout is not uniform:

| Sheet | Header row | Patient column | Payment column |
|---|---|---|---|
| Ana | 1 | `Patient Name` | **`Paid by Ins toAna`** |
| Marcia | 1 | `Patient Name` | `Paid by Insurance` |
| Oxana | **2** | **`Patient`** | `Paid by Insurance` |

Because the header row moves, it is **located by scanning the first rows for
the expected labels** rather than assumed, and labels are matched
case-insensitively with internal whitespace collapsed (the real file contains
`Co-payment   Old` and `Paid by Ins toAna`).

**The tabs are filled from the EOBs, not from the schedule.** Both outputs
are independent consumers of the same reconciled visit set, taken *after*
OA-18 / multi-EFT reconciliation — this path never reads a schedule cell. That
matters because a schedule value can lag or be hand-edited: a visit the
schedule records as `0.00` but the EOB actually paid puts the **paid** amount
in the provider's tab.

**Mapped columns**, from the matched EOB visit:

| Employee column | EOB source |
|---|---|
| `Patient Name` / `Patient` | claim `NAME`, title-cased |
| `Date of Session` | `SERV DATE` |
| `Insurance` | `Medicare` / `POS 10(95)`, from POS + modifiers |
| `Co-pay by EOB` | Σ `COINS` |
| provider payment column | Σ `PROV-PD` |
| `Remit Check/EFT #` | the **paying** remit's `CHECK/EFT #` |

Everything else is left blank, and a non-blank cell is never overwritten.

#### Which rows go where

Filling and appending are decided by two different things, because they carry
different risks. Filling a row the staff already created is safe — the tab
roster says the session is theirs. *Adding* a row is a claim about who
performed the work, and only the EOB can make it.

1. **Fill — all three tabs.** Every existing tab row that matches a reconciled
   EOB visit on patient name (suffix-aware) and parsed `Date of Session` is
   filled from that visit.
2. **Append — Ana and Oxana only, gated on the EOB's PERF PROV NPI.** A visit
   is appended to the end of a tab only when it was billed under **that
   associate's own NPI** and its (patient + `Date of Session`) is not already
   in the tab. A patient being "known" to a tab is *not* enough: a session the
   physician performed for one of Ana's patients is never added to Ana's tab.
3. **Marcia — fill only, never append.** Her sessions are billed incident-to
   under the supervising physician's NPI, so no EOB can prove one is hers. Her
   tab roster defines ownership; a Marcia visit with no row waiting for it is
   listed, not placed.
4. **Practitioner cross-check.** When filling an Ana or Oxana row, if the
   matched EOB visit was billed under the *other associate's* NPI the row is
   flagged **practitioner mismatch — needs review** rather than filled. A
   supervising-physician NPI is **not** a conflict — it is the ordinary way an
   associate's session is billed and says nothing about who performed it.
5. **Ambiguity.** If several reconciled EOB visits match one patient + date
   (two providers billed that day), the tab's own NPI picks between them. On
   Marcia's tab, or when the tie survives, the row is flagged
   **ambiguous EOB match — needs review** and nothing is written.
6. **Unassigned.** Anything that neither filled a row nor appended is listed as
   *unassigned — needs manual placement*, never guessed at, with the EOB's NPI
   shown so the reason is visible. **Most visits bill under the supervising
   physician's NPI, so this list is normally long** — it is the physician's own
   direct patients plus every Marcia session without a row.

#### The tab → NPI mapping

Appends need to know each associate's own NPI. Like the provider mapping, the
real values are **never committed**: the app ships a synthetic placeholder
(`remit/config.py`). To use real values, copy `employee_npi.example.json` to
`employee_npi.local.json` (repo root, gitignored), or set an `[employee_npi]`
table in Streamlit secrets when deployed.

| Tab | NPI | Appends? |
|---|---|---|
| Ana | her own PERF PROV NPI | yes, for visits carrying it |
| Oxana | her own PERF PROV NPI | yes, for visits carrying it |
| Marcia | *(none — bills incident-to)* | **no, fill-only** |

A tab omitted from the mapping is fill-only, which is the safe default: the
app can only ever *fail to add* a row, never add someone else's.

#### Audit columns on each tab

Every provider tab gains two columns after its existing headers, on **that
tab's own header row** (Ana/Marcia row 1, Oxana row 2):

| Column | Value |
|---|---|
| `Processed On` | the date the app filled or appended the row, `MM/DD/YYYY` |
| `Remit Check/EFT #` | the **paying** remit's check/EFT number — per the OA-18 rule this is never a duplicate's; several authoritative remits are joined with `; ` |

They are written only for rows the app fills or appends in that run; every
other row is left untouched. The headers are created only on tabs the run
actually writes to, and are reused rather than duplicated on later runs.

Dedup is by patient + Date of Session, so re-running adds nothing.

### EOB-date cutoff

An optional **Ignore EOBs dated before** input, **empty by default**.

**The unit is the whole remittance, and the date compared is the remit's own
header `DATE:`** — the EOB/check date — never the service date. When set, an
uploaded remit dated before the cutoff is skipped entirely and none of its
visits enter the run. A remit dated exactly **on** the cutoff is processed,
and everything inside it is kept however old the session.

That distinction is the point. A remit settles claims long after the fact: the
sample paying remittance is dated 07/20/2026 and pays sessions going back to
01/15/2026. Filtering by *service* date would tear such a remit in half and
drop payments you are actively reconciling. Filtering by the EOB's own date
lets you archive by batch instead — *"everything up to the June check is done,
start from July."*

The exclusion happens **at parse time, before reconciliation**, which is what
makes it honest: a skipped remit contributes no visits *and* no reconciliation
occurrence, so it can neither restate an amount nor mark another remit's visit
as a duplicate from outside the run. It is one filter applied once, ahead of
**both** the schedule and the employees file, so the two outputs can never
disagree about what was in scope.

Leaving it empty processes every uploaded remit, exactly as before; data is
never dropped unless you ask for it. The active cutoff, and every remit it
skipped, are named in the page and in the employees section. A remit whose
header `DATE:` cannot be read is **processed rather than dropped**, and
flagged — losing payments silently is the worse failure.

### Where new rows go

New rows are no longer always appended. Placement is decided per patient, using
the same suffix-aware name matching as everything else:

- **Patient already in the schedule** → the new row is **inserted directly
  below that patient's last existing row**, so their visits stay grouped. If a
  patient's rows are scattered, the *last* occurrence is the anchor. Several
  new visits for one patient go in as a single block in `Data` order.
- **Patient not in the schedule** → appended at the **bottom**, as before.

*Fill existing row* cases never move — only genuinely new rows are placed.

Mid-sheet insertion is the delicate part, so the implementation is deliberate
about it: all placements are computed against the **original** layout, then
inserts are applied **bottom-up** (highest anchor first) so an insertion lower
down cannot shift an anchor still to be processed. Bottom appends happen last,
against the final layout. Inserted cells copy the anchor row's font, fill,
border, alignment and number format, since `insert_rows` leaves new cells
unstyled.

`insert_rows` also does **not** adjust merged ranges, formulas, conditional
formatting or data validations. So before inserting, the app checks whether the
anchor is safe — if inserting would split a **merged region**, or if the row
just below the anchor looks like a **totals/summary row**, that patient's new
rows are appended at the bottom instead and the preview says why.

### Audit columns

The app maintains two columns of its own, created on first use in the first
empty columns after `CPT Code` (L and M in the standard layout), with headers
in row 2 matching the header row's formatting:

- **`Processed On`** — the run date as `MM/DD/YYYY`.
- **`Remit Check/EFT #`** — the `CHECK/EFT #` from the remittance header. If
  more than one remit contributes to a row, the numbers are joined with `; `.

These are the app's own columns, so unlike every other column they are
refreshed on each row the app touches. Rows it skips or never touches are left
unchanged.

---

## Run locally

Requires Python 3.11+.

```bash
git clone <your-repo-url>
cd remit-audit-stream

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
streamlit run streamlit_app.py
```

Run the tests with:

```bash
pip install -r requirements-dev.txt
pytest
```

---

## Deploy

On [Streamlit Community Cloud](https://share.streamlit.io):

1. Push this repo to GitHub.
2. **New app** → pick the repo and branch.
3. Set **Main file path** to `streamlit_app.py`. `requirements.txt` is picked up
   automatically.
4. Deploy.

Because the app handles PHI, set the app's visibility so only your account (or
specific invited viewers) can open it, rather than leaving it public.

---

## Usage

1. Upload `List_of_Patients_Schedule.xlsx`.
2. Upload one or more remittance PDFs.
3. Optionally upload `List_of_Patients_Mutual.xlsx` to source the `DX` column.
4. Optionally upload `AMSMC_employees.xlsx` to fill the provider tabs.
5. Optionally set **Ignore EOBs dated before**. Leave it empty to process
   every uploaded remit. When set, any remittance whose **header `DATE:`** is
   before that date is skipped whole — every visit in it is excluded, from the
   schedule *and* the employees file — so re-uploading a batch the providers
   have already archived cannot re-apply it. This is the EOB/check date, **not**
   the service date: a remit that is kept keeps all of its visits, however old
   the sessions. A remit dated on the cutoff itself is processed. The page
   names the active cutoff and every remit it skipped.
6. Read the summary line: *N visits parsed · X to fill · Y new rows · Z skipped
   (already paid) · W need review*.
7. Work through the preview table. Untick anything you do not want applied.
   Items flagged **Needs review** start unticked. The **DX (from Mutual)**
   column shows what would go into a blank `DX` cell, and **Processed On** /
   **Remit Check/EFT #** show the audit stamps.
8. Review the per-provider **Employees file** section. Every value there comes
   **directly from the EOB**, not from the updated schedule. Each row shows its
   action — *fill*, *append (NPI-matched)*, *practitioner mismatch*,
   *ambiguous* or *unassigned* — alongside the EOB's NPI, which is what decides
   whether a session may be appended. Ana and Oxana take appends for visits
   billed under their own NPI; **Marcia is fill-only**.
9. Click **Confirm & generate file**, then **Download updated schedule** — and,
   if you uploaded it, **Download updated employees file**. They are named
   `List_of_Patients_Schedule_updated_YYYY-MM-DD.xlsx` and
   `AMSMC_employees_updated_YYYY-MM-DD.xlsx` so successive archived copies do
   not collide.
10. Use **Clear all data** when you are done to wipe the session.

---

## Matching & update rules

For each aggregated visit the app looks for a row in `2026 Medicare` with a
matching **patient name** and the same **parsed calendar date** in `Data`, then:

| Situation | Action |
|---|---|
| Match found, `Payment` empty | **Fill existing row N** — writes only the blank cells among `Billed`, `Payment`, `Co-pay`, `Comment`, `DX` |
| Match found, `Payment` populated, **same** amounts | **Skip — already recorded** |
| Match found, `Payment` populated, **different** amounts, remit is newer | **Updated (adjusted EOB)** — see below |
| Match found, `Payment` populated, **different** amounts, remit is older | **Needs review** — nothing changes |
| No match, patient already in the sheet | **New row** inserted under that patient's existing rows |
| No match, patient not in the sheet | **New row** appended at the bottom |
| Low-confidence name, unknown NPI, ambiguous multi-match, or ambiguous DX name | **Needs review** — never auto-applied |

- **The dedup key is Patient + Data + CPT.** A payment is considered already
  recorded when the matched row has anything in `Payment`.
- **`0.00` and `0` count as populated.** Only a truly empty cell is *fillable*.
  A formula such as `=23.52+18.92` also counts as a value. A `0.00` row is
  never filled — but if a later remit actually pays, it is *restated*, which is
  the adjusted-EOB path below.
- **Never overwrite.** Existing `DX`, `CPT Code`, `Co-pays Paid`, `Office`,
  `Comment` or any other prefilled value is left exactly as-is. There are two
  deliberate exceptions: the app's own `Processed On` / `Remit Check/EFT #`
  audit columns, which are refreshed on rows it touches this run; and a
  confirmed **Updated (adjusted EOB)** row.

### Adjusted EOBs (`replace_with_latest`)

Medicare often issues a second remittance for a visit it has already reported —
classically a first EOB paying `$0.00` and a later one that actually pays.
Skipping those loses the real payment, so a differing amount is reconciled
instead of ignored.

The policy is `REPROCESS_POLICY = "replace_with_latest"` in
[`remit/config.py`](remit/config.py): **a later Medicare remit restates the
claim, so the recorded amount is replaced by the later one — amounts are never
summed.** Two alternatives ship but are not the default: `sum` (add to the
recorded amount) and `flag_only` (never propose a write).

When an accepted update is applied, the matched row gets:

| Column | New value |
|---|---|
| `Payment` | the later remit's amount |
| `Co-pay` | the later remit's amount |
| `Billed` | the later remit's header `DATE:` |
| `Remit Check/EFT #` | the existing value **plus** the new number, joined with `; ` so the history stays visible |
| `Processed On` | today |

Rules that keep this safe:

- **Order matters.** An update only happens when the incoming remit is *newer*
  than the recorded `Billed`. An older remit arriving out of order never
  downgrades a newer value — it is flagged *Needs review* and changes nothing.
  When the recorded `Billed` is a placeholder like `2/32/26` the order cannot be
  verified, so the update is proposed *with that stated in the preview*.
- **Never a second row.** A restatement always targets the row it matched, on
  patient + service date. If a later remit changed the CPT set it is still the
  same visit: the change is flagged *Needs review* and, if accepted, updates
  that row rather than appending a duplicate.
- **Unreadable values are never clobbered.** If the recorded `Payment` is a
  formula or free text it cannot be compared, so the row is flagged for review
  instead of overwritten.
- **Nothing is silent.** Every restatement shows **old → new** for `Payment`,
  `Co-pay`, `Billed` and `Remit Check/EFT #` in the preview, plus a plain
  explanation such as `was $0.00, now $130.46 (payment received)`. It is applied
  only when you confirm.
- **Several remits in one upload** are ordered by header `DATE:` and reconciled
  to the newest; the preview notes how many contributed. Within a single
  remittance the service lines of a visit are still summed as before.
- **Exact duplicates never win.** Medicare reason code **18** (`OA-18` /
  `CO-18`, *"exact duplicate claim/service"*) marks a claim re-adjudicated at
  `$0.00` because the original already paid. Such an occurrence is **not
  authoritative**: the amounts come from the newest *non-duplicate* remit, in
  any upload order, so a duplicate can never zero out a real payment. The
  preview says so — *"OA-18 duplicate from EFT x ignored; kept payment from
  EFT y"* — and the audit trail still lists both EFTs. If a visit is seen
  *only* as a duplicate (the paying remit was not uploaded) it is flagged
  *Duplicate only (OA-18) … verify* rather than recorded as a real `$0.00`.
  The discriminator is the **reason code, not the amount**: a `$0.00` line
  carrying `CO-45` because the deductible consumed the allowed amount is a
  genuine zero and is unaffected.
- Re-running an applied update is a no-op: the row now agrees with the remit,
  so it classifies as *Skip — already recorded*.
- **Names are compared, not rewritten.** Generational suffixes (`Jr`, `Sr`,
  `II`–`V`) are stripped for comparison only, so `Marchetti Jr, Dean` matches
  `MARCHETTI, DEAN` and gets filled instead of duplicated. The stored spelling of
  an existing row is never changed; only newly appended names are title-cased.
- **The `Billed` placeholder caveat.** `Billed` is frequently pre-filled with
  junk such as `2/32/26` or `No Billing`. Because of the never-overwrite rule
  that placeholder **stays in place** when a row is filled. The preview shows
  **Existing Billed** next to the proposed value precisely so you can spot these
  and fix them by hand.
- When several rows share a patient and date (e.g. two providers), the app
  disambiguates by CPT overlap, then by the provider `Comment`, then by which
  row is still unpaid. If the tie survives all three it is flagged for review.
- New rows are grouped under their patient where that patient already exists,
  and appended at the bottom otherwise (see *Where new rows go*). Existing rows
  are never re-sorted, edited beyond their fill cells, or moved relative to one
  another — they only shift down to make room for an insertion.

---

## Data & privacy note

This app processes **PHI**. Uploaded files are held in memory for the duration
of your session and are **not persisted server-side** — there is no database and
nothing is written to disk by the app. The only output is the file you download.
That said, Streamlit Community Cloud is third-party hosting: deploy it as a
private app, restrict viewer access to the people who need it, and treat the URL
as sensitive. If your practice's agreements require a BAA, host it somewhere
covered by one instead.

Real patient files are excluded from git by `.gitignore` — including
`AMSMC_employees*.xlsx`, which is PHI. Everything under `tests/fixtures/` is
synthetic (see `tests/fixtures/README.md`) and is the only intentional
exception.

The app is hardened for this: uploads and downloads are held only in
`BytesIO`, nothing is written to disk or `/tmp`, tracebacks are suppressed in
the UI and exception text is scrubbed before display, telemetry is off, no PHI
goes into caches or globals, downloads are named from the workbook and date
only, and a **Clear all data** button wipes the session.

**The app must not be left public.** Restrict it to an allowlist of Google
accounts in Streamlit Community Cloud and do not share the URL beyond
authorized staff. Note that Streamlit Community Cloud and GitHub are **not
BAA-covered**, so processing real PHI there is itself a gap — see
[`HIPAA.md`](HIPAA.md) for the full list of safeguards and that caveat, and
`SECURITY.md` before pushing any change.

---

## Limitations / assumptions

- **`Ins` is always `Medicare`** on new rows. Other payers are out of scope.
- **Dates are written as `MM/DD/YYYY` text**, not Excel date serials, to match
  the existing free-text columns. The format is a single constant
  (`DATE_FMT` in `remit/config.py`).
- **Tuned for the Noridian RA layout.** A different payer's remittance format
  will not parse. The extraction rules assume six dollar amounts per service
  line, in the documented order.
- **Only the `2026 Medicare` sheet is read or written.** The sheet name is a
  constant and will need updating for a new plan year.
- **Name truncation vs. typos.** A name that is a strict prefix of another is
  treated as a confident match, because that is exactly how Medicare truncates.
  A dropped trailing character therefore reads as truncation rather than as a
  typo. Requiring an exact date match is what keeps this safe in practice — and
  it applies to suffix matching too, so stripping `Jr` can never pull in a
  different person on a different date.
- **`Mac` surnames are not special-cased** when title-casing a new name.
  `MCDONALD` becomes `McDonald`, but `MACDONALD` becomes `Macdonald`, because
  `MacDonald` and `Macy`/`Machado` cannot be told apart without a name list.
  Fix those few by hand, or extend `_title_word` in `remit/matching.py`.
- **A duplicate row from an earlier run is not deleted.** If a previous version
  appended an all-caps `MARCHETTI, DEAN` next to `Marchetti Jr, Dean`, the app now
  matches the suffix row correctly but only *flags* the leftover duplicate in
  the preview — removing it is a manual decision.
- **Unknown NPIs** leave `Comment` blank and are flagged rather than guessed.
- **An associate who bills incident-to gets no employee-tab appends.** Adding a
  row to a provider's tab is a claim about who performed the session, and only
  the EOB's PERF PROV NPI can support one. A practitioner whose sessions bill
  under the supervising physician's NPI is therefore fill-only: her existing
  rows are filled, and her other sessions are listed for manual placement. This
  is deliberate — the alternative is guessing from the tab roster, which
  attributes the physician's own patients to whichever associate happens to
  share them.
- **The EOB-date cutoff is all-or-nothing per remit.** It skips a remittance
  by its header `DATE:` alone. It does not know which claims inside it were
  already reconciled, so a cutoff set too late skips a whole remit that still
  had work in it — and the page says which remits it skipped for exactly that
  reason. It is empty by default. There is deliberately no service-date
  equivalent: splitting a remit by service date would drop part of a payment
  batch you are reconciling as a unit.
- **Workbook repair.** Excel writes `<family val="18">`/`"34"` font attributes
  that openpyxl's schema rejects (it caps the value at 14). The real schedule
  hits this. The app clamps that one attribute in an in-memory copy of the zip
  when a load fails; nothing else is altered.
- Cross-file aggregation sums service lines that share
  (patient, date, provider). If two different remittances genuinely pay the same
  visit, the visit is flagged for review rather than silently doubled.
