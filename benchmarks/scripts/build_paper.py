#!/usr/bin/env python3
"""Typeset paper/manuscript.tex into paper/manuscript.pdf.

  build_paper.py            build paper/manuscript.pdf
  build_paper.py --check    exit 1 if the committed PDF is not what the
                            current sources and result sets produce
  build_paper.py --pages N  page budget to enforce (default: 2)

---------------------------------------------------------------------------
Why this is not build_pdf.py
---------------------------------------------------------------------------
`build_pdf.py` previews the generated artifacts: it writes the wrapper
document itself, one artifact per page, landscape, and has no opinion about
length. This typesets a document somebody wrote, which pulls in the generated
fragments and the generated macros, and which has a page budget an applications
note has to meet. Different inputs, different failure modes, same engine
machinery -- which is imported rather than copied.

---------------------------------------------------------------------------
Determinism
---------------------------------------------------------------------------
The PDF is committed, so the build has to be a pure function of its inputs or
the file churns on every run and `--check` degrades to a smoke test.
`SOURCE_DATE_EPOCH` comes from the result set's own measurement date, so TeX
stamps the data's date and not the moment of the build -- the rule
`build_pdf.py` already documents, applied to the same result sets.

---------------------------------------------------------------------------
The page budget
---------------------------------------------------------------------------
Enforced, not advisory. Two pages is the format, and a paper that quietly
becomes three because a benchmark grew a row is a submission that bounces. A
build over budget still writes the PDF -- seeing the overflow is how you fix
it -- but exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout import REPO, available_arches, current_dir  # noqa: E402
from run import load_suite  # noqa: E402
from build_pdf import ENGINES, find_engine  # noqa: E402

DRAFT = REPO / "paper" / "manuscript.tex"
OUT = REPO / "paper" / "manuscript.pdf"
GENERATED = REPO / "paper" / "generated"
MAX_PAGES = 2

PAGES_RE = re.compile(r"Output written on .*?\((\d+) pages?")


def measurement_epoch() -> str:
    """The build clock: the earliest date any promoted set was measured.

    Earliest rather than latest so that promoting one architecture does not
    restamp a PDF whose other half did not move. A missing manifest yields a
    constant, because a deterministic wrong date is better here than a correct
    one that churns the repository on every build.
    """
    dates = []
    suite_version = load_suite()["suite_version"]
    for a in available_arches(suite_version):
        p = current_dir(suite_version, a) / "manifest.json"
        if not p.exists():
            continue
        try:
            fin = json.loads(p.read_text()).get("finished", "")[:10]
            dates.append(datetime.strptime(fin, "%Y-%m-%d"))
        except (ValueError, OSError, json.JSONDecodeError):
            continue
    if not dates:
        return "0"
    return str(int(min(dates).replace(tzinfo=timezone.utc).timestamp()))


INPUT_RE = re.compile(r"\\input\{([^}]+)\}")


def sources(tmp: Path) -> list[str]:
    """Copy the draft and exactly the fragments it \\inputs, flattened beside it.

    Only what the draft names, resolved by searching the generated tree. Two
    things fall out of doing it this way rather than copying everything:
    an `\\input` of a fragment that no longer exists fails here, naming it,
    instead of deep inside a TeX log; and an ambiguous name is refused rather
    than resolved by copy order. The per-architecture directories deliberately
    hold identically-named artifacts -- `table_mapper_comparison.tex` exists
    once per machine -- so a draft citing one of those has to say which.
    """
    shutil.copy(DRAFT, tmp / DRAFT.name)
    wanted = INPUT_RE.findall(DRAFT.read_text())
    for name in wanted:
        name = name if name.endswith(".tex") else f"{name}.tex"
        found = sorted(GENERATED.rglob(name))
        if not found:
            sys.exit(f"error: {DRAFT.name} inputs {name}, which is not in "
                     f"{GENERATED.relative_to(REPO)}/.\n"
                     f"  Regenerate with benchmarks/scripts/manuscript.py, paper.py "
                     f"and crossarch.py.")
        if len(found) > 1:
            rel = ", ".join(str(p.relative_to(REPO)) for p in found)
            sys.exit(f"error: {DRAFT.name} inputs {name}, which exists more than "
                     f"once ({rel}).\n"
                     f"  Name the directory in the \\input so the draft says which "
                     f"machine's artifact it means.")
        shutil.copy(found[0], tmp / name)
    return wanted


def page_count(outdir: Path, stem: str, pdf: Path) -> int | None:
    log = outdir / f"{stem}.log"
    if log.exists():
        m = PAGES_RE.search(log.read_text(errors="replace"))
        if m:
            return int(m.group(1))
    # No log, or an engine that words it differently: fall back to the page
    # tree. Undercounts a PDF using object streams, so it is the fallback and
    # not the method.
    n = len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))
    return n or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help=f"output PDF (default: {OUT})")
    ap.add_argument("--engine", help="LaTeX engine to use")
    ap.add_argument("--pages", type=int, default=MAX_PAGES,
                    help=f"page budget to enforce (default: {MAX_PAGES}; 0 disables)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the built PDF would differ from the one on disk")
    a = ap.parse_args()

    if not DRAFT.exists():
        print(f"no draft at {DRAFT}", file=sys.stderr)
        return 2
    if not (GENERATED / "macros.tex").exists():
        print(f"no macros at {GENERATED / 'macros.tex'}; "
              f"run benchmarks/scripts/manuscript.py first", file=sys.stderr)
        return 2
    out = Path(a.out) if a.out else OUT

    engine = find_engine(a.engine)
    if not engine:
        # build_pdf.py's rule: a machine with no TeX cannot judge whether the
        # committed PDF is stale and must not claim it is.
        print(f"no LaTeX engine found, so {out.name} was not "
              f"{'checked' if a.check else 'built'}.\n"
              f"  The draft and its generated inputs are unaffected.\n"
              f"  Install any of: {', '.join(n for n, _ in ENGINES)}. Tectonic needs no\n"
              f"  system TeX: extract its release binary to ~/tools/tectonic/tectonic.")
        return 0
    exe, argtmpl = engine

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        sources(tmpd)
        tex = tmpd / DRAFT.name
        cmd = [exe] + [t.format(outdir=str(tmpd), tex=str(tex)) for t in argtmpl]
        env = {**os.environ,
               "SOURCE_DATE_EPOCH": measurement_epoch(),
               "FORCE_SOURCE_DATE": "1"}
        # Twice: \ref to a float's label is only right on the second pass, and
        # a paper that says "Table ??" is worse than a slow build.
        for _ in range(2):
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=tmpd, env=env)
        pdf = tmpd / f"{tex.stem}.pdf"
        if not pdf.exists():
            print(f"{Path(exe).name} produced no PDF (exit {r.returncode}).", file=sys.stderr)
            print("\n".join((r.stderr or r.stdout or "").strip().splitlines()[-30:]),
                  file=sys.stderr)
            return 1
        pages = page_count(tmpd, tex.stem, pdf)
        blob = pdf.read_bytes()

        if a.check:
            if not out.exists():
                print(f"{out.name} is missing; build it with "
                      f"python3 benchmarks/scripts/build_paper.py", file=sys.stderr)
                return 1
            if out.read_bytes() != blob:
                print(f"{out.name} differs from what the current draft and result sets "
                      f"produce.\nrebuild with: python3 benchmarks/scripts/build_paper.py",
                      file=sys.stderr)
                return 1
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(blob)

    where = "is current" if a.check else f"wrote {out} ({len(blob) / 1024:.0f} KB)"
    print(f"{where}, {pages if pages else '?'} page(s), via {Path(exe).name}")

    if a.pages and pages and pages > a.pages:
        print(f"\nover budget: {pages} pages against a limit of {a.pages}.\n"
              f"  The PDF was written so the overflow can be seen, but this is a "
              f"failure: the format is {a.pages} pages.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
