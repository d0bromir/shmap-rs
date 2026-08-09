#!/usr/bin/env python3
"""Self-test for charts.py.

A chart that is merely *wrong* is worse than no chart: it looks authoritative
and nobody re-derives it by hand. The three things that could silently make
one wrong are pinned here — the under-5% aggregation, the wedge geometry, and
the counter-partition guard that stops an invalid pie being drawn at all.

  python3 benchmarks/scripts/test_charts.py
"""

from __future__ import annotations

import math
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from charts import (  # noqa: E402
    MIN_SLICE, OTHER_COLOR, arc_path, assert_partitions, build_charts, fold_small, pie_svg,
)

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:52} got {got!r}")
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")


def approx(name: str, got: float, want: float, tol: float = 1e-6):
    ok = abs(got - want) < tol
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:52} got {got:.6f}")
    if not ok:
        FAIL.append(f"{name}: got {got}, want {want}")


def labels(sl):
    return [lab for lab, _, _ in sl]


PROFILES_HEADER = (
    "# test fixture\n"
    "benchmark\tmetric\tthreads\twall_total\twall_indexing\twall_mapping\t"
    "cpu_index_reading\tcpu_index_sketching\tcpu_index_collecting\tcpu_index_finalizing\t"
    "cpu_query_mapping\tcpu_sketching\tcpu_seeding\tcpu_prepare\tcpu_collect_kmer_info\t"
    "cpu_match_seeds\tcpu_match_rest\tcpu_refine\tcpu_bucket_merge\t"
    "n_seeds\tn_matches\tn_seeded_buckets\tn_refined_buckets\tn_refine_memo_hits\t"
    "n_final_buckets\tn_mapped_reads\tn_mapq60\n")


def fixture_row(seeded=1000, refined=400, memo=300, final=200, mapped=100, q60=80) -> str:
    return ("B01\tContainment\t1\t100\t20\t80\t"
            "5\t4\t3\t1\t"          # index stages
            "40\t8\t6\t10\t4\t9\t12\t5\t1\t"   # mapping stages (nested ones included)
            f"0\t0\t{seeded}\t{refined}\t{memo}\t{final}\t{mapped}\t{q60}\n")


def main() -> int:
    print("under-5% aggregation (the rule the charts are asked to obey):")
    sl = [("a", 50.0, "#1"), ("b", 45.0, "#2"), ("c", 3.0, "#3"), ("d", 2.0, "#4")]
    got = fold_small(sl)
    check("two sub-5% wedges collapse to one 'other'", labels(got), ["a", "b", "other (2 stages)"])
    approx("'other' carries their summed value", got[-1][1], 5.0)
    check("'other' uses the neutral colour", got[-1][2], OTHER_COLOR)
    approx("folding conserves the total", sum(v for _, v, _ in got), 100.0)

    got = fold_small([("a", 96.0, "#1"), ("b", 4.0, "#2")])
    check("a lone sub-5% wedge is still aggregated, but named",
          labels(got), ["a", "other: b"])

    got = fold_small([("a", 50.0, "#1"), ("b", 50.0, "#2")])
    check("nothing is folded when every wedge clears 5%", labels(got), ["a", "b"])

    exactly_5 = fold_small([("a", 95.0, "#1"), ("b", 5.0, "#2")])
    check("exactly 5% is kept (the rule is 'not less than')", labels(exactly_5), ["a", "b"])

    check("empty input yields no wedges", fold_small([]), [])
    check("all-zero input yields no wedges", fold_small([("a", 0.0, "#1")]), [])

    print("\nwedge geometry:")
    # A quarter turn from 12 o'clock must land on the 3 o'clock rim.
    d = arc_path(100.0, 100.0, 50.0, 0.0, math.pi / 2)
    check("quarter wedge starts at 12 o'clock", d.split("L ")[1].split(" A")[0], "100.00,50.00")
    check("quarter wedge ends at 3 o'clock", d.split("A 50.00,50.00 0 ")[1].split(" Z")[0].split(",1 ")[1],
          "150.00,100.00")
    check("small wedge sets large-arc flag 0", " 0,1 " in arc_path(0, 0, 1, 0, math.pi / 2), True)
    check("wedge over half a turn sets large-arc flag 1",
          " 1,1 " in arc_path(0, 0, 1, 0, 1.9 * math.pi), True)

    print("\nrendered SVG:")
    svg = pie_svg("T", "sub", [("x", 3.0, "#111111"), ("y", 1.0, "#222222")], "s", ["foot"])
    check("declares an svg root", svg.startswith("<svg"), True)
    check("percentages are of the total, not of 100 each",
          bool(re.search(r"75\.0%", svg)) and bool(re.search(r"25\.0%", svg)), True)
    check("a single wedge renders as a circle, not a degenerate arc",
          "<circle" in pie_svg("T", "s", [("only", 1.0, "#333333")], "s", []), True)
    check("no timestamp leaks in (would break --check)",
          bool(re.search(r"20\d\d-\d\d-\d\d", svg)), False)

    print("\ncounter-partition guard:")
    rows = [{"benchmark": "B01", "metric": "Containment", "threads": "1",
             "n_seeded_buckets": "1000", "n_refined_buckets": "400",
             "n_refine_memo_hits": "300", "n_mapped_reads": "100", "n_mapq60": "80"}]
    try:
        assert_partitions(rows)
        check("a valid set of counters passes", True, True)
    except ValueError as e:
        check("a valid set of counters passes", f"raised {e}", True)

    for broken, why in [
        ({"n_refine_memo_hits": "500"}, "memo hits exceeding refined"),
        ({"n_mapq60": "500"}, "mapq60 exceeding mapped"),
        ({"n_refined_buckets": "5000"}, "refined exceeding seeded"),
    ]:
        bad = [dict(rows[0], **broken)]
        try:
            assert_partitions(bad)
            check(f"rejects {why}", "accepted", "raised")
        except ValueError:
            check(f"rejects {why}", "raised", "raised")

    print("\nend to end, against a fixture result set:")
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "profiles.tsv").write_text(PROFILES_HEADER + fixture_row())
        files = build_charts(d)
        check("builds a chart set", len(files) > 0, True)
        check("emits a browsable index", "chart-index.html" in files, True)
        check("emits the headline time chart", "chart-B01-time.svg" in files, True)
        check("separates the two phases into their own charts",
              {"chart-B01-time-indexing.svg", "chart-B01-time-mapping.svg"} <= set(files), True)
        head = files["chart-B01-time.svg"]
        # index 5+4+3+1 = 13; mapping = cpu_query_mapping 40 => 53 total.
        check("headline total is indexing + query_mapping", "53.00 s" in head, True)
        check("names indexing's share", "indexing 13.00s" in head, True)
        check("names mapping's share", "mapping 40.00s" in head, True)
        check("nested timers are named as excluded, not drawn",
              "would double-count" in head and "refine" in head, True)
        check("every chart records the table row it came from",
              all("profiles.tsv row" in b for n, b in files.items() if n.endswith(".svg")), True)
        check("rebuilding is byte-identical (what --check relies on)",
              build_charts(d) == files, True)

    print()
    if FAIL:
        for f in FAIL:
            print(f"  {f}")
        print(f"{len(FAIL)} failure(s)")
        return 1
    print("OK — charts.py aggregates, draws and guards its data as documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
