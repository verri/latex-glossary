---
name: verify
description: Check that glossary descriptions say what the LaTeX book actually means by each term, using parallel read-only verifier subagents that cite the book. Use before applying new or updated glossary entries, or when the user asks whether glossary definitions are consistent with the book.
argument-hint: "[review.tex | label,label,...]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py *)
---

# Verify glossary descriptions against the book

Run from the book root. Targets, in order of preference:
- a reviewed entries file given by the user (default `.latex-glossary/review.tex`): its uncommented
  entries, new and updated;
- labels the user names;
- with no target given and no review file, every described entry of the glossary (say how many
  before starting; it can be large).

## 1. Collect usages

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py contexts MAIN \
   (--entries .latex-glossary/review.tex | --labels a,b,c) -k 6 -o .latex-glossary/contexts.md
```

Each `## label: name` section holds the description and passages where the book uses the term,
spread across files. Entries with no usage are listed with 0 usages: report them as unused.

## 2. Fan out to verifiers

Split the labels into batches of about 10 and launch one `description-verifier` subagent per batch,
all in the same message so they run in parallel (a single subagent for 10 labels or fewer). Give each: the contexts file path, its labels, the
path of `glossary-style.md`, and the book root. They are read-only and return, per label, a verdict
(`consistent`, `mismatch`, `style`, `unclear`), evidence with file:line, and a proposed replacement.

## 3. Report and fix

Show the user a table of the entries that are not `consistent`, with the evidence and the proposed
description. Spot-check at least one `mismatch` yourself by reading the cited lines before
presenting it. For the entries the user accepts, update the description in the reviewed file (or,
for entries already in the glossary, in the glossary file). Do not change entries the user did not
approve.
