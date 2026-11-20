#!/usr/bin/env python3
"""glosstool: build and apply the glossary of a LaTeX book, optionally with a local LLM.

Pipeline (each stage reads/writes a JSON state file, so it can be inspected and resumed):

  extract  Split the prose into small chunks and ask the LLM to raise candidate terms
           in each chunk (terms not literally present in the chunk are discarded).
           Heuristic extraction (defbox titles, \\emph near definition cues,
           "long form (ABBR)", frequent n-grams) can be added or used alone.
  filter   Deduplicate candidates (inflection, abbreviation vs. long form, existing
           entries, LLM check against similar entries), drop rare ones, let the LLM
           keep/reject the rest and draft descriptions.  Writes a .tex file with
           the proposed entries for human review.
  wrap     Replace plain occurrences of glossary entries (existing ones plus the
           reviewed new ones) with \\gls, \\Gls, \\glspl, \\Glspl.  Prints a diff
           unless --write is given; with --write the new entries are appended to
           the glossary file.  Ambiguous single-word terms can be checked
           occurrence by occurrence with the LLM.

LLM backends (--llm): 'ollama:MODEL' (or a bare model name), served by a local Ollama, or
'anthropic:MODEL' through the Anthropic API (needs the `anthropic` package and ANTHROPIC_API_KEY,
read from the environment or a .env file).  Otherwise only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import atexit
import difflib
import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# LaTeX masking: mark characters where a substitution must not happen and
# which must not feed the term extractor.
# --------------------------------------------------------------------------

VERBATIM_ENVS = r"verbatim|Verbatim|lstlisting|minted|comment|tikzpicture|tikzcd|axis|forest"
MATH_ENVS = r"equation|align|alignat|gather|multline|flalign|eqnarray|math|displaymath|dmath"

# Commands whose arguments (all consecutive [..] and {..} groups) are masked.
MASK_ARGS = set("""
label ref cref Cref eqref pageref autoref nameref vref hyperref
cite citet citep textcite parencite autocite footcite footfullcite citeauthor citeyear nocite
url href includegraphics input include subfile bibliography addbibresource
begin end
gls Gls GLS glspl Glspl GLSpl glsdisp glstext glslink glsplural glsfirst
glsxtrshort glsxtrlong glsxtrlongpl glsxtrfull glsfmtlong glsfmtshort glsfmttext
acrshort acrlong acrfull acrshortpl acrlongpl glsunset glsreset glsadd
newglossaryentry newabbreviation newacronym longnewglossaryentry
index texttt code lstinline mintinline path
hspace vspace setlength addtolength setcounter addcontentsline
usepackage RequirePackage documentclass newcommand renewcommand providecommand
newenvironment renewenvironment DeclareMathOperator def let
color textcolor colorbox pagecolor definecolor
resizebox scalebox rotatebox reflectbox raisebox makebox framebox parbox
multicolumn multirow cline cmidrule
""".split())
HEADINGS = set("part chapter section subsection subsubsection paragraph subparagraph".split())
CAPTIONS = set("caption captionof".split())


def _match_group(text: str, i: int, open_: str, close: str) -> int:
    """Given text[i] == open_, return the index just past the matching close."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == open_:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _skip_args(text: str, i: int) -> int:
    """Skip a star and any consecutive [..] / {..} groups (blanks allowed, no paragraph break)."""
    n = len(text)
    if i < n and text[i] == "*":
        i += 1
    while True:
        j = i
        while j < n and (text[j] in " \t" or (text[j] == "\n" and not text.startswith("\n", j + 1))):
            j += 1
        if j < n and text[j] == "{":
            i = _match_group(text, j, "{", "}")
        elif j < n and text[j] == "[":
            i = _match_group(text, j, "[", "]")
        else:
            return i


def compute_mask(text: str, skip_headings=True, skip_captions=True) -> bytearray:
    m = bytearray(len(text))

    def mark(a, b):
        m[a:b] = b"\x01" * (b - a)

    for mo in re.finditer(r"(?<!\\)%.*", text):
        mark(*mo.span())
    for envs in (VERBATIM_ENVS, MATH_ENVS):
        pat = r"\\begin\{(" + envs + r")(\*?)\}.*?\\end\{\1\2\}"
        for mo in re.finditer(pat, text, re.S):
            if not m[mo.start()]:
                mark(*mo.span())
    for mo in re.finditer(r"\\verb\*?(.).*?\1", text):
        mark(*mo.span())
    for pat in (r"\\\[.*?\\\]", r"\\\(.*?\\\)", r"(?<!\\)\$\$.*?(?<!\\)\$\$"):
        for mo in re.finditer(pat, text, re.S):
            if not m[mo.start()]:
                mark(*mo.span())
    i, n = 0, len(text)  # inline $...$
    while i < n:
        if text[i] == "$" and not m[i] and (i == 0 or text[i - 1] != "\\"):
            j = i + 1
            while j < n and not (text[j] == "$" and text[j - 1] != "\\"):
                j += 1
            mark(i, j + 1)
            i = j + 1
        else:
            i += 1
    masked_cmds = MASK_ARGS | (HEADINGS if skip_headings else set()) | (CAPTIONS if skip_captions else set())
    for mo in re.finditer(r"\\([A-Za-z@]+)", text):
        a = mo.start()
        if m[a]:
            continue
        if mo.group(1) in masked_cmds:
            mark(a, _skip_args(text, mo.end()))
        else:
            mark(a, mo.end())
    for mo in re.finditer(r"\\[^A-Za-z@]", text):  # \%, \\, \&, ...
        mark(*mo.span())
    return m


# --------------------------------------------------------------------------
# Project loading
# --------------------------------------------------------------------------

@dataclass
class Source:
    path: Path
    text: str
    mask: bytearray = field(repr=False, default=None)


def load_project(main: Path, skip_headings=True, skip_captions=True):
    """Return (body sources, glossary sources) following \\input/\\include."""
    seen, body, gloss = set(), [], []

    def visit(p: Path):
        if not p.suffix:
            p = p.with_suffix(".tex")
        p = p.resolve()
        if p in seen or not p.exists():
            return
        seen.add(p)
        text = p.read_text(encoding="utf-8")
        nocomment = re.sub(r"(?<!\\)%.*", "", text)
        is_gloss = re.search(r"\\(newglossaryentry|newabbreviation|newacronym)\b", nocomment)
        src = Source(p, text, compute_mask(text, skip_headings, skip_captions))
        if is_gloss and p != main.resolve():
            gloss.append(src)
        else:
            body.append(src)
        for mo in re.finditer(r"\\(?:input|include|subfile)\{([^}]+)\}", nocomment):
            visit(main.parent / mo.group(1))

    visit(main)
    return body, gloss


def rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------
# Glossary entries
# --------------------------------------------------------------------------

@dataclass
class Entry:
    label: str
    kind: str  # "term" or "abbr"
    name: str = ""
    plural: str = ""
    short: str = ""
    long: str = ""
    description: str = ""
    category: str = ""

    @property
    def display(self) -> str:
        return self.short if self.kind == "abbr" else self.name


def _strip_braces(s: str) -> str:
    s = s.strip()
    while s.startswith("{") and _match_group(s, 0, "{", "}") == len(s):
        s = s[1:-1].strip()
    return s


def _split_keyvals(s: str) -> dict:
    out, depth, cur, parts = {}, 0, [], []
    for c in s:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k.strip()] = re.sub(r"\s+", " ", _strip_braces(v))
    return out


def parse_entries(text: str) -> list[Entry]:
    text = re.sub(r"(?<!\\)%.*", "", text)
    entries = []
    for mo in re.finditer(r"\\(newglossaryentry|longnewglossaryentry|newabbreviation|newacronym)\s*(\[[^\]]*\])?\s*\{",
                          text):
        groups, i = [], mo.end() - 1
        nargs = 3 if mo.group(1) in ("newabbreviation", "newacronym", "longnewglossaryentry") else 2
        while len(groups) < nargs:
            while text[i].isspace():
                i += 1
            j = _match_group(text, i, "{", "}")
            groups.append(text[i + 1:j - 1])
            i = j
        if mo.group(1) in ("newabbreviation", "newacronym"):
            label, short, long = (g.strip() for g in groups)
            kv = _split_keyvals(mo.group(2)[1:-1]) if mo.group(2) else {}
            entries.append(Entry(label, "abbr", short=short, long=long, plural=kv.get("shortplural", ""),
                                 description=kv.get("description", ""), category=kv.get("category", "")))
        else:
            label = groups[0].strip()
            kv = _split_keyvals(groups[1])
            desc = kv.get("description", "") or (re.sub(r"\s+", " ", groups[2]).strip() if nargs == 3 else "")
            entries.append(Entry(label, "term", name=kv.get("name", label), plural=kv.get("plural", ""),
                                 description=desc))
    return entries


DESCRIBED = "described"  # category of abbreviations that carry a description
DESCRIBED_STYLE = f"\\setabbreviationstyle[{DESCRIBED}]{{long-short-desc}}"


def entry_to_tex(e: Entry) -> str:
    if e.kind == "abbr":
        opts = []
        if e.description:
            opts.append(f"category={e.category or DESCRIBED}")
        elif e.category:
            opts.append(f"category={e.category}")
        if e.plural:
            opts.append(f"shortplural={{{e.plural}}}")
        if e.description:
            opts.append(f"description={{{e.description}}}")
        if not opts:
            return f"\\newabbreviation{{{e.label}}}{{{e.short}}}{{{e.long}}}\n"
        return (f"\\newabbreviation[%\n  " + ",\n  ".join(opts) +
                f"]{{{e.label}}}{{{e.short}}}{{{e.long}}}\n")
    plural = f"  plural={{{e.plural}}},\n" if e.plural else ""
    return (f"\\newglossaryentry{{{e.label}}}{{%\n  name={{{e.name}}},\n{plural}"
            f"  description={{{e.description}}}\n}}\n")


def entry_span(text: str, label: str):
    """Span of the (uncommented) definition of `label` in a glossary file, or None."""
    rx = re.compile(r"\\(newglossaryentry|longnewglossaryentry|newabbreviation|newacronym)\s*(\[)?")
    for mo in rx.finditer(text):
        line_start = text.rfind("\n", 0, mo.start()) + 1
        if re.search(r"(?<!\\)%", text[line_start:mo.start()]):
            continue
        i = _match_group(text, mo.end() - 1, "[", "]") if mo.group(2) else mo.end()
        while text[i].isspace():
            i += 1
        j = _match_group(text, i, "{", "}")
        if text[i + 1:j - 1].strip() != label:
            continue
        nargs = 1 if mo.group(1) == "newglossaryentry" else 2
        for _ in range(nargs):
            while text[j].isspace():
                j += 1
            j = _match_group(text, j, "{", "}")
        if j < len(text) and text[j] == "\n":
            j += 1
        return mo.start(), j
    return None


def default_plural(word: str) -> str:
    if re.search(r"[^aeiou]y$", word):
        return word[:-1] + "ies"
    if re.search(r"(s|x|z|ch|sh)$", word):
        return word + "es"
    return word + "s"


def make_label(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower()) or "entry"


def tex_escape(s: str) -> str:
    return re.sub(r"(?<!\\)([%&#_])", r"\\\1", s)


STOP = set("""
a an the this that these those there here its it is are was were be been being of in on at to for
from by with as and or but not no nor so than then thus also only very more most less least such
same other others another any all each every some many much few several both either neither
we our us you your they their them he she his her i my one two three first second third
can could may might must should would will shall do does did done have has had having
if when where which who whom whose what why how while whereas because since although though
into onto over under about above below between among through during before after up down out
off again further once just however therefore hence moreover e g i e eg ie etc vs via per
use used using uses based given new well like often usually always never even still yet
let way ways case cases kind type types example examples number numbers set sets value values
part parts point points result results fact order terms term thing things lot section chapter
figure table book see following follows called known
""".split())


def norm_key(phrase: str) -> str:
    """Lowercase, collapse blanks, singularize the last word."""
    words = re.sub(r"[^\w\s/-]", "", phrase.lower()).split()
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith(("ss", "is", "us", "ias", "ics")):
        w = words[-1]
        words[-1] = w[:-3] + "y" if w.endswith("ies") else w[:-1]
    return " ".join(words)


def is_abbr(term: str) -> bool:
    t = term[:-1] if term.endswith("s") else term
    return " " not in t and sum(c.isupper() for c in t) >= 2 and bool(re.fullmatch(r"[A-Za-z0-9/&-]+", t))


def abbr_key(term: str) -> str:
    t = term[:-1] if term.endswith("s") and term[:-1].upper() == term[:-1] else term
    return "abbr:" + t


def content_words(phrase: str) -> set[str]:
    return {w for w in norm_key(phrase).split() if w not in STOP and len(w) > 2}


# --------------------------------------------------------------------------
# LLM (Ollama)
# --------------------------------------------------------------------------

def load_dotenv(path: Path = Path(".env")):
    """Minimal .env reader: KEY=VALUE lines; does not override variables already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            os.environ.setdefault(k, v.strip().strip("'\""))


def strict_schema(schema):
    """Structured outputs require additionalProperties: false on every object."""
    if isinstance(schema, dict):
        out = {k: strict_schema(v) for k, v in schema.items()}
        if out.get("type") == "object":
            out["additionalProperties"] = False
        return out
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    return schema


# USD per million tokens (input, output), for the cost estimate printed at the end.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}


class LLMError(Exception):
    pass


def LLM(spec: str, host: str, cache_path: Path, num_ctx: int = 4096, effort: str = "low"):
    """Backend from a spec: 'anthropic:MODEL', 'ollama:MODEL', or a bare Ollama model name."""
    backend, _, model = spec.partition(":") if spec.startswith(("anthropic:", "ollama:")) else ("ollama", "", spec)
    if backend == "anthropic":
        return AnthropicLLM(model, cache_path, effort)
    return OllamaLLM(model, host, cache_path, num_ctx)


class CachedLLM:
    def __init__(self, spec: str, cache_path: Path):
        self.spec, self.cache_path = spec, cache_path
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        self.calls = self.in_tok = self.out_tok = 0
        atexit.register(self.report)

    def ask(self, system: str, user: str, schema: dict) -> dict:
        key = hashlib.sha1(json.dumps([self.spec, system, user, schema]).encode()).hexdigest()
        if key in self.cache:
            return self.cache[key]
        out = self._ask(system, user, schema)
        self.calls += 1
        self.cache[key] = out
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, indent=1))
        tmp.replace(self.cache_path)
        return out

    def report(self):
        if not self.calls:
            return
        msg = f"{self.spec}: {self.calls} uncached calls, {self.in_tok} input / {self.out_tok} output tokens"
        model = self.spec.split(":", 1)[-1]
        if model in PRICES:
            pi, po = PRICES[model]
            msg += f", about ${(self.in_tok * pi + self.out_tok * po) / 1e6:.2f}"
        print(msg, file=sys.stderr)


class AnthropicLLM(CachedLLM):
    def __init__(self, model: str, cache_path: Path, effort: str):
        super().__init__(f"anthropic:{model}@{effort}", cache_path)
        try:
            import anthropic
        except ImportError:
            sys.exit("the anthropic backend needs the SDK: pip install anthropic (e.g. in tools/.venv)")
        load_dotenv()
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE")):
            print("note: no ANTHROPIC_API_KEY in the environment or .env; trying other SDK credentials",
                  file=sys.stderr)
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model, self.effort = model, effort

    def _ask(self, system, user, schema):
        a = self.anthropic
        try:
            # fallbacks="default": if a safety classifier declines, the API re-runs the request on
            # Anthropic's recommended fallback model instead of returning a refusal.
            r = self.client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": strict_schema(schema)}},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        except (a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError) as e:
            sys.exit(f"Anthropic API: {e}")  # not retryable: stop the run
        except TypeError as e:  # raised by the SDK when no credential can be resolved
            if "authentication" in str(e).lower():
                sys.exit("Anthropic API: no credentials; put ANTHROPIC_API_KEY=... in .env")
            raise
        except (a.APIStatusError, a.APIConnectionError) as e:  # the SDK already retried 429/5xx
            raise LLMError(str(e)) from e
        self.in_tok += r.usage.input_tokens
        self.out_tok += r.usage.output_tokens
        if r.stop_reason == "refusal":
            raise LLMError(f"refusal ({r.stop_details.category if r.stop_details else '?'})")
        if r.stop_reason == "max_tokens":
            raise LLMError("hit max_tokens")
        text = next((b.text for b in r.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text block in response")
        return json.loads(text)


class OllamaLLM(CachedLLM):
    def __init__(self, model: str, host: str, cache_path: Path, num_ctx: int = 4096):
        super().__init__(model, cache_path)  # bare name keeps cache keys of earlier runs
        self.model, self.host, self.num_ctx = model, host.rstrip("/"), num_ctx
        self.think_ok = True

    def _ask(self, system: str, user: str, schema: dict) -> dict:
        body = {"model": self.model, "stream": False, "format": schema,
                "options": {"temperature": 0, "num_ctx": self.num_ctx},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if self.think_ok:
            body["think"] = False
        try:
            res = self._post(body)
        except urllib.error.HTTPError as e:
            if e.code == 400 and "think" in body:  # model without thinking support
                self.think_ok = False
                body.pop("think")
                res = self._post(body)
            else:
                raise
        self.in_tok += res.get("prompt_eval_count", 0)
        self.out_tok += res.get("eval_count", 0)
        return json.loads(res["message"]["content"])

    def _post(self, body):
        req = urllib.request.Request(self.host + "/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read())


LLM_ERRORS = (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError, LLMError)


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

def line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def sentence_around(text: str, a: int, b: int, width=220) -> str:
    left = text[max(0, a - width):a]
    right = text[b:min(len(text), b + width)]
    cut = max(left.rfind(". "), left.rfind("\n\n"))
    if cut >= 0:
        left = left[cut + 2:]
    cuts = [x for x in (right.find(". "), right.find("\n\n")) if x >= 0]
    if cuts:
        right = right[:min(cuts) + 1]
    s = left + "[[" + text[a:b] + "]]" + right
    return re.sub(r"\s+", " ", re.sub(r"(?<!\\)%.*", "", s)).strip()


def render_gls(text: str, by_label: dict[str, Entry]) -> str:
    """Replace \\gls-family commands with the displayed text (for the LLM's benefit)."""
    def sub(mo):
        cmd, lab = mo.group(1), mo.group(2)
        e = by_label.get(lab)
        if e is None:
            return lab
        if "long" in cmd:
            s = e.long
        else:
            s = e.display
            if cmd.endswith("pl"):
                s = (e.plural or (s + "s" if e.kind == "abbr" else default_plural(s)))
        return s[:1].upper() + s[1:] if cmd[0] in "G" else s
    return re.sub(r"\\(gls|Gls|glspl|Glspl|glsxtrshort|glsxtrlong|glsxtrlongpl|glsfmtlong)\{([^}]+)\}", sub, text)


def flex_pattern(phrase: str, ci_first=True) -> str:
    """Regex for a phrase allowing any blank/~ between words and a case-insensitive first letter."""
    parts = []
    for i, w in enumerate(phrase.split()):
        esc = re.escape(w)
        if i == 0 and ci_first and w[0].isalpha():
            esc = f"[{w[0].lower()}{w[0].upper()}]" + re.escape(w[1:])
        parts.append(esc)
    return r"(?:\s|~)+".join(parts)


# --------------------------------------------------------------------------
# Candidates and the JSON state file
# --------------------------------------------------------------------------

@dataclass
class Cand:
    key: str
    name: str
    kind: str = "term"
    long: str = ""
    # every place the term was raised: {file, line, text, meaning, source, weight, surface}
    mentions: list = field(default_factory=list)
    status: str = "pending"  # pending, keep, reject, existing, merged, rare
    label: str = ""          # existing label (status existing) or new label (keep)
    merged_into: str = ""
    count: int = 0   # occurrences in the book (filter)
    df: int = 0      # chunks containing it (filter)
    ndocs: int = 0   # total chunks (filter)
    relevance: int = 0  # LLM rating 1-5 (filter); 0 = not rated
    description: str = ""
    reason: str = ""

    @property
    def score(self) -> float:
        return sum(m["weight"] for m in self.mentions)

    @property
    def sources(self) -> Counter:
        return Counter(m["source"] for m in self.mentions)

    def contexts(self, k: int) -> list[dict]:
        """Up to k mentions, strongest first, preferring distinct files."""
        ms = sorted(self.mentions, key=lambda m: (-m["weight"], not DEF_CUES.search(m["text"])))
        out, files = [], set()
        for m in ms:
            if m["file"] not in files:
                out.append(m)
                files.add(m["file"])
        out += [m for m in ms if m not in out]
        return out[:k]

    def add_mention(self, m: dict):
        if not any(x["file"] == m["file"] and x["line"] == m["line"] and x["source"] == m["source"]
                   for x in self.mentions):
            self.mentions.append(m)

    def to_json(self):
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, d):
        return cls(**d)


class CandSet:
    def __init__(self):
        self.c: dict[str, Cand] = {}

    def add(self, phrase, kind, f, ln, sent, source, weight, long="", meaning=""):
        phrase = re.sub(r"\s+", " ", phrase).strip()
        if kind == "abbr":
            key = abbr_key(phrase)
            name = key[5:]
        else:
            key = norm_key(phrase)
            name = key
        if not key or key == "abbr:":
            return
        c = self.c.get(key)
        if c is None:
            c = self.c[key] = Cand(key, name, kind, long)
        if long and not c.long:
            c.long = long
        c.add_mention({"file": f, "line": ln, "text": sent, "meaning": meaning, "source": source,
                       "weight": weight, "surface": phrase})

    def drop(self, pred):
        """Remove mentions matching pred(mention); drop candidates left without mentions."""
        for k in list(self.c):
            c = self.c[k]
            c.mentions = [m for m in c.mentions if not pred(m)]
            if not c.mentions:
                del self.c[k]

    def save(self, path: Path, meta: dict):
        data = {"meta": meta, "candidates": [c.to_json() for c in sorted(self.c.values(), key=lambda c: c.key)]}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        s = cls()
        for d in data["candidates"]:
            c = Cand.from_json(d)
            s.c[c.key] = c
        return s, data.get("meta", {})


# --------------------------------------------------------------------------
# Heuristic extraction
# --------------------------------------------------------------------------

DEF_CUES = re.compile(r"\b(called|known as|refer(?:s|red)? to|defined? as|we define|we call|denote[sd]?|"
                      r"is termed|so-called|definition|consists? of|means)\b", re.I)


def match_initials(words: list[str], letters: list[str]) -> str:
    """Pick the trailing words whose initials spell `letters` (skipping function words)."""
    got, chosen = [], []
    for w in reversed(words):
        chosen.insert(0, w)
        if w.lower() in ("of", "and", "the", "for", "in", "to", "a", "on"):
            continue
        got = [p[0].upper() for p in w.split("-") if p] + got
        if len(got) >= len(letters):
            break
    return " ".join(chosen) if got == letters else ""


def heuristic_candidates(cs: CandSet, body: list[Source], root: Path, min_freq: int):
    ngrams = defaultdict(list)
    for src in body:
        t, m, f = src.text, src.mask, rel(src.path, root)

        def add(phrase, kind, a, b, source, w, long=""):
            cs.add(phrase, kind, f, line_of(t, a), sentence_around(t, a, b), source, w, long)

        for mo in re.finditer(r"\\begin\{defbox\}\{([^{}]+)\}", t):
            add(mo.group(1).lower(), "term", mo.start(1), mo.end(1), "defbox", 5)
        for mo in re.finditer(r"\\(emph|textbf|textit)\{([^{}\\$]{3,60})\}", t):
            phrase = mo.group(2)
            if m[mo.start(2)] or len(phrase.split()) > 5 or not re.fullmatch(r"[A-Za-z][A-Za-z -]*", phrase):
                continue
            if phrase.lower() not in STOP and DEF_CUES.search(t[max(0, mo.start() - 120):mo.end() + 120]):
                add(phrase, "abbr" if is_abbr(phrase) else "term", mo.start(2), mo.end(2), "emph-def", 3)
        for mo in re.finditer(r"\(([A-Z][A-Za-z/&-]*[A-Z])s?\)", t):
            if m[mo.start(1)]:
                continue
            letters = [c for c in mo.group(1) if c.isupper()]
            before = re.sub(r"\\[A-Za-z]+\{?|[{}]", " ", t[max(0, mo.start() - 200):mo.start()])
            long = match_initials(re.findall(r"[A-Za-z][\w'-]*", before), letters)
            add(mo.group(1), "abbr", mo.start(1), mo.end(1), "abbr-def", 4 if long else 2, long.lower())
        prose = "".join(c if not mk else "\x01" for c, mk in zip(t, m))
        for chunk_mo in re.finditer(r"[^\x01.,;:!?(){}\[\]\"`'\n]+(?:\n[^\x01.,;:!?(){}\[\]\"`'\n]+)*", prose):
            toks = [(w.group(0), chunk_mo.start() + w.start(), chunk_mo.start() + w.end())
                    for w in re.finditer(r"[A-Za-z][A-Za-z-]*[A-Za-z]", chunk_mo.group(0))]
            for n in (2, 3):
                for k in range(len(toks) - n + 1):
                    seq = toks[k:k + n]
                    ws = [w.lower() for w, _, _ in seq]
                    if ws[0] in STOP or ws[-1] in STOP or any(w in STOP and w != "of" for w in ws[1:-1]):
                        continue
                    if any(len(w) < 3 for w in ws if w != "of"):
                        continue
                    ngrams[norm_key(" ".join(ws))].append((f, t, seq[0][1], seq[-1][2]))
    for key, occ in ngrams.items():
        files = {o[0] for o in occ}
        if len(occ) < min_freq or len(files) < 2:
            continue
        w = min(3.0, math.log2(len(occ))) + 0.5 * min(4, len(files) - 1)
        for i, (f, t, a, b) in enumerate(occ[:4]):
            cs.add(key, "term", f, line_of(t, a), sentence_around(t, a, b), "ngram", w if i == 0 else 0)


# --------------------------------------------------------------------------
# extract (LLM over chunks)
# --------------------------------------------------------------------------

EXTRACT_SYSTEM = ("You extract glossary candidates from passages of a technical textbook written in LaTeX. "
                  "Answer with JSON following the given schema.")

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {
        "type": "object",
        "properties": {"term": {"type": "string"}, "expansion": {"type": "string"},
                       "meaning": {"type": "string"}},
        "required": ["term", "expansion", "meaning"]}}},
    "required": ["terms"],
}

EXTRACT_PROMPT = """Passage:
\"\"\"
{chunk}
\"\"\"

List the technical terms in the passage that a reader of the book might need to look up in a
glossary: domain concepts, methods, principles, metrics, and abbreviations.
- Copy each term exactly as it is written in the passage.
- Prefer complete noun phrases ("empirical risk", not "empirical").
- Do not list mathematical symbols, people, citations, software names, or everyday words.
- For an abbreviation, put its expansion in "expansion" if the passage spells it out,
  otherwise "".  For other terms, "expansion" is "".
- In "meaning", explain in at most 12 words what the term means *as used in this passage*
  (the same word can mean different things in different parts of a book).
- If there are no such terms, return an empty list."""


def chunks_of(src: Source, words: int):
    """Yield (start, end) of paragraph-aligned chunks of about `words` words."""
    paras = [0] + [mo.end() for mo in re.finditer(r"\n[ \t]*\n", src.text)] + [len(src.text)]
    a = 0
    for i in range(1, len(paras)):
        b = paras[i]
        if len(src.text[a:b].split()) >= words or b == len(src.text):
            yield a, b
            a = b


def locate(term: str, text: str):
    """Find a term in text (case-insensitive first letter, flexible blanks). Return span or None."""
    rx = re.compile(r"(?<![\w])" + flex_pattern(term) + r"(?![\w])", 0 if is_abbr(term) else re.I)
    mo = rx.search(text)
    return mo.span() if mo else None


def cmd_extract(args):
    main = Path(args.main)
    root = main.parent.resolve()
    body, gloss = load_project(main)
    entries = [e for g in gloss for e in parse_entries(g.text)]
    by_label = {e.label: e for e in entries}
    out = Path(args.state)
    cs, meta = CandSet.load(out) if out.exists() and not args.fresh else (CandSet(), {})
    meta.update({"main": str(main)})
    if args.llm:
        meta["model"] = args.llm

    def checkpoint(msg):
        cs.save(out, meta)
        report = out.with_suffix(".txt")
        report.write_text(inspect_report(cs, entries), encoding="utf-8")
        print(f"{msg}; {len(cs.c)} candidates in {out}, report in {report}", file=sys.stderr)

    if args.heuristics:
        cs.drop(lambda m: m["source"] != "llm")
        heuristic_candidates(cs, body, root, args.min_freq)
        checkpoint("heuristics done")
    if args.llm:
        llm = LLM(args.llm, args.host, Path(args.cache), args.num_ctx, args.effort)
        todo = []
        for src in body:
            if args.files and rel(src.path, root) not in args.files:
                continue
            for a, b in chunks_of(src, args.chunk_words):
                prose_words = len("".join(c for c, mk in zip(src.text[a:b], src.mask[a:b]) if not mk).split())
                if prose_words >= 15:
                    todo.append((src, a, b))
        if args.limit:
            todo = todo[:args.limit]
        done_files = set()
        for i, (src, a, b) in enumerate(todo):
            f = rel(src.path, root)
            if f not in done_files:  # (re)extracting a file replaces its previous LLM mentions
                if done_files:
                    checkpoint(f"finished {prev}")
                cs.drop(lambda m: m["source"] == "llm" and m["file"] == f)
                done_files.add(f)
                prev = f
            chunk = render_gls(re.sub(r"(?<!\\)%.*", "", src.text[a:b]), by_label).strip()
            print(f"[{i + 1}/{len(todo)}] {f}:{line_of(src.text, a)}", file=sys.stderr, end=" ", flush=True)
            try:
                res = llm.ask(EXTRACT_SYSTEM, EXTRACT_PROMPT.format(chunk=chunk), EXTRACT_SCHEMA)
            except LLM_ERRORS as e:
                print(f"LLM error: {e}", file=sys.stderr)
                continue
            found = []
            for item in res.get("terms", []):
                term = re.sub(r"\s+", " ", item.get("term", "")).strip(" .,;:")
                if not term or len(term) > 60 or "\\" in term or "$" in term:
                    continue
                span = locate(term, chunk)
                if span is None:  # hallucinated or paraphrased
                    continue
                kind = "abbr" if is_abbr(term) else "term"
                exp = item.get("expansion", "").strip()
                long = exp.lower() if kind == "abbr" and exp and locate(exp, chunk) else ""
                ln = line_of(src.text, a) + chunk.count("\n", 0, span[0])
                meaning = re.sub(r"\s+", " ", item.get("meaning", "")).strip()
                cs.add(term, kind, f, ln, sentence_around(chunk, *span), "llm", 1 + (2 if long else 0), long,
                       meaning)
                found.append(term)
            print(f"-> {', '.join(found)}", file=sys.stderr)
        if done_files:
            checkpoint(f"finished {prev}")


def inspect_report(cs: CandSet, entries: list[Entry], files=None, show_text=False, min_mentions=1,
                   sort="name") -> str:
    """Candidates grouped by normalized name; one line per mention with its meaning."""
    known = {}
    for e in entries:
        if e.kind == "abbr":
            known["abbr:" + e.short] = known[norm_key(e.long)] = e.label
        else:
            known[norm_key(e.name)] = e.label
            if e.plural:
                known[norm_key(e.plural)] = e.label

    def name_key(c):  # abbreviations sort among terms by their short form
        return (c.key[5:].lower() if c.kind == "abbr" else c.key), c.key

    sort_keys = {
        "name": name_key,
        "mentions": lambda c: (-len(c.mentions), name_key(c)),
        "df": lambda c: (-(c.df / c.ndocs if c.ndocs else 0), name_key(c)),
        "ridf": lambda c: (-ridf(c.count, c.df, c.ndocs), name_key(c)),
    }
    lines = []
    for c in sorted(cs.c.values(), key=sort_keys[sort]):
        ms = [m for m in c.mentions if not files or m["file"] in files]
        if len(ms) < min_mentions:
            continue
        nfiles = len({m["file"] for m in ms})
        head = c.name + (f" ({c.long})" if c.long else "")
        tags = [f"{len(ms)} mentions", f"{nfiles} files"]
        if c.key in known or (c.long and norm_key(c.long) in known):
            tags.append(f"EXISTING: {known.get(c.key) or known.get(norm_key(c.long))}")
        if c.ndocs:
            tags.append(f"tf={c.count} df={c.df}/{c.ndocs} ridf={ridf(c.count, c.df, c.ndocs):.2f}")
        if c.status != "pending":
            tags.append(c.status.upper())
        lines.append(f"{head}  [{', '.join(tags)}]")
        for m in sorted(ms, key=lambda m: (m["meaning"].lower(), m["file"], m["line"])):
            where = f'{m["file"]}:{m["line"]}'
            meaning = m["meaning"] or f'({m["source"]})'
            lines.append(f"    {where:38} {meaning}")
            if show_text:
                lines.append(f"    {'':38} > {m['text'][:200]}")
        lines.append("")
    return "\n".join(lines)


def cmd_contexts(args):
    """Print each entry's description next to passages where the book uses it (for verification)."""
    main = Path(args.main)
    root = main.parent.resolve()
    body, gloss = load_project(main)
    entries = {e.label: e for g in gloss for e in parse_entries(g.text)}
    selected = []
    if args.entries:  # reviewed new/updated entries take precedence over the glossary
        for e in parse_entries(Path(args.entries).read_text(encoding="utf-8")):
            entries[e.label] = e
            selected.append(e.label)
    if args.labels:
        selected = args.labels.split(",")
    elif not selected:
        selected = [lab for lab, e in entries.items() if e.description]
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        out = open(args.output, "w", encoding="utf-8")
    else:
        out = sys.stdout

    def print(*a):  # noqa: A001 - write sections to the chosen output
        out.write(" ".join(str(x) for x in a) + "\n")

    for lab in selected:
        e = entries.get(lab)
        if e is None:
            print(f"## {lab}\n(not found)\n")
            continue
        pats = [re.compile(r"\\[gG]ls[a-z]*\{" + re.escape(lab) + r"\}")] + \
               [rx for rx, _, _ in entry_patterns(e)]
        found, seen = [], set()
        for src in body:
            f = rel(src.path, root)
            for rx in pats:
                for mo in rx.finditer(src.text):
                    a, b = mo.span()
                    if not mo.group(0).startswith("\\") and any(src.mask[a:b]):
                        continue
                    ln = line_of(src.text, a)
                    if (f, ln) not in seen:
                        seen.add((f, ln))
                        found.append((f, ln, sentence_around(src.text, a, b)))
        # spread over files: round-robin by file, in book order
        by_file = defaultdict(list)
        for item in found:
            by_file[item[0]].append(item)
        picked = []
        while len(picked) < args.k and any(by_file.values()):
            for f in list(by_file):
                if by_file[f] and len(picked) < args.k:
                    picked.append(by_file[f].pop(0))
        head = f"{e.short} ({e.long})" if e.kind == "abbr" else e.name
        print(f"## {lab}: {head}\nDescription: {e.description or '(none)'}\n"
              f"Usages ({len(found)} in total, {len(picked)} shown):")
        for f, ln, s in picked:
            print(f"- {f}:{ln}: {s[:400]}")
        print()
    if args.output:
        out.close()
        sys.stderr.write(f"wrote {len(selected)} entries to {args.output}\n")


def cmd_inspect(args):
    main = Path(args.main)
    _, gloss = load_project(main)
    entries = [e for g in gloss for e in parse_entries(g.text)]
    cs, _ = CandSet.load(Path(args.state))
    if args.stats or args.sort in ("df", "ridf"):  # frequency statistics over the whole book
        body, _ = load_project(main)
        docs = [(src, a, b) for src in body for a, b in chunks_of(src, args.doc_words)]
        for c in cs.c.values():
            c.count, c.df, c.ndocs = term_stats(docs, cand_entry(c))
    sys.stdout.write(inspect_report(cs, entries, set(args.files or []), args.text, args.min_mentions, args.sort))


# --------------------------------------------------------------------------
# filter
# --------------------------------------------------------------------------

DUP_SCHEMA = {"type": "object", "properties": {"duplicate_of": {"type": "integer"}, "reason": {"type": "string"}},
              "required": ["duplicate_of", "reason"]}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"relevance": {"type": "integer"}, "reason": {"type": "string"}, "name": {"type": "string"},
                   "description": {"type": "string"}},
    "required": ["relevance", "reason", "name", "description"],
}

REVIEW_SYSTEM = ("You help the author of a technical textbook build its glossary. "
                 "Answer with JSON following the schema.")


def cand_desc(c: Cand) -> str:
    return f'"{c.name}"' + (f' (abbreviation of "{c.long}")' if c.long else "")


def term_stats(docs: list, e: Entry) -> tuple[int, int, int]:
    """Return (term frequency, document frequency, number of documents); docs are (src, a, b)."""
    tf = df = 0
    pats = [rx for rx, _, mode in entry_patterns(e) if mode != "long"] + \
        [re.compile(r"\\[gG]ls(?:pl)?\{" + re.escape(e.label) + r"\}")]
    for src, a, b in docs:
        n = sum(1 for rx in pats for mo in rx.finditer(src.text, a, b)
                if not any(src.mask[mo.start():mo.end()]) or mo.group(0).startswith("\\"))
        tf += n
        df += n > 0
    return tf, df, len(docs)


def ridf(tf: int, df: int, n: int) -> float:
    """Residual IDF (Church & Gale 1995): observed IDF minus the IDF a Poisson model predicts."""
    if not df or not n:
        return 0.0
    return -math.log2(df / n) + math.log2(1 - math.exp(-tf / n))


def cand_entry(c: Cand) -> Entry:
    if c.kind == "abbr":
        return Entry("__cand__", "abbr", short=c.name, long=c.long or "\x00")
    return Entry("__cand__", "term", name=c.name)


ABBR_SCHEMA = {
    "type": "object",
    "properties": {"is_abbreviation": {"type": "boolean"}, "long": {"type": "string"},
                   "concept": {"type": "boolean"}, "description": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["is_abbreviation", "long", "concept", "description", "reason"],
}


def guide_text(style: str, headings: list[str]) -> str:
    """The given '## ' sections of the book's style guide, formatted for a prompt ("" if none)."""
    parts = []
    for h in headings:
        mo = re.search(r"^## " + re.escape(h) + r"\s*\n(.*?)(?=^## |\Z)", style, re.M | re.S)
        if mo and mo.group(1).strip():
            parts.append(f"### {h}\n{mo.group(1).strip()}")
    if not parts:
        return ""
    return ("\n\nThe author's guide for this book's glossary (follow it; it overrides the generic "
            "criteria below):\n" + "\n\n".join(parts) + "\n")


def abbr_review(llm: LLM, short: str, long: str, ctx: str, examples: str, title: str, found: bool) -> dict:
    exp = (f'Its expansion, as written in the book: "{long}".' if found and long
           else "The book does not spell out its expansion.")
    user = f"""Book: {title}

Candidate abbreviation: "{short}".  {exp}
Occurrences (marked with [[ ]]):
{ctx}

Existing glossary entries, for style:
{examples}

Answer:
- is_abbreviation: true if "{short}" is an abbreviation or acronym as used in these passages;
  false for variable names, symbols, ordinary words written in capitals, roman numerals, units.
- long: its expansion as meant in this book, lowercase unless it is a proper noun.
- concept: true if it names a technical concept, method, model, metric, principle or theory that a
  reader would look up (e.g. support vector machine, empirical risk minimization); false if it
  names an organization, product, programming language, file format, standard or other proper name.
- description: if concept is true, a single short sentence of at most 20 words, in the style of
  the existing entries, faithful to how this book uses it; prefer the essential definition over
  elaboration; do not start with the term itself, no LaTeX.  Otherwise "".
- reason: a short justification."""
    try:
        return llm.ask(REVIEW_SYSTEM, user, ABBR_SCHEMA)
    except LLM_ERRORS as e:
        print(f"LLM error: {e}", file=sys.stderr)
        return {}


def unique_label(lab: str, used: set) -> str:
    base, n = lab, 2
    while lab in used:
        lab, n = f"{base}{n}", n + 1
    used.add(lab)
    return lab


def entry_contexts(body: list[Source], root: Path, e: Entry, k: int) -> list:
    """Up to k (file, line, sentence) where an existing entry is used, preferring distinct files."""
    rx = re.compile(r"\\[gG]ls(?:pl)?\{" + re.escape(e.label) + r"\}")
    found = []
    for src in body:
        for mo in rx.finditer(src.text):
            text = sentence_around(src.text, mo.start(), mo.end())
            found.append((rel(src.path, root), line_of(src.text, mo.start()), text))
            break  # one per file first
    return found[:k]


def cmd_filter(args):
    main = Path(args.main)
    root = main.parent.resolve()
    state = Path(args.state)
    cs, meta = CandSet.load(state)
    body, gloss = load_project(main)
    entries = [e for g in gloss for e in parse_entries(g.text)]
    llm = LLM(args.llm, args.host, Path(args.cache), args.num_ctx, args.effort) if args.llm else None
    title_mo = re.search(r"\\title\{([^}]*)\}", body[0].text) if body else None
    title = title_mo.group(1) if title_mo else main.stem
    cands = list(cs.c.values())
    for c in cands:  # filter is re-runnable: start from scratch
        c.status, c.label, c.merged_into, c.reason = "pending", "", "", ""

    # 1. deterministic deduplication
    known = {}
    for e in entries:
        if e.kind == "abbr":
            known["abbr:" + e.short] = e.label
            known[norm_key(e.long)] = e.label
        else:
            known[norm_key(e.name)] = e.label
            if e.plural:
                known[norm_key(e.plural)] = e.label
    long_of = {norm_key(c.long): c for c in cands if c.kind == "abbr" and c.long}
    for c in cands:
        hit = known.get(c.key) or (known.get(norm_key(c.long)) if c.long else None)
        if hit:
            c.status, c.label, c.reason = "existing", hit, "same name as existing entry"
        elif c.kind == "term" and c.key in long_of:
            a = long_of[c.key]
            c.status, c.merged_into, c.reason = "merged", a.key, "long form of abbreviation"

    # 2. frequency statistics over the whole book (documents = chunks of --doc-words words)
    docs = [(src, a, b) for src in body for a, b in chunks_of(src, args.doc_words)]
    for c in cands:
        if c.status != "pending":
            continue
        c.count, c.df, c.ndocs = term_stats(docs, cand_entry(c))
        if c.kind == "abbr":  # every abbreviation the reader meets must be defined: no frequency cuts
            continue
        strong = c.sources.get("defbox") or c.sources.get("emph-def")
        if c.count < args.min_count and not strong:
            c.status, c.reason = "rare", f"{c.count} occurrence(s)"
        elif args.max_df and c.df / c.ndocs > args.max_df:
            c.status, c.reason = "pervasive", f"in {c.df}/{c.ndocs} chunks"
    # fragments that (almost) only occur inside a longer candidate: "handling operation" in
    # "data handling operation"
    terms = [c for c in cands if c.kind == "term" and c.status == "pending"]
    for c in terms:
        if c.sources.get("defbox") or c.sources.get("emph-def"):
            continue
        for d in terms:
            if d is not c and d.status == "pending" and f" {c.key} " in f" {d.key} " and d.count >= 0.8 * c.count:
                c.status, c.merged_into, c.reason = "merged", d.key, f"fragment of '{d.key}'"
                break

    pending = sorted((c for c in cands if c.status == "pending"),
                     key=lambda c: (-(c.score + math.log2(1 + c.count)), c.key))
    if args.max:
        for c in pending[args.max:]:
            c.status, c.reason = "deferred", "beyond --max"
        pending = pending[:args.max]
    print(f"{len(cands)} candidates: {sum(c.status == 'existing' for c in cands)} existing, "
          f"{sum(c.status == 'merged' for c in cands)} merged, {sum(c.status == 'rare' for c in cands)} rare, "
          f"{len(pending)} to review", file=sys.stderr)

    # 3. LLM: duplicate check against similar entries, then rate and describe
    defboxes = {norm_key(mo.group(1)) for src in body
                for mo in re.finditer(r"\\begin\{defbox\}\{([^{}]+)\}", src.text)}
    defboxes |= {k.rstrip("s") for k in defboxes}
    pool = [(e.label, e.name or f"{e.short} ({e.long})", e.description, None) for e in entries]
    used_labels = {e.label for e in entries}
    examples = "\n".join(f"- {e.name}: {e.description}" for e in entries if e.kind == "term" and e.description)
    style = Path(args.style).read_text(encoding="utf-8") if args.style else ""
    term_guide = guide_text(style, ["What deserves an entry", "Descriptions"])
    abbr_guide = guide_text(style, ["Abbreviations", "Descriptions"])
    for i, c in enumerate(pending):
        if llm:
            print(f"[{i + 1}/{len(pending)}] {c.name}", file=sys.stderr, end=" ", flush=True)
        ctx = "\n".join(f"{j + 1}. {m['text']}" for j, m in enumerate(c.contexts(args.contexts)))
        if c.kind == "abbr":
            verdict = abbr_review(llm, c.name, c.long, ctx, examples + abbr_guide, title,
                                  found=bool(c.long)) if llm else {}
            if llm and verdict and not verdict.get("is_abbreviation"):
                c.status, c.reason = "reject", verdict.get("reason", "")
                print("-> not an abbreviation", file=sys.stderr)
                continue
            c.status, c.reason = "keep", verdict.get("reason", "")
            c.long = verdict.get("long") or c.long
            c.description = verdict.get("description", "") if verdict.get("concept") else ""
            c.label = unique_label(make_label(c.name), used_labels)
            pool.append((c.label, f"{c.name} ({c.long})", c.description, c.key))
            if llm:
                print("-> keep" + (" (concept)" if c.description else ""), file=sys.stderr)
            continue
        cw = content_words(c.name + " " + c.long)
        similar = []
        for item in pool:
            iw = content_words(item[1])
            if cw and iw and (len(cw & iw) / len(cw | iw) >= 0.5 or cw <= iw or iw <= cw):
                similar.append(item)
        similar = similar[:6]
        if llm and similar:
            lst = "\n".join(f"{j + 1}. {name}: {desc or '(no description)'}"
                            for j, (_, name, desc, _) in enumerate(similar))
            user = (f"Candidate glossary term {cand_desc(c)}, used in the book as:\n{ctx}\n\n"
                    f"Glossary entries with similar names:\n{lst}\n\n"
                    "Is the candidate exactly the same concept as one of these entries (a synonym, spelling "
                    "or inflection variant, or an abbreviation/expansion of it)?  A more specific, broader or "
                    "related concept is NOT the same.  Answer duplicate_of with the entry number, or 0.")
            try:
                d = llm.ask(REVIEW_SYSTEM, user, DUP_SCHEMA)
                k = d.get("duplicate_of", 0)
                if 1 <= k <= len(similar):
                    lab, name, _, key = similar[k - 1]
                    if key is None:
                        c.status, c.label = "existing", lab
                    else:
                        c.status, c.merged_into = "merged", key
                    c.reason = f"duplicate of {name}: {d.get('reason', '')}"
                    print(f"-> duplicate of {name}", file=sys.stderr)
                    continue
            except LLM_ERRORS as e:
                print(f"LLM error: {e}", file=sys.stderr)
        verdict = {}
        if llm:
            kind = "abbreviation" + (f' (expansion found: "{c.long}")' if c.long else "") \
                if c.kind == "abbr" else "term"
            user = f"""Book: {title}

Candidate {kind}: "{c.name}"
Facts: occurs {c.count} times, in {100 * c.df / max(1, c.ndocs):.0f}% of the book's passages.{
    " The book gives it a formal definition (a definition box)." if c.key in defboxes else ""}
Occurrences (the candidate is marked with [[ ]]):
{ctx}

Existing glossary entries, for style:
{examples}
{term_guide}
Rate how useful a glossary entry for this candidate would be to a reader of this book:
5 = concept the book defines or uses in a specific, operational sense, central to its method;
4 = important technical concept used repeatedly;
3 = technical concept a reader may want to look up;
2 = marginal: mentioned in passing, the name of a whole field or discipline, or too general to
    need a definition;
1 = not a term: everyday phrase, sentence fragment, incomplete noun phrase, person, product.
Always fill:
- name: canonical singular form, lowercase unless it is a proper noun.
- description: a single short sentence of at most 20 words, in the style of the existing entries,
  faithful to how this book uses the term; prefer the essential definition over elaboration;
  do not start with the term itself and do not use LaTeX ("" if relevance 1).
- reason: a short justification of the rating."""
            try:
                verdict = llm.ask(REVIEW_SYSTEM, user, REVIEW_SCHEMA)
            except LLM_ERRORS as e:
                print(f"LLM error: {e}", file=sys.stderr)
        c.relevance = int(verdict.get("relevance", 0) or 0)
        c.reason = verdict.get("reason", "")
        if verdict.get("name"):
            c.name = verdict["name"]
        c.description = verdict.get("description", "")
        c.label = unique_label(make_label(c.name), used_labels)
        if llm and verdict and c.relevance < args.min_relevance:
            c.status = "reject"
            print(f"-> relevance {c.relevance}", file=sys.stderr)
            continue
        c.status = "keep"
        pool.append((c.label, c.name, c.description, c.key))
        if llm:
            print(f"-> relevance {c.relevance}", file=sys.stderr)

    # 4. existing abbreviations without a description: concepts get one (as an update)
    updates = []
    if llm and not args.skip_existing:
        todo = [e for e in entries if e.kind == "abbr" and not e.description]
        for i, e in enumerate(todo):
            print(f"[existing {i + 1}/{len(todo)}] {e.short}", file=sys.stderr, end=" ", flush=True)
            ms = entry_contexts(body, root, e, args.contexts)
            if not ms:
                print("-> unused", file=sys.stderr)
                continue
            ctx = "\n".join(f"{j + 1}. {s}" for j, (_, _, s) in enumerate(ms))
            v = abbr_review(llm, e.short, e.long, ctx, examples + abbr_guide, title, found=True)
            if v.get("concept") and v.get("description"):
                updates.append((Entry(e.label, "abbr", short=e.short, long=e.long, plural=e.plural,
                                      description=tex_escape(v["description"])), ms, v.get("reason", "")))
                print("-> concept, description drafted", file=sys.stderr)
            else:
                print("-> name, no description", file=sys.stderr)

    cs.save(state, meta)
    write_review_tex(Path(args.output), cands, main.name, updates)
    st = Counter(c.status for c in cands)
    print(f"wrote {args.output} and {state}: " + ", ".join(f"{v} {k}" for k, v in st.most_common()),
          file=sys.stderr)


def write_review_tex(path: Path, cands: list[Cand], main_name: str, updates=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [f"% Glossary candidates generated by glosstool from {main_name}.",
           "% Uncommented entries are applied by `glosstool wrap --new-entries <this file> --write`:",
           "% new labels are added to the glossary, existing labels replace their definition.",
           "% Edit names and descriptions freely; comment out entries you do not want.", ""]

    def render(c: Cand, commented: bool):
        rel_ = f"relevance={c.relevance} " if c.relevance else ""
        lines = [f"% [{c.status}] {rel_}count={c.count} df={c.df}/{c.ndocs} ridf={ridf(c.count, c.df, c.ndocs):.2f} "
                 f"score={c.score:.1f} sources=" + ",".join(f"{k}:{v}" for k, v in sorted(c.sources.items()))]
        if c.reason:
            lines.append(f"% reason: {c.reason}")
        for m in c.contexts(3):
            lines.append(f"%   {m['file']}:{m['line']}: {m['text'][:300]}")
            if m["meaning"]:
                lines.append(f"%     meaning: {m['meaning']}")
        label = c.label or make_label(c.name)
        if c.kind == "abbr":
            e = Entry(label, "abbr", short=c.name, long=tex_escape(c.long or "TODO"),
                      description=tex_escape(c.description))
        else:
            e = Entry(label, "term", name=c.name, description=tex_escape(c.description or "TODO"))
        lines += [("% " if commented else "") + ln for ln in entry_to_tex(e).splitlines()]
        return lines + [""]

    order = lambda c: (c.kind != "abbr", -c.relevance, -c.score, c.key)  # noqa: E731
    out.append("% ===== Proposed entries =====\n")
    for c in sorted((c for c in cands if c.status == "keep"), key=order):
        out += render(c, False)
    if updates:
        out.append("% ===== Updates: descriptions for existing concept abbreviations =====\n")
        for e, ms, reason in updates:
            out.append(f"% reason: {reason}")
            out += [f"%   {f}:{ln}: {s[:300]}" for f, ln, s in ms]
            out += entry_to_tex(e).splitlines() + [""]
    out.append("% ===== Low relevance or not abbreviations, per the LLM (uncomment to keep) =====\n")
    for c in sorted((c for c in cands if c.status == "reject"), key=order):
        out += render(c, True)
    out.append("% ===== Too pervasive, above --max-df (uncomment to keep) =====\n")
    for c in sorted((c for c in cands if c.status == "pervasive"), key=order):
        out += render(c, True)
    out.append("% ===== Duplicates of existing entries or other candidates =====")
    for c in sorted((c for c in cands if c.status in ("existing", "merged")), key=order):
        out.append(f"%   {c.name} -> {c.label or c.merged_into} ({c.reason})")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# wrap
# --------------------------------------------------------------------------

@dataclass
class Hit:
    src: Source
    start: int
    end: int
    entry: Entry
    plural: bool
    cap: bool
    mode: str  # "term", "short", "full" ("long form (ABBR)") or "long" (bare long form)

    def replacement(self) -> str:
        cmd = ("Gls" if self.cap else "gls") + ("pl" if self.plural else "")
        return f"\\{cmd}{{{self.entry.label}}}"


def entry_patterns(e: Entry):
    """Yield (regex, plural, mode) for an entry."""
    if e.kind == "abbr":
        shortpl = e.plural or e.short + "s"
        longp = flex_pattern(e.long)
        for pl, short in ((True, shortpl), (False, e.short)):
            yield re.compile(r"(?<![\w\\{-])" + longp + r"s?\s*\(" + re.escape(short) + r"\)"), pl, "full"
        for pl, short in ((True, shortpl), (False, e.short)):
            yield re.compile(r"(?<![\w\\{])" + re.escape(short) + r"(?![\w}])"), pl, "short"
        words = e.long.split()
        longpl = " ".join(words[:-1] + [default_plural(words[-1])])
        for pl, form in ((True, longpl), (False, e.long)):
            yield re.compile(r"(?<![\w\\{-])" + flex_pattern(form) + r"(?![\w}-])"), pl, "long"
    else:
        name = e.name
        plural = e.plural or " ".join(name.split()[:-1] + [default_plural(name.split()[-1])])
        for pl, form in ((True, plural), (False, name)):
            yield re.compile(r"(?<![\w\\{-])" + flex_pattern(form) + r"(?![\w}-])"), pl, "term"


DISAMBIG_SCHEMA = {"type": "object", "properties": {"technical": {"type": "boolean"}},
                   "required": ["technical"]}


def cmd_wrap(args):
    main = Path(args.main)
    root = main.parent.resolve()
    body, gloss = load_project(main, skip_headings=not args.headings, skip_captions=not args.captions)
    entries = [e for g in gloss for e in parse_entries(g.text)]
    new_entries, updates = [], []
    if args.new_entries:
        by_label = {e.label: i for i, e in enumerate(entries)}
        for e in parse_entries(Path(args.new_entries).read_text(encoding="utf-8")):
            if "TODO" in (e.description + e.long):
                print(f"warning: entry '{e.label}' still has a TODO", file=sys.stderr)
            if e.label in by_label:  # same label: update the existing definition
                entries[by_label[e.label]] = e
                updates.append(e)
            else:
                by_label[e.label] = len(entries)
                entries.append(e)
                new_entries.append(e)
    if args.only:
        wanted = set(args.only.split(","))
        entries = [e for e in entries if e.label in wanted]
    ambiguous = set(args.ambiguous.split(",")) if args.ambiguous is not None else \
        {e.label for e in entries if e.kind == "term" and len(e.name.split()) == 1}
    llm = LLM(args.llm, args.host, Path(args.cache), args.num_ctx, args.effort) if args.llm else None
    stats = defaultdict(Counter)
    patterns = [(e, rx, pl, mode) for e in entries for rx, pl, mode in entry_patterns(e)]
    decisions = open(args.decisions, "w", encoding="utf-8") if args.decisions else None

    for src in body:
        name = rel(src.path, root)
        if args.files and name not in args.files:
            continue
        t, m = src.text, src.mask
        hits = []
        for e, rx, pl, mode in patterns:
            for mo in rx.finditer(t):
                a, b = mo.span()
                if any(m[a:b]):
                    continue
                form = e.name if mode == "term" else e.long
                cap = mode in ("term", "long") and t[a].isupper() and not form[0].isupper()
                hits.append(Hit(src, a, b, e, pl, cap, mode))
        hits.sort(key=lambda h: (h.start, -(h.end - h.start)))  # earliest, then longest
        chosen, last_end = [], -1
        for h in hits:
            if h.start >= last_end:
                chosen.append(h)
                last_end = h.end
        kept = []
        for h in chosen:
            if h.mode == "long" and args.keep_long:
                stats[h.entry.label]["long-form-left"] += 1
            else:
                kept.append(h)
        chosen = kept
        bounds_of = {"first-per-paragraph": r"\n\s*\n", "first-per-chapter": r"\\(?:chapter|glsresetall)\b"}
        bounds = {p: [mo.start() for mo in re.finditer(rx, t)] for p, rx in bounds_of.items()}
        existing = [(mo.start(), mo.group(1)) for mo in re.finditer(r"\\[gG]ls(?:pl)?\{([^}]+)\}", t)]
        used = set()
        kept = []
        for h in chosen:
            policy = args.abbr_policy if h.entry.kind == "abbr" else args.policy
            if policy == "all":
                kept.append(h)
                continue
            bs = bounds[policy]
            region = sum(1 for x in bs if x < h.start)
            k = (policy, region, h.entry.label)
            if k in used or any(lab == h.entry.label and sum(1 for x in bs if x < p) == region
                                for p, lab in existing):
                stats[h.entry.label]["skipped-policy"] += 1
                continue
            used.add(k)
            kept.append(h)
        chosen = kept
        if llm:
            kept = []
            for h in chosen:
                if h.entry.label in ambiguous:
                    sent = sentence_around(t, h.start, h.end)
                    user = (f'Glossary term: "{h.entry.name}"\nGlossary definition: {h.entry.description}\n\n'
                            f"Passage (the occurrence is marked with [[ ]]):\n{sent}\n\n"
                            "Is the marked occurrence used in the specific technical sense of the glossary "
                            "definition (technical=true), or in an everyday or different sense (technical=false)?")
                    try:
                        ok = llm.ask("You are a careful technical editor. Answer in JSON.", user,
                                     DISAMBIG_SCHEMA).get("technical", False)
                    except LLM_ERRORS as e:
                        print(f"LLM error: {e}", file=sys.stderr)
                        ok = False
                    if decisions is not None:
                        decisions.write(json.dumps({"label": h.entry.label, "file": name,
                                                    "line": line_of(t, h.start), "technical": ok,
                                                    "text": sent}, ensure_ascii=False) + "\n")
                    if not ok:
                        stats[h.entry.label]["skipped-llm"] += 1
                        continue
                kept.append(h)
            chosen = kept
        elif args.no_ambiguous:
            for h in chosen:
                if h.entry.label in ambiguous:
                    stats[h.entry.label]["skipped-ambiguous"] += 1
            chosen = [h for h in chosen if h.entry.label not in ambiguous]
        for h in chosen:
            stats[h.entry.label]["wrapped"] += 1
        new = t
        for h in sorted(chosen, key=lambda h: -h.start):
            new = new[:h.start] + h.replacement() + new[h.end:]
        if new != t:
            if args.write:
                src.path.write_text(new, encoding="utf-8")
                print(f"updated {name}: {len(chosen)} substitutions", file=sys.stderr)
            elif not args.stats:
                sys.stdout.writelines(difflib.unified_diff(t.splitlines(True), new.splitlines(True),
                                                           "a/" + name, "b/" + name, n=0))
    if (new_entries or updates) and args.write:
        gpath = Path(args.glossary) if args.glossary else gloss[0].path
        gtext = gpath.read_text(encoding="utf-8")
        for e in updates:
            span = entry_span(gtext, e.label)
            if span is None:
                print(f"warning: definition of '{e.label}' not found in {gpath.name}, appending", file=sys.stderr)
                new_entries.append(e)
                continue
            gtext = gtext[:span[0]] + entry_to_tex(e) + gtext[span[1]:]
        for e in new_entries:
            gtext = gtext.rstrip("\n") + "\n\n" + entry_to_tex(e)
        if f"category={DESCRIBED}" in gtext and DESCRIBED_STYLE not in gtext:
            gtext = DESCRIBED_STYLE + "\n\n" + gtext
            print(f"added {DESCRIBED_STYLE} to {gpath.name}", file=sys.stderr)
        gpath.write_text(gtext, encoding="utf-8")
        used = {lab for lab, s in stats.items() if s["wrapped"]}
        unused = [e.label for e in new_entries if e.label not in used]
        print(f"{rel(gpath, root)}: {len(new_entries)} entries added, {len(updates)} updated"
              + (f" (new but never used: {', '.join(unused)})" if unused else ""), file=sys.stderr)
    cols = ["wrapped", "skipped-policy", "skipped-llm", "skipped-ambiguous", "long-form-left"]
    print("\n" + f"{'label':26}" + "".join(f"{c:>18}" for c in cols), file=sys.stderr)
    for lab in sorted(stats, key=lambda k: (-stats[k]["wrapped"], k)):
        flag = "  (ambiguous)" if lab in ambiguous else ""
        print(f"{lab:26}" + "".join(f"{stats[lab][c]:18}" for c in cols) + flag, file=sys.stderr)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="http://localhost:11434", help="Ollama server URL")
    ap.add_argument("--cache", default=".glosstool-cache.json", help="LLM answer cache file")
    ap.add_argument("--num-ctx", type=int, default=4096, help="LLM context window (Ollama)")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"],
                    help="reasoning effort (Anthropic backend)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("extract", help="raise candidate terms (LLM over chunks and/or heuristics)")
    x.add_argument("main", help="main .tex file")
    x.add_argument("--state", default="glossary-candidates.json", help="candidate state file (output)")
    x.add_argument("--llm", metavar="MODEL", help="Ollama model that raises candidates chunk by chunk")
    x.add_argument("--heuristics", action="store_true", help="also use heuristic extraction")
    x.add_argument("--chunk-words", type=int, default=250, help="approximate chunk size in words")
    x.add_argument("--min-freq", type=int, default=5, help="minimum frequency of heuristic n-grams")
    x.add_argument("--files", nargs="*", help="restrict LLM extraction to these files")
    x.add_argument("--limit", type=int, help="process only the first N chunks (for testing)")
    x.add_argument("--fresh", action="store_true", help="start a new state file instead of merging into it")

    c = sub.add_parser("contexts", help="print entries' descriptions next to passages that use them")
    c.add_argument("main", help="main .tex file")
    c.add_argument("--labels", help="comma-separated labels (default: entries of --entries, else all described)")
    c.add_argument("--entries", metavar="TEX", help="reviewed new/updated entries (e.g. the filter output)")
    c.add_argument("-k", type=int, default=6, help="passages per entry")
    c.add_argument("-o", "--output", help="write to this file instead of stdout")
    c.set_defaults(func=cmd_contexts)

    i = sub.add_parser("inspect", help="show extracted candidates grouped by name, with meanings")
    i.add_argument("main", help="main .tex file")
    i.add_argument("--state", default="glossary-candidates.json", help="candidate state file")
    i.add_argument("--files", nargs="*", help="only mentions from these files")
    i.add_argument("--text", action="store_true", help="also show the sentence of each mention")
    i.add_argument("--min-mentions", type=int, default=1, help="hide candidates with fewer mentions")
    i.add_argument("--sort", choices=["name", "mentions", "df", "ridf"], default="name",
                   help="order of candidates (df: most pervasive first; ridf: most term-like first)")
    i.add_argument("--stats", action="store_true", help="compute tf/df/ridf over the whole book")
    i.add_argument("--doc-words", type=int, default=250, help="chunk size used as 'document' for df")
    i.set_defaults(func=cmd_inspect)
    x.set_defaults(func=cmd_extract)

    f = sub.add_parser("filter", help="deduplicate, filter and describe candidates")
    f.add_argument("main", help="main .tex file")
    f.add_argument("--state", default="glossary-candidates.json", help="candidate state file (updated)")
    f.add_argument("-o", "--output", default="glossary-candidates.tex", help="entries for review")
    f.add_argument("--llm", metavar="MODEL", help="Ollama model for dedup and keep/reject decisions")
    f.add_argument("--min-count", type=int, default=2, help="minimum occurrences in the book")
    f.add_argument("--max", type=int, default=0, help="review at most N candidates (0: all)")
    f.add_argument("--contexts", type=int, default=3, help="contexts shown to the LLM per candidate")
    f.add_argument("--doc-words", type=int, default=250, help="chunk size used as 'document' for df statistics")
    f.add_argument("--style", metavar="MD", help="the book's glossary-style.md (criteria and labeled examples)")
    f.add_argument("--min-relevance", type=int, default=3,
                   help="LLM relevance (1-5) below which entries start commented out")
    f.add_argument("--skip-existing", action="store_true",
                   help="do not draft descriptions for existing concept abbreviations")
    f.add_argument("--max-df", type=float, default=0.0,
                   help="set aside candidates present in more than this fraction of chunks (0: off)")
    f.set_defaults(func=cmd_filter)

    w = sub.add_parser("wrap", help="replace plain occurrences with \\gls commands")
    w.add_argument("main", help="main .tex file")
    w.add_argument("--new-entries", metavar="TEX", help="reviewed entries file to add (e.g. the filter output)")
    w.add_argument("--glossary", help="glossary file to append new entries to (default: detected)")
    w.add_argument("--write", action="store_true", help="edit files in place (default: print a diff)")
    w.add_argument("--stats", action="store_true", help="print only the summary")
    w.add_argument("--policy", choices=["all", "first-per-paragraph", "first-per-chapter"],
                   default="first-per-paragraph")
    w.add_argument("--only", help="comma-separated labels to process")
    w.add_argument("--files", nargs="*", help="restrict to these files (relative to the main file)")
    w.add_argument("--headings", action="store_true", help="also wrap inside sectioning commands")
    w.add_argument("--captions", action="store_true", help="also wrap inside captions")
    w.add_argument("--abbr-policy", choices=["all", "first-per-paragraph", "first-per-chapter"], default="all",
                   help="placement policy for abbreviations")
    w.add_argument("--keep-long", action="store_true",
                   help="leave bare abbreviation long forms as prose (default: replace them with \\gls)")
    w.add_argument("--llm", metavar="MODEL", help="Ollama model that disambiguates ambiguous terms")
    w.add_argument("--ambiguous", help="comma-separated labels needing disambiguation (default: single-word terms)")
    w.add_argument("--no-ambiguous", action="store_true", help="without --llm, leave ambiguous terms untouched")
    w.add_argument("--decisions", metavar="JSONL", help="log each LLM disambiguation decision to this file")
    w.set_defaults(func=cmd_wrap)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
