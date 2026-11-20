---
name: apply
description: Apply reviewed glossary entries to a LaTeX book and replace plain occurrences of glossary terms with \gls, \Gls, \glspl or \Glspl, following the book's placement policies; shows the diff, writes only after approval, then checks the book still compiles. Use when the user wants to add entries to the glossary or wrap terms with \gls.
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py *)
---

# Apply glossary entries and \gls substitutions

Run from the book root. Inputs: `glossary-style.md` (main file, policies, model, build command) and,
optionally, reviewed entries in `.latex-glossary/review.tex` (new labels are added to the glossary
file; existing labels replace their definition).

## 1. Preconditions

- `git status`: the files to be changed (chapters, glossary file) should have no uncommitted
  changes, so the result can be reviewed and reverted with git. If they do, ask the user first.
- If the reviewed file has entries with `TODO`, stop and send the user back to `review`.

## 2. Dry run

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py --cache .latex-glossary/cache.json \
   wrap MAIN [--new-entries .latex-glossary/review.tex] \
   --policy first-per-paragraph --abbr-policy all \
   --llm MODEL --decisions .latex-glossary/decisions.jsonl > .latex-glossary/wrap.diff
```

Take the policies and the model (for the technical-sense check of single-word terms) from
`glossary-style.md`; without a model, add `--no-ambiguous` to leave single-word terms alone. The
summary table goes to stderr. Show the user the per-label counts, the size of the diff, and a
sample of hunks (include some for single-word terms and for bare long forms of abbreviations, which
change the printed text). Ask for approval.

## 3. Write and check

On approval, rerun the same command with `--write` (it also appends or updates the entries in the
glossary file, and adds `\setabbreviationstyle[described]{long-short-desc}` when needed). Then run
the build command from `glossary-style.md` and check for LaTeX errors (`grep '^!'` in the log). If
the build fails, show the error and the offending substitution; `git diff` and `git checkout -- FILE`
undo the changes. Report: files changed, substitutions per label, entries added or updated, build
result. Do not commit unless the user asks.
