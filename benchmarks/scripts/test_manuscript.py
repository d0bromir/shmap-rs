#!/usr/bin/env python3
"""Self-test for manuscript.py.

The paper's prose is generated, so the ways it could go quietly wrong are not
the ways a table can. What is pinned here:

  - the derivations that produce a range are paired within a cell, not built
    from two unrelated extremes, which is wider than anything measured;
  - a missing measurement is never counted as agreement;
  - every macro is a legal LaTeX control sequence and carries its provenance,
    because an illegal one fails at typeset time and an undocumented one
    cannot be audited;
  - the `.tex` and the `.tsv` cannot state different values;
  - and the lint that keeps hand-typed numbers out of the draft catches one,
    while not firing on the layout, addresses and citation years that are not
    measurements.

  python3 benchmarks/scripts/test_manuscript.py
"""

from __future__ import annotations

import hashlib
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare import load_set  # noqa: E402
from charts import read_profiles  # noqa: E402
import crossarch as x  # noqa: E402
import manuscript as m  # noqa: E402
from test_crossarch import make_set  # noqa: E402

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:62} got {got!r}")
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")


BENCHMARKS = ("B01", "B02")
METRICS = ("Containment", "Jaccard")


def add_reference(d: Path, walls: dict[tuple[str, str], float],
                  rss_kb: dict[tuple[str, str], int]) -> None:
    """Append cpp-shmap rows, which make_set does not write.

    Given per-cell rather than as a constant so a test can make the extremes
    of the two implementations fall in *different* cells -- the case that
    separates a paired ratio from a ratio of extremes.
    """
    with open(d / "results.tsv", "a") as f:
        for (b, metric), wall in sorted(walls.items()):
            f.write(f"{b}\tcpp-shmap\t{metric}\t1\tmedian3\tREF\tRD\tpaper\t0\t"
                    f"{wall:.2f}\t\t\t{rss_kb[(b, metric)]}\t900\t800\tcmd\n")


def add_checks(d: Path, rows: list[tuple[str, str, str, bool, str]]) -> None:
    lines = ["check\tbenchmark\tmetric\tpassed\tdetail\n"]
    for c, b, metric, passed, detail in rows:
        lines.append(f"{c}\t{b}\t{metric}\t{passed}\t{detail}\n")
    (d / "checks.tsv").write_text("".join(lines))


def set_subject_rss(d: Path, rss_kb: dict[tuple[str, str], int]) -> None:
    """Give each cell its own shmap-rs footprint.

    make_set writes one constant, which cannot tell a paired ratio from a
    ratio of two extremes -- both give the same answer when the denominator
    never varies. That is the whole distinction under test.
    """
    out = []
    for line in (d / "results.tsv").read_text().splitlines(keepends=True):
        f = line.split("\t")
        if len(f) > 12 and f[1] == "shmap-rs" and (f[0], f[2]) in rss_kb:
            f[12] = str(rss_kb[(f[0], f[2])])
            line = "\t".join(f)
        out.append(line)
    (d / "results.tsv").write_text("".join(out))


def ctx_for(dirs: list[tuple[str, Path]], suite: dict | None = None) -> m.MCtx:
    sets = [x.ArchSet(arch=a, rs=load_set(d), profiles=read_profiles(d)) for a, d in dirs]
    return m.MCtx(sets=sets, hosts={}, digest=x.input_digest(sets),
                  suite=suite or {"suite_version": "1.0"})


def write(p: Path, body: str) -> Path:
    p.write_text(body)
    return p


DRAFT_HEAD = r"""\documentclass[10pt,a4paper]{article}
\usepackage[margin=1.8cm]{geometry}
\pgfplotsset{compat=1.18}
\begin{document}
"""
DRAFT_TAIL = r"""\begin{thebibliography}{9}
\bibitem{mash} Ondov,B.D. et al. (2016) Mash. Genome Biol., 17, 132.
\end{thebibliography}
\end{document}
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # make_set gives shmap-rs wall = 100/t^0.8 scaled, and peak_rss_kb =
        # 2000000 in every cell. The reference rows below are deliberately
        # lopsided: B01/Containment is the slowest cell for the C++ and
        # B02/Jaccard the heaviest, so extremes do not share a row.
        walls = {("B01", "Containment"): 300.0, ("B01", "Jaccard"): 200.0,
                 ("B02", "Containment"): 250.0, ("B02", "Jaccard"): 220.0}
        rss = {("B01", "Containment"): 4000000, ("B01", "Jaccard"): 18000000,
               ("B02", "Containment"): 6000000, ("B02", "Jaccard"): 8000000}
        # Deliberately anti-correlated with `rss`: the cell with the smallest
        # C++ footprint is not the cell with the largest shmap-rs one, so a
        # bound built from the two extremes overstates the range.
        rs_rss = {("B01", "Containment"): 2000000, ("B01", "Jaccard"): 2000000,
                  ("B02", "Containment"): 2000000, ("B02", "Jaccard"): 4000000}

        dx = make_set(root, "x86_64", "a2", scale=1.0,
                      benchmarks=BENCHMARKS, metrics=METRICS)
        da = make_set(root, "aarch64", "galaxy", scale=1.2,
                      benchmarks=BENCHMARKS, metrics=METRICS)
        set_subject_rss(dx, rs_rss)
        add_reference(dx, walls, rss)
        add_reference(da, {k: v * 1.2 for k, v in walls.items()}, rss)
        add_checks(dx, [
            ("thread_determinism", b, mt, True, "identical across all thread counts")
            for b in BENCHMARKS for mt in METRICS
        ] + [
            ("ground_truth", "B02", "Containment", True, "99000/100000 = 0.990 (need 0.98)"),
            ("ground_truth", "B02", "Jaccard", True, "98000/100000 = 0.980 (need 0.98)"),
            ("wrong_q60", "B02", "Containment", True, "0/90000 = 0.000000"),
            ("wrong_q60", "B02", "Jaccard", True, "7/90000 = 0.000078"),
        ])
        c = ctx_for([("x86_64", dx), ("aarch64", da)])

        print("pairing: a range must be of ratios measured, not of two extremes")
        # shmap-rs is 2 000 000 kB everywhere, so every paired ratio is
        # cpp/2 000 000: 2.0, 9.0, 3.0, 4.0. Min 2.0 and max 9.0.
        check("smallest peak-RSS ratio is a ratio that exists",
              m.f1(min(c.rss_ratios(c.x))), "2.0")
        check("largest peak-RSS ratio is a ratio that exists",
              m.f1(max(c.rss_ratios(c.x))), "9.0")
        naive_min = min(c.rss_gb(c.x, "cpp-shmap")) / max(c.rss_gb(c.x, "shmap-rs"))
        check("paired minimum is tighter than the extremes-of-extremes bound",
              m.f1(min(c.rss_ratios(c.x))) != m.f1(naive_min), True)

        print("\nspeedups pair the two implementations inside one cell")
        # wall(shmap-rs, -@1) = 100 on x86_64, so speedup == cpp wall / 100.
        check("every cell with both implementations contributes",
              len(c.speedups(c.x)), 4)
        check("smallest speedup", m.f2(min(c.speedups(c.x))), "2.00")
        check("largest speedup", m.f2(max(c.speedups(c.x))), "3.00")

        print("\nscaling reports where the peak was, not just how big")
        peak, threads, bench = c.peak_scaling(c.x)
        check("peak is at the widest thread count measured", threads, 4)
        check("peak names its benchmark", bench in BENCHMARKS, True)
        check("peak exceeds the single-threaded baseline", peak > 1.0, True)

        print("\nagreement never counts what was not measured")
        check("identical counters on both machines agree", c.agreement(), (4, 4))
        # Break one counter on the second machine.
        p = da / "profiles.tsv"
        lines = p.read_text().splitlines(keepends=True)
        lines[2] = lines[2].replace("\t900\t800\n", "\t901\t800\n")
        p.write_text("".join(lines))
        c2 = ctx_for([("x86_64", dx), ("aarch64", da)])
        check("a differing counter is not agreement", c2.agreement(), (3, 4))
        # Remove the column entirely from the second machine.
        comment, head, *rest = p.read_text().splitlines()
        p.write_text("\n".join([comment, head]
                               + [r.rsplit("\t", 2)[0] + "\t\t" for r in rest]) + "\n")
        c3 = ctx_for([("x86_64", dx), ("aarch64", da)])
        check("a missing counter is compared, not assumed equal", c3.agreement()[0], 0)

        print("\nevery macro is legal LaTeX and carries its provenance")
        names = [mc.name for mc in m.MACROS]
        check("macro names are unique", len(set(names)), len(names))
        check("macro names are letters only (a digit is not a valid csname)",
              [n for n in names if not n.isalpha()], [])
        check("every macro declares where it is taken from",
              [mc.name for mc in m.MACROS if not mc.source.strip()], [])
        check("every macro declares what it means",
              [mc.name for mc in m.MACROS if not mc.note.strip()], [])

        print("\nthe .tex and the .tsv cannot disagree")
        vals = m.values(c)
        tex, tsv = m.render_tex(c, vals), m.render_tsv(c, vals)
        defined = dict(re.findall(r"\\newcommand\{\\shm(\w+)\}\{(.*?)\}(?:\s|$)", tex))
        check("every macro reaches the .tex", len(defined), len(m.MACROS))
        mismatched = []
        for line in tsv.splitlines():
            if line.startswith("#") or line.startswith("macro\t"):
                continue
            name, value = line.split("\t")[:2]
            want = defined.get(name[len(r"\shm"):], None)
            # `group` macros are thin-spaced in the .tex only; compare on digits.
            if want is not None and want.replace(r"\,", "") != value:
                mismatched.append(name)
        check("no macro has one value in the .tex and another in the .tsv",
              mismatched, [])
        check("output is a pure function of the inputs",
              hashlib.sha256(m.render_tex(c, m.values(c)).encode()).hexdigest()[:16],
              hashlib.sha256(tex.encode()).hexdigest()[:16])
        check("metric count comes from the data, not a constant",
              dict((mc.name, v) for mc, v in vals)["NumMetrics"], "2")

    print("\nthe lint keeps hand-typed numbers out of the draft")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        clean = write(d / "clean.tex", DRAFT_HEAD + r"""
It is \shmSpeedupXMin--\shmSpeedupXMax$\times$ faster, see
Table~\ref{tab:xarch:speedup} and \url{https://example.org/v2/x}.
\vspace{-0.4cm}
A line break with a skip.\\[0.25em]
""" + DRAFT_TAIL)
        check("a draft holding only macros is clean", m.lint_draft(clean), [])

        dirty = write(d / "dirty.tex", DRAFT_HEAD +
                      "shmap-rs is 2.14 times faster.\n" + DRAFT_TAIL)
        check("a hand-typed measurement is caught", len(m.lint_draft(dirty)), 1)

        excused = write(d / "excused.tex", DRAFT_HEAD +
                        "Built -O3. % lint-ok: upstream build flag\n" + DRAFT_TAIL)
        check("an explained digit is allowed", m.lint_draft(excused), [])

        unexcused = write(d / "unexcused.tex", DRAFT_HEAD +
                          "Built -O3. % lint-ok:\n" + DRAFT_TAIL)
        check("silencing the lint without a reason does not work",
              len(m.lint_draft(unexcused)), 1)

        commented = write(d / "commented.tex", DRAFT_HEAD +
                          "Prose. % it was 2.14 when written\n" + DRAFT_TAIL)
        check("a digit inside a comment is not prose", m.lint_draft(commented), [])

        uses = write(d / "uses.tex", DRAFT_HEAD +
                     r"\shmSpeedupXMin and \shmNotAMacro." + "\n" + DRAFT_TAIL)
        check("macro uses are found", m.draft_uses(uses),
              {"SpeedupXMin", "NotAMacro"})
        check("an undefined macro is detectable",
              sorted(m.draft_uses(uses) - {mc.name for mc in m.MACROS}),
              ["NotAMacro"])

    print()
    for f in FAIL:
        print(f"FAIL: {f}")
    print(f"{'FAILED' if FAIL else 'all checks passed'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
