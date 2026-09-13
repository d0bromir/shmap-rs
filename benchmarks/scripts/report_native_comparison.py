#!/usr/bin/env python3
"""Render and verify the archived native 1.6.0 optimization comparison."""

import argparse
import csv
import hashlib
import io
import itertools
import json
from pathlib import Path
import statistics
import tarfile

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "benchmarks/results/native-compare-6e33e5c"
MODES = ["default", "adaptive", "parsing"]
METRICS = ["wall_s", "mapping_s", "cpu_s", "peak_rss_kb"]


def render():
    rows = []
    fingerprints = {}
    expected = set(itertools.product(
        [f"B{number:02}" for number in range(1, 6)],
        [prefix + mode for mode in MODES for prefix in ["baseline-", ""]],
        [1, 16, 64], range(3),
    ))
    checksums = []
    for host in ["a2", "galaxy"]:
        path = ARCHIVE / host / "report.json"
        report = json.loads(path.read_text())
        assert report["status"] == "complete", host
        assert report["commit"] == "6e33e5c6634dd7e8d5803f732094d7f3a414c9d6"
        assert report["baseline_commit"] == "2363d92eaceac4cfffcbdc46203ef26b9e363f41"
        assert report["host"] == host
        assert len(report["rows"]) == len(expected) == 270
        assert {(row["benchmark"], row["mode"], row["threads"], row["repeat"]) for row in report["rows"]} == expected
        with tarfile.open(ARCHIVE / host / "raw-profiles.tar.gz") as archive:
            for row in report["rows"]:
                assert not row["invalid"] and row["deterministic"] and row["work_parity"]
                key = (row["benchmark"], row["mode"].removeprefix("baseline-"))
                value = (row["paf_without_timing_sha256"], row["adaptive_counters"])
                assert fingerprints.setdefault(key, value) == value, (host, key)
                tag = f'{row["benchmark"]}_{row["mode"]}_t{row["threads"]}_r{row["repeat"]}'
                profile = json.load(archive.extractfile(f"raw/{tag}.json"))["global"]
                assert profile["timers_secs"]["mapping"] == row["mapping_s"]
                assert {key: value for key, value in profile["counters"].items()
                        if key.startswith("adaptive_")} == row["adaptive_counters"]
        checksums.append(f'- {host} report SHA-256: `{hashlib.sha256(path.read_bytes()).hexdigest()}`')
        for benchmark, mode, threads in itertools.product([f"B{number:02}" for number in range(1, 6)], MODES, [1, 16, 64]):
            result = dict(host=host, benchmark=benchmark, mode=mode, threads=threads)
            for label, prefix in [("baseline", "baseline-"), ("candidate", "")]:
                group = [row for row in report["rows"] if
                         (row["benchmark"], row["mode"], row["threads"]) == (benchmark, prefix + mode, threads)]
                result.update({f"{label}_{metric}": statistics.median(row[metric] for row in group) for metric in METRICS})
            for metric in ["wall_s", "mapping_s"]:
                result[f"{metric}_speedup"] = result[f"baseline_{metric}"] / result[f"candidate_{metric}"]
            rows.append(result)
    table = io.StringIO()
    writer = csv.DictWriter(table, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    lines = [
        "# Native Optimization Validation", "",
        "Candidate `6e33e5c6634dd7e8d5803f732094d7f3a414c9d6` versus release 1.6.0",
        "(`2363d92eaceac4cfffcbdc46203ef26b9e363f41`), measured on a2 and galaxy.", "",
        "All **540 invocations passed**: B01-B05, default/adaptive/parsing modes,",
        "1/16/64 mapping workers, three repeats per revision on each host.",
        "Normalized PAF hashes and adaptive work counters match across revisions,",
        "worker counts, repeats, and hosts. B02 retains all 125,000 mapped reads",
        "and 123,977 truth-overlap-correct placements in every mode.", "",
        "## Performance", "",
        "Ratios are baseline/candidate; greater than 1 is faster. Each cell uses",
        "the median of three runs. Matrix aggregates are geometric means across",
        "five datasets and three worker counts, not pooled read throughput.", "",
        "| Host | Mode | Total Speedup | Mapping Speedup |",
        "| --- | --- | ---: | ---: |",
    ]
    for host, mode in itertools.product(["a2", "galaxy"], MODES):
        group = [row for row in rows if row["host"] == host and row["mode"] == mode]
        lines.append(f'| {host} | {mode} | {statistics.geometric_mean(row["wall_s_speedup"] for row in group):.3f}x | {statistics.geometric_mean(row["mapping_s_speedup"] for row in group):.3f}x |')
    lines += ["", "### Deep HiFi (B04)", "",
              "| Host | Mode | Workers | Total Seconds (Old / New) | Mapping Seconds (Old / New) |",
              "| --- | --- | ---: | ---: | ---: |"]
    for row in rows:
        if row["benchmark"] == "B04":
            lines.append(f'| {row["host"]} | {row["mode"]} | {row["threads"]} | {row["baseline_wall_s"]:.2f} / {row["candidate_wall_s"]:.2f} | {row["baseline_mapping_s"]:.2f} / {row["candidate_mapping_s"]:.2f} |')
    lines += ["", "## Interpretation and Scope", "",
              "Total-time gains in compact-index modes must not be presented as mapping",
              "acceleration. The candidate bundles exact compact preallocation, ordered",
              "sampled anchors, and reused postings; these across-build comparisons do not",
              "isolate each change, and compiler layout and host variation remain factors.",
              "No 10x gain is established. Small mapping differences are not decisive evidence.", "",
              "Only native shmap was executed. Original-mapper fallback remains enabled;",
              "MAPQ 255 means unavailable. Parser mode uses two additional reader workers.",
              "Inputs are warmed before each run; index construction is included, with no",
              "persistent cache reuse. Dense rescue, short reads, cold-cache behavior, and",
              "the full maintained suite gate are not validated by this matrix.", "",
              "## Evidence", "",
              "[results.tsv](results.tsv) contains all 90 median comparisons, CPU time, and RSS.",
              "Each host subdirectory preserves its original report, run log, and compressed",
              "profiles/stderr/resource logs. Raw PAF files remain on each host under",
              "`~/bench-results/native-compare-6e33e5c/raw/`.", "",
              *checksums, "",
              "Regenerate with `python3 benchmarks/scripts/report_native_comparison.py`;",
              "verify with `--check`. Verification checks complete matrix coverage,",
              "cross-host/revision PAF and counter parity, and archived profile agreement.", ""]
    return {ARCHIVE / "README.md": "\n".join(lines), ARCHIVE / "results.tsv": table.getvalue()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path, content in render().items():
        if args.check:
            assert path.read_text() == content, f"stale report: {path}"
        else:
            path.write_text(content)
        print(f"checked {path}" if args.check else f"wrote {path}")


if __name__ == "__main__":
    main()