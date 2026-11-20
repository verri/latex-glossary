---
name: review
description: Turn extracted glossary candidates into proposed entries (deduplicated, rated and described by a local LLM following the book's glossary-style.md), then walk the user through accepting or rejecting them and record the decisions in the style guide. Use after the extract skill, or when the user wants to review or decide glossary candidates.
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py *) Bash(${CLAUDE_PLUGIN_DATA}/venv/bin/python ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py *)
---

# Review glossary candidates

Run from the book root. Inputs: `.latex-glossary/candidates.json` (from `extract`) and
`glossary-style.md`. Output: `.latex-glossary/review.tex`, the entries the user accepted.

## 1. Filter

Read `glossary-style.md` for the main file and the filtering model (a local Ollama model by default;
check it is served with `ollama list`). Run:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py --cache .latex-glossary/cache.json \
   filter MAIN --llm MODEL --style glossary-style.md \
   --state .latex-glossary/candidates.json -o .latex-glossary/review.tex
```

What it does: merges inflections, abbreviation/expansion pairs and existing entries; sets aside
candidates seen fewer than twice; asks the LLM, for the rest, whether it duplicates a similar entry,
how relevant it is (1-5) and for a description. Abbreviations are never cut by frequency: the LLM
checks they are real abbreviations and whether they name a concept (gets a description) or a name.
Existing abbreviations without a description get proposed updates. Use `--skip-existing` to skip
those, `--max N` to limit a first pass.

## 2. Walk the user through the proposals

`review.tex` has sections: proposed entries (by relevance), low-relevance ones (commented out),
updates for existing abbreviations, and duplicates. Each entry carries comments with its rating,
reason and contexts.

Present the proposals compactly in batches (name, relevance, description) and ask the user which to
keep, reject or rename, and whether to promote any commented candidate. Apply the answers by editing
`review.tex` (comment out rejected entries with `% `, uncomment promoted ones, fix names and
descriptions as the user says). Descriptions must follow the `## Descriptions` section of the style
guide.

## 3. Record the decisions

Append the accepted names to the "Included" list and the rejected names to the "Excluded" list in
`## What deserves an entry` of `glossary-style.md` (and concept/name decisions for abbreviations to
`## Abbreviations`). These labels become examples for the LLM on the next chapters, so they are how
the tool learns the author's taste. Do not remove earlier labels.

Next step: suggest the `verify` skill on `.latex-glossary/review.tex`, then `apply`.
