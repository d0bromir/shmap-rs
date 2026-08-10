#!/usr/bin/env python3
"""Typeset every generated table and figure into one PDF.

  build_pdf.py                 build paper/generated/artifacts.pdf
  build_pdf.py --out FILE.pdf  write somewhere else
  build_pdf.py --dir DIR       use a different artifact directory
  build_pdf.py --engine PATH   use a specific LaTeX engine

---------------------------------------------------------------------------
Why
---------------------------------------------------------------------------
The `.tex` fragments are for the paper and the `.tsv` files are for auditing,
but neither lets anyone *look* at the result. A figure whose axes overlap its
legend, or a table too wide for the column, is invisible in both. This wraps
the fragments in a minimal document and typesets them, so a run ends with
something a person can open.

It is a preview of the artifacts, not a draft of the paper: the wrapper adds a
title block, the provenance of the result set, and nothing else. Captions and
labels come from the fragments themselves, unchanged, so what you see here is
what the paper will get.

---------------------------------------------------------------------------
Engine
---------------------------------------------------------------------------
Any of tectonic, latexmk, pdflatex, xelatex or lualatex, whichever is found
first, unless --engine says otherwise. Tectonic is preferred because it needs
no system TeX installation and fetches only the packages the document uses --
which is why it is what the benchmark host has.

A missing engine is not an error. The build prints what it would need and exits
0, because a benchmark run that produced correct artifacts should not be
reported as failed by a document previewer.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
from layout import REPO, arch  # noqa: E402
GENERATED_ROOT = REPO / "paper" / "generated"


def default_dir(a: str | None = None) -> Path:
    """Artifacts live per architecture; typeset the one asked for."""
    return GENERATED_ROOT / (a or arch())

# Order is deliberate: tables before figures, and within each the same order the
# paper argues in. Anything found on disk but not named here is appended after,
# so a new artifact appears in the PDF without being registered twice.
PREFERRED_ORDER = [
    # cross-architecture, in the order crossarch.py argues: which machines,
    # then the evidence they ran the same computation, then the comparison
    # that evidence licenses.
    "table_crossarch_machines",
    "table_crossarch_agreement",
    "table_crossarch_headline",
    "table_crossarch_speedup",
    "table_crossarch_stages",
    "fig_crossarch_thread_scaling",
    "fig_crossarch_stages",
    # single architecture
    "table_mapper_comparison",
    "table_seed_heuristic",
    "fig_thread_scaling",
    "fig_memory_vs_threads",
    "fig_time_vs_matches",
    "fig_stage_breakdown",
]

# Engines that take `file.tex` and leave `file.pdf` beside it. Tectonic needs a
# subcommand, hence the separate argv.
ENGINES = [
    ("tectonic", ["-X", "compile", "--keep-logs", "--outdir", "{outdir}", "{tex}"]),
    ("latexmk", ["-pdf", "-interaction=nonstopmode", "-outdir={outdir}", "{tex}"]),
    ("pdflatex", ["-interaction=nonstopmode", "-output-directory={outdir}", "{tex}"]),
    ("xelatex", ["-interaction=nonstopmode", "-output-directory={outdir}", "{tex}"]),
    ("lualatex", ["-interaction=nonstopmode", "-output-directory={outdir}", "{tex}"]),
]

# Searched in addition to PATH: the host has no system TeX and a userspace
# tectonic is the supported way to get one.
EXTRA_BINS = [Path.home() / "tools" / "tectonic" / "tectonic"]

PREAMBLE = r"""\documentclass[10pt,a4paper]{article}
\usepackage[margin=2cm,landscape]{geometry}
\usepackage{booktabs}
\usepackage{pgfplots}
\usepackage{longtable}
\pgfplotsset{compat=1.18}
\usepgfplotslibrary{fillbetween}
\usepackage{hyperref}
\hypersetup{colorlinks=true,linkcolor=blue!50!black,urlcolor=blue!50!black}
% Floats are forced into place: this is a preview, so an artifact must appear
% where it is listed rather than wherever LaTeX would prefer to put it.
\usepackage{float}
\makeatletter
\renewcommand{\fps@table}{H}
\renewcommand{\fps@figure}{H}
\makeatother
\setlength{\parindent}{0pt}
\begin{document}
"""


def find_engine(explicit: str | None) -> tuple[str, list[str]] | None:
    if explicit:
        name = Path(explicit).name
        args = next((a for n, a in ENGINES if name.startswith(n)), ENGINES[0][1])
        return explicit, args
    for name, args in ENGINES:
        found = shutil.which(name)
        if found:
            return found, args
    for p in EXTRA_BINS:
        if p.exists():
            name = p.name
            args = next((a for n, a in ENGINES if name.startswith(n)), ENGINES[0][1])
            return str(p), args
    return None


def source_date_epoch(prov: list[str]) -> str:
    """A fixed build clock, so the PDF is a pure function of the artifacts.

    TeX stamps its own creation time into the PDF, which made every rebuild a
    different file even when nothing had changed -- unusable for a document
    that is committed. `SOURCE_DATE_EPOCH` replaces that clock, and taking it
    from the result set's own measurement date means the stamp describes the
    data rather than the moment someone happened to run the build.

    The fallback is a constant rather than "now": a deterministic wrong date is
    much better here than a correct one that churns the repository.
    """
    from datetime import datetime, timezone
    for line in prov:
        if line.startswith("measured:"):
            try:
                d = datetime.strptime(line.split(":", 1)[1].strip(), "%Y-%m-%d")
                return str(int(d.replace(tzinfo=timezone.utc).timestamp()))
            except ValueError:
                break
    return "0"


def tex_escape(name: str) -> str:
    """LaTeX-safe form of a path component.

    Architecture names carry underscores (`x86_64`), which LaTeX reads as
    subscript in text mode and refuses to typeset.
    """
    return name.replace("\\", "").replace("_", r"\_")


def provenance_of(d: Path) -> list[str]:
    """The header lines the generator wrote into every artifact, lifted from
    whichever one is present so the PDF states the same provenance."""
    for f in sorted(d.glob("*.tex")):
        out = []
        for line in f.read_text().splitlines():
            if not line.startswith("%"):
                break
            t = line.lstrip("% ").strip()
            if t.startswith(("result set:", "commit:", "host:", "measured:", "inputs:")):
                out.append(t)
        if out:
            return out
    return []


def build_document(d: Path) -> tuple[str, list[str]]:
    present = {p.stem for p in d.glob("*.tex")}
    order = [n for n in PREFERRED_ORDER if n in present]
    order += sorted(present - set(order))
    if not order:
        return "", []

    prov = provenance_of(d)
    body = [PREAMBLE,
            r"\begin{center}",
            r"{\Large\bfseries Generated benchmark artifacts}\\[0.4em]",
            rf"{{\small Typeset from \texttt{{paper/generated/{tex_escape(d.name)}/}} by "
            r"\texttt{benchmarks/scripts/build\_pdf.py}. Every number resolves to a row in the "
            r"result set named below; see \texttt{PROVENANCE.md} for the inputs, "
            r"transformation and caveats of each artifact.}\\[0.6em]",
            r"{\small\ttfamily " + r" \\ ".join(escape(p) for p in prov) + r"}",
            r"\end{center}",
            r"\vspace{1em}"]
    for name in order:
        body.append(rf"\input{{{name}.tex}}")
        body.append(r"\clearpage")
    body.append(r"\end{document}")
    return "\n".join(body) + "\n", order


def escape(s: str) -> str:
    for a, b in (("_", r"\_"), ("&", r"\&"), ("#", r"\#"), ("%", r"\%")):
        s = s.replace(a, b)
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arch", default=None,
                    help="architecture whose artifacts to typeset "
                         "(default: this machine's)")
    ap.add_argument("--dir", default=None,
                    help="artifact directory (default: paper/generated/<arch>/)")
    ap.add_argument("--out", help="output PDF (default: <dir>/artifacts.pdf)")
    ap.add_argument("--engine", help="LaTeX engine to use")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the built PDF would differ from the one on disk")
    a = ap.parse_args()

    d = Path(a.dir) if a.dir else default_dir(a.arch)
    if not d.is_dir():
        print(f"no artifact directory at {d}; run benchmarks/scripts/paper.py first", file=sys.stderr)
        return 2
    out = Path(a.out) if a.out else d / "artifacts.pdf"

    doc, order = build_document(d)
    if not order:
        print(f"no .tex artifacts in {d}; nothing to typeset")
        return 0

    engine = find_engine(a.engine)
    if not engine:
        # Also the --check path: a machine that cannot build the PDF cannot
        # judge whether the committed one is stale, and must not claim it is.
        print(f"no LaTeX engine found, so {out.name} was not {'checked' if a.check else 'built'}.\n"
              f"  The {len(order)} artifacts in {d} are complete and unaffected.\n"
              f"  Install any of: {', '.join(n for n, _ in ENGINES)}. Tectonic needs no\n"
              f"  system TeX: extract its release binary to ~/tools/tectonic/tectonic.")
        return 0
    exe, argtmpl = engine

    # Built in a scratch directory holding copies of the fragments, so \input
    # resolves without a graphicspath and no .aux/.log lands beside the
    # artifacts -- which would make paper.py --check report drift.
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        for p in d.glob("*.tex"):
            shutil.copy(p, tmpd / p.name)
        tex = tmpd / "artifacts.tex"
        tex.write_text(doc)
        cmd = [exe] + [t.format(outdir=str(tmpd), tex=str(tex)) for t in argtmpl]
        # FORCE_SOURCE_DATE as well as SOURCE_DATE_EPOCH: pdfTeX honours the
        # epoch only when forced, while XeTeX (what tectonic runs) takes it
        # either way. Setting both makes every supported engine deterministic.
        env = {**os.environ,
               "SOURCE_DATE_EPOCH": source_date_epoch(provenance_of(d)),
               "FORCE_SOURCE_DATE": "1"}
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpd, env=env)
        pdf = tmpd / "artifacts.pdf"
        if not pdf.exists():
            print(f"{Path(exe).name} produced no PDF (exit {r.returncode}).", file=sys.stderr)
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-25:]
            print("\n".join(tail), file=sys.stderr)
            return 1
        if a.check:
            # Byte comparison is meaningful because the build is deterministic
            # (see source_date_epoch): the same artifacts always typeset to the
            # same file, so any difference is a real one.
            if out.exists() and out.read_bytes() == pdf.read_bytes():
                print(f"{out.name} is current with the artifacts in {d}")
                return 0
            why = "differs from" if out.exists() else "is missing beside"
            print(f"{out.name} {why} the artifacts in {d}\n"
                  f"rebuild with: python3 benchmarks/scripts/build_pdf.py", file=sys.stderr)
            return 1
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pdf, out)

    kb = out.stat().st_size / 1024
    print(f"wrote {out} ({kb:.0f} KB) with {len(order)} artifacts, "
          f"via {Path(exe).name}: {', '.join(order)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
