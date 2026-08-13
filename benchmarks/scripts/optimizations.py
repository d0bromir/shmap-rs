#!/usr/bin/env python3
"""The optimization table, generated from PORT_CHANGES.md.

  optimizations.py            build paper/generated/table_optimizations.{tex,tsv}
  optimizations.py --check    exit 1 if it would change, or has desynced
  optimizations.py --list     every row, its layer and its C++ citation

---------------------------------------------------------------------------
Why generate it
---------------------------------------------------------------------------
`PORT_CHANGES.md` owns the account of what changed against the C++: one row
per optimization, each with the figure that change was worth against the build
it landed on, and each with the upstream source it replaces quoted verbatim at
a pinned commit. A paper about those optimizations must not restate that from
memory -- a second hand-maintained list is a second thing to get wrong, and
the failure is silent.

So the table is read out of that document. Two things are taken:

  * the rows of the current-state table at its top, verbatim; and
  * the C++ citations, from the `// file:lines` header of each section's first
    quoted block -- which is what makes "compared to the C++" checkable rather
    than asserted, since every one carries a line range at a pinned commit.

---------------------------------------------------------------------------
What is declared here rather than read
---------------------------------------------------------------------------
`LAYERS` -- whether a change is a data structure, an algorithm, the parallel
decomposition, or code. That is the paper's own classification and not a fact
about the repository, so it is declared, not parsed. It is checked against the
parsed rows in both directions: a row without a layer, or a layer without a
row, fails rather than being dropped from the table.

Effects are NOT summarised. The `.tex` carries the short columns a printed
table can hold; the `.tsv` beside it carries every parsed cell in full,
including the effect text, so nothing is lost to the presentation and a
reviewer can check a row without a LaTeX toolchain.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout import REPO  # noqa: E402

SOURCE = REPO / "PORT_CHANGES.md"
OUT = REPO / "paper" / "generated"
NAME = "table_optimizations"

# The paper's classification of each numbered change. Checked against the rows
# actually present, both ways -- see module docstring.
LAYERS: dict[int, str] = {
    1: "data structure",
    2: "algorithm",
    3: "algorithm",
    4: "parallelism",
    5: "parallelism",
    6: "parallelism",
    7: "code",
    8: "code",
    9: "algorithm",
}

LAYER_ORDER = ["data structure", "algorithm", "parallelism", "code"]

# Rows whose upstream code is quoted under another row's section, so the
# per-section scan below cannot find it. Declared rather than inferred, and
# verified to appear in the source: "the C++ has no analogue" is a real and
# different claim (rows 4-6), and printing it for a row that simply documents
# its citation elsewhere would be a false one.
CITE_ELSEWHERE: dict[int, str] = {
    9: "buckets.h:151-174",   # quoted in section 1, where get_sorted_buckets is
}

# `| 1 | ... | ... | ... | ... |` -- five cells, the first a bare number.
ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|")
SECTION_RE = re.compile(r"^## (\d+)\.\s+(.*)$")
# The provenance header every quoted C++ block carries.
CITE_RE = re.compile(r"^//\s*([A-Za-z0-9_]+\.(?:h|cpp)):([0-9]+(?:-[0-9]+)?)")
COMMIT_RE = re.compile(r"github\.com/pesho-ivanov/shmap/(?:blob|tree)/([0-9a-f]{40})")


def parse() -> tuple[list[dict], str]:
    """Rows of the current-state table, each with its section's C++ citation."""
    text = SOURCE.read_text()
    lines = text.splitlines()

    rows: list[dict] = []
    for line in lines:
        m = ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) != 5:
            continue
        rows.append({
            "n": int(cells[0]),
            "change": cells[1],
            "targets": cells[2],
            "effect": cells[3],
            "exact": cells[4],
        })

    # First C++ citation inside each `## N.` section: the upstream code the row
    # replaces. A section with no quoted C++ (a capability the C++ lacks
    # entirely, such as threading) legitimately has none.
    cites: dict[int, str] = {}
    current = None
    for line in lines:
        m = SECTION_RE.match(line)
        if m:
            current = int(m.group(1))
            continue
        if current is None or current in cites:
            continue
        c = CITE_RE.match(line.strip())
        if c:
            cites[current] = f"{c.group(1)}:{c.group(2)}"
    for r in rows:
        r["cpp"] = cites.get(r["n"], "") or CITE_ELSEWHERE.get(r["n"], "")
        r["layer"] = LAYERS.get(r["n"], "")

    # An override that names code the document does not actually quote would
    # be a citation this script invented.
    quoted = {f"{m.group(1)}:{m.group(2)}"
              for line in lines
              if (m := CITE_RE.match(line.strip()))}
    for n, cite in sorted(CITE_ELSEWHERE.items()):
        if cite not in quoted:
            sys.exit(f"optimizations.py cites {cite} for row {n}, but {SOURCE.name} "
                     f"quotes no such block. Citations must be quoted upstream code, "
                     f"not this script's recollection of it.")

    commits = set(COMMIT_RE.findall(text))
    if len(commits) != 1:
        sys.exit(f"{SOURCE.name}: expected exactly one pinned upstream commit, "
                 f"found {len(commits)}. Every C++ citation must be at the same "
                 f"revision or the comparison is not one comparison.")
    return rows, commits.pop()


def verify(rows: list[dict]) -> None:
    """Fail loudly on a desync rather than quietly dropping a row."""
    parsed = {r["n"] for r in rows}
    if not parsed:
        sys.exit(f"{SOURCE.name}: no optimization rows found. The current-state "
                 f"table is what this reads; if its shape changed, change this too.")
    missing = sorted(parsed - set(LAYERS))
    extra = sorted(set(LAYERS) - parsed)
    if missing:
        sys.exit(f"{SOURCE.name} has optimization(s) {missing} that this script does "
                 f"not classify. Add them to LAYERS in optimizations.py -- a paper "
                 f"about the optimizations must not silently omit one.")
    if extra:
        sys.exit(f"optimizations.py classifies {extra}, which {SOURCE.name} no longer "
                 f"lists. Remove them from LAYERS.")
    if sorted(parsed) != list(range(1, len(parsed) + 1)):
        sys.exit(f"{SOURCE.name}: optimization numbers are not 1..{len(parsed)}: "
                 f"{sorted(parsed)}")


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def tex_escape(s: str) -> str:
    for a, b in (("\\", ""), ("&", r"\&"), ("#", r"\#"), ("%", r"\%"),
                 ("$", r"\$"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}"),
                 ("~", r"\textasciitilde{}"), ("^", r"\textasciicircum{}")):
        s = s.replace(a, b)
    return s


def md_to_tex(s: str) -> str:
    """Markdown as PORT_CHANGES.md writes it, in LaTeX.

    Code spans and bold are converted before escaping, so their contents are
    escaped exactly once and the markers themselves never are.
    """
    out, i = [], 0
    for m in re.finditer(r"`([^`]*)`|\*\*([^*]*)\*\*|\*([^*]*)\*", s):
        out.append(tex_escape(s[i:m.start()]))
        if m.group(1) is not None:
            out.append(r"\texttt{" + tex_escape(m.group(1)) + "}")
        elif m.group(2) is not None:
            out.append(r"\textbf{" + tex_escape(m.group(2)) + "}")
        else:
            out.append(r"\emph{" + tex_escape(m.group(3)) + "}")
        i = m.end()
    out.append(tex_escape(s[i:]))
    t = "".join(out)
    for a, b in (("---", "---"), ("--", "--"), ("—", "---"), ("→", r"$\rightarrow$"),
                 ("§", r"\S"), ("≈", r"$\approx$"), ("×", r"$\times$")):
        t = t.replace(a, b)
    return t


def short_change(s: str) -> str:
    """The change, without the clause explaining it.

    PORT_CHANGES.md's cells are written to be read on their own; a table column
    holds the naming half. Cut at the first em dash or colon, which is where
    those cells consistently switch from naming to explaining.
    """
    for sep in ("—", " -- ", ":"):
        if sep in s:
            return s.split(sep)[0].strip().rstrip(",")
    return s.strip()


def exact_mark(s: str) -> str:
    return "yes" if s.strip().lower().startswith("yes") else "no"


def digest() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:16]


def header(commit: str, comment: str) -> list[str]:
    return [
        f"{comment} GENERATED by benchmarks/scripts/optimizations.py -- do not edit.",
        f"{comment} artifact:   {NAME} (table)",
        f"{comment} source:     PORT_CHANGES.md (the account of what changed vs the C++)",
        f"{comment} upstream:   pesho-ivanov/shmap @ {commit[:12]} (every citation is at it)",
        f"{comment} inputs:     sha256:{digest()}",
        f"{comment} provenance: paper/generated/OPTIMIZATIONS.md",
    ]


def render(rows: list[dict], commit: str) -> dict[str, str]:
    ordered = sorted(rows, key=lambda r: (LAYER_ORDER.index(r["layer"]), r["n"]))

    body = [r"\begin{tabular}{@{}rp{0.24\textwidth}lp{0.43\textwidth}c@{}}", r"\toprule",
            r"\# & Change & C++ replaced & Effect, as measured when it landed & Exact \\",
            r"\midrule"]
    layer = None
    for r in ordered:
        if r["layer"] != layer:
            layer = r["layer"]
            if body[-1] != r"\midrule":
                body.append(r"\midrule")
            body.append(r"\multicolumn{5}{@{}l}{\emph{" + tex_escape(layer) + r"}} \\")
        cpp = r"\texttt{" + tex_escape(r["cpp"]) + "}" if r["cpp"] else r"--- \emph{none}"
        body.append(" & ".join([
            str(r["n"]),
            md_to_tex(short_change(r["change"])),
            cpp,
            md_to_tex(r["effect"]),
            exact_mark(r["exact"]),
        ]) + r" \\")
    body += [r"\bottomrule", r"\end{tabular}"]

    tex = "\n".join([
        *header(commit, "%"), "%",
        r"\begin{table*}[t]",
        r"\centering\scriptsize",
        "\n".join(body),
        r"\caption{Every change that makes shmap-rs cheaper than the C++, grouped by the "
        r"layer it acts on, with the upstream source each replaces at commit "
        r"\texttt{" + commit[:7] + r"}. Effects are what the change was worth against the "
        r"build it landed on and are deliberately not restated against the current one. "
        r"They do not sum: the whole-run comparison is measured separately. "
        r"\emph{Exact} means the mapping output is byte-identical.}",
        r"\label{tab:optimizations}",
        r"\end{table*}",
        "",
    ])

    tsv = [*header(commit, "#"),
           "\t".join(["n", "layer", "change", "targets", "effect", "exact", "cpp"])]
    for r in ordered:
        tsv.append("\t".join([str(r["n"]), r["layer"], r["change"], r["targets"],
                              r["effect"], r["exact"], r["cpp"]]))

    prov = [
        "# Provenance of the optimization table",
        "",
        "GENERATED by `benchmarks/scripts/optimizations.py` — do not edit.",
        "",
        "| | |",
        "|---|---|",
        "| source | `PORT_CHANGES.md`, the current-state table at its top |",
        f"| upstream | `pesho-ivanov/shmap` @ `{commit}` |",
        f"| input digest | `sha256:{digest()}` |",
        "",
        "**Taken from the source, verbatim:** every row of the current-state table, and the",
        "`// file:lines` header of the first C++ block quoted in each numbered section. A",
        "section quoting no C++ is a capability the C++ does not have at all, and prints an",
        "em dash rather than a citation.",
        "",
        "**Declared here, not parsed:** the layer each change acts on — data structure,",
        "algorithm, parallelism, code. That is this paper's classification rather than a fact",
        "about the repository. It is checked against the parsed rows in both directions, so a",
        "new optimization cannot be silently omitted from a paper that claims to list them all.",
        "",
        "**Not summarised:** the `.tex` carries the columns a printed table can hold; the `.tsv`",
        "carries every parsed cell in full, including the effect text and what each change",
        "targets. Both come from one parse, so they cannot disagree.",
        "",
        "**Read with**",
        "",
        "- Effects are what a change was worth *against the build it landed on*, and are",
        "  deliberately not refreshed. They do not sum, and they are not comparable with each",
        "  other unless they name the same stage. The whole-run comparison is generated",
        "  separately, from the promoted result sets.",
        "- *Exact* is the claim that mapping output is byte-identical, not that the change is",
        "  bug-free. Each row's basis is in the source document's `Exact?` column.",
        "",
        "## Rows",
        "",
        "| # | layer | change | C++ replaced |",
        "|---|---|---|---|",
    ]
    for r in ordered:
        prov.append(f"| {r['n']} | {r['layer']} | {r['change']} | "
                    f"{'`' + r['cpp'] + '`' if r['cpp'] else '—'} |")
    prov.append("")

    return {f"{NAME}.tex": tex,
            f"{NAME}.tsv": "\n".join(tsv) + "\n",
            "OPTIMIZATIONS.md": "\n".join(prov)}


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help=f"output directory (default: {OUT})")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the table would change (does not write)")
    ap.add_argument("--list", action="store_true",
                    help="print every row, its layer and its citation, and stop")
    a = ap.parse_args()

    if not SOURCE.exists():
        print(f"no {SOURCE}; nothing to generate", file=sys.stderr)
        return 2

    rows, commit = parse()
    verify(rows)

    if a.list:
        print(f"upstream pinned at {commit}\n")
        for r in sorted(rows, key=lambda r: (LAYER_ORDER.index(r["layer"]), r["n"])):
            print(f"#{r['n']}  [{r['layer']}]  {short_change(r['change'])}")
            print(f"    replaces  {r['cpp'] or '(nothing -- the C++ has no analogue)'}")
            print(f"    targets   {r['targets']}")
            print(f"    effect    {r['effect'][:110]}")
            print(f"    exact     {exact_mark(r['exact'])}")
        return 0

    files = render(rows, commit)
    out = Path(a.out) if a.out else OUT

    if a.check:
        stale = [n for n, body in sorted(files.items())
                 if not (out / n).exists() or (out / n).read_text() != body]
        if stale:
            print(f"optimization table out of date in {out}: {', '.join(stale)}\n"
                  f"regenerate with: python3 benchmarks/scripts/optimizations.py",
                  file=sys.stderr)
            return 1
        print(f"the {len(rows)}-row optimization table is current with {SOURCE.name}")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    for n, body in sorted(files.items()):
        (out / n).write_text(body)
    by_layer = {l: sum(1 for r in rows if r["layer"] == l) for l in LAYER_ORDER}
    print(f"wrote {NAME} to {out}: {len(rows)} rows from {SOURCE.name} "
          f"({', '.join(f'{v} {k}' for k, v in by_layer.items() if v)}), "
          f"upstream @ {commit[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
