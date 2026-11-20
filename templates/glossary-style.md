# Glossary style for this book

Read by the latex-glossary tools and skills. Edit freely; it records the author's decisions.
The `## What deserves an entry`, `## Descriptions` and `## Abbreviations` sections are passed to
the LLM verbatim, so keep their headings.

## Setup

- Main file: `main.tex`.
- Glossary file: `glossary.tex` (glossaries-extra).
- Build command: `latexmk -pdf main.tex`

## What deserves an entry

Include concepts that this book defines or uses in a specific sense, the vocabulary a reader needs
to follow the book. Exclude names of whole fields, very general words, and the book's broad subject.

Included (author's labels):

Excluded (author's labels):

## Descriptions

- A single sentence, at most 20 words.
- The essential definition only, no elaboration or examples.
- Faithful to how this book uses the term, not a generic textbook definition.
- Do not start with the term itself; no LaTeX.

## Abbreviations

- Every abbreviation the reader meets is defined (`\newabbreviation`).
- Concept abbreviations get a description: `category=described` with
  `\setabbreviationstyle[described]{long-short-desc}`.
- Names (organizations, products, languages, formats, standards, modifiers) get no description.
  - Names:
  - Concepts:

## Wrapping (`\gls` substitutions)

- Terms: first occurrence per paragraph (`--policy first-per-paragraph`).
- Abbreviations: every occurrence (`--abbr-policy all`).
- Bare long forms of abbreviations become `\gls`.
- Single-word terms only where used in the glossary's technical sense.

## Models

- Extraction: `anthropic:claude-opus-5-5` with `--effort low` (needs ANTHROPIC_API_KEY in `.env`).
- Filtering, descriptions and the technical-sense check: a local Ollama model, e.g. `gemma4:26b-a4b`.
