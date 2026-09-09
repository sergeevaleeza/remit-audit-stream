#!/usr/bin/env python
"""Heuristic guard against re-committing known-real PHI into this repo.

Scans staged changes (or the whole working tree with --all) for:

  * MBI-shaped strings (11-char Medicare Beneficiary Identifier format)
  * the specific real NPIs and real clinic name this project has, in the
    past, accidentally committed (see CHANGELOG.md 1.1.0)
  * a real .xlsx/.PDF file about to be staged outside tests/fixtures/

This is a backstop, not a guarantee: it cannot recognize every possible real
patient name. Treat a clean run as "no *known* problem found", not as proof
the diff is PHI-free -- exercise judgment on top of it.

Usage:
    python scripts/check_for_phi.py            # staged changes only
    python scripts/check_for_phi.py --all       # entire working tree

Exit code 0 = clean, 1 = a likely problem was found.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# MBI format: digit, letter(no S/L/O/I/B/Z), alnum(no S/L/O/I/B/Z), digit,
# letter, alnum, digit, letter, letter, digit, digit. Approximated loosely
# here since we only need to catch obvious real-looking values, not validate.
MBI_RE = re.compile(r"\b[1-9][ACEFGHJKMNPQRTUVWXY][A-Z0-9][0-9][ACEFGHJKMNPQRTUVWXY][A-Z0-9][0-9][ACEFGHJKMNPQRTUVWXY]{2}[0-9]{2}\b")

# Real identifiers previously committed to this repo (working tree, never
# pushed) -- see CHANGELOG.md 1.1.0. Kept here so they can never come back.
RETIRED_REAL_NPIS = ["1306876883", "1114400215", "1346590437", "1245365782"]
RETIRED_REAL_STRINGS = [
    "RemitDoc-2656190831",
    "803372845",
    "ACCESS MULTI-SPECIALTY",
]

FIXTURE_ALLOWED_PREFIX = "tests/fixtures/"

# This script necessarily contains the retired identifiers as literals to
# check against, and would otherwise flag itself on every run.
SELF_EXEMPT_PATH = "scripts/check_for_phi.py"

# Optional, gitignored: one term per line (blank lines and #comments ignored).
# Real patient surnames belong HERE, never in this tracked file -- listing them
# in the repo would itself be the leak this script exists to prevent.
LOCAL_DENYLIST_PATH = REPO_ROOT / "phi_denylist.local.txt"


def local_denylist() -> list[str]:
    """Extra terms to block, read from an untracked local file if present."""
    if not LOCAL_DENYLIST_PATH.is_file():
        return []
    terms = []
    for line in LOCAL_DENYLIST_PATH.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    return terms


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def all_tracked_and_staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def staged_text(path: str) -> str | None:
    out = subprocess.run(
        ["git", "show", f":{path}"], cwd=REPO_ROOT, capture_output=True, check=False,
    )
    if out.returncode != 0:
        return None
    try:
        return out.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None  # binary file; handled separately by the extension check


def check_file(path: str, get_text, extra_terms: list[str] | None = None) -> list[str]:
    if path == SELF_EXEMPT_PATH:
        return []

    problems = []

    if re.search(r"\.(xlsx|pdf)$", path, re.IGNORECASE) and not path.startswith(FIXTURE_ALLOWED_PREFIX):
        problems.append(f"{path}: an .xlsx/.PDF file outside tests/fixtures/ -- likely real patient data")

    text = get_text(path)
    if text is None:
        return problems

    for match in MBI_RE.finditer(text):
        problems.append(f"{path}: MBI-shaped string '{match.group(0)}'")
    for npi in RETIRED_REAL_NPIS:
        if npi in text:
            problems.append(f"{path}: retired real NPI '{npi}'")
    for needle in RETIRED_REAL_STRINGS:
        if needle in text:
            problems.append(f"{path}: retired real identifier '{needle}'")
    for needle in extra_terms or []:
        if needle.lower() in text.lower():
            problems.append(f"{path}: term from phi_denylist.local.txt ('{needle}')")

    return problems


def main() -> int:
    scan_all = "--all" in sys.argv[1:]
    if scan_all:
        files = all_tracked_and_staged_files()

        def get_text(path: str) -> str | None:
            full = REPO_ROOT / path
            try:
                return full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None
    else:
        files = staged_files()
        get_text = staged_text

    extra_terms = local_denylist()
    problems: list[str] = []
    for path in files:
        problems.extend(check_file(path, get_text, extra_terms))

    if problems:
        print("check_for_phi: possible PHI/real-identifier found:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nIf this is a false positive, fix the pattern in "
            "scripts/check_for_phi.py rather than bypassing the check.",
            file=sys.stderr,
        )
        return 1

    print("check_for_phi: clean (heuristic only -- see SECURITY.md).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
