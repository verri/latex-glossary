# latex-glossary

A Claude Code plugin (and a standalone Python CLI) to build and maintain the glossary of a LaTeX
book that uses [glossaries-extra](https://ctan.org/pkg/glossaries-extra): find candidate terms,
review them against the author's own criteria, check that descriptions match how the book uses each
term, and replace plain occurrences with `\gls`, `\Gls`, `\glspl`, `\Glspl`.

The mechanical work (parsing LaTeX, masking math/verbatim/labels/citations, chunking, deduplication,
frequency statistics, substitutions, editing the glossary file) is deterministic Python. LLMs are
used only for judgment, and the author decides.

## Install

In Claude Code:

```
/plugin marketplace add verri/latex-glossary
/plugin install latex-glossary@latex-glossary
```

The CLI alone needs only Python 3.10+ (standard library). The Anthropic backend needs the
`anthropic` package; the Ollama backend needs a running [Ollama](https://ollama.com).

## Workflow

Each book keeps its decisions in `glossary-style.md` at its root (created from
`templates/glossary-style.md` on first use): what deserves an entry (with the author's labeled
examples), description rules, abbreviation rules, placement policies and models. Working files go in
`.latex-glossary/`.

| Skill | What it does |
|---|---|
| `/latex-glossary:extract` | Raises candidate terms chunk by chunk with an LLM, each with its meaning in context; terms not literally in the text are discarded. Report grouped by name, file by file. |
| `/latex-glossary:review` | Deduplicates, rates and describes candidates with an LLM guided by `glossary-style.md`; the user accepts or rejects; decisions are recorded as examples for later chapters. |
| `/latex-glossary:verify` | Parallel read-only subagents check each description against the passages where the book uses the term, citing file:line. |
| `/latex-glossary:apply` | Adds/updates entries and wraps occurrences (terms: first per paragraph; abbreviations: every occurrence; single-word terms only in their technical sense), shows the diff, writes on approval, rebuilds. |

## CLI

```
scripts/glosstool.py extract  MAIN --llm anthropic:claude-opus-5-5 --files chapters/x.tex
scripts/glosstool.py inspect  MAIN --sort ridf
scripts/glosstool.py filter   MAIN --llm gemma4:26b-a4b --style glossary-style.md -o review.tex
scripts/glosstool.py contexts MAIN --entries review.tex
scripts/glosstool.py wrap     MAIN --new-entries review.tex            # prints a diff
scripts/glosstool.py wrap     MAIN --new-entries review.tex --write    # edits files
```

`--llm` takes `anthropic:MODEL` (API key from the environment or `.env`; structured outputs,
`--effort`) or `ollama:MODEL` / a bare Ollama model name. LLM answers are cached (`--cache`), so
re-runs are free. Run `glosstool.py <command> --help` for all options.

`scripts/build_sheets.py` and `scripts/score.py` build blind A/B label sheets to compare two runs
(e.g. two models) on a chapter and score them against the author's labels.

## Evaluation notes

On one chapter (about 3,600 words) of a data science textbook, with the author's labels:

| | local gemma4:26b-a4b | Claude Opus 5.5 (low effort) |
|---|---|---|
| Extraction recall | 83% | 96% |
| Technical-sense check accuracy | 90% | 97% |
| Descriptions acceptable | about the same; author preferred shorter ones | |

Filtering precision was low for both models until the author's criteria and examples were given
(hence `glossary-style.md`). The default split is therefore Opus for extraction and a local model
for the rest, with verification by Claude Code subagents.

## License

MIT
