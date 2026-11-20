#!/usr/bin/env python3
"""Build blind label sheets comparing two glosstool runs on the same chapter.

Usage: build_sheets.py OUTDIR RUN1 RUN2 [--seed N]
  RUNi = name:state.json:review.tex:decisions.jsonl

Writes, in OUTDIR:
  1-terms.tsv         one row per distinct candidate term: label include = y/n
  2-descriptions.tsv  drafted descriptions of kept terms, as A/B: label ok/edit/wrong
  3-abbreviations.tsv existing abbreviations: label concept/name, and A/B description ok/edit/wrong
  4-sense.tsv         occurrences of single-word terms: label technical = y/n
  key.json            which run produced A and B on each row, and each run's verdicts (for score.py)
Model verdicts are not shown in the sheets.
"""

import argparse
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from glosstool import parse_entries  # noqa: E402


def load_run(spec):
    name, state, tex, dec = spec.split(":")
    cands = json.loads(Path(state).read_text())["candidates"]
    entries = parse_entries(Path(tex).read_text())
    decisions = [json.loads(line) for line in Path(dec).read_text().splitlines() if line.strip()]
    return {"name": name, "cands": {c["key"]: c for c in cands}, "entries": {e.label: e for e in entries},
            "decisions": decisions}


def clean(s):
    return re.sub(r"\s+", " ", s or "").replace("\t", " ").strip()


def write_tsv(path, header, rows):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\t".join(header) + "\n")
        for r in rows:
            fh.write("\t".join(clean(str(x)) for x in r) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("run1")
    ap.add_argument("run2")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--main", default="main.tex")
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    runs = [load_run(args.run1), load_run(args.run2)]
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    key = {"runs": [r["name"] for r in runs], "terms": {}, "descriptions": {}, "abbreviations": {}, "sense": {}}

    # existing glossary, to recognize abbreviation updates
    from glosstool import load_project
    _, gloss = load_project(Path(args.main))
    existing = {e.label: e for g in gloss for e in parse_entries(g.text)}

    # 1. terms: union of candidates that are not existing entries or merged duplicates
    keys = sorted({k for r in runs for k, c in r["cands"].items() if c["status"] not in ("existing", "merged")})
    rows = []
    for i, k in enumerate(keys, 1):
        c = next(r["cands"][k] for r in runs if k in r["cands"])
        m = c["mentions"][0]
        meaning = next((x["meaning"] for x in c["mentions"] if x["meaning"]), "")
        name = c["name"] + (f" ({c['long']})" if c.get("long") else "")
        rows.append([f"T{i}", name, f"{m['file']}:{m['line']}", m["text"][:300], meaning, ""])
        key["terms"][f"T{i}"] = {"key": k, **{r["name"]: (r["cands"][k]["status"] if k in r["cands"] else "absent")
                                            for r in runs}}
    write_tsv(out / "1-terms.tsv", ["id", "term", "where", "context", "meaning (as raised)",
                                    "LABEL include? (y/n)"], rows)

    # 2. descriptions of kept terms
    rows = []
    n = 0
    for k in keys:
        descs = [(r["name"], r["cands"][k]) for r in runs
                 if k in r["cands"] and r["cands"][k]["status"] == "keep" and r["cands"][k].get("description")]
        if not descs:
            continue
        n += 1
        rnd.shuffle(descs)
        name = descs[0][1]["name"]
        a = descs[0]
        b = descs[1] if len(descs) > 1 else (None, {"description": ""})
        rows.append([f"D{n}", name, a[1]["description"], b[1]["description"], "", "" if b[0] else "-"])
        key["descriptions"][f"D{n}"] = {"key": k, "A": a[0], "B": b[0]}
    write_tsv(out / "2-descriptions.tsv", ["id", "term", "A description", "B description",
                                           "LABEL A (ok/edit/wrong)", "LABEL B (ok/edit/wrong)"], rows)

    # 3. existing abbreviations: concept/name and drafted descriptions
    rows = []
    n = 0
    for lab, e in existing.items():
        if e.kind != "abbr" or e.description:
            continue
        n += 1
        drafts = [(r["name"], r["entries"][lab].description if lab in r["entries"] else "") for r in runs]
        rnd.shuffle(drafts)
        rows.append([f"A{n}", e.short, e.long, "", drafts[0][1], drafts[1][1],
                     "" if drafts[0][1] else "-", "" if drafts[1][1] else "-"])
        key["abbreviations"][f"A{n}"] = {"label": lab, "A": drafts[0][0], "B": drafts[1][0],
                                         **{nm: bool(d) for nm, d in drafts}}
    write_tsv(out / "3-abbreviations.tsv",
              ["id", "abbr", "expansion", "LABEL concept or name? (c/n)", "A description", "B description",
               "LABEL A (ok/edit/wrong)", "LABEL B (ok/edit/wrong)"], rows)

    # 4. technical sense of single-word terms (same occurrences in both runs)
    occ = {}
    for r in runs:
        for d in r["decisions"]:
            occ.setdefault((d["file"], d["line"], d["label"]), {"text": d["text"]})[r["name"]] = d["technical"]
    rows = []
    for i, ((f, ln, lab), d) in enumerate(sorted(occ.items()), 1):
        term = existing[lab].name if lab in existing else lab
        rows.append([f"S{i}", term, f"{f}:{ln}", d["text"], ""])
        key["sense"][f"S{i}"] = {"label": lab, **{r["name"]: d.get(r["name"]) for r in runs}}
    write_tsv(out / "4-sense.tsv", ["id", "term", "where", "passage (occurrence in [[ ]])",
                                    "LABEL technical sense? (y/n)"], rows)

    (out / "key.json").write_text(json.dumps(key, indent=1, ensure_ascii=False))
    print(f"{len(keys)} terms, {len(key['descriptions'])} description pairs, "
          f"{len(key['abbreviations'])} abbreviations, {len(key['sense'])} occurrences -> {out}")


if __name__ == "__main__":
    main()
