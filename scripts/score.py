#!/usr/bin/env python3
"""Score two glosstool runs against the labels filled in the sheets built by build_sheets.py.

Usage: score.py SHEETDIR
Rows with an empty label are skipped (so partial labeling already gives numbers).
"""


import json
import sys
from collections import Counter
from pathlib import Path


def read(path):
    """Tab-split rows as dicts; tolerant of spreadsheets that wrap cells in quotes on save."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()

    def cells(line):
        return [c[1:-1].replace('""', '"') if len(c) >= 2 and c[0] == c[-1] == '"' else c
                for c in line.split("\t")]

    header = cells(lines[0])
    return [dict(zip(header, cells(line) + [""] * (len(header) - len(cells(line)))))
            for line in lines[1:] if line.strip()]


def label(row, prefix):
    col = next(c for c in row if c.startswith(prefix))
    return (row[col] or "").strip().lower()


def pct(a, b):
    return f"{100 * a / b:5.1f}% ({a}/{b})" if b else "    -"


def main():
    d = Path(sys.argv[1])
    key = json.loads((d / "key.json").read_text())
    runs = key["runs"]

    print("== 1. Terms: should it be in the glossary? ==")
    rows = [r for r in read(d / "1-terms.tsv") if label(r, "LABEL") in ("y", "n")]
    yes = {r["id"] for r in rows if label(r, "LABEL") == "y"}
    print(f"labeled {len(rows)}, of which {len(yes)} yes")
    for run in runs:
        kept = {r["id"] for r in rows if key["terms"][r["id"]][run] == "keep"}
        raised = {r["id"] for r in rows if key["terms"][r["id"]][run] != "absent"}
        print(f"  {run:8} extract recall {pct(len(raised & yes), len(yes))}   "
              f"filter precision {pct(len(kept & yes), len(kept))}   recall {pct(len(kept & yes), len(yes))}")
        fp = sorted(r["term"] for r in rows if r["id"] in kept - yes)
        fn = sorted(r["term"] for r in rows if r["id"] in yes - kept)
        print(f"           kept but no: {', '.join(fp) or '-'}")
        print(f"           yes but not kept: {', '.join(fn) or '-'}")

    print("\n== 2. Descriptions of kept terms ==")
    counts = {run: Counter() for run in runs}
    for r in read(d / "2-descriptions.tsv"):
        k = key["descriptions"][r["id"]]
        for side in ("A", "B"):
            lab = label(r, f"LABEL {side}")
            if k[side] and lab in ("ok", "edit", "wrong"):
                counts[k[side]][lab] += 1
    for run in runs:
        c = counts[run]
        n = sum(c.values())
        print(f"  {run:8} ok {pct(c['ok'], n)}   edit {pct(c['edit'], n)}   wrong {pct(c['wrong'], n)}")

    print("\n== 3. Existing abbreviations ==")
    rows = read(d / "3-abbreviations.tsv")
    counts = {run: Counter() for run in runs}
    for r in rows:
        k = key["abbreviations"][r["id"]]
        truth = label(r, "LABEL concept")
        for run in runs:
            if truth in ("c", "n"):
                counts[run]["labeled"] += 1
                counts[run]["correct"] += (k[run] == (truth == "c"))
        for side in ("A", "B"):
            lab = label(r, f"LABEL {side}")
            if lab in ("ok", "edit", "wrong"):
                counts[k[side]][lab] += 1
    for run in runs:
        c = counts[run]
        n = c["ok"] + c["edit"] + c["wrong"]
        print(f"  {run:8} concept/name accuracy {pct(c['correct'], c['labeled'])}   descriptions: "
              f"ok {pct(c['ok'], n)}   edit {pct(c['edit'], n)}   wrong {pct(c['wrong'], n)}")

    print("\n== 4. Technical sense of single-word terms ==")
    rows = [r for r in read(d / "4-sense.tsv") if label(r, "LABEL") in ("y", "n")]
    for run in runs:
        c = Counter()
        for r in rows:
            truth = label(r, "LABEL") == "y"
            pred = key["sense"][r["id"]][run]
            c["correct"] += pred == truth
            c["tp"] += pred and truth
            c["pred"] += bool(pred)
            c["true"] += truth
        print(f"  {run:8} accuracy {pct(c['correct'], len(rows))}   "
              f"precision {pct(c['tp'], c['pred'])}   recall {pct(c['tp'], c['true'])}")


if __name__ == "__main__":
    main()
