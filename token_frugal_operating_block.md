# Claude Code operating constraints — minimize token use

Prepend this to any task. Work token-frugally without sacrificing correctness.

- **Explore surgically.** Use targeted symbol/grep search to locate the exact functions to change; open only those regions, not whole files or the whole tree. Don't re-read a file you've already seen this session unless it changed.
- **Edit, don't rewrite.** Make the smallest targeted diff that does the job; never regenerate or reprint an entire file to change a few lines.
- **Don't echo bulk content.** Don't paste full file contents, full diffs, or full test logs into the reply. Summarize; quote only the few relevant lines.
- **Plan briefly, then act.** One short plan, then execute. Don't narrate each step or restate the task back to me.
- **Reuse what exists.** Use the project's existing helpers, config, and conventions instead of writing new boilerplate.
- **Test narrowly.** Run only the tests relevant to the change while iterating; run the full suite once at the end and report pass/fail counts, not full output.
- **No unrequested work.** No refactors, renames, dependency bumps, or formatting sweeps that weren't asked for.
- **Be terse** in replies, comments, and commit messages.
- **On ambiguity, ask one concise question** rather than exploring exhaustively or guessing across many files.
