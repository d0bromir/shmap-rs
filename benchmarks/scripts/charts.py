#!/usr/bin/env python3
"""Pie charts of a result set's profiling tables.

  charts.py                      regenerate charts for results/suite-<v>/current/
  charts.py DIR                  a different result set
  charts.py --check              exit 1 if any chart would change (CI uses this)
  charts.py --metric Jaccard     chart a different metric (default: Containment)
  charts.py --threads 16         chart a different thread count (default: 1)

---------------------------------------------------------------------------
Where this sits in the chain
---------------------------------------------------------------------------
    shmap -x --profile-log      one JSON report per invocation      (profiling)
      -> run.py write_profiles_tsv()                                (script)
      -> <result set>/profiles.tsv                                  (tables)
      -> charts.py            [this file]                           (script)
      -> <result set>/chart-*.svg + chart-index.html                (graphics)

This reads `profiles.tsv` — the tables — and never the raw JSON. That is
deliberate: the tables are the reviewed, checked-in, human-auditable form of
the profiling data, so a chart can always be traced back to a specific row of
a specific table rather than to a blob nobody reads. Every chart footers the
exact row it was drawn from.

---------------------------------------------------------------------------
Why hand-written SVG instead of matplotlib
---------------------------------------------------------------------------
No new dependency: the benchmark host has neither matplotlib nor a LaTeX
engine, and adding one would have to be installed everywhere this runs,
including CI. SVG is also text, so a chart diffs in review like the tables do,
and `--check` can detect a stale one byte-for-byte the way `report.py --check`
already guards RESULTS.md. Output carries no timestamp for the same reason
`paper/README.md` gives: a regenerated artifact must be identical, or "changed"
stops meaning anything.

---------------------------------------------------------------------------
Two correctness rules these charts obey
---------------------------------------------------------------------------
1. CPU-seconds are never mixed with wall-clock. `profiles.tsv`'s own header
   warns that `cpu_*` is summed across threads and will exceed `wall_total`;
   the time pies are therefore labelled CPU-seconds and drawn only from
   `cpu_*`. At `-@1` indexing already sums to ~2x its wall time, because its
   reader and worker threads overlap -- so even the single-threaded pie is a
   CPU-time breakdown, not a wall-clock one.

2. Only non-overlapping quantities share a pie. The stage timers nest:

       query_mapping
       |- sketching
       |- prepare ......... (contains seeding, which contains collect_kmer_info)
       |- match_seeds
       |- bucket_merge
       `- match_rest ...... (contains refine)

   so `seeding`, `collect_kmer_info` and `refine` are deliberately NOT slices
   -- they are inside slices already drawn, and including them would
   double-count. They are reported in each chart's footer instead.

   The same rule killed a bucket-funnel chart: `n_final_buckets` is not a
   subset of `n_refined_buckets` (it exceeds it on B02 and B05, because
   `final_buckets` counts buckets that beat the threshold while
   `refined_buckets` counts buckets that entered refine, and the memo
   decouples the two), so no valid pie can be drawn from that pair.
   `assert_partitions()` re-checks the three that ARE valid on every run.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from compare import RESULTS  # noqa: E402
from layout import current_dir  # noqa: E402
from run import load_suite  # noqa: E402

# Slices below this are folded into a single "other" wedge: a pie cannot show
# a 1% sliver honestly, and a legend of twelve near-zero entries hides the
# three that matter.
MIN_SLICE = 0.05

# Indexing in blues, mapping in oranges, so the two phases are separable at a
# glance without reading a single label. Ordered light -> dark within a phase
# so neighbouring wedges stay distinguishable.
INDEX_COLORS = ["#bcd9ef", "#8ec0e0", "#5aa2cd", "#2e77aa"]
MAP_COLORS = ["#fbd9a5", "#f7bc6d", "#f09a3e", "#dd7020", "#b74d10"]
# Distinct from every MAP_COLORS entry: the unattributed remainder must not
# be mistaken for match_rest, which is the wedge it sits next to.
MAP_REMAINDER_COLOR = "#7d3208"
OTHER_COLOR = "#c2c2c2"

# (column, label) in pipeline order. Kept in this order in the pie rather than
# sorted by size, so each phase occupies one contiguous arc -- that contiguity
# is what makes "indexing vs mapping" readable as two blocks.
INDEX_STAGES = [
    ("cpu_index_reading", "index: reading"),
    ("cpu_index_sketching", "index: sketching"),
    ("cpu_index_collecting", "index: collecting"),
    ("cpu_index_finalizing", "index: finalizing"),
]
MAP_STAGES = [
    ("cpu_sketching", "map: sketching"),
    ("cpu_prepare", "map: prepare"),
    ("cpu_match_seeds", "map: match_seeds"),
    ("cpu_bucket_merge", "map: bucket_merge"),
    ("cpu_match_rest", "map: match_rest"),
]
# Nested inside a stage above; shown in footers, never as slices. See rule 2.
NESTED = [
    ("cpu_seeding", "seeding", "inside prepare"),
    ("cpu_collect_kmer_info", "collect_kmer_info", "inside seeding"),
    ("cpu_refine", "refine", "inside match_rest"),
]


def read_profiles(d: Path) -> list[dict]:
    p = d / "profiles.tsv"
    if not p.exists():
        raise FileNotFoundError(f"no profiles.tsv in {d} — run the benchmark first")
    with open(p) as f:
        return list(csv.DictReader((l for l in f if not l.startswith("#")), delimiter="\t"))


def num(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except ValueError:
        return 0.0


def count(row: dict, key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except ValueError:
        return 0


def assert_partitions(rows: list[dict]) -> None:
    """Re-check, on every run, the nestings the counter pies assume.

    These held across all 105 rows of the result set this was written
    against, but they are properties of the *counters*, not of arithmetic --
    a future counter change could break one silently and turn a pie into a
    lie. Checked here so it fails loudly instead.

    Raises rather than exiting: `run.py` calls this mid-benchmark, and a
    chart problem must never take down a measured run.
    """
    checks = [
        ("n_refined_buckets <= n_seeded_buckets", "n_refined_buckets", "n_seeded_buckets"),
        ("n_refine_memo_hits <= n_refined_buckets", "n_refine_memo_hits", "n_refined_buckets"),
        ("n_mapq60 <= n_mapped_reads", "n_mapq60", "n_mapped_reads"),
    ]
    for name, small, big in checks:
        bad = [f"{r['benchmark']}/{r['metric']}/-@{r['threads']}"
               for r in rows if count(r, small) > count(r, big)]
        if bad:
            raise ValueError(
                f"counter partition broken ({name}) on: {', '.join(bad[:5])}; "
                f"a pie drawn from it would be meaningless — fix the counters or "
                f"drop the chart, do not relax this check")


def fold_small(slices: list[tuple[str, float, str]]) -> list[tuple[str, float, str]]:
    """Fold every wedge under MIN_SLICE into one trailing 'other'.

    Wedges keep the order given, so phase blocks stay contiguous, with
    'other' last. When 'other' ends up holding a single stage it is named
    outright ("other: map: bucket_merge") rather than left anonymous — the
    wedge is still aggregated, so the under-5% rule holds exactly, but the
    reader is not made to guess what one grey sliver is.
    """
    total = sum(v for _, v, _ in slices)
    if total <= 0:
        return []
    big = [(lab, v, c) for lab, v, c in slices if v / total >= MIN_SLICE]
    small = [(lab, v, c) for lab, v, c in slices if v / total < MIN_SLICE]
    if not small:
        return big
    label = f"other: {small[0][0]}" if len(small) == 1 else f"other ({len(small)} stages)"
    return big + [(label, sum(v for _, v, _ in small), OTHER_COLOR)]


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def arc_path(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    """Wedge from a0 to a1, radians, 0 at 12 o'clock, growing clockwise."""
    x0, y0 = cx + r * math.sin(a0), cy - r * math.cos(a0)
    x1, y1 = cx + r * math.sin(a1), cy - r * math.cos(a1)
    large = 1 if (a1 - a0) > math.pi else 0
    return f"M {cx:.2f},{cy:.2f} L {x0:.2f},{y0:.2f} A {r:.2f},{r:.2f} 0 {large},1 {x1:.2f},{y1:.2f} Z"


def pie_svg(title: str, subtitle: str, slices: list[tuple[str, float, str]],
            unit: str, footer: list[str]) -> str:
    """One pie plus a legend. `slices` is [(label, value, color)] in draw order."""
    total = sum(v for _, v, _ in slices)
    W, H = 760, 420
    cx, cy, r = 200.0, 210.0, 140.0
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="DejaVu Sans, Helvetica, Arial, sans-serif">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        f'<text x="20" y="30" font-size="17" font-weight="bold">{esc(title)}</text>',
        f'<text x="20" y="50" font-size="12" fill="#555555">{esc(subtitle)}</text>',
    ]
    if total <= 0:
        out.append(f'<text x="20" y="100" font-size="13" fill="#999999">no data</text></svg>')
        return "\n".join(out)

    # Wedges.
    a = 0.0
    if len(slices) == 1:
        lab, v, col = slices[0]
        out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{col}" stroke="#ffffff" stroke-width="2"/>')
    else:
        for _lab, v, col in slices:
            a1 = a + 2 * math.pi * (v / total)
            out.append(f'<path d="{arc_path(cx, cy, r, a, a1)}" fill="{col}" '
                       f'stroke="#ffffff" stroke-width="2"/>')
            a = a1

    # Legend.
    lx, ly = 400, 92
    for lab, v, col in slices:
        pct = 100.0 * v / total
        out.append(f'<rect x="{lx}" y="{ly - 11}" width="14" height="14" fill="{col}" '
                   f'stroke="#dddddd" stroke-width="1"/>')
        out.append(f'<text x="{lx + 22}" y="{ly}" font-size="13">{esc(lab)}</text>')
        out.append(f'<text x="{W - 20}" y="{ly}" font-size="13" text-anchor="end" '
                   f'fill="#333333">{v:,.2f} {esc(unit)} · {pct:.1f}%</text>')
        ly += 23
    out.append(f'<line x1="{lx}" y1="{ly - 6}" x2="{W - 20}" y2="{ly - 6}" stroke="#dddddd"/>')
    ly += 12
    out.append(f'<text x="{lx}" y="{ly}" font-size="13" font-weight="bold">total</text>')
    out.append(f'<text x="{W - 20}" y="{ly}" font-size="13" text-anchor="end" '
               f'font-weight="bold">{total:,.2f} {esc(unit)}</text>')

    fy = 372
    for line in footer:
        out.append(f'<text x="20" y="{fy}" font-size="10.5" fill="#666666">{esc(line)}</text>')
        fy += 15
    out.append("</svg>")
    return "\n".join(out)


def provenance(row: dict, d: Path) -> str:
    return (f"source: {d.name}/profiles.tsv row "
            f"{row['benchmark']}/{row['metric']}/-@{row['threads']}")


def time_stages(row: dict) -> tuple[list[tuple[str, float, str]],
                                    list[tuple[str, float, str]]]:
    """(indexing, mapping) stages in pipeline order, before any folding.

    The one place that decides what the stages of a run are, including the
    unattributed remainder below, which is computed rather than read from a
    column and is therefore the thing two independent implementations would
    disagree about first.

    Unfolded, because folding is a presentation rule for pies: a wedge under
    5% cannot be drawn honestly, but a table cell can hold any number.
    `crossarch.py`'s stage table takes these raw; the pies take the folded
    form below. Both agree about the stages themselves.
    """
    idx = [(lab, num(row, col), INDEX_COLORS[i]) for i, (col, lab) in enumerate(INDEX_STAGES)]
    mp = [(lab, num(row, col), MAP_COLORS[i]) for i, (col, lab) in enumerate(MAP_STAGES)]

    # Whatever query_mapping accounts for that its five children do not.
    mp_sum = sum(v for _, v, _ in mp)
    rest = num(row, "cpu_query_mapping") - mp_sum
    if rest > 0:
        mp = mp + [("map: other (unattributed)", rest, MAP_REMAINDER_COLOR)]
    return idx, mp


def time_slices(row: dict) -> dict[str, list[tuple[str, float, str]]]:
    """The wedges of each time pie, folded, before anything renders them.

    Two renderers read this: the SVGs below, and the TikZ pies `crossarch.py`
    typesets into the cross-architecture PDF. One function feeding both means
    they cannot disagree about what a wedge is.
    """
    idx, mp = time_stages(row)
    return {"time": fold_small(idx + mp),
            "time-indexing": fold_small(idx),
            "time-mapping": fold_small(mp)}


def time_charts(row: dict, d: Path) -> dict[str, str]:
    """The headline chart plus a per-phase zoom of each half."""
    sl = time_slices(row)
    # Taken from the folded slices because folding conserves the total, so
    # these are the same numbers the unfolded stage lists would give.
    i_tot = sum(v for _, v, _ in sl["time-indexing"])
    m_tot = sum(v for _, v, _ in sl["time-mapping"])
    grand = i_tot + m_tot
    nested = ", ".join(f"{lab} {num(row, col):.2f}s ({why})" for col, lab, why in NESTED)

    b, met, th = row["benchmark"], row["metric"], row["threads"]
    common = [
        provenance(row, d),
        "CPU-seconds summed across threads, not wall-clock — cpu_* and wall_* are "
        "different units and are never mixed.",
        f"not shown (nested inside a slice above, would double-count): {nested}",
    ]
    out = {}
    out[f"chart-{b}-time.svg"] = pie_svg(
        f"{b} — where the CPU time goes ({met}, -@{th})",
        f"indexing {i_tot:.2f}s ({100 * i_tot / grand:.1f}%) in blue · "
        f"mapping {m_tot:.2f}s ({100 * m_tot / grand:.1f}%) in orange · "
        f"slices under {int(MIN_SLICE * 100)}% folded into 'other'",
        sl["time"], "s", common)
    out[f"chart-{b}-time-indexing.svg"] = pie_svg(
        f"{b} — indexing phase only ({met}, -@{th})",
        f"{i_tot:.2f} CPU-seconds total · wall_indexing was {num(row, 'wall_indexing'):.2f}s "
        f"(lower: the reader and sketch workers overlap)",
        sl["time-indexing"], "s", common[:2])
    out[f"chart-{b}-time-mapping.svg"] = pie_svg(
        f"{b} — mapping phase only ({met}, -@{th})",
        f"{m_tot:.2f} CPU-seconds total · wall_mapping was {num(row, 'wall_mapping'):.2f}s",
        sl["time-mapping"], "s", common)
    return out


def counter_charts(row: dict, d: Path) -> dict[str, str]:
    """Relative-composition pies for the counters whose nesting is valid.

    Deliberately not a seeded->refined->final funnel: see the module docstring.
    """
    b, met, th = row["benchmark"], row["metric"], row["threads"]
    seeded, refined = count(row, "n_seeded_buckets"), count(row, "n_refined_buckets")
    memo = count(row, "n_refine_memo_hits")
    mapped, q60 = count(row, "n_mapped_reads"), count(row, "n_mapq60")
    src = [provenance(row, d)]
    out = {}

    out[f"chart-{b}-pruning.svg"] = pie_svg(
        f"{b} — what pruning removes ({met}, -@{th})",
        "of every bucket seeding produced, the share the seed heuristic "
        "discarded before it reached refine",
        fold_small([
            ("pruned before refine", float(seeded - refined), "#8ec0e0"),
            ("reached refine", float(refined), "#dd7020"),
        ]), "buckets",
        src + ["n_seeded_buckets vs n_refined_buckets; both counted per run, not per read."])

    out[f"chart-{b}-refine-memo.svg"] = pie_svg(
        f"{b} — RefineCache hit rate ({met}, -@{th})",
        "of the buckets that reached refine, the share answered from the "
        "first sweep's memo instead of being rescored",
        fold_small([
            ("served from memo", float(memo), "#5aa2cd"),
            ("rescored", float(refined - memo), "#dd7020"),
        ]), "buckets",
        src + ["n_refine_memo_hits vs n_refined_buckets. Memoization applies only to "
               "Containment/Jaccard; bucket_SH/bucket_LCS never memoize."])

    out[f"chart-{b}-mapq.svg"] = pie_svg(
        f"{b} — confidence of mapped reads ({met}, -@{th})",
        "of the reads that mapped, the share reported at mapq 60",
        fold_small([
            ("mapq 60", float(q60), "#2e77aa"),
            ("below mapq 60", float(mapped - q60), "#f7bc6d"),
        ]), "reads",
        src + ["n_mapq60 vs n_mapped_reads. Says nothing about reads that did not map."])
    return out


def index_html(names: list[str], d: Path, metric: str, threads: str) -> str:
    rows = "\n".join(
        f'    <figure><img src="{n}" alt="{esc(n)}" loading="lazy"><figcaption>{esc(n)}</figcaption></figure>'
        for n in names)
    return f"""<!doctype html>
<meta charset="utf-8">
<title>shmap-rs profiling charts — {esc(d.name)}</title>
<style>
 body {{ font-family: DejaVu Sans, Helvetica, Arial, sans-serif; margin: 2rem; color: #222; }}
 figure {{ margin: 0 0 1.5rem; }} img {{ max-width: 100%; border: 1px solid #e2e2e2; }}
 figcaption {{ font-size: .8rem; color: #777; margin-top: .25rem; }}
 code {{ background: #f4f4f4; padding: .1rem .3rem; }}
</style>
<h1>shmap-rs profiling charts</h1>
<p>Result set <code>{esc(d.name)}</code> · metric <code>{esc(metric)}</code> ·
   <code>-@{esc(threads)}</code>. Regenerate with <code>python3 benchmarks/scripts/charts.py</code>.</p>
<p>Chain: <code>shmap -x</code> &rarr; <code>run.py</code> &rarr;
   <code>profiles.tsv</code> &rarr; <code>charts.py</code> &rarr; these files.
   Time pies are CPU-seconds summed across threads, never wall-clock.</p>
{rows}
"""


def build_charts(d: Path, metric: str = "Containment", threads: str = "1") -> dict[str, str]:
    """Every chart for one result set, as {filename: contents}.

    Pure: builds the text, writes nothing. `write_charts` does the writing
    and `--check` compares against it — same bytes either way, which is what
    makes staleness detection meaningful.
    """
    rows = read_profiles(d)
    assert_partitions(rows)
    picked = [r for r in rows if r["metric"] == metric and r["threads"] == threads]
    if not picked:
        raise ValueError(f"no rows for metric={metric} threads={threads} in {d}/profiles.tsv")
    picked.sort(key=lambda r: r["benchmark"])

    files: dict[str, str] = {}
    for row in picked:
        files.update(time_charts(row, d))
        files.update(counter_charts(row, d))
    files["chart-index.html"] = index_html(sorted(files), d, metric, threads)
    return files


def write_charts(d: Path, metric: str = "Containment", threads: str = "1") -> int:
    """Render and write; returns how many files were written. Used by run.py."""
    files = build_charts(d, metric, threads)
    for n, body in sorted(files.items()):
        (d / n).write_text(body)
    return len(files)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result_set", nargs="?", help="default: the suite's current/")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any chart would change (does not write)")
    ap.add_argument("--arch", default=None,
                    help="architecture whose results to use; default: this machine's "
                         "(x86_64, aarch64)")
    ap.add_argument("--metric", default="Containment")
    ap.add_argument("--threads", default="1")
    args = ap.parse_args()

    d = Path(args.result_set) if args.result_set else (
        current_dir(load_suite()["suite_version"], args.arch))
    if not d.is_dir():
        sys.exit(f"no such result set: {d}")

    try:
        files = build_charts(d, args.metric, args.threads)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))

    stale = [n for n, body in sorted(files.items())
             if not (d / n).exists() or (d / n).read_text() != body]
    if args.check:
        if stale:
            print(f"charts are out of date in {d.name} ({len(stale)}): "
                  f"{', '.join(stale[:4])}{' …' if len(stale) > 4 else ''}")
            print("run `python3 benchmarks/scripts/charts.py` and commit the result")
            return 1
        print(f"{len(files)} charts are current with {d.name}")
        return 0

    for n, body in sorted(files.items()):
        (d / n).write_text(body)
    print(f"wrote {len(files)} files to {d}"
          f"{'' if stale else ' (no change)'}")
    print(f"open {d / 'chart-index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
