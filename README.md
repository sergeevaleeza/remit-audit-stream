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
  trailing middle initials (`SMITH, JANE R` → `Smith, Jane`) and
  Slavic/Armenian gendered surname endings (`Ivanova` / `Ivanov`).
- **Only fills blank cells, never overwrites.** This is the core safety
  guarantee. A cell that already holds *any* value is left exactly as it was.
  Every write is re-checked against the live cell immediately before it happens.
- **Dedup / idempotency.** Re-running the same remittance against an
  already-updated schedule is a no-op.
- **Messy-date tolerant.** `Data` and `Billed` are hand-typed free text holding
  a mix of real dates, `MM/DD/YYYY` strings and junk placeholders. Dates are
  only ever compared by parsed calendar value, never as strings.
- **Preserves the whole workbook.** openpyxl is used directly for both read and
  write, so all 24 sheets, styling and formulas survive into the download.
- **Duplicate-remit warning.** If the same `CHECK/EFT #` appears in two uploaded
  files, you get a warning instead of silently doubled amounts.

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
| `Ins` | literal `Medicare` |
| `Data` | SERV DATE — the `MMDDYY` token → `MM/DD/YYYY` |
| `Billed` | the header `DATE:` value → `MM/DD/YYYY` |
| `Payment` | **sum of PROV-PD** across the visit's service lines |
| `Co-pay` | **sum of COINS** (the 4th dollar amount) across those lines |
| `Comment` | doctor resolved from the PERF-PROV NPI |
| `CPT Code` | E/M code first, then the add-on, joined with `/` (new rows only) |
| `DX` | left blank on new rows |
| `Co-pays Paid`, `Office` | never touched |

`PROV-PD` is already net of the CO-45 write-off and CO-253 sequestration, so it
is taken as-is. Visits aggregate all service lines sharing
**(patient, service date, provider NPI)**.

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

An NPI outside this table leaves `Comment` blank and flags the visit for review.

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
3. Read the summary line: *N visits parsed · X to fill · Y new rows · Z skipped
   (already paid) · W need review*.
4. Work through the preview table. Untick anything you do not want applied.
   Items flagged **Needs review** start unticked.
5. Click **Confirm & generate file**, then **Download updated workbook**. The
   file is named `List_of_Patients_Schedule_updated_YYYY-MM-DD.xlsx` so
   successive archived copies do not collide.

---

## Matching & update rules

For each aggregated visit the app looks for a row in `2026 Medicare` with a
matching **patient name** and the same **parsed calendar date** in `Data`, then:

| Situation | Action |
|---|---|
| Match found, `Payment` empty | **Fill existing row N** — writes only the blank cells among `Billed`, `Payment`, `Co-pay`, `Comment` |
| Match found, `Payment` holds any value | **Skip (already paid)** |
| No match | **New row** appended after the last data row |
| Low-confidence name, unknown NPI, or ambiguous multi-match | **Needs review** — never auto-applied |

- **The dedup key is Patient + Data + CPT.** A payment is considered already
  recorded when the matched row has anything in `Payment`.
- **`0.00` and `0` count as populated.** Only a truly empty cell is fillable.
  A formula such as `=23.52+18.92` also counts as a value.
- **Never overwrite.** Existing `DX`, `CPT Code`, `Co-pays Paid`, `Office`,
  `Comment` or any other prefilled value is left exactly as-is.
- **The `Billed` placeholder caveat.** `Billed` is frequently pre-filled with
  junk such as `2/32/26` or `No Billing`. Because of the never-overwrite rule
  that placeholder **stays in place** when a row is filled. The preview shows
  **Existing Billed** next to the proposed value precisely so you can spot these
  and fix them by hand.
- When several rows share a patient and date (e.g. two providers), the app
  disambiguates by CPT overlap, then by the provider `Comment`, then by which
  row is still unpaid. If the tie survives all three it is flagged for review.
- New rows are appended after the last data row; existing rows are never
  re-sorted.

---

## Data & privacy note

This app processes **PHI**. Uploaded files are held in memory for the duration
of your session and are **not persisted server-side** — there is no database and
nothing is written to disk by the app. The only output is the file you download.
That said, Streamlit Community Cloud is third-party hosting: deploy it as a
private app, restrict viewer access to the people who need it, and treat the URL
as sensitive. If your practice's agreements require a BAA, host it somewhere
covered by one instead.

Real patient files are excluded from git by `.gitignore`. Everything under
`tests/fixtures/` is synthetic (see `tests/fixtures/README.md`) and is the only
intentional exception. See `SECURITY.md` before working with real data in this
repo, or before pushing any change to GitHub.

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
  typo, and a suffix like `Smith Jr` matches `Smith`. Requiring an exact date
  match is what keeps this safe in practice.
- **Unknown NPIs** leave `Comment` blank and are flagged rather than guessed.
- **Workbook repair.** Excel writes `<family val="18">`/`"34"` font attributes
  that openpyxl's schema rejects (it caps the value at 14). The real schedule
  hits this. The app clamps that one attribute in an in-memory copy of the zip
  when a load fails; nothing else is altered.
- Cross-file aggregation sums service lines that share
  (patient, date, provider). If two different remittances genuinely pay the same
  visit, the visit is flagged for review rather than silently doubled.
