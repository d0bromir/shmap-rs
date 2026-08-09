#!/usr/bin/env python3
"""Self-test for crossarch.py.

This script is the only place in the project allowed to put two machines'
numbers in one table, so the things that could make it quietly dishonest are
pinned here: that it notices when the two runs disagree about the algorithm,
that it never calls a missing measurement agreement, that it compares only
what both machines actually ran, and that its pies are geometrically what
they claim to be.

  python3 benchmarks/scripts/test_crossarch.py
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare import load_set  # noqa: E402
from charts import read_profiles  # noqa: E402
import crossarch as x  # noqa: E402

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:60} got {got!r}")
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")


RESULTS_HEAD = ("benchmark\timpl\tmetric\tthreads\trepeat\treference_id\treads_id\t"
                "params_id\trc\twall_s\tindex_s\tmap_s\tpeak_rss_kb\tmapped\tmapq60\tcmd\n")

PROFILES_HEAD = (
    "# fixture\n"
    "benchmark\tmetric\tthreads\twall_total\twall_indexing\twall_mapping\t"
    "cpu_index_reading\tcpu_index_sketching\tcpu_index_collecting\tcpu_index_finalizing\t"
    "cpu_query_mapping\tcpu_sketching\tcpu_seeding\tcpu_prepare\tcpu_collect_kmer_info\t"
    "cpu_match_seeds\tcpu_match_rest\tcpu_refine\tcpu_bucket_merge\t"
    "n_seeds\tn_matches\tn_seeded_buckets\tn_refined_buckets\tn_refine_memo_hits\t"
    "n_final_buckets\tn_mapped_reads\tn_mapq60\n")

THREADS = [1, 2, 4]


def make_set(root: Path, arch: str, host: str, *, scale: float = 1.0,
             commit: str = "abc123def456", seeded: int = 1000,
             benchmarks=("B01", "B02"), metrics=("Containment", "Jaccard")) -> Path:
    """A minimal but structurally real result set.

    `scale` stretches every time, so the two fixture machines differ in speed
    the way two real ones do while running the identical computation --
    which is exactly the case the agreement table has to recognise.
    """
    d = root / arch / "current"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(
        '{"schema": 1, "suite_version": "1.0", "dataset_version": 1, '
        f'"commit": "{commit}", "host": "{host}", "arch": "{arch}", '
        '"rustc": "1.97.1", "finished": "2026-08-09T10:00:00+00:00"}\n')

    rows = [RESULTS_HEAD]
    for b in benchmarks:
        for m in metrics:
            for t in THREADS:
                # Imperfect scaling, so speedup curves are not degenerate.
                wall = scale * 100.0 / (t ** 0.8)
                rows.append(f"{b}\tshmap-rs\t{m}\t{t}\t0\tREF\tRD\tpaper\t0\t"
                            f"{wall:.2f}\t{scale * 20:.2f}\t{wall - scale * 20:.2f}\t"
                            f"{2000000}\t900\t800\tcmd\n")
    (d / "results.tsv").write_text("".join(rows))

    prof = [PROFILES_HEAD]
    for b in benchmarks:
        for m in metrics:
            prof.append(
                f"{b}\t{m}\t1\t{scale * 100:.2f}\t{scale * 20:.2f}\t{scale * 80:.2f}\t"
                f"{scale * 5:.2f}\t{scale * 4:.2f}\t{scale * 3:.2f}\t{scale * 1:.2f}\t"
                f"{scale * 40:.2f}\t{scale * 8:.2f}\t{scale * 6:.2f}\t{scale * 10:.2f}\t"
                f"{scale * 4:.2f}\t{scale * 9:.2f}\t{scale * 12:.2f}\t{scale * 5:.2f}\t"
                f"{scale * 1:.2f}\t0\t0\t{seeded}\t400\t300\t200\t900\t800\n")
    (d / "profiles.tsv").write_text("".join(prof))
    return d


def ctx_for(dirs: list[tuple[str, Path]]) -> x.XCtx:
    sets = [x.ArchSet(arch=a, rs=load_set(d), profiles=read_profiles(d)) for a, d in dirs]
    return x.XCtx(sets=sets, hosts={}, digest=x.input_digest(sets))


def main() -> int:
    print("wedge geometry (plain TikZ, coordinates computed here):")
    body = "\n".join(x.tikz_pie([("a", 1.0, "#112233"), ("b", 3.0, "#eeddcc")],
                                0.0, 0.0, 2.0, "T", "p0"))
    check("first wedge starts at 12 o'clock", "++(90.000:2.00)" in body, True)
    check("a quarter of the total sweeps a quarter turn clockwise",
          "start angle=90.000, end angle=0.000" in body, True)
    check("wedges close the circle", "end angle=-270.000" in body, True)
    check("percentages are of the total", {"25\\%", "75\\%"} <= set(re.findall(r"\d+\\%", body)), True)
    check("dark wedge gets light ink", "text=white" in body, True)
    check("light wedge gets dark ink", "text=black" in body, True)
    single = "\n".join(x.tikz_pie([("only", 1.0, "#333333")], 0, 0, 2.0, "T", "p0"))
    check("a lone wedge is a circle, not a 360-degree arc",
          "circle[radius=2.00]" in single and "arc[" not in single, True)
    check("draws without a tikz library or the pie package",
          any(t in body for t in ("usetikzlibrary", r"\pie", "($")), False)

    print("\nink and signs:")
    check("mid grey resolves to a readable ink", x._ink("#8ec0e0"), "black")
    check("a near-zero delta is not printed as -0.0", x.signed(-0.004), "+0.0")
    check("a real negative keeps its sign", x.signed(-1.24), "-1.2")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        a = make_set(root, "x86_64", "a2", scale=1.0)
        b = make_set(root, "aarch64", "galaxy", scale=1.6)
        c = ctx_for([("x86_64", a), ("aarch64", b)])

        print("\nthe agreement table -- the document's premise:")
        _, _, data = x.build_agreement(c)
        check("every counter agrees when only the clock differs",
              {r[-1] for r in data}, {"agree"})
        check("one row per (benchmark, metric)", len(data), 4)

        root2 = Path(tmp) / "differ"
        a2 = make_set(root2, "x86_64", "a2", seeded=1000)
        b2 = make_set(root2, "aarch64", "galaxy", seeded=1001)
        c2 = ctx_for([("x86_64", a2), ("aarch64", b2)])
        _, cols, data2 = x.build_agreement(c2)
        check("a one-bucket divergence is caught", {r[-1] for r in data2}, {"differ"})
        check("and reported as a signed delta, not just a flag",
              data2[0][cols.index("seeded")], 1)

        root3 = Path(tmp) / "missing"
        a3 = make_set(root3, "x86_64", "a2", metrics=("Containment", "Jaccard"))
        b3 = make_set(root3, "aarch64", "galaxy", metrics=("Containment",))
        c3 = ctx_for([("x86_64", a3), ("aarch64", b3)])
        check("only metrics both machines ran are compared",
              c3.metrics("B01"), ["Containment"])
        _, _, data3 = x.build_agreement(c3)
        check("so an unmeasured metric is absent, not silently 'agree'",
              [r[1] for r in data3], ["Containment", "Containment"])

        root4 = Path(tmp) / "partial"
        a4 = make_set(root4, "x86_64", "a2", benchmarks=("B01", "B02"))
        b4 = make_set(root4, "aarch64", "galaxy", benchmarks=("B01",))
        c4 = ctx_for([("x86_64", a4), ("aarch64", b4)])
        check("benchmarks are intersected, not unioned", c4.benchmarks(), ["B01"])

        print("\nheadline and stages:")
        _, hcols, hdata = x.build_headline(c)
        i = hcols.index("map_s_ratio_aarch64_over_x86_64")
        check("the ratio column is other-over-reference", hdata[0][i], 1.6)
        check("memory is reported in GB, not KB",
              hdata[0][hcols.index("peak_rss_gb_x86_64")], round(2000000 / 1048576, 3))

        _, scols, sdata = x.build_stages(c)
        deltas = [v for r in sdata for v in r[2:] if v is not None]
        check("a pure clock difference moves no stage's share",
              all(abs(v) < 0.05 for v in deltas), True)
        check("shares are of the whole run, and sum to 100%",
              round(sum(r[1] for r in sdata)), 100)

        print("\nscaling figure:")
        tex, fcols, fdata = x.build_thread_scaling(c)
        check("every (architecture, metric) pair gets its own series",
              tex.count("addlegendentry"), 4)
        check("speedup is against the same machine's own -@1, not the reference's",
              {r[-1] for r in fdata if r[0] == "aarch64" and r[3] == 1}, {1.0})
        check("only thread counts both machines ran are plotted",
              sorted({r[3] for r in fdata}), THREADS)

        print("\nthe artifact set as a whole:")
        files = x.build_all(c, root)
        check("every artifact is emitted as tex and tsv",
              all(f"{art.name}.tex" in files and f"{art.name}.tsv" in files
                  for art in x.ARTIFACTS), True)
        check("provenance is generated too", "PROVENANCE.md" in files, True)
        check("and the browsable all-charts page", "charts.html" in files, True)
        check("every artifact identifies both runs by host and commit",
              all(b.count("set:        ") == 2 and "on galaxy @ abc123def456" in b
                  for n, b in files.items() if n.endswith(".tex")), True)
        check("every artifact names which architecture deltas are against",
              all("reference:  x86_64" in b for n, b in files.items() if n.endswith(".tex")),
              True)
        check("the stage table gives every stage one full row, unfolded",
              all(len([v for v in r[2:] if v is None]) == 0 for r in sdata), True)
        check("including stages too small to earn a wedge in the pies",
              "index: finalizing" in {r[0] for r in sdata}, True)
        check("the not-a-controlled-experiment caveat is in the provenance",
              "not a controlled experiment" in files["PROVENANCE.md"], True)
        check("no timestamp leaks in (would break --check)",
              any(re.search(r"\d\d:\d\d:\d\d", b) for b in files.values()), False)
        check("rebuilding is byte-identical (what --check relies on)",
              x.build_all(ctx_for([("x86_64", a), ("aarch64", b)]), root) == files, True)

    print()
    if FAIL:
        for f in FAIL:
            print(f"  {f}")
        print(f"{len(FAIL)} failure(s)")
        return 1
    print("OK — crossarch.py compares only what is comparable, and says so")
    return 0


if __name__ == "__main__":
    sys.exit(main())
