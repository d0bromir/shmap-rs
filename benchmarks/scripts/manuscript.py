#!/usr/bin/env python3
"""Every number the manuscript's prose quotes, as a LaTeX macro.

  manuscript.py                 build paper/generated/macros.{tex,tsv} + MACROS.md
  manuscript.py --check         exit 1 if any macro would change, or the draft
                                uses one that no longer exists
  manuscript.py --list          each macro's value, source and meaning
  manuscript.py --lint          report literal numbers in the draft's prose

---------------------------------------------------------------------------
Why this exists
---------------------------------------------------------------------------
`paper.py` and `crossarch.py` already regenerate every *table* and *figure*
from the promoted result sets, so a float in the draft cannot go stale. The
prose could, and that is the more dangerous half: nobody re-reads a sentence
after a benchmark run, and "2.14--2.97x faster" keeps typesetting perfectly
long after the measurement behind it moved.

So the draft contains no numerals at all. It writes

    shmap-rs is \\shmSpeedupXMin--\\shmSpeedupXMax$\\times$ faster

and this script defines those two macros from `results.tsv`. A re-measurement
changes the sentence, because the sentence was never holding the number.

`--lint` is the other half of the guarantee: it reads the draft and reports
any bare numeral in running text, which is how a hand-typed figure would get
back in. Without it the discipline is a convention; with it, it is checked.

---------------------------------------------------------------------------
Scope
---------------------------------------------------------------------------
Both architectures at once, from each one's promoted `current/` -- the paper
is about running on two machines, so a per-architecture macro file would be
the wrong shape. Macros naming one machine carry an `X` (the reference
architecture, x86_64) or an `A` (the second one) in their name.

With fewer than two promoted result sets this writes nothing and exits 0, for
the same reason `crossarch.py` does: a comparison that does not exist yet is
not stale.

Output is a pure function of the result sets -- no timestamps, sorted
iteration, fixed formatting -- which is what makes `--check` a real equality
test rather than a smoke test.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import load_suite  # noqa: E402
from layout import REPO  # noqa: E402
from charts import time_stages  # noqa: E402
from crossarch import (  # noqa: E402
    ArchSet, METRIC_ORDER, REFERENCE, SUBJECT,
    input_digest, load_hosts, load_sets, set_id,
)

OUT = REPO / "paper" / "generated"
DRAFT = REPO / "paper" / "manuscript.tex"

# Every macro is \shm<Name>. One prefix, so `--lint` can find the draft's uses
# with a single pattern, and so a macro can never collide with LaTeX's own or a
# package's -- which would fail at \newcommand rather than silently rebind.
PREFIX = "shm"

# The stage profiles are read at one (metric, threads), the same pair charts.py
# and crossarch.py draw their pies at, so the shares here and the wedges there
# describe the same runs.
STAGE_METRIC = "Containment"
STAGE_THREADS = "1"

# Thread counts the prose singles out: the one where a2 stops improving, and
# the sweep's cap. Both machines measure both, so the comparison is like-for-like.
KNEE_THREADS = 16
CAP_THREADS = 64

# The parameter set every headline number is measured at, by its suite.toml name.
PARAM_SET = "paper"

CARGO_TOML = REPO / "Cargo.toml"

# The byline. Taken from the archive record rather than typed into the draft,
# for the reason every other number here is: the paper and the DOI have to name
# the same people, and two hand-maintained lists diverge. Zenodo's own split is
# preserved -- `creators` are authors, `contributors` are acknowledged -- so the
# paper says what the archive says rather than reinterpreting it.
ZENODO_JSON = REPO / ".zenodo.json"


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

@dataclass
class MCtx:
    """Both architectures' promoted sets, loaded once.

    `x` is the reference architecture and `a` the second. Macros are named for
    that split rather than for `x86_64`/`aarch64` so that adding a third
    machine does not silently change what an existing macro means.
    """
    sets: list[ArchSet]
    hosts: dict
    digest: str
    suite: dict

    @property
    def suite_version(self) -> str:
        return str(self.suite.get("suite_version", "?"))

    def param(self, key: str) -> str:
        """One flag from the parameter set every headline number is measured at.

        From suite.toml rather than retyped, because the paper's claims are
        only about the parameters the suite actually passed -- and because
        which set is authoritative is itself an open question upstream.
        """
        v = self.suite.get("params", {}).get(PARAM_SET, {}).get(key)
        return "" if v is None else f"{v:g}" if isinstance(v, float) else str(v)

    @property
    def x(self) -> ArchSet:
        return self.sets[0]

    @property
    def a(self) -> ArchSet:
        return self.sets[1]

    def benchmarks(self) -> list[str]:
        return sorted({r["benchmark"] for r in self.x.rs["rows"]})

    def metrics(self, bid: str) -> list[str]:
        present = {r["metric"] for r in self.x.rs["rows"] if r["benchmark"] == bid}
        return [m for m in METRIC_ORDER if m in present]

    def cells(self) -> list[tuple[str, str]]:
        return [(b, m) for b in self.benchmarks() for m in self.metrics(b)]

    def host_fact(self, s: ArchSet, key: str) -> str:
        return str(self.hosts.get(s.host, {}).get(key, ""))

    # -- derived quantities the macros are built from ------------------------

    def speedups(self, s: ArchSet) -> list[float]:
        """C++ wall over shmap-rs wall, at -@1, for every (benchmark, metric).

        Both terms come from the same machine, so the host's speed cancels;
        this is the one performance figure in the paper that is a property of
        the two programs rather than of the hardware. -@1 because the C++ is
        single-threaded by design.
        """
        out = []
        for bid, metric in self.cells():
            rs = s.row(bid, metric, 1)
            cpp = next((r for r in s.rs["rows"]
                        if r["benchmark"] == bid and r["metric"] == metric
                        and r["impl"] == REFERENCE), None)
            if rs and cpp and rs["wall_s"]:
                out.append(cpp["wall_s"] / rs["wall_s"])
        return out

    def rss_gb(self, s: ArchSet, impl: str) -> list[float]:
        out = []
        for bid, metric in self.cells():
            r = self._rss_row(s, bid, metric, impl)
            if r:
                out.append(r["peak_rss_kb"] / 1048576)
        return out

    @staticmethod
    def _rss_row(s: ArchSet, bid: str, metric: str, impl: str) -> dict | None:
        # The C++ is single-threaded by design and carries no thread column to
        # match on; shmap-rs is taken at -@1 so both sides describe one worker.
        return next((r for r in s.rs["rows"]
                     if r["benchmark"] == bid and r["metric"] == metric
                     and r["impl"] == impl
                     and (impl == REFERENCE or r["threads"] == 1)), None)

    def rss_ratios(self, s: ArchSet) -> list[float]:
        """C++ peak RSS over shmap-rs peak RSS, per (benchmark, metric).

        Paired within a cell rather than taken as a ratio of the two extremes:
        the extremes need not come from the same row, and a bound built that
        way is wider than any ratio actually measured.
        """
        out = []
        for bid, metric in self.cells():
            rs = self._rss_row(s, bid, metric, SUBJECT)
            cpp = self._rss_row(s, bid, metric, REFERENCE)
            if rs and cpp and rs["peak_rss_kb"]:
                out.append(cpp["peak_rss_kb"] / rs["peak_rss_kb"])
        return out

    def scaling(self, s: ArchSet, threads: int) -> list[float]:
        """wall(-@1) / wall(-@t) for every (benchmark, metric) at one t."""
        out = []
        for bid, metric in self.cells():
            one, many = s.row(bid, metric, 1), s.row(bid, metric, threads)
            if one and many and many["wall_s"]:
                out.append(one["wall_s"] / many["wall_s"])
        return out

    def peak_scaling(self, s: ArchSet) -> tuple[float, int, str]:
        """The best speedup this machine reached, where, and at what width.

        Returns (speedup, threads, benchmark). Reported alongside the median
        across the whole matrix rather than instead of it: the median is
        dragged down by the benchmarks whose wall time is dominated by
        indexing, and the peak is one favourable row, so neither alone
        describes the machine. The benchmark is part of the value so a reader
        can find the row rather than take the number on trust.
        """
        best = (0.0, 0, "")
        for t in sorted({r["threads"] for r in s.rs["rows"] if r["impl"] == SUBJECT}):
            for bid, metric in self.cells():
                one, many = s.row(bid, metric, 1), s.row(bid, metric, t)
                if one and many and many["wall_s"]:
                    v = one["wall_s"] / many["wall_s"]
                    if v > best[0]:
                        best = (v, t, bid)
        return best

    def stage_shares(self, s: ArchSet) -> dict[str, dict[str, float]]:
        """stage -> benchmark -> share of that run's total CPU seconds.

        Stages come from charts.time_stages, the same function the pies are
        drawn from, so a share here and a wedge there cannot disagree about
        what a stage is.
        """
        out: dict[str, dict[str, float]] = {}
        for bid in self.benchmarks():
            row = s.profile(bid, STAGE_METRIC, STAGE_THREADS)
            if not row:
                continue
            idx, mp = time_stages(row)
            total = sum(v for _, v, _ in idx + mp)
            if total <= 0:
                continue
            for label, v, _ in idx + mp:
                out.setdefault(label, {})[bid] = 100.0 * v / total
        return out

    def map_ratios(self) -> list[float]:
        """Second machine's mapping seconds over the reference machine's."""
        out = []
        for bid, metric in self.cells():
            rx, ra = self.x.row(bid, metric, 1), self.a.row(bid, metric, 1)
            if rx and ra and rx.get("map_s") and ra.get("map_s"):
                out.append(ra["map_s"] / rx["map_s"])
        return out

    def agreement(self) -> tuple[int, int]:
        """(cells whose counters match on both machines, cells compared).

        The premise of every timing comparison in the paper: the mapper is
        deterministic, so for one commit these must be identical on every
        machine. A cell missing a counter on either side is not counted as
        agreement -- it is not counted at all.
        """
        cols = ["n_mapped_reads", "n_mapq60", "n_seeded_buckets",
                "n_refined_buckets", "n_final_buckets"]
        agree = total = 0
        for bid, metric in self.cells():
            px = self.x.profile(bid, metric, "1")
            pa = self.a.profile(bid, metric, "1")
            if not px or not pa:
                continue
            vals = [(px.get(c), pa.get(c)) for c in cols]
            if any(v is None or w is None or v == "" or w == "" for v, w in vals):
                continue
            total += 1
            agree += all(v == w for v, w in vals)
        return agree, total

    def checks(self, s: ArchSet, name: str) -> list[dict]:
        return [c for c in s.rs["checks"] if c["check"] == name]


# ---------------------------------------------------------------------------
# formatting
#
# Every macro's value is produced once, as a string, and written to both the
# .tex and the .tsv. `group` is the single exception: a TeX value may carry
# thin spaces a TSV must not, so the two forms are derived from one number
# rather than computed twice.
# ---------------------------------------------------------------------------

def f2(x: float) -> str:
    return f"{x:.2f}"


def f1(x: float) -> str:
    return f"{x:.1f}"


MISSING = "---"


def agg(fmt, fn, vals):
    """Reduce a series to a formatted value, or an em dash if it has none.

    A result set that does not sweep to a thread count the prose names, or has
    no reference rows for a benchmark, is a real situation -- the suite
    re-measures the C++ only when its binary changes. Crashing the generator
    there would take the whole paper down over one absent row; an em dash says
    "this set cannot answer that" in the sentence itself, which is honest and
    visible in review.
    """
    try:
        return fmt(fn(vals))
    except (ValueError, statistics.StatisticsError, ZeroDivisionError,
            IndexError, TypeError):
        return MISSING


def tex_escape(s: str) -> str:
    for a, b in (("\\", ""), ("_", r"\_"), ("&", r"\&"), ("#", r"\#"),
                 ("%", r"\%"), ("$", r"\$")):
        s = s.replace(a, b)
    return s


def topology(s: str) -> str:
    """hosts.toml writes `4 sockets x 16 cores`; the paper wants a times sign."""
    return tex_escape(s).replace(" x ", r" $\times$ ")


def grouped(n: int) -> str:
    """Thin-spaced thousands, for the typeset form only."""
    out, digits = [], str(n)
    while len(digits) > 3:
        out.insert(0, digits[-3:])
        digits = digits[:-3]
    out.insert(0, digits)
    return r"\,".join(out)


# ---------------------------------------------------------------------------
# the macros
# ---------------------------------------------------------------------------

@dataclass
class Macro:
    """One number in the paper, and the provenance that must travel with it.

    `source` and `note` are not comments: MACROS.md is generated from them, so
    a macro that starts reading a new column without saying so produces
    documentation that is visibly wrong.
    """
    name: str
    unit: str
    note: str
    source: str
    compute: Callable[[MCtx], str]
    group: bool = False          # thin-space the typeset form
    caveats: tuple[str, ...] = field(default_factory=tuple)


def zenodo() -> dict:
    """The archive record, or an empty one if it is absent."""
    if not ZENODO_JSON.exists():
        return {}
    try:
        return json.loads(ZENODO_JSON.read_text())
    except (OSError, ValueError):
        return {}


def _people(key: str) -> list[str]:
    """Zenodo names are `Family, Given`; a byline wants `Given Family`."""
    out = []
    for person in zenodo().get(key, []):
        name = str(person.get("name", "")).strip()
        if not name:
            continue
        family, _, given = name.partition(",")
        out.append(tex_escape(f"{given.strip()} {family.strip()}".strip()))
    return out


def _and_list(names: list[str], sep: str) -> str:
    """`A`, `A sep B`, `A, B sep C` -- the form a byline and a sentence both want."""
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} {sep} {names[-1]}"


def _orcid_line() -> str:
    parts = []
    for person in zenodo().get("creators", []):
        orcid = str(person.get("orcid", "")).strip()
        name = str(person.get("name", "")).strip()
        if orcid and name:
            parts.append(f"{tex_escape(name.partition(',')[0])} {tex_escape(orcid)}")
    return "; ".join(parts)


def cargo_field(key: str) -> str:
    """One `[package]` field, read rather than retyped.

    A hand-copied version number in a paper is the same failure this whole
    file exists to prevent, one directory further out.
    """
    if not CARGO_TOML.exists():
        return ""
    m = re.search(rf'^{key}\s*=\s*"([^"]*)"', CARGO_TOML.read_text(), re.M)
    return m.group(1) if m else ""


def _top_stage(c: MCtx) -> tuple[str, float]:
    shares = c.stage_shares(c.x)
    means = {k: statistics.fmean(v.values()) for k, v in shares.items() if v}
    if not means:
        return "", None
    top = max(sorted(means), key=lambda k: means[k])
    return top, means[top]


def _stage_shift(c: MCtx) -> tuple[str, str, float]:
    """The stage whose share moves most between the machines, and by how much."""
    sx, sa = c.stage_shares(c.x), c.stage_shares(c.a)
    best: tuple[str, str, float | None] = ("", "", None)
    for stage in sorted(sx):
        for bid in sorted(sx[stage]):
            if bid not in sa.get(stage, {}):
                continue
            d = sa[stage][bid] - sx[stage][bid]
            if best[2] is None or abs(d) > abs(best[2]):
                best = (stage, bid, d)
    return best


def _gt_fractions(c: MCtx) -> list[float]:
    """Ground-truth accuracy on the simulated benchmark, per metric.

    The detail column reads `124008/125000 = 0.992064 (need 0.98)`; the ratio
    is parsed from the counts rather than the printed quotient so the paper
    cannot inherit a rounding the check happened to display.
    """
    out = []
    for ch in c.checks(c.x, "ground_truth"):
        m = re.search(r"(\d+)/(\d+)", ch.get("detail", ""))
        if m and int(m.group(2)):
            out.append(100.0 * int(m.group(1)) / int(m.group(2)))
    return sorted(out)


NOT_A_MEASUREMENT = "Configuration from benchmarks/data/hosts.toml, not a measurement."
TWO_MACHINES = ("The machines differ in cores, sockets, NUMA topology, memory and clock, "
                "and none is held fixed. A ratio across them compares two machines we own, "
                "not two instruction sets.")

MACROS: list[Macro] = [
    # -- the machines --------------------------------------------------------
    Macro("HostX", "", "Name of the reference machine.",
          "current/manifest.json :: host", lambda c: tex_escape(c.x.host)),
    Macro("HostA", "", "Name of the second machine.",
          "current/manifest.json :: host", lambda c: tex_escape(c.a.host)),
    Macro("ArchX", "", "Reference architecture, as uname -m reports it.",
          "current/manifest.json :: arch", lambda c: tex_escape(c.x.arch)),
    Macro("ArchA", "", "Second architecture, as uname -m reports it.",
          "current/manifest.json :: arch", lambda c: tex_escape(c.a.arch)),
    Macro("CoresX", "cores", "Cores on the reference machine.",
          "hosts.toml :: cores", lambda c: c.host_fact(c.x, "cores"),
          caveats=(NOT_A_MEASUREMENT,)),
    Macro("CoresA", "cores", "Cores on the second machine.",
          "hosts.toml :: cores", lambda c: c.host_fact(c.a, "cores"),
          caveats=(NOT_A_MEASUREMENT,)),
    Macro("TopologyX", "", "Socket and NUMA layout of the reference machine.",
          "hosts.toml :: topology", lambda c: topology(c.host_fact(c.x, "topology")),
          caveats=(NOT_A_MEASUREMENT,)),
    Macro("TopologyA", "", "Socket and NUMA layout of the second machine.",
          "hosts.toml :: topology", lambda c: topology(c.host_fact(c.a, "topology")),
          caveats=(NOT_A_MEASUREMENT,)),
    Macro("Commit", "", "Commit both result sets measure.",
          "current/manifest.json :: commit", lambda c: tex_escape(c.x.commit),
          caveats=("Only meaningful if both sets measure it. build_all() warns and "
                   "MACROS.md records both when they differ.",)),
    Macro("MeasuredX", "", "Date the reference set was measured.",
          "current/manifest.json :: finished", lambda c: c.x.measured),
    Macro("MeasuredA", "", "Date the second set was measured.",
          "current/manifest.json :: finished", lambda c: c.a.measured),
    Macro("Rustc", "", "Compiler both sets were built with.",
          "current/manifest.json :: rustc",
          lambda c: tex_escape(c.x.rustc.replace("rustc ", "").split(" (")[0])),
    Macro("SuiteVersion", "", "Benchmark suite version the sets belong to.",
          "benchmarks/data/suite.toml :: suite_version", lambda c: c.suite_version),
    Macro("Version", "", "Release of shmap-rs the paper describes.",
          "Cargo.toml :: package.version", lambda c: cargo_field("version")),
    Macro("License", "", "Licence the implementation is published under.",
          "Cargo.toml :: package.license", lambda c: tex_escape(cargo_field("license"))),

    # -- the byline, from the archive record --------------------------------
    Macro("Authors", "", "Byline, in the archive's own creator order.",
          ".zenodo.json :: creators[].name",
          lambda c: r" \and ".join(_people("creators")) or MISSING,
          caveats=("Zenodo's creator/contributor split is preserved rather than "
                   "reinterpreted: creators are authors, contributors are "
                   "acknowledged. Change the archive record, not the draft.",)),
    Macro("Orcids", "", "ORCID of each author, in the same order.",
          ".zenodo.json :: creators[].orcid", lambda c: _orcid_line() or MISSING),
    Macro("Contributors", "", "Project members credited on the archive but not authors.",
          ".zenodo.json :: contributors[].name",
          lambda c: _and_list(_people("contributors"), "and") or MISSING),

    # -- the parameters every headline number is measured at -----------------
    Macro("ParamK", "", "k-mer length.",
          f"benchmarks/data/suite.toml :: params.{PARAM_SET}.k", lambda c: c.param("k")),
    Macro("ParamR", "", "FracMinHash sampling rate.",
          f"benchmarks/data/suite.toml :: params.{PARAM_SET}.hashratio",
          lambda c: c.param("hashratio")),
    Macro("ParamTheta", "", "Similarity threshold.",
          f"benchmarks/data/suite.toml :: params.{PARAM_SET}.threshold",
          lambda c: c.param("threshold")),
    Macro("ParamDelta", "", "Minimum score difference for a confident call.",
          f"benchmarks/data/suite.toml :: params.{PARAM_SET}.min_diff",
          lambda c: c.param("min_diff")),
    Macro("ParamPhi", "", "Maximum overlap between reported mappings.",
          f"benchmarks/data/suite.toml :: params.{PARAM_SET}.max_overlap",
          lambda c: c.param("max_overlap")),

    # -- how much evidence there is -----------------------------------------
    Macro("NumBenchmarks", "", "Benchmarks in the suite.",
          "current/results.tsv :: benchmark", lambda c: str(len(c.benchmarks()))),
    Macro("NumMetrics", "", "Similarity metrics each benchmark is run under.",
          "current/results.tsv :: metric",
          lambda c: str(len({mt for b in c.benchmarks() for mt in c.metrics(b)}))),
    Macro("NumCells", "", "(benchmark, metric) pairs behind every range quoted.",
          "current/results.tsv :: benchmark, metric", lambda c: str(len(c.cells()))),

    # -- against the C++ -----------------------------------------------------
    Macro("SpeedupXMin", "x", "Smallest single-threaded speedup over the C++, reference machine.",
          "current/results.tsv :: wall_s at threads=1, shmap-rs and cpp-shmap",
          lambda c: agg(f2, min, c.speedups(c.x))),
    Macro("SpeedupXMax", "x", "Largest single-threaded speedup over the C++, reference machine.",
          "current/results.tsv :: wall_s at threads=1, shmap-rs and cpp-shmap",
          lambda c: agg(f2, max, c.speedups(c.x))),
    Macro("SpeedupXMedian", "x", "Median single-threaded speedup over the C++, reference machine.",
          "current/results.tsv :: wall_s at threads=1, shmap-rs and cpp-shmap",
          lambda c: agg(f2, statistics.median, c.speedups(c.x))),
    Macro("SpeedupAMin", "x", "Smallest single-threaded speedup over the C++, second machine.",
          "current/results.tsv :: wall_s at threads=1, shmap-rs and cpp-shmap",
          lambda c: agg(f2, min, c.speedups(c.a))),
    Macro("SpeedupAMax", "x", "Largest single-threaded speedup over the C++, second machine.",
          "current/results.tsv :: wall_s at threads=1, shmap-rs and cpp-shmap",
          lambda c: agg(f2, max, c.speedups(c.a))),
    Macro("SpeedupAMedian", "x", "Median single-threaded speedup over the C++, second machine.",
          "current/results.tsv :: wall_s at threads=1, shmap-rs and cpp-shmap",
          lambda c: agg(f2, statistics.median, c.speedups(c.a))),

    # -- memory --------------------------------------------------------------
    Macro("RssRsMin", "GB", "Lowest peak RSS of shmap-rs, single-threaded, reference machine.",
          "current/results.tsv :: peak_rss_kb at threads=1, shmap-rs",
          lambda c: agg(f2, min, c.rss_gb(c.x, SUBJECT))),
    Macro("RssRsMax", "GB", "Highest peak RSS of shmap-rs, single-threaded, reference machine.",
          "current/results.tsv :: peak_rss_kb at threads=1, shmap-rs",
          lambda c: agg(f2, max, c.rss_gb(c.x, SUBJECT))),
    Macro("RssCpp", "GB", "Peak RSS of the C++, reference machine (median over cells).",
          "current/results.tsv :: peak_rss_kb, cpp-shmap",
          lambda c: agg(f2, statistics.median, c.rss_gb(c.x, REFERENCE))),
    Macro("RssRatioMin", "x", "Smallest peak-RSS advantage over the C++, paired within a cell.",
          "current/results.tsv :: peak_rss_kb, both implementations",
          lambda c: agg(f1, min, c.rss_ratios(c.x))),
    Macro("RssRatioMax", "x", "Largest peak-RSS advantage over the C++, paired within a cell.",
          "current/results.tsv :: peak_rss_kb, both implementations",
          lambda c: agg(f1, max, c.rss_ratios(c.x))),

    # -- threads -------------------------------------------------------------
    Macro("KneeThreads", "", "Thread count the prose calls the reference machine's knee.",
          "constant in manuscript.py", lambda c: str(KNEE_THREADS)),
    Macro("CapThreads", "", "Highest thread count both machines sweep.",
          "benchmarks/data/hosts.toml :: thread_cap", lambda c: str(CAP_THREADS)),
    Macro("ScaleXKnee", "x", "Median speedup at the knee, reference machine.",
          "current/results.tsv :: wall_s at threads=1 and threads=16",
          lambda c: agg(f1, statistics.median, c.scaling(c.x, KNEE_THREADS))),
    Macro("ScaleAKnee", "x", "Median speedup at the knee, second machine.",
          "current/results.tsv :: wall_s at threads=1 and threads=16",
          lambda c: agg(f1, statistics.median, c.scaling(c.a, KNEE_THREADS))),
    Macro("ScaleXCap", "x", "Median speedup at the sweep cap, reference machine.",
          "current/results.tsv :: wall_s at threads=1 and threads=64",
          lambda c: agg(f1, statistics.median, c.scaling(c.x, CAP_THREADS))),
    Macro("ScaleACap", "x", "Median speedup at the sweep cap, second machine.",
          "current/results.tsv :: wall_s at threads=1 and threads=64",
          lambda c: agg(f1, statistics.median, c.scaling(c.a, CAP_THREADS))),
    Macro("ScaleXPeak", "x", "Best speedup the reference machine reached anywhere in the matrix.",
          "current/results.tsv :: wall_s across the thread sweep",
          lambda c: agg(f1, lambda v: v or None, c.peak_scaling(c.x)[0]),
          caveats=("One favourable row, not the machine's typical behaviour; read with "
                   "ScaleXCap, which is the median across every cell.",)),
    Macro("ScaleXPeakThreads", "", "Thread count at which it reached it.",
          "current/results.tsv :: wall_s across the thread sweep",
          lambda c: str(c.peak_scaling(c.x)[1] or MISSING)),
    Macro("ScaleXPeakBench", "", "Benchmark on which it reached it.",
          "current/results.tsv :: wall_s across the thread sweep",
          lambda c: tex_escape(c.peak_scaling(c.x)[2]) or MISSING),
    Macro("ScaleAPeak", "x", "Best speedup the second machine reached anywhere in the matrix.",
          "current/results.tsv :: wall_s across the thread sweep",
          lambda c: agg(f1, lambda v: v or None, c.peak_scaling(c.a)[0]),
          caveats=("One favourable row, not the machine's typical behaviour; read with "
                   "ScaleACap, which is the median across every cell.",)),
    Macro("ScaleAPeakThreads", "", "Thread count at which it reached it.",
          "current/results.tsv :: wall_s across the thread sweep",
          lambda c: str(c.peak_scaling(c.a)[1] or MISSING)),
    Macro("ScaleAPeakBench", "", "Benchmark on which it reached it.",
          "current/results.tsv :: wall_s across the thread sweep",
          lambda c: tex_escape(c.peak_scaling(c.a)[2]) or MISSING),

    # -- the premise: same computation on both machines ----------------------
    Macro("AgreeCells", "", "(benchmark, metric) pairs whose counters are identical on both machines.",
          "current/profiles.tsv :: n_mapped_reads, n_mapq60, n_seeded_buckets, "
          "n_refined_buckets, n_final_buckets",
          lambda c: str(c.agreement()[0]),
          caveats=("Agreement at -@1. Agreement across thread counts is a separate "
                   "per-machine check (thread_determinism).",)),
    Macro("AgreeTotal", "", "(benchmark, metric) pairs compared.",
          "current/profiles.tsv :: the same five counters",
          lambda c: str(c.agreement()[1])),
    Macro("DetChecks", "", "thread_determinism checks passing on the reference machine.",
          "current/checks.tsv :: check=thread_determinism, passed",
          lambda c: str(sum(1 for ch in c.checks(c.x, "thread_determinism") if ch["passed"]))),
    Macro("DetTotal", "", "thread_determinism checks run on the reference machine.",
          "current/checks.tsv :: check=thread_determinism",
          lambda c: str(len(c.checks(c.x, "thread_determinism")))),

    # -- accuracy ------------------------------------------------------------
    Macro("AccMin", "%", "Lowest ground-truth accuracy across metrics on the simulated benchmark.",
          "current/checks.tsv :: check=ground_truth, detail",
          lambda c: agg(f2, min, _gt_fractions(c))),
    Macro("AccMax", "%", "Highest ground-truth accuracy across metrics on the simulated benchmark.",
          "current/checks.tsv :: check=ground_truth, detail",
          lambda c: agg(f2, max, _gt_fractions(c))),
    Macro("WrongQMax", "reads", "Most confidently-wrong placements any metric produced.",
          "current/checks.tsv :: check=wrong_q60, detail",
          lambda c: str(max((int(re.search(r"(\d+)/", ch["detail"]).group(1))
                             for ch in c.checks(c.x, "wrong_q60")
                             if re.search(r"(\d+)/", ch["detail"])), default=0)),
          group=True),

    # -- where the time goes -------------------------------------------------
    Macro("TopStage", "", "Costliest pipeline stage on the reference machine.",
          "current/profiles.tsv :: cpu_* stage timers at Containment/-@1",
          lambda c: tex_escape(_top_stage(c)[0]) or MISSING),
    Macro("TopStageShare", "%", "Its mean share of total CPU time across benchmarks.",
          "current/profiles.tsv :: cpu_* stage timers at Containment/-@1",
          lambda c: agg(f1, lambda v: v, _top_stage(c)[1])),
    Macro("ShiftStage", "", "Stage whose CPU share moves most between the machines.",
          "current/profiles.tsv :: cpu_* stage timers at Containment/-@1",
          lambda c: tex_escape(_stage_shift(c)[0]) or MISSING),
    Macro("ShiftPP", "pp", "How far it moves, in percentage points.",
          "current/profiles.tsv :: cpu_* stage timers at Containment/-@1",
          lambda c: agg(f1, abs, _stage_shift(c)[2])),

    # -- the two machines side by side --------------------------------------
    Macro("MapRatioMin", "x", "Smallest ratio of second-machine to reference-machine mapping time.",
          "current/results.tsv :: map_s at threads=1, shmap-rs",
          lambda c: agg(f2, min, c.map_ratios()), caveats=(TWO_MACHINES,)),
    Macro("MapRatioMax", "x", "Largest ratio of second-machine to reference-machine mapping time.",
          "current/results.tsv :: map_s at threads=1, shmap-rs",
          lambda c: agg(f2, max, c.map_ratios()), caveats=(TWO_MACHINES,)),
]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

def header(c: MCtx, comment: str) -> list[str]:
    return [
        f"{comment} GENERATED by benchmarks/scripts/manuscript.py -- do not edit.",
        f"{comment} Every number the manuscript's prose quotes, as a LaTeX macro.",
        *[f"{comment} set:        {set_id(s)}" for s in c.sets],
        f"{comment} reference:  {c.x.arch} (macros named ...X; ...A is {c.a.arch})",
        f"{comment} inputs:     sha256:{c.digest}",
        f"{comment} provenance: paper/generated/MACROS.md",
    ]


def values(c: MCtx) -> list[tuple[Macro, str]]:
    return [(m, m.compute(c)) for m in MACROS]


def render_tex(c: MCtx, vals: list[tuple[Macro, str]]) -> str:
    out = [*header(c, "%"), "%"]
    for m, v in vals:
        shown = grouped(int(v)) if m.group and v.isdigit() else v
        unit = f"  % {m.unit}" if m.unit else ""
        out.append(rf"\newcommand{{\{PREFIX}{m.name}}}{{{shown}}}{unit}")
    return "\n".join(out) + "\n"


def render_tsv(c: MCtx, vals: list[tuple[Macro, str]]) -> str:
    out = [*header(c, "#"), "\t".join(["macro", "value", "unit", "source", "meaning"])]
    for m, v in vals:
        out.append("\t".join([f"\\{PREFIX}{m.name}", v, m.unit, m.source, m.note]))
    return "\n".join(out) + "\n"


def render_provenance(c: MCtx, vals: list[tuple[Macro, str]]) -> str:
    out = [
        "# Provenance of the manuscript's numbers",
        "",
        "GENERATED by `benchmarks/scripts/manuscript.py` — do not edit. Every row is built from",
        "the `Macro` declaration that also produced the value, so this cannot describe a source",
        "the code does not read.",
        "",
        "[`paper/manuscript.tex`](manuscript.tex) contains **no numerals in its prose**. Each one",
        "is a `\\shm…` macro defined in `macros.tex` from the result sets below, so a",
        "re-measurement rewrites the sentences rather than leaving them quietly wrong.",
        "`manuscript.py --lint` enforces that by reporting any bare numeral in running text.",
        "",
        "| | |",
        "|---|---|",
    ]
    for s in c.sets:
        out.append(f"| `{s.arch}` | {set_id(s)} |")
    out += [
        f"| reference | `{c.x.arch}` — macros ending `X` name it, `A` names `{c.a.arch}` |",
        f"| input digest | `sha256:{c.digest}` |",
        "",
        "Regenerate with `python3 benchmarks/scripts/manuscript.py`; verify with `--check`,",
        "which fails if any value would change **or** if the draft uses a macro that no longer",
        "exists. Values are emitted twice: `macros.tex` for the typesetter, `macros.tsv` for",
        "everyone else, both from one computation so they cannot disagree.",
        "",
        "## The macros",
        "",
        "| macro | value | unit | meaning | taken from |",
        "|---|---:|---|---|---|",
    ]
    for m, v in vals:
        out.append(f"| `\\{PREFIX}{m.name}` | {v} | {m.unit or '—'} | {m.note} | `{m.source}` |")

    caveated = [(m, v) for m, v in vals if m.caveats]
    if caveated:
        out += ["", "## Read with", ""]
        for m, _ in caveated:
            for cv in m.caveats:
                out.append(f"- `\\{PREFIX}{m.name}` — {cv}")
    out += [
        "",
        "## Not generated",
        "",
        "- **Anything the tables and figures already carry.** A macro exists for a number the",
        "  *prose* states. A number a reader looks up in a table belongs to that table, which",
        "  `paper.py` and `crossarch.py` regenerate from the same result sets.",
        "- **Claims about the optimizations' individual effects.** Those were measured against",
        "  the build each change landed on and are deliberately not refreshed; they live in",
        "  [`PORT_CHANGES.md`](../PORT_CHANGES.md) and the manuscript cites them as such.",
        "",
    ]
    return "\n".join(out) + "\n"


def build_all(c: MCtx) -> dict[str, str]:
    vals = values(c)
    return {
        "macros.tex": render_tex(c, vals),
        "macros.tsv": render_tsv(c, vals),
        "MACROS.md": render_provenance(c, vals),
    }


# ---------------------------------------------------------------------------
# the draft, checked against the macros
# ---------------------------------------------------------------------------

USE_RE = re.compile(r"\\" + PREFIX + r"([A-Za-z]+)")

# Commands whose arguments are addresses, not prose: a label, a URL or the file
# name of a generated fragment may carry digits without any of them being a
# measurement. Removed before the scan rather than causing their line to be
# skipped, so a hand-typed number sharing a line with a \cite is still caught.
ADDRESS_CMDS = ("label", "ref", "cite", "input", "url", "href", "hypersetup",
                "bibitem", "pagestyle", "thispagestyle")
ADDRESS_RE = re.compile(r"\\(?:" + "|".join(ADDRESS_CMDS) + r")\s*(\[[^]]*\])?(\{[^{}]*\})*")
COMMENT_RE = re.compile(r"(?<!\\)%.*$")

# TeX lengths are layout, wherever they appear: `\vspace{-0.9cm}` and the
# `\\[0.25em]` after a line break are as much typesetting inside the body as
# `\geometry` is in the preamble.
LENGTH_RE = re.compile(
    r"\\(?:vspace|hspace|vskip|hskip|setlength|addtolength|rule)\*?\s*(\{[^{}]*\})*"
    r"|\\\\\s*\[[^]]*\]"
    r"|-?\d*\.?\d+\s*(?:pt|em|ex|cm|mm|in|bp|dd|pc|sp)\b"
)

# The deliberate exception. A line ending in `% lint-ok: <reason>` is exempt,
# and the reason is required -- the point is that somebody looked at the digit
# and can say why it is not a measurement, not that the check can be silenced.
LINT_OK_RE = re.compile(r"%\s*lint-ok:\s*\S")

NUMERAL_RE = re.compile(r"\d")

# Everything before \begin{document} is typesetting -- margins, font sizes,
# pgfplots compat levels -- and the bibliography is publication years. Neither
# is a measurement, and neither is prose.
BODY_START = re.compile(r"\\begin\{document\}")
SKIP_ENVS = ("thebibliography",)


def draft_uses(path: Path = DRAFT) -> set[str]:
    if not path.exists():
        return set()
    return set(USE_RE.findall(path.read_text()))


def lint_draft(path: Path = DRAFT) -> list[str]:
    """Lines of running text carrying a literal numeral.

    The whole point of the macros is that the draft holds no measurements, so
    this is what turns that from a convention into a checked property. It
    reads the body only, with comments, addresses and the bibliography removed
    first, and reports whatever digits are left -- each one a number somebody
    typed by hand, which the next benchmark run will not update.
    """
    if not path.exists():
        return []
    bad, in_body, skipping = [], False, ""
    for i, raw in enumerate(path.read_text().splitlines(), 1):
        if not in_body:
            in_body = bool(BODY_START.search(raw))
            continue
        if skipping:
            if rf"\end{{{skipping}}}" in raw:
                skipping = ""
            continue
        env = next((e for e in SKIP_ENVS if rf"\begin{{{e}}}" in raw), "")
        if env:
            skipping = env
            continue
        if LINT_OK_RE.search(raw):
            continue
        line = LENGTH_RE.sub("", ADDRESS_RE.sub("", COMMENT_RE.sub("", raw)))
        if NUMERAL_RE.search(line):
            bad.append(f"{path.name}:{i}: literal numeral in prose: {raw.strip()}")
    return bad


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help=f"output directory (default: {OUT})")
    ap.add_argument("--arch", action="append", default=None,
                    help="restrict to these architectures (repeatable)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any macro would change, or the draft uses an undefined one")
    ap.add_argument("--list", action="store_true",
                    help="print every macro's value, source and meaning, and stop")
    ap.add_argument("--lint", action="store_true",
                    help="report literal numerals in the draft's prose, and stop")
    a = ap.parse_args()

    if a.lint:
        bad = lint_draft()
        if not DRAFT.exists():
            print(f"no draft at {DRAFT}; nothing to lint")
            return 0
        for line in bad:
            print(line, file=sys.stderr)
        if bad:
            print(f"\n{len(bad)} literal numeral(s) in the draft's prose. Every measured "
                  f"number must be a \\{PREFIX}… macro, so that a re-measurement rewrites "
                  f"the sentence.\n  Add it to MACROS in benchmarks/scripts/manuscript.py.",
                  file=sys.stderr)
            return 1
        print(f"{DRAFT.name}: no literal numerals in prose")
        return 0

    suite = load_suite()
    suite_version = str(suite["suite_version"])
    sets = load_sets(suite_version, a.arch)
    if len(sets) < 2:
        have = ", ".join(s.arch for s in sets) or "none"
        # Not an error, for crossarch.py's reason: a two-machine paper whose
        # second machine has not been promoted yet is not stale, it is early.
        print(f"the manuscript's macros need two promoted result sets; "
              f"suite {suite_version} has {len(sets)} ({have}).")
        return 0

    c = MCtx(sets=sets, hosts=load_hosts(), digest=input_digest(sets), suite=suite)

    if a.list:
        for m, v in values(c):
            print(f"\\{PREFIX}{m.name} = {v} {m.unit}".rstrip())
            print(f"    from      {m.source}")
            print(f"    meaning   {m.note}")
            for cv in m.caveats:
                print(f"    caveat    {cv}")
        return 0

    files = build_all(c)
    out = Path(a.out) if a.out else OUT

    commits = {s.commit for s in c.sets}
    if len(commits) > 1:
        print(f"WARNING: the result sets measure different commits "
              f"({', '.join(sorted(commits))}). Every cross-machine number in the "
              f"manuscript is then between two different programs.", file=sys.stderr)

    defined = {m.name for m in MACROS}
    undefined = sorted(draft_uses() - defined)

    if a.check:
        stale = [n for n, body in sorted(files.items())
                 if not (out / n).exists() or (out / n).read_text() != body]
        if stale:
            print(f"manuscript macros out of date in {out}: {', '.join(stale)}\n"
                  f"regenerate with: python3 benchmarks/scripts/manuscript.py", file=sys.stderr)
            return 1
        if undefined:
            names = ", ".join("\\" + PREFIX + u for u in undefined)
            print(f"{DRAFT.name} uses macros that manuscript.py does not define: {names}\n"
                  f"  The draft would fail to typeset. Define them in MACROS, or stop "
                  f"using them.", file=sys.stderr)
            return 1
        print(f"{len(MACROS)} manuscript macros are current with "
              f"{', '.join(s.arch for s in c.sets)}")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    for n, body in sorted(files.items()):
        (out / n).write_text(body)
    print(f"wrote {len(MACROS)} macros to {out}/macros.tex from "
          f"{', '.join(f'{s.arch} ({s.host})' for s in c.sets)}")

    if undefined:
        names = ", ".join("\\" + PREFIX + u for u in undefined)
        print(f"\nWARNING: {DRAFT.name} uses undefined macros: {names}", file=sys.stderr)
    unused = sorted(defined - draft_uses()) if DRAFT.exists() else []
    if unused:
        # Not a failure: a macro may be defined ahead of the sentence that will
        # use it. Worth saying, because the usual cause is a sentence that was
        # rewritten to hardcode the number instead.
        print(f"note: {len(unused)} macro(s) defined but unused by the draft: "
              f"{', '.join(unused)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
