---
name: extract
description: Raise glossary candidate terms from a LaTeX book, file by file, with an LLM (each term with its meaning in context), and show a grouped report for inspection. Use when the user wants to find terms for the glossary of a LaTeX book or start/continue glossary extraction.
argument-hint: "[file.tex ...]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py *) Bash(${CLAUDE_PLUGIN_DATA}/venv/bin/python ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py *)
---

# Extract glossary candidates

Tool: `${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py` (run from the book's root directory).
Working files go in `.latex-glossary/` at the book's root (the tool creates it).

## 1. Setup (once per book)

- If `glossary-style.md` does not exist at the book root, copy
  `${CLAUDE_PLUGIN_ROOT}/templates/glossary-style.md` there, fill in the Setup section (main file,
  glossary file, build command) from the repository, and show it to the user.
- Read `glossary-style.md`: the main file and the extraction model come from it (default
  `anthropic:claude-opus-5-5`, `--effort low`).
- Python: an `anthropic:` model needs the `anthropic` package. If
  `${CLAUDE_PLUGIN_DATA}/venv/bin/python` does not exist, ask the user before creating it with
  `python3 -m venv ${CLAUDE_PLUGIN_DATA}/venv && ${CLAUDE_PLUGIN_DATA}/venv/bin/pip install anthropic`.
  Use that interpreter for every command below; use `python3` for Ollama-only work.
- The API key is read from the environment or a `.env` file at the book root. Never print it. Make
  sure `.env` and `.latex-glossary/cache.json` are in the book's `.gitignore`.
- Extraction through the API costs money (about $0.15 per 3,500-word chapter at low effort). Tell
  the user before the first run.

## 2. Extract file by file

Files: the ones the user named, otherwise every file reached from the main file (in book order;
list them with `grep -o '\\input{[^}]*}' main.tex` or similar). For each file:

```
PY ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py --cache .latex-glossary/cache.json \
   extract MAIN --llm MODEL --effort low --files FILE --state .latex-glossary/candidates.json
```

The state file accumulates; re-running a file replaces only that file's mentions. After each file
the tool rewrites `.latex-glossary/candidates.txt`, a report grouped by name with each mention's
meaning. Show the user a short summary of the new candidates (names, and any name raised with
clearly different meanings) and ask whether to continue with the next file. Stop when the user
says so.

## 3. Inspect

On request, show the report sorted another way:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/glosstool.py inspect MAIN --state .latex-glossary/candidates.json \
   --sort ridf|df|mentions|name [--files FILE] [--text]
```

`df` puts the most pervasive candidates first (likely too generic); `ridf` the most term-like.
Nothing is dropped automatically: the user decides in the `review` skill.
