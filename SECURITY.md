# Security & PHI policy

This app processes **Protected Health Information (PHI)**: Medicare
remittance advice PDFs and a patient schedule workbook, both containing real
patient names, dates of service, diagnosis codes, and payment amounts.

## GitHub is not a BAA-covered environment

Treat this repository — **private or public** — as a place real patient data
must never live, even briefly. GitHub is not a covered entity under a Business
Associate Agreement for this project, and anything committed here persists in
git history even after a later commit deletes it, is visible to anyone with
repo access, and may be cached, indexed, or forked. History rewrites reduce
exposure but do not reliably erase already-pushed content — see "If real PHI
is ever committed" below.

## Rules

1. **Never commit a real patient file.** Not the schedule workbook, not a
   remittance PDF, not a screenshot, not a "just for a quick test" copy.
   `.gitignore` blocks the obvious paths (real `.xlsx`/`.PDF` files at the repo
   root, `_updated.xlsx` outputs, a `/data/`, `/private/`, or `/local/`
   directory) but a gitignore rule is a backstop, not a substitute for judgment
   — a file placed inside `tests/fixtures/` or renamed to dodge a pattern
   would still be tracked.
2. **Only synthetic data belongs in `tests/fixtures/`.** See
   `tests/fixtures/README.md`. If you need a new edge case, add it to the
   fictional roster in `tests/fixtures/synthetic_remit_data.py` and regenerate
   — never drop in a redacted real file. "Redacted" real data is not a safe
   substitute: real dates, dollar amounts, and CPT/DX codes can combine with
   even a partially redacted name to re-identify someone.
3. **Run against real data only locally, with real files kept outside the
   repo directory** (or in an ignored path). The app itself never writes
   uploaded files to disk — everything is processed in memory for the
   session — but you are still responsible for where you keep the source files
   on your own machine.
4. **The real NPI → doctor mapping is configuration, not code.** Keep it in a
   gitignored `npi_map.local.json` (see `npi_map.example.json`) or in
   Streamlit secrets when deployed. Never hardcode a real doctor's name or a
   real clinic's NPI into a tracked file.
5. **Deploying to Streamlit Community Cloud:** set the app to private, and
   restrict access to only the people who need it. Treat the app's URL as
   sensitive.

## Before pushing

Run a search for anything that looks like real patient data before every
push, not just once:

```bash
python scripts/check_for_phi.py
```

This is a heuristic, not a guarantee — it looks for the specific identifiers
this project has previously leaked (see `CHANGELOG.md` 1.1.0) and for
MBI-shaped strings. It cannot know every possible real name.

To block real patient surnames, create **`phi_denylist.local.txt`** in the repo
root — one term per line, `#` for comments. It is gitignored, which is the
point: a list of real patient names must never be committed, not even to a
security tool. The checker picks it up automatically.

If you have `gitleaks` available, also run:

```bash
gitleaks detect --source . --no-git   # working tree
gitleaks detect --source .            # + full history
```

To wire the check in automatically, copy it into your local hook:

```bash
cp scripts/check_for_phi.py .git/hooks/pre-commit-phi-check   # optional
```

or use it as a `pre-commit` local hook — see the commented example in
`.pre-commit-config.yaml` if present.

## If real PHI is ever committed

1. **Stop.** Do not push if you haven't already.
2. If it was pushed to a remote (public or private), a history rewrite
   (`git filter-repo`, BFG) followed by a force-push reduces exposure but does
   **not** guarantee erasure: GitHub's cached views, any forks, and any open
   pull requests can retain the content independently of your rewrite.
3. Contact GitHub Support to request purging cached views of the affected
   commits.
4. Escalate to your organization's privacy/compliance officer. Exposure of
   real PHI outside a covered environment may be a reportable breach under
   HIPAA — that determination is theirs to make, not an engineering call.

## Not legal advice

This document describes engineering hygiene for keeping real patient data out
of source control. It is not a HIPAA compliance determination. Have your
practice's privacy/compliance officer review this project's handling of PHI
before it is used with real patients or made accessible beyond your own
machine.
