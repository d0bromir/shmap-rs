#!/usr/bin/env python3
"""Self-test for compare.py, run by CI via validate_suite.py's sibling check.

compare.py decides whether a pull request may merge, so a bug in it is either a
regression waved through or a good change blocked. It is exercised here against
synthetic result sets rather than real ones, because the cases that matter most
(an accuracy drop, a failed check) should never appear in a real set.

  python3 benchmarks/test_compare.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPARE = HERE / "compare.py"

BENCHES = ["B01", "B02"]
METRICS = ["Containment", "Jaccard", "bucket_SH"]
THREADS = [1, 2, 4, 8, 16, 32, 64]

COLS = ["benchmark", "impl", "metric", "threads", "repeat", "reference_id", "reads_id",
        "params_id", "rc", "wall_s", "index_s", "map_s", "peak_rss_kb", "mapped",
        "mapq60", "cmd"]


def make_set(d: Path, *, wall_scale=1.0, mapped_delta=0, suite="1.0", dataset_version=1,
             host="a2", commit="a" * 40, fail_check=None, agreement=0.9792,
             rc=0, drop_config=None, rss_scale=1.0, ground_truth=0.992064,
             concordance=0.9633, ref_wall_scale=1.0, ref_binary="shmap 1.0.0-cpp",
             map_scale=1.0, index_frac=0.5, rss_outlier=1.0):
    d.mkdir(parents=True, exist_ok=True)
    rows, checks = [], []
    for b in BENCHES:
        for m in METRICS:
            for t in THREADS:
                if drop_config == (b, m, t):
                    continue
                # A plausible scaling curve; exact values do not matter, only ratios.
                # wall is built FROM its parts so the two stay consistent: a
                # mapper-only change must move map_s fully and wall only by the
                # mapper's share, which is the situation the split exists for.
                base_wall = (100.0 / (1 + (t - 1) * 0.8))
                # wall_scale is a whole-run effect (host drift, or a change that
                # touches both phases) so it multiplies BOTH parts; map_scale is
                # mapper-only. wall is their sum, never scaled independently.
                index_s = base_wall * index_frac * wall_scale
                map_s = base_wall * (1 - index_frac) * map_scale * wall_scale
                wall = index_s + map_s
                rows.append(dict(
                    benchmark=b, impl="shmap-rs", metric=m, threads=t, repeat=0,
                    reference_id="REF-HS1", reads_id="D1-HIFI23K", params_id="paper",
                    rc=rc, wall_s=round(wall, 2),
                    index_s=round(index_s, 3), map_s=round(map_s, 3),
                    peak_rss_kb=int(8_000_000 * rss_scale
                                    * (rss_outlier if t >= 32 else 1.0)),
                    mapped=130000 + mapped_delta, mapq60=120000 + mapped_delta,
                    cmd="shmap -s ref -p reads"))
            rows.append(dict(
                benchmark=b, impl="cpp-shmap", metric=m, threads=1, repeat="median3",
                reference_id="REF-HS1", reads_id="D1-HIFI23K", params_id="paper",
                rc=0, wall_s=round(250.0 * ref_wall_scale, 2),
                index_s="", map_s="", peak_rss_kb=9_000_000,
                mapped=130000, mapq60=120000, cmd="shmap -s ref -p reads"))
            for name in ("thread_determinism", "validate_paf"):
                checks.append(dict(check=name, benchmark=b, metric=m,
                                   passed=(fail_check != name), detail="synthetic"))
            checks.append(dict(check="impl_agreement", benchmark=b, metric=m,
                               passed=True, detail=f"127000/130000 = {agreement:.4f}"))
            checks.append(dict(
                check="concordance_winnowmap2", benchmark=b, metric=m, passed=True,
                detail=f"good={concordance:.4f} recall=0.9988 agreement=0.9644 ref=149376"))
            # Only B02 carries ground truth, as in the real suite.
            if b == "B02":
                ok = round(ground_truth * 125000)
                checks.append(dict(
                    check="ground_truth", benchmark=b, metric=m,
                    passed=(fail_check != "ground_truth" and ground_truth >= 0.98),
                    detail=f"{ok}/125000 = {ground_truth:.6f} (need 0.98)"))

    with open(d / "results.tsv", "w") as f:
        f.write("\t".join(COLS) + "\n")
        for r in rows:
            f.write("\t".join(str(r[c]) for c in COLS) + "\n")
    with open(d / "checks.tsv", "w") as f:
        f.write("check\tbenchmark\tmetric\tpassed\tdetail\n")
        for c in checks:
            f.write(f"{c['check']}\t{c['benchmark']}\t{c['metric']}\t{c['passed']}\t{c['detail']}\n")
    (d / "manifest.json").write_text(json.dumps(dict(
        schema=1, suite_version=suite, dataset_version=dataset_version, commit=commit,
        host=host, authorized_by="test", started="2026-07-30T00:00:00+00:00",
        finished="2026-07-30T01:00:00+00:00", duration_s=3600, invocations=len(rows),
        failures=0, datasets={"REF-HS1": dict(bytes=3179638084, records=25)},
        binaries={"shmap-rs": "shmap 1.2.0", "cpp-shmap": ref_binary}), indent=2) + "\n")
    return d


ACCEPT, REVIEW, BLOCK, ERROR = 0, 1, 2, 3
NAMES = {ACCEPT: "ACCEPT", REVIEW: "REVIEW", BLOCK: "BLOCK", ERROR: "ERROR"}


def run(cand: Path, base: Path, *extra) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(COMPARE), str(cand), str(base), *extra],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cmp-test-"))
    failures = []

    def case(name, expect, cand_kw, base_kw=None, extra=(), expect_in=None):
        base = make_set(tmp / f"{name}-base", **(base_kw or {}))
        cand = make_set(tmp / f"{name}-cand", commit="b" * 40, **cand_kw)
        rc, out = run(cand, base, *extra)
        ok = rc == expect and (expect_in is None or expect_in in out)
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name:34} -> {NAMES.get(rc, rc)} "
              f"(expected {NAMES[expect]})")
        if not ok:
            failures.append((name, expect, rc, out))
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(cand, ignore_errors=True)

    print("compare.py verdicts:")

    # --- no change --------------------------------------------------------
    case("identical", ACCEPT, {})
    case("2% slower (host noise)", ACCEPT, dict(wall_scale=1.02))
    case("5% faster", ACCEPT, dict(wall_scale=0.95))
    case("40% faster", ACCEPT, dict(wall_scale=0.60))

    # --- speed ------------------------------------------------------------
    case("5% slower", REVIEW, dict(wall_scale=1.05), expect_in="wall")
    case("15% slower", BLOCK, dict(wall_scale=1.15))
    case("10% more memory everywhere", REVIEW, dict(rss_scale=1.10), expect_in="peak RSS")
    # Peak RSS is a max-over-time statistic and swings ~30% at high thread
    # counts on deep data. Two such outliers must not read as a regression when
    # the typical configuration did not move — measured on identical code.
    case("RSS outliers at 2 of 7 thread counts", ACCEPT, dict(rss_outlier=1.30))

    # --- accuracy: any drop blocks, regardless of speed -------------------
    case("1 fewer read mapped", BLOCK, dict(mapped_delta=-1), expect_in="mapped regressed")
    case("faster but maps fewer", BLOCK, dict(wall_scale=0.5, mapped_delta=-100))
    case("maps more", ACCEPT, dict(mapped_delta=+50))
    case("agreement dropped", BLOCK, dict(agreement=0.9700), expect_in="impl_agreement")
    case("agreement improved", ACCEPT, dict(agreement=0.9900))

    # Ground truth is compared against the baseline, not only against its floor:
    # a drift well clear of the floor still means reads moved off true position.
    case("ground truth drifted down", BLOCK, dict(ground_truth=0.991000),
         expect_in="ground_truth")
    case("ground truth improved", ACCEPT, dict(ground_truth=0.995000))
    case("ground truth below floor", BLOCK, dict(ground_truth=0.970000))
    case("ground truth drift, override", REVIEW, dict(ground_truth=0.991000),
         extra=("--allow-output-change",))

    # --- the override for an argued correctness fix -----------------------
    case("fewer mapped, override", REVIEW, dict(mapped_delta=-100),
         extra=("--allow-output-change",))

    # Falling behind Winnowmap2 is the thing the project is trying not to do,
    # but the reference is not truth, so it is REVIEW rather than BLOCK.
    case("concordance dropped", REVIEW, dict(concordance=0.9500),
         expect_in="concordance")
    case("concordance improved", ACCEPT, dict(concordance=0.9700))

    # Host drift: the reference binary is unchanged, so its movement is the
    # host's. A run where everything moved together must not read as a
    # regression in the subject.
    case("5% slower, host also 5% slower", ACCEPT,
         dict(wall_scale=1.05, ref_wall_scale=1.05))
    case("5% slower, host steady", REVIEW, dict(wall_scale=1.05))
    case("5% faster, host 5% faster too", ACCEPT,
         dict(wall_scale=0.95, ref_wall_scale=0.95))
    # A real regression must still be caught when the host drifts the other way.
    case("15% slower, host 3% faster", BLOCK,
         dict(wall_scale=1.15, ref_wall_scale=0.97))
    # A rebuilt reference is not a control, so no correction is applied.
    case("host moved but reference rebuilt", REVIEW,
         dict(wall_scale=1.05, ref_wall_scale=1.05, ref_binary="shmap 2.0.0-cpp"),
         expect_in="not a control")
    # Drift too large to correct is itself a finding.
    case("reference moved 20%", REVIEW, dict(ref_wall_scale=1.20),
         expect_in="reference implementation moved")

    # A mapper regression hidden inside a large fixed index cost. The total
    # moves under the review line; the mapping time does not. This is the case
    # the split exists for.
    case("mapper 12% slower, index dominates", BLOCK,
         dict(map_scale=1.12, index_frac=0.9), dict(index_frac=0.9),
         expect_in="MAPPING time")
    case("mapper 5% slower, index dominates", REVIEW,
         dict(map_scale=1.05, index_frac=0.9), dict(index_frac=0.9),
         expect_in="MAPPING time")
    case("mapper unchanged, index dominates", ACCEPT,
         dict(index_frac=0.9), dict(index_frac=0.9))

    # --- checks -----------------------------------------------------------
    case("thread determinism failed", BLOCK, dict(fail_check="thread_determinism"),
         expect_in="thread_determinism")
    case("validate_paf failed", BLOCK, dict(fail_check="validate_paf"))

    # --- unusable runs ----------------------------------------------------
    case("non-zero exit", BLOCK, dict(rc=1), expect_in="non-zero exit")
    case("incomplete run", BLOCK, dict(drop_config=("B01", "Containment", 8)),
         expect_in="incomplete")

    # --- not comparable at all --------------------------------------------
    case("suite MAJOR differs", ERROR, dict(suite="2.0"), expect_in="not comparable")
    case("suite MINOR differs", ACCEPT, dict(suite="1.1"))
    case("dataset version differs", ERROR, dict(dataset_version=2))
    case("different host", ERROR, dict(host="laptop"), expect_in="host differs")

    shutil.rmtree(tmp, ignore_errors=True)
    print()
    if failures:
        for name, expect, rc, out in failures:
            print(f"--- {name}: expected {NAMES[expect]}, got {NAMES.get(rc, rc)} ---")
            print(out[:1500])
        print(f"{len(failures)} case(s) failed")
        return 1
    print("OK — all compare.py verdicts behave as VERSIONING.md specifies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
