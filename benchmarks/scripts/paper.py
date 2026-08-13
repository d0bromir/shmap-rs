#!/usr/bin/env python3
"""Regenerate the paper's data-driven tables and figures from a result set.

  paper.py                    regenerate paper/generated/ from results/suite-<v>/current/
  paper.py <result-set-dir>   regenerate from a specific set
  paper.py --out DIR          write somewhere else (run.py uses this per run)
  paper.py --check            exit 1 if any artifact would change (CI uses this)
  paper.py --list             print what each artifact is built from, and stop

---------------------------------------------------------------------------
Why this exists, and what it guarantees
---------------------------------------------------------------------------
RESULTS.md is for us; the paper is for readers who cannot run anything. Both
used to be transcribed by hand from a run, which is how RESULTS.md came to
carry contradictory figures (see its header). report.py fixed that for
RESULTS.md. This does the same job for the paper's tables and figures.

Three guarantees, in the order they matter:

1. **Provenance.** Every artifact declares its inputs down to the column, the
   transformation applied, and the caveats a reader must carry. PROVENANCE.md
   is generated from those declarations, so the documentation cannot drift from
   the code that produced the numbers -- there is no second place to update.

2. **Auditability.** Every artifact is emitted twice: once as LaTeX to be
   \\input into the paper, and once as a .tsv holding exactly the numbers that
   LaTeX draws. The .tsv is the audit trail. A reader who distrusts a bar in a
   figure can read the value without a LaTeX toolchain, and a diff of two runs
   shows what moved without rendering anything.

3. **Reproducibility.** Output is a pure function of the result set: no
   timestamps of its own, no locale-dependent formatting, sorted iteration
   throughout. Running twice over one result set produces byte-identical files,
   which is what makes --check meaningful. Each file's header names the result
   set, its commit, and a digest of the inputs, so an artifact in a paper draft
   can be traced back to the exact measurement.

---------------------------------------------------------------------------
What is NOT generated
---------------------------------------------------------------------------
Only what the result set actually measured. Two consequences worth stating
because their absence is otherwise invisible:

- The per-read `time vs number of matches` scatter, which is the direct
  evidence for the logarithmic-scaling claim, needs per-read instrumentation
  that does not exist yet. It is listed in PROVENANCE.md as unavailable rather
  than approximated from whole-run totals, which would not show the shape of
  the curve at all.
- External mappers are measured threaded and without an index/mapping split,
  so their rows carry a threads column and empty phase cells rather than
  numbers that would look comparable and not be.

LaTeX requirements for the emitted fragments: booktabs (tables) and pgfplots
with `\\pgfplotsset{compat=1.18}` (figures). Neither is installed on the
benchmark host, so the fragments are emitted but never compiled here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import REPO, load_registry, load_suite  # noqa: E402
from compare import RESULTS, load_set  # noqa: E402
from layout import arch, current_dir, reference_mappers_dir  # noqa: E402
import report  # noqa: E402

# One directory per architecture: the artifacts are a pure function of a
# result set, and result sets are per-architecture, so a flat output
# directory let the second architecture generated overwrite the first --
# with --check still passing, because the files were consistent with
# whichever set was written last.
GENERATED_ROOT = REPO / "paper" / "generated"


def out_default(a: str | None = None) -> Path:
    return GENERATED_ROOT / (a or arch())
SUBJECT = "shmap-rs"
REFERENCE = "cpp-shmap"

# Metric order is the suite's, not alphabetical: "default, stricter, none"
# is the order the prose argues in.
METRIC_ORDER = ["Containment", "Jaccard", "bucket_SH"]

# pgfplots needs a colour per series and the paper is printed in greyscale as
# often as not, so series are distinguished by mark as well as colour.
SERIES_STYLE = [
    ("blue!70!black", "*"),
    ("red!70!black", "square*"),
    ("green!45!black", "triangle*"),
    ("orange!85!black", "diamond*"),
    ("violet", "pentagon*"),
]


# ---------------------------------------------------------------------------
# formatting helpers -- LaTeX-safe and locale-independent
# ---------------------------------------------------------------------------

def tex_escape(s: str) -> str:
    """Escape the characters that appear in our identifiers. `bucket_SH` in a
    table cell is a subscript and a missing-`$` error, not a metric name."""
    for a, b in (("\\", r"\textbackslash{}"), ("_", r"\_"), ("%", r"\%"),
                 ("&", r"\&"), ("#", r"\#")):
        s = s.replace(a, b)
    return s


def thousands(n) -> str:
    r"""LaTeX thin space as the separator: 149\,194. Matches the paper's style
    and avoids a comma that would be read as a decimal point in half of Europe."""
    return f"{int(round(float(n))):,}".replace(",", r"\,")


def fnum(x, places=2) -> str:
    return f"{float(x):.{places}f}"


def dash() -> str:
    """One em dash for 'not measured', never 0 and never blank. A blank cell
    reads as an oversight and a 0 reads as a measurement."""
    return "---"


# ---------------------------------------------------------------------------
# the artifact declaration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    """One paper table or figure, plus the provenance that must travel with it.

    `sources`, `transform` and `caveats` are not comments: PROVENANCE.md is
    built from these fields, so a builder that starts reading a new column
    without declaring it produces documentation that is visibly wrong.
    """
    name: str
    kind: str                       # "table" | "figure"
    caption: str
    label: str                      # LaTeX \label, so the paper can \ref it
    sources: tuple[str, ...]
    transform: tuple[str, ...]
    presentation: str
    build: Callable[["Ctx"], tuple[str, list[str], list[list]]]
    caveats: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class Ctx:
    """Everything a builder may read, loaded once and shared.

    Builders take this rather than reaching for files themselves, so the
    `sources` declaration on each artifact is the whole truth about its inputs.
    """
    rs: dict                        # result set: rows, checks, manifest, dir
    registry: dict                  # datasets.tsv
    counters: dict                  # (bench, metric, threads) -> -x counters
    timers: dict                    # (bench, metric, threads) -> -x stage times
    external: list                  # cached external-mapper runs
    per_read: dict                  # benchmark -> per-read rows, if collected
    digest: str                     # sha256 over the input files

    def rows(self, impl=None, threads=None) -> list[dict]:
        out = self.rs["rows"]
        if impl is not None:
            out = [r for r in out if r["impl"] == impl]
        if threads is not None:
            out = [r for r in out if r["threads"] == threads]
        return out

    def benchmarks(self) -> list[str]:
        return sorted({r["benchmark"] for r in self.rs["rows"]})

    def metrics(self, bid: str) -> list[str]:
        present = {r["benchmark"] == bid and r["metric"] or None
                   for r in self.rs["rows"] if r["benchmark"] == bid}
        return [m for m in METRIC_ORDER if m in present]

    def thread_counts(self) -> list[int]:
        return sorted({r["threads"] for r in self.rows(impl=SUBJECT)})

    def check(self, name: str, bid: str, metric: str) -> str | None:
        for c in self.rs["checks"]:
            if c["check"] == name and c["benchmark"] == bid and c["metric"] == metric:
                return c["detail"]
        return None

    def reads_in(self, bid: str) -> int:
        """Records in the read set, from the dataset registry -- needed for the
        'missed' column, which the PAF cannot supply because unmapped reads are
        simply absent from it."""
        r = next((r for r in self.rs["rows"] if r["benchmark"] == bid), None)
        if not r:
            return 0
        entry = self.registry.get(r["reads_id"], {})
        try:
            return int(entry.get("records", 0))
        except (TypeError, ValueError):
            return 0


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

# The headline benchmark for the peer table: real HiFi against the whole
# genome, the dataset the rest of the narrative argues on. One benchmark and
# one metric, because a note has room for four rows and not for forty --
# `table_mapper_comparison` carries every dataset and every metric.
PEER_BENCHMARK = "B01"
PEER_METRIC = "Containment"


def build_peers(c: Ctx) -> tuple[str, list[str], list[list]]:
    """shmap-rs beside the other long-read mappers, on one dataset.

    Cost is directly comparable only in the sense that each row is what that
    tool needed to map the same reads on the same host; the thread column is
    part of the measurement, not a footnote, because the peers are run at the
    thread count their own documentation assumes and shmap-rs is shown at one.

    The agreement column is CONCORDANCE, never accuracy. Winnowmap2 is the
    most accurate long-read mapper available and is still an estimate; where
    it and shmap-rs disagree, nothing here says which is right.
    """
    bid, metric = PEER_BENCHMARK, PEER_METRIC
    cols = ["tool", "threads", "map_s", "peak_rss_gb", "agreement_with_shmap_rs"]
    data: list[list] = []

    for impl in (SUBJECT, REFERENCE):
        r = next((x for x in c.rows(impl=impl)
                  if x["benchmark"] == bid and x["metric"] == metric
                  and x["threads"] == 1), None)
        if r:
            data.append([impl, 1,
                         float(r["map_s"]) if r.get("map_s") not in (None, "") else None,
                         round(int(r["peak_rss_kb"]) / 1048576, 2), None])

    for e in c.external:
        if e.get("benchmark") != bid or not e.get("wall_s"):
            continue
        # `good=` is the share of this tool's mappings that shmap-rs places
        # compatibly; the check that computes it is named for the tool.
        detail = c.check(f"concordance_{e['mapper']}", bid, metric) or ""
        agree = None
        for field in detail.split():
            if field.startswith("good="):
                try:
                    agree = round(float(field.split("=", 1)[1]), 4)
                except ValueError:
                    agree = None
        data.append([e["mapper"], e.get("threads"), float(e["wall_s"]),
                     round(int(e["peak_rss_kb"]) / 1048576, 2)
                     if e.get("peak_rss_kb") else None,
                     agree])

    lines = [
        r"\begin{tabular}{lrrrr}",
        r"\toprule",
        r"Tool & \texttt{-@} & Map (s) & Mem (GB) & Agreement \\",
        r"\midrule",
    ]
    for row in data:
        lines.append(" & ".join([
            tex_escape(str(row[0])),
            str(row[1]) if row[1] else dash(),
            fnum(row[2]) if row[2] is not None else dash(),
            fnum(row[3]) if row[3] is not None else dash(),
            fnum(row[4], 4) if row[4] is not None else dash(),
        ]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines), cols, data


def build_comparison(c: Ctx) -> tuple[str, list[str], list[list]]:
    """The paper's tool-comparison table: sensitivity, confident errors, cost."""
    cols = ["benchmark", "tool", "metric", "threads", "mapped", "mapq60",
            "missed_pct", "wrong_q60", "index_s", "map_s", "peak_rss_gb"]
    data: list[list] = []

    for bid in c.benchmarks():
        total = c.reads_in(bid)
        for impl in (SUBJECT, REFERENCE):
            for metric in c.metrics(bid):
                r = next((x for x in c.rows(impl=impl)
                          if x["benchmark"] == bid and x["metric"] == metric
                          and x["threads"] == 1), None)
                if not r:
                    continue
                # "wrong at mapq 60" only exists where reads carry truth, i.e.
                # the simulated benchmark. Elsewhere it is unknowable, not zero.
                wq = c.check("wrong_q60", bid, metric) if impl == SUBJECT else None
                wrong = wq.split("/")[0] if wq else None
                q60 = int(r["mapq60"])
                data.append([
                    bid, impl, metric, 1, int(r["mapped"]), q60,
                    round(100.0 * (total - q60) / total, 2) if total else None,
                    int(wrong) if wrong is not None else None,
                    float(r["index_s"]) if r.get("index_s") not in (None, "") else None,
                    float(r["map_s"]) if r.get("map_s") not in (None, "") else None,
                    round(int(r["peak_rss_kb"]) / 1048576, 2),
                ])
        for e in c.external:
            if e.get("benchmark") != bid or not e.get("wall_s"):
                continue
            mapped = e.get("mapped")
            data.append([
                bid, e["mapper"], None, e.get("threads"), mapped, None, None, None,
                None, float(e["wall_s"]),
                round(int(e["peak_rss_kb"]) / 1048576, 2) if e.get("peak_rss_kb") else None,
            ])

    lines = [
        r"\begin{tabular}{lllrrrrrrrr}",
        r"\toprule",
        r"Dataset & Tool & Metric & \texttt{-@} & Mapped & Mapq 60 & Missed & "
        r"Wrong & Index & Map & Mem \\",
        r" & & & & & & (\%) & Q60 & (s) & (s) & (GB) \\",
        r"\midrule",
    ]
    last = None
    for row in data:
        if last is not None and row[0] != last:
            lines.append(r"\midrule")
        last = row[0]
        cells = [tex_escape(str(row[0])), tex_escape(str(row[1])),
                 tex_escape(row[2]) if row[2] else dash(),
                 str(row[3]) if row[3] else dash(),
                 thousands(row[4]) if row[4] is not None else dash(),
                 thousands(row[5]) if row[5] is not None else dash(),
                 fnum(row[6], 2) if row[6] is not None else dash(),
                 thousands(row[7]) if row[7] is not None else dash(),
                 fnum(row[8]) if row[8] is not None else dash(),
                 fnum(row[9]) if row[9] is not None else dash(),
                 fnum(row[10]) if row[10] is not None else dash()]
        lines.append(" & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines), cols, data


def build_seed_heuristic(c: Ctx) -> tuple[str, list[str], list[list]]:
    """Efficiency of the seed heuristic: work avoided, and work still wasted."""
    cols = ["benchmark", "metric", "possible_per_read", "examined_per_read",
            "in_mapping_per_read", "realized_potential", "unrealized_potential",
            "seeded_buckets_per_read", "final_buckets_per_read"]
    data: list[list] = []
    for bid in c.benchmarks():
        for metric in c.metrics(bid):
            k = c.counters.get((bid, metric, 1))
            if not k:
                continue
            reads = k.get("reads", 0)
            poss, seen = k.get("possible_matches", 0), k.get("total_matches", 0)
            used = k.get("matches_in_reported_mappings", 0)
            if not (reads and seen and used):
                continue
            data.append([
                bid, metric,
                round(poss / reads, 1), round(seen / reads, 1), round(used / reads, 1),
                round(poss / seen, 1), round(seen / used, 1),
                round(k.get("seeded_buckets", 0) / reads, 1),
                round(k.get("final_buckets", 0) / reads, 2),
            ])

    lines = [
        r"\begin{tabular}{llrrrrrrr}",
        r"\toprule",
        r"Dataset & Metric & \multicolumn{3}{c}{Matches per read} & "
        r"\multicolumn{2}{c}{Potential} & \multicolumn{2}{c}{Buckets per read} \\",
        r"\cmidrule(lr){3-5}\cmidrule(lr){6-7}\cmidrule(lr){8-9}",
        r" & & possible & examined & in mapping & realized & unrealized & seeded & final \\",
        r"\midrule",
    ]
    last = None
    for row in data:
        if last is not None and row[0] != last:
            lines.append(r"\midrule")
        last = row[0]
        lines.append(" & ".join([
            tex_escape(row[0]), tex_escape(row[1]),
            thousands(row[2]), thousands(row[3]), thousands(row[4]),
            f"{fnum(row[5], 1)}$\\times$", f"{fnum(row[6], 1)}$\\times$",
            fnum(row[7], 1), fnum(row[8], 2)]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines), cols, data


def _axis(body: list[str], *, xlabel, ylabel, extra=(), legend="north east") -> str:
    opts = [f"xlabel={{{xlabel}}}", f"ylabel={{{ylabel}}}",
            "width=0.8\\linewidth", "height=6cm",
            f"legend pos={legend}", "legend cell align=left",
            "grid=major", "grid style={gray!25}", "tick align=outside"]
    opts.extend(extra)
    return "\n".join([r"\begin{tikzpicture}", r"\begin{axis}[",
                      "  " + ",\n  ".join(opts), "]", *body,
                      r"\end{axis}", r"\end{tikzpicture}"])


def build_thread_scaling(c: Ctx) -> tuple[str, list[str], list[list]]:
    """Whole-run speedup against thread count, one series per dataset."""
    cols = ["benchmark", "threads", "wall_s", "speedup_vs_1"]
    data: list[list] = []
    body: list[str] = []
    threads = c.thread_counts()
    for i, bid in enumerate(c.benchmarks()):
        colour, mark = SERIES_STYLE[i % len(SERIES_STYLE)]
        pts = []
        base = next((r for r in c.rows(impl=SUBJECT)
                     if r["benchmark"] == bid and r["metric"] == "Containment"
                     and r["threads"] == 1), None)
        if not base:
            continue
        for t in threads:
            r = next((x for x in c.rows(impl=SUBJECT)
                      if x["benchmark"] == bid and x["metric"] == "Containment"
                      and x["threads"] == t), None)
            if not r:
                continue
            sp = base["wall_s"] / r["wall_s"]
            pts.append((t, sp))
            data.append([bid, t, round(r["wall_s"], 2), round(sp, 2)])
        if pts:
            body.append(f"\\addplot[color={colour},mark={mark},thick] coordinates {{"
                        + " ".join(f"({t},{sp:.2f})" for t, sp in pts) + "};")
            body.append(f"\\addlegendentry{{{tex_escape(bid)}}}")
    # Linear speedup, so a reader sees the gap rather than inferring it.
    if threads:
        body.append("\\addplot[dashed,gray,forget plot] coordinates {"
                    + " ".join(f"({t},{t})" for t in threads) + "};")
    tex = _axis(body, xlabel="threads (\\texttt{-@})", ylabel="speedup vs \\texttt{-@1}",
                extra=["xmode=log", "ymode=log", "log basis x=2", "log basis y=2",
                       f"xtick={{{','.join(str(t) for t in threads)}}}",
                       "xticklabels={" + ",".join(str(t) for t in threads) + "}"],
                legend="north west")
    return tex, cols, data


def build_memory_vs_threads(c: Ctx) -> tuple[str, list[str], list[list]]:
    """Peak RSS against thread count -- the claim §1 of RESULTS.md gets wrong
    if it is quoted single-threaded."""
    cols = ["benchmark", "threads", "peak_rss_gb"]
    data: list[list] = []
    body: list[str] = []
    for i, bid in enumerate(c.benchmarks()):
        colour, mark = SERIES_STYLE[i % len(SERIES_STYLE)]
        pts = []
        for t in c.thread_counts():
            r = next((x for x in c.rows(impl=SUBJECT)
                      if x["benchmark"] == bid and x["metric"] == "Containment"
                      and x["threads"] == t), None)
            if not r:
                continue
            g = int(r["peak_rss_kb"]) / 1048576
            pts.append((t, g))
            data.append([bid, t, round(g, 3)])
        if pts:
            body.append(f"\\addplot[color={colour},mark={mark},thick] coordinates {{"
                        + " ".join(f"({t},{g:.3f})" for t, g in pts) + "};")
            body.append(f"\\addlegendentry{{{tex_escape(bid)}}}")
    # The C++ is flat at its single figure whatever the thread count, which is
    # the entire point of the comparison.
    cpp = next((r for r in c.rows(impl=REFERENCE)), None)
    if cpp:
        g = int(cpp["peak_rss_kb"]) / 1048576
        ts = c.thread_counts()
        body.append("\\addplot[black,dashed,thick,mark=none] coordinates {"
                    + " ".join(f"({t},{g:.3f})" for t in ts) + "};")
        body.append(r"\addlegendentry{C++ \texttt{shmap}}")
        data.append([REFERENCE, None, round(g, 3)])
    tex = _axis(body, xlabel="threads (\\texttt{-@})", ylabel="peak RSS (GB)",
                extra=["xmode=log", "log basis x=2", "ymin=0",
                       f"xtick={{{','.join(str(t) for t in c.thread_counts())}}}",
                       "xticklabels={" + ",".join(str(t) for t in c.thread_counts()) + "}"],
                legend="north west")
    return tex, cols, data


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank quantile. No interpolation and no numpy: bins hold hundreds
    to thousands of reads, where the difference is far below the run-to-run
    variation the band exists to show."""
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


def _fit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Ordinary least squares, returning (slope, intercept, r_squared).

    Written out rather than pulled from a library because the benchmark host
    has no scientific Python stack and adding one to draw a trend line would
    be a poor trade.
    """
    n = len(xs)
    if n < 3:
        return 0.0, 0.0, 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return 0.0, my, 0.0
    slope = sxy / sxx
    intercept = my - slope * mx
    sst = sum((y - my) ** 2 for y in ys)
    ssr = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ssr / sst if sst > 0 else 0.0
    return slope, intercept, r2


def build_time_vs_matches(c: Ctx) -> tuple[str, list[str], list[list]]:
    """Per-read mapping time against the number of matches the read examined.

    The direct test of the scaling claim: if per-read cost were linear in
    matches this is a straight line on log-log and a sharp curve on log-linear;
    if it is logarithmic it is a straight line on log-linear, which is how it
    is drawn.
    """
    import math

    cols = ["benchmark", "bin", "n_reads", "matches_median", "time_median_us",
            "time_q1_us", "time_q3_us"]
    data: list[list] = []
    body: list[str] = []
    fits: list[str] = []

    for i, (bid, rows) in enumerate(sorted(c.per_read.items())):
        colour, mark = SERIES_STYLE[i % len(SERIES_STYLE)]
        pts = [(r["examined_matches"], r["query_mapping_s"]) for r in rows
               if r["examined_matches"] > 0 and r["query_mapping_s"] > 0]
        if len(pts) < 50:
            continue
        lo = math.log10(min(x for x, _ in pts))
        hi = math.log10(max(x for x, _ in pts))
        nbins = 18
        width = (hi - lo) / nbins if hi > lo else 1.0
        buckets: dict[int, list[tuple[float, float]]] = {}
        for x, y in pts:
            b = min(nbins - 1, int((math.log10(x) - lo) / width)) if width else 0
            buckets.setdefault(b, []).append((x, y))

        coords, fx, fy = [], [], []
        band_hi, band_lo = [], []
        for b in sorted(buckets):
            vals = buckets[b]
            # A bin holding a handful of reads is noise, not a data point.
            if len(vals) < 25:
                continue
            xs = sorted(x for x, _ in vals)
            ys = sorted(y for _, y in vals)
            xm = _quantile(xs, 0.5)
            ymed, yq1, yq3 = (_quantile(ys, q) * 1e6 for q in (0.5, 0.25, 0.75))
            coords.append((xm, ymed))
            band_hi.append((xm, yq3))
            band_lo.append((xm, yq1))
            fx.append(math.log10(xm))
            fy.append(ymed)
            data.append([bid, b, len(vals), round(xm, 1), round(ymed, 2),
                         round(yq1, 2), round(yq3, 2)])
        if not coords:
            continue

        # Interquartile band on the simulated set only: five overlapping bands
        # is an unreadable figure, and B02 is the set with known truth.
        if bid == "B02":
            body.append("\\addplot[name path=hi,draw=none,forget plot] coordinates {"
                        + " ".join(f"({x:.1f},{y:.2f})" for x, y in band_hi) + "};")
            body.append("\\addplot[name path=lo,draw=none,forget plot] coordinates {"
                        + " ".join(f"({x:.1f},{y:.2f})" for x, y in band_lo) + "};")
            body.append(f"\\addplot[{colour}!15,forget plot] fill between[of=hi and lo];")
        body.append(f"\\addplot[color={colour},mark={mark},thick] coordinates {{"
                    + " ".join(f"({x:.1f},{y:.2f})" for x, y in coords) + "};")
        body.append(f"\\addlegendentry{{{tex_escape(bid)}}}")

        slope, _, r2 = _fit(fx, fy)
        lin_slope, _, lin_r2 = _fit([10 ** v for v in fx], fy)
        fits.append(f"{bid}: {slope:.1f} us per decade, R2={r2:.3f} "
                    f"(linear-in-matches fit R2={lin_r2:.3f}, {lin_slope*1000:.3f} ns/match)")
        data.append([bid, "fit", len(fx), None, round(slope, 3), round(r2, 4),
                     round(lin_r2, 4)])

    tex = _axis(body, xlabel="matches examined by the read",
                ylabel="per-read mapping time ($\\mu$s)",
                extra=["xmode=log", "log basis x=10", "ymin=0"],
                legend="north west")
    # `fillbetween` is not loaded by pgfplots itself, and the band silently
    # vanishes without it -- worse than an error, so it is stated in the file.
    tex = ("% requires: \\usepgfplotslibrary{fillbetween}\n"
           + "\n".join(f"% fit {f}" for f in fits) + ("\n" if fits else "") + tex)
    return tex, cols, data


def build_stage_breakdown(c: Ctx) -> tuple[str, list[str], list[list]]:
    """Where mapping time goes, as shares of query_mapping, per metric."""
    stages = ["match_seeds", "match_rest", "prepare", "sketching", "bucket_merge"]
    cols = ["benchmark", "metric", "stage", "share_pct", "query_mapping_s"]
    data: list[list] = []
    bars: list[str] = []
    labels: list[str] = []
    per_stage: dict[str, list[float]] = {s: [] for s in stages}
    for bid in c.benchmarks():
        for metric in c.metrics(bid):
            t = c.timers.get((bid, metric, 1))
            if not t or not t.get("query_mapping"):
                continue
            total = t["query_mapping"]
            labels.append(f"{bid} {metric}")
            for s in stages:
                share = 100.0 * t.get(s, 0.0) / total
                per_stage[s].append(share)
                data.append([bid, metric, s, round(share, 2), round(total, 2)])
    for i, s in enumerate(stages):
        colour, _ = SERIES_STYLE[i % len(SERIES_STYLE)]
        bars.append(f"\\addplot+[ybar,fill={colour},draw=none] coordinates {{"
                    + " ".join(f"({j},{v:.2f})" for j, v in enumerate(per_stage[s]))
                    + "};")
        bars.append(f"\\addlegendentry{{{tex_escape(s)}}}")
    tex = _axis(bars, xlabel="", ylabel="share of \\texttt{query\\_mapping} (\\%)",
                extra=["ybar stacked", "bar width=6pt", "ymin=0", "ymax=100",
                       "xtick={" + ",".join(str(i) for i in range(len(labels))) + "}",
                       "xticklabels={" + ",".join("{" + tex_escape(x) + "}" for x in labels) + "}",
                       "x tick label style={rotate=60,anchor=east,font=\\tiny}",
                       "legend style={font=\\small}"],
                legend="outer north east")
    return tex, cols, data


# ---------------------------------------------------------------------------
# the registry: every artifact, with its provenance
# ---------------------------------------------------------------------------

ARTIFACTS: tuple[Artifact, ...] = (
    Artifact(
        name="table_peers",
        kind="table",
        caption=(r"shmap-rs beside other long-read mappers on the headline real-HiFi "
                 r"dataset, same host, same reads. The thread column is part of the "
                 r"measurement: the peers run at the count their own documentation "
                 r"assumes, shmap-rs at one. \emph{Agreement} is the share of that "
                 r"tool's mappings shmap-rs places compatibly --- concordance, not "
                 r"accuracy: where they differ, nothing here says which is right."),
        label="tab:peers",
        sources=(
            "benchmarks/results/suite-<v>/<arch>/current/results.tsv :: map_s, "
            "peak_rss_kb for shmap-rs and cpp-shmap at -@1",
            "the cached external-mapper corpus :: wall_s, peak_rss_kb, threads",
            "benchmarks/results/suite-<v>/<arch>/current/checks.tsv :: "
            "concordance_<mapper>, the good= field",
        ),
        transform=(
            f"One benchmark ({PEER_BENCHMARK}) and one metric ({PEER_METRIC}), because a "
            f"two-page note has room for four rows; table_mapper_comparison carries every "
            f"dataset and metric.",
            "shmap-rs and the C++ are taken at -@1; the peers at whatever thread count "
            "their cached run used, which is printed rather than normalised away.",
            "Agreement is parsed from the concordance check's good= field, which is the "
            "share of the peer's mappings shmap-rs places compatibly.",
            "peak_rss_gb = peak_rss_kb / 1048576.",
        ),
        presentation="booktabs tabular, one row per tool.",
        caveats=(
            "AGREEMENT IS CONCORDANCE, NEVER ACCURACY. Winnowmap2 is the most accurate "
            "long-read mapper available and is still an estimate, not ground truth; the "
            "only accuracy number in this suite comes from the simulated benchmark, whose "
            "reads carry true positions.",
            "The map-time column compares tools doing different amounts of work: shmap-rs "
            "and the C++ emit mappings only, and the peers were run to emit the same, but "
            "their algorithms differ in what they compute on the way.",
            "mapquik is a low-divergence mapper by its own paper's account and is not run "
            "on the ONT benchmark at all; its numbers here are also specific to a host "
            "with AVX-512, since its SIMD and scalar paths are not interchangeable.",
        ),
        build=build_peers,
    ),
    Artifact(
        name="table_mapper_comparison",
        kind="table",
        caption="Long-read mapping tools on the benchmark suite. "
                "Mapq~60 is the count of confident mappings; Missed is the share of input reads "
                "either unmapped or below mapq~60; Wrong Q60 counts confident mappings that are "
                "wrong against ground truth.",
        label="tab:comparison",
        sources=(
            "results.tsv :: benchmark, impl, metric, threads, mapped, mapq60, index_s, map_s, peak_rss_kb",
            "checks.tsv :: wrong_q60 detail (numerator)",
            "benchmarks/data/datasets.tsv :: records, for the read count the 'missed' share needs",
            "benchmarks/results/reference-mappers/<arch>/manifest.json :: mapper, wall_s, "
            "peak_rss_kb, mapped — this architecture's corpus only",
        ),
        transform=(
            "Keep the -@1 row for each (benchmark, implementation, metric); -@1 is the "
            "like-for-like column because the C++ has no threading.",
            "missed_pct = 100 * (records - mapq60) / records, using the registry's record "
            "count rather than the PAF's, since unmapped reads are absent from a PAF.",
            "wrong_q60 = numerator of the wrong_q60 check detail, present only where reads "
            "carry truth in their headers.",
            "peak_rss_gb = peak_rss_kb / 1048576.",
            "External mappers are appended per benchmark from this architecture's cached "
            "corpus; they have no metric and no phase split, so those cells are em-dashes. "
            "An architecture whose corpus has not been built has no external rows at all.",
        ),
        presentation="booktabs tabular, grouped by dataset with a rule between groups; "
                     "counts use LaTeX thin spaces as thousands separators.",
        caveats=(
            "External mappers were measured at --threads 32 and shmap-rs rows at -@1. The "
            "threads column carries this; the numbers are not comparable down the column.",
            "Wrong Q60 is unknowable for real reads and shown as an em-dash there, not as 0.",
            "The C++ reference is a median of three runs; shmap-rs rows are a single run "
            "(RESULTS.md 10 explains why, and that per-row variance reaches ~10%).",
            "The C++ is re-measured only when its binary changes, so its rows may come from an "
            "earlier date than the shmap-rs rows beside them. RESULTS.md names that date when "
            "the two differ.",
        ),
        build=build_comparison,
    ),
    Artifact(
        name="table_seed_heuristic",
        kind="table",
        caption="Efficiency of the seed heuristic. Realized potential is possible matches "
                "divided by matches examined (work avoided); unrealized potential is matches "
                "examined divided by matches inside the reported mapping (work still wasted).",
        label="tab:seedheuristic",
        sources=(
            "raw-profiles.tar.gz :: global.counters {possible_matches, total_matches, "
            "matches_in_reported_mappings, seeded_buckets, final_buckets, reads}, -@1 reports only",
        ),
        transform=(
            "Per-read figures divide each whole-run counter by the reads counter.",
            "realized = possible_matches / total_matches.",
            "unrealized = total_matches / matches_in_reported_mappings.",
        ),
        presentation="booktabs tabular with grouped column headers over the three match "
                     "columns, the two ratios, and the two bucket columns.",
        caveats=(
            "'examined' counts matches enumerated during seeding only. The per-block pruning "
            "pass binary-searches to a block and then walks the hits inside it, and those are "
            "never added to the counter, so realized potential is an upper bound on work avoided.",
            "Counters are whole-run sums taken at -@1; they are thread-invariant because the "
            "output is byte-identical across thread counts.",
            "lost_on_seeding and lost_on_pruning are deliberately absent: both are inert in the "
            "C++ this was ported from and ported as such, so they measure nothing.",
        ),
        build=build_seed_heuristic,
    ),
    Artifact(
        name="fig_thread_scaling",
        kind="figure",
        caption="Whole-run speedup against thread count, Containment. The dashed line is "
                "linear speedup.",
        label="fig:threadscaling",
        sources=("results.tsv :: benchmark, threads, wall_s, for impl=shmap-rs, metric=Containment",),
        transform=(
            "speedup = wall_s at -@1 / wall_s at -@N, per dataset.",
            "Both axes are log2 so that linear speedup is a straight line and the departure "
            "from it is the visible quantity.",
        ),
        presentation="pgfplots line plot, one series per dataset, distinguished by both colour "
                     "and mark so it survives greyscale printing.",
        caveats=(
            "Whole-run, so it is bounded by the serial index share and is always lower than "
            "the mapper's own scaling; RESULTS.md 3b separates them.",
            "Containment only. The other metrics scale the same way and would triple the "
            "series count for no additional information.",
            "These curves sit below the speedup column of RESULTS.md 3, and the two do not "
            "disagree: that column reports the best of the three metrics, which is Jaccard, "
            "because Jaccard is slowest at -@1 and so has the most room to recover. B01 at "
            "-@32 is 6.96x here and 8.28x there for exactly that reason.",
        ),
        build=build_thread_scaling,
    ),
    Artifact(
        name="fig_memory_vs_threads",
        kind="figure",
        caption="Peak resident memory against thread count, Containment, with the C++ "
                "reference as a horizontal line.",
        label="fig:memorythreads",
        sources=(
            "results.tsv :: benchmark, threads, peak_rss_kb, for impl=shmap-rs, metric=Containment",
            "results.tsv :: peak_rss_kb for impl=cpp-shmap",
        ),
        transform=("peak_rss_gb = peak_rss_kb / 1048576.",
                   "The C++ is drawn as a constant line across the same x range: it is "
                   "single-threaded, so its memory does not vary with this axis."),
        presentation="pgfplots line plot, log2 x axis, linear y from zero so that the ratio "
                     "between the two implementations is read off the axis honestly.",
        caveats=(
            "The memory advantage is a function of thread count, not a single number. "
            "Quoting the -@1 ratio alone under-provisions a deep, highly parallel run.",
        ),
        build=build_memory_vs_threads,
    ),
    Artifact(
        name="fig_time_vs_matches",
        kind="figure",
        caption="Per-read mapping time against the number of matches the read examined, "
                "Containment. Points are per-bin medians over reads grouped into 18 "
                "logarithmic bins; the shaded band is the interquartile range for the "
                "simulated set.",
        label="fig:timevsmatches",
        sources=("per-read-<benchmark>-<metric>.tsv :: examined_matches, query_mapping_s, "
                 "written by shmap --per-read-stats",),
        transform=(
            "Drop reads with zero examined matches or zero measured time; both are "
            "degenerate rather than fast, and neither can be placed on a log axis.",
            "Bin reads into 18 equal-width bins in log10(examined_matches); discard bins "
            "holding fewer than 25 reads, which are noise rather than data points.",
            "Plot the per-bin median time against the per-bin median match count; the band "
            "is the 25th to 75th percentile of time within the bin.",
            "Fit per-bin median time against log10(matches) by least squares, and against "
            "matches directly for comparison. Both fits are recorded as comments at the top "
            "of the .tex and as 'fit' rows in the .tsv.",
        ),
        presentation="pgfplots line plot on a log x axis and a linear y axis from zero, so a "
                     "logarithmic relationship reads as a straight line. Needs "
                     "\\usepgfplotslibrary{fillbetween} for the band.",
        caveats=(
            "This figure is descriptive and is NOT a test of the O(R*m*log M) bound. Read it "
            "as what a read of a given match count costs, which is the quantity a user feels.",
            "The two fits recorded in the .tex disagree with each other and neither settles "
            "the question. Against examined matches, a linear fit beats a logarithmic one on "
            "B02 (R2 0.958 vs 0.677) and B05 (0.949 vs 0.763), while B03 favours logarithmic "
            "(0.897 vs 0.761). That is not evidence against the bound: the bound has three "
            "factors -- blocks visited R, sketch size m, and log M -- which co-vary across "
            "reads, so a single-variable fit attributes their combined growth to whichever "
            "variable is on the axis. Normalising by m leaves linear ahead (R2 ~0.92 vs "
            "~0.59); normalising by m*R puts logarithmic ahead but with a negative slope and "
            "R2 only 0.52-0.73. Isolating the log term needs reads matched on R and m, which "
            "this sampling was not designed for.",
            "Medians, not a raw scatter. 60 000 points would dominate the PDF and hide the "
            "relationship; the .tsv carries the binned values and the reads are on disk.",
            "'examined matches' is the seeding count, so this measures how per-read cost "
            "grows with the work seeding hands on -- not with every match the mapper touches. "
            "The per-block pruning pass walks hits it never counts.",
            "Per-read times are microseconds measured with a single clock read either side. "
            "Individual reads are noisy at that scale, which is why nothing below a bin "
            "median is plotted.",
            "Collected by a separate invocation from the timing matrix, and possibly at a "
            "later commit: manifest.json records per_read_stats_provenance when it was.",
        ),
        build=build_time_vs_matches,
    ),
    Artifact(
        name="fig_stage_breakdown",
        kind="figure",
        caption="Where mapping time goes: stage shares of \\texttt{query\\_mapping}, "
                "single-threaded.",
        label="fig:stages",
        sources=("raw-profiles.tar.gz :: global.timers_secs {query_mapping, match_seeds, "
                 "match_rest, prepare, sketching, bucket_merge}, -@1 reports only",),
        transform=("share = 100 * stage seconds / query_mapping seconds, per "
                   "(dataset, metric).",
                   "Stages are the top-level ones only; nested stages such as refine and "
                   "collect_kmer_info are inside their parents and would double-count."),
        presentation="pgfplots stacked bar chart, one bar per (dataset, metric).",
        caveats=(
            "Shares do not reach 100%: the listed stages are the top-level ones and the "
            "remainder is unattributed time inside query_mapping.",
            "CPU time summed across workers would exceed the wall at high thread counts, "
            "which is why this is the -@1 report.",
        ),
        build=build_stage_breakdown,
    ),
)


# ---------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------

def input_digest(rs: dict) -> str:
    """sha256 over the files an artifact can read, so a paper draft can be tied
    to an exact measurement even if the result set is later moved or renamed."""
    h = hashlib.sha256()
    for name in ("results.tsv", "checks.tsv", "profiles.tsv", "manifest.json",
                 "raw-profiles.tar.gz"):
        p = rs["dir"] / name
        if p.exists():
            h.update(name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:16]


def header(a: Artifact, ctx: Ctx, comment: str) -> list[str]:
    """The same provenance on every artifact, in the file itself. A fragment
    pasted into a draft six months from now still says where it came from."""
    man = ctx.rs["manifest"]
    return [
        f"{comment} GENERATED by benchmarks/scripts/paper.py -- do not edit.",
        f"{comment} artifact:   {a.name} ({a.kind})",
        f"{comment} result set: {ctx.rs['dir'].name}",
        f"{comment} commit:     {man.get('commit', '?')[:12]}",
        f"{comment} host:       {man.get('host', '?')}",
        f"{comment} measured:   {man.get('finished', '?')[:10]}",
        f"{comment} inputs:     sha256:{ctx.digest}",
        f"{comment} provenance: paper/generated/PROVENANCE.md#{a.name}",
    ]


def render(a: Artifact, ctx: Ctx) -> dict[str, str]:
    """Returns {filename: content} for one artifact: the LaTeX and its data."""
    body, cols, data = a.build(ctx)
    tex = "\n".join([
        *header(a, ctx, "%"),
        "%",
        r"\begin{" + ("table" if a.kind == "table" else "figure") + "}[tb]",
        r"\centering",
        body,
        r"\caption{" + a.caption + "}",
        r"\label{" + a.label + "}",
        r"\end{" + ("table" if a.kind == "table" else "figure") + "}",
        "",
    ])
    tsv_lines = [*header(a, ctx, "#"), "\t".join(cols)]
    for row in data:
        tsv_lines.append("\t".join("" if v is None else str(v) for v in row))
    return {f"{a.name}.tex": tex, f"{a.name}.tsv": "\n".join(tsv_lines) + "\n"}


def render_provenance(ctx: Ctx) -> str:
    man = ctx.rs["manifest"]
    out = [
        "# Provenance of the generated paper artifacts",
        "",
        "GENERATED by `benchmarks/scripts/paper.py` — do not edit. Every entry below is built from the",
        "`Artifact` declaration that also produced the file, so this document cannot describe a",
        "transformation the code does not perform.",
        "",
        "| | |",
        "|---|---|",
        f"| result set | `{ctx.rs['dir'].name}` |",
        f"| commit | `{man.get('commit', '?')[:12]}` |",
        f"| host | `{man.get('host', '?')}` |",
        f"| measured | {man.get('finished', '?')[:10]} |",
        f"| suite / datasets | {man.get('suite_version', '?')} / {man.get('dataset_version', '?')} |",
        f"| input digest | `sha256:{ctx.digest}` |",
        "",
        "Regenerate with `python3 benchmarks/scripts/paper.py`; verify with `--check`, which fails if any",
        "artifact would change. Each artifact is emitted twice: a `.tex` fragment to `\\input`",
        "into the paper, and a `.tsv` holding exactly the numbers the fragment draws.",
        "",
        "LaTeX requirements: `booktabs` for the tables, `pgfplots` (`\\pgfplotsset{compat=1.18}`)",
        "for the figures.",
        "",
    ]
    for a in ARTIFACTS:
        out += [f"## {a.name}", "",
                f"**{a.kind.capitalize()}** — {a.caption}", "",
                f"Files: `{a.name}.tex`, `{a.name}.tsv`. LaTeX label: `{a.label}`.", "",
                "**Taken from**", ""]
        out += [f"- `{s}`" for s in a.sources]
        out += ["", "**Transformed by**", ""]
        out += [f"{i}. {s}" for i, s in enumerate(a.transform, 1)]
        out += ["", "**Presented as**", "", a.presentation, ""]
        if a.caveats:
            out += ["**Read with**", ""]
            out += [f"- {s}" for s in a.caveats]
            out += [""]
    out += [
        "## Not generated",
        "",
        "Stated here because an absent figure is otherwise indistinguishable from one nobody",
        "thought of.",
        "",
        "- **Per-read runtime against per-read match count.** The direct evidence for the",
        "  logarithmic-scaling claim, and the one figure the algorithm's own argument most wants.",
        "  It needs per-read timing and per-read match counts emitted together, which the `-x`",
        "  reports do not do — they aggregate over the whole run. Approximating it from whole-run",
        "  totals would produce a plot of five points that shows none of the shape.",
        "- **Accuracy against sampling rate.** The measurements exist (RESULTS.md 8) but were made",
        "  by a standalone sweep rather than by the suite, so they are not in any result set. They",
        "  become generable when the sweep becomes a suite parameter set.",
        "",
    ]
    return "\n".join(out)


def build_all(rs: dict) -> dict[str, str]:
    ctx = Ctx(
        rs=rs,
        registry=load_registry(),
        counters=report._profiles(rs, "counters"),
        timers=report._profiles(rs, "timers_secs"),
        external=load_external(rs["manifest"].get("arch")),
        per_read=load_per_read(rs),
        digest=input_digest(rs),
    )
    files: dict[str, str] = {}
    for a in ARTIFACTS:
        files.update(render(a, ctx))
    files["PROVENANCE.md"] = render_provenance(ctx)
    return files


def load_per_read(rs: dict) -> dict[str, list[dict]]:
    """Per-read rows written by `--per-read-stats`, keyed by benchmark.

    Optional in exactly the same way the external corpus is: a result set
    measured before the instrumentation existed simply has none, and the
    figure that needs them reports that instead of failing the run.
    """
    out: dict[str, list[dict]] = {}
    for p in sorted(rs["dir"].glob("per-read-*.tsv")):
        bid = p.name.split("-")[2]
        rows = []
        with p.open() as f:
            head = f.readline().rstrip("\n").split("\t")
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) != len(head):
                    continue
                r = dict(zip(head, parts))
                try:
                    rows.append({
                        "examined_matches": float(r["examined_matches"]),
                        "possible_matches": float(r["possible_matches"]),
                        "query_mapping_s": float(r["query_mapping_s"]),
                        "mapped": int(r["mapped"]),
                    })
                except (KeyError, ValueError):
                    continue
        if rows:
            out[bid] = rows
    return out


def load_external(a: str | None = None) -> list[dict]:
    """The cached external-mapper corpus for one architecture, if it exists.

    Takes an architecture because the corpus records `wall_s` and
    `peak_rss_kb`: they belong to the machine that ran the mapper, and putting
    one machine's seconds in another's table is not a caveat, it is a wrong
    number. An architecture whose corpus has not been built simply has no
    external rows -- which is what its own checks.tsv already says, since
    concordance against a mapper that was never run cannot be scored either.

    Optional by design: building it takes hours and a missing corpus should
    cost a table its external rows, not fail the run.
    """
    p = reference_mappers_dir(a) / "manifest.json"
    if not p.exists():
        return []
    try:
        entries = json.loads(p.read_text()).get("entries", [])
    except (OSError, ValueError):
        return []
    out = []
    for e in entries:
        if e.get("status") != "cached" or not e.get("wall_s"):
            continue
        # The corpus records the command line but not the thread count as a
        # field; recovering it from the command is better than omitting it,
        # because the number is meaningless without it.
        threads = None
        cmd = e.get("cmd", "").split()
        for flag in ("--threads", "-t", "-@"):
            if flag in cmd:
                try:
                    threads = int(cmd[cmd.index(flag) + 1])
                except (IndexError, ValueError):
                    pass
                break
        out.append({**e, "threads": threads})
    return sorted(out, key=lambda e: (e.get("benchmark", ""), e.get("mapper", "")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result_set", nargs="?", help="default: the suite's current/")
    ap.add_argument("--out",
                    help="output directory (default: paper/generated/<arch>/)")
    ap.add_argument("--arch", default=None,
                    help="architecture whose results to use; default: this machine's "
                         "(x86_64, aarch64)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any artifact would change (does not write)")
    ap.add_argument("--list", action="store_true",
                    help="print each artifact's sources and transformation, and stop")
    a = ap.parse_args()

    if a.list:
        for art in ARTIFACTS:
            print(f"{art.name}  ({art.kind})")
            for s in art.sources:
                print(f"    from      {s}")
            for t in art.transform:
                print(f"    transform {t}")
            print(f"    presented {art.presentation}")
            for c in art.caveats:
                print(f"    caveat    {c}")
            print()
        return 0

    if a.result_set:
        rs = load_set(Path(a.result_set))
    else:
        d = current_dir(load_suite()["suite_version"], a.arch)
        if not d.is_dir():
            print(f"no baseline at {d}; pass a result set explicitly", file=sys.stderr)
            return 2
        rs = load_set(d)

    out = Path(a.out) if a.out else out_default(a.arch)
    files = build_all(rs)

    if a.check:
        stale = [n for n, body in sorted(files.items())
                 if not (out / n).exists() or (out / n).read_text() != body]
        if stale:
            print(f"paper artifacts out of date in {out}: {', '.join(stale)}\n"
                  f"regenerate with: python3 benchmarks/scripts/paper.py "
                  f"--arch {a.arch or arch()}", file=sys.stderr)
            return 1
        print(f"{len(files)} paper artifacts are current with {rs['dir'].name}")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    for n, body in sorted(files.items()):
        (out / n).write_text(body)
    print(f"wrote {len(files)} paper artifacts to {out} from {rs['dir'].name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
