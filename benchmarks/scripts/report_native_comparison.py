#!/usr/bin/env python3
"""Render and verify the archived native 1.6.0 optimization comparison."""

import argparse
import csv
import hashlib
import io
import itertools
import json
import math
from pathlib import Path
import re
import shlex
import statistics
import tarfile

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "benchmarks/results/native-compare-6e33e5c"
MODES = ["default", "adaptive", "parsing"]
METRICS = ["wall_s", "mapping_s", "cpu_s", "peak_rss_kb"]


def cpp_comparison():
    comparisons = []
    for host, arch in [("a2", "x86_64"), ("galaxy", "aarch64")]:
        source = ROOT / f"benchmarks/results/suite-1.0/{arch}/current"
        manifest = json.loads((source / "manifest.json").read_text())
        native = json.loads((ARCHIVE / host / "report.json").read_text())
        assert manifest["host"] == host
        assert manifest["commit"] == "18d0f83627b5fcf9299a9e270460858f321c39ad"
        assert manifest["dataset_version"] == native["dataset_version"]
        with (source / "results.tsv").open() as handle:
            cpp_rows = [row for row in csv.DictReader(handle, delimiter="\t")
                        if row["impl"] == "cpp-shmap" and row["metric"] == "Containment"]
        assert len(cpp_rows) == 5
        for cpp in sorted(cpp_rows, key=lambda row: row["benchmark"]):
            assert cpp["rc"] == "0" and cpp["threads"] == "1" and cpp["repeat"] == "median3"
            command = shlex.split(cpp["cmd"])
            timing_path = command[command.index("-o") + 1]
            date = re.search(r"\d{4}-\d{2}-\d{2}", timing_path).group()
            row = dict(host=host, benchmark=cpp["benchmark"], cpp_date=date, cpp_wall_s=float(cpp["wall_s"]))
            for mode in ["default", "adaptive"]:
                group = [item for item in native["rows"] if item["benchmark"] == cpp["benchmark"]
                         and item["mode"] == mode and item["threads"] == 1]
                assert len(group) == 3
                for item in group:
                    native_command = item["command"]
                    for flag in ["-k", "-r", "-t", "-d", "-m"]:
                        assert command[command.index(flag) + 1] == native_command[native_command.index(flag) + 1]
                    cpp_overlap = command[max(index for index, value in enumerate(command) if value == "-o") + 1]
                    native_overlap = native_command[max(index for index, value in enumerate(native_command) if value == "-o") + 1]
                    assert cpp_overlap == native_overlap
                    for flag, dataset in [("-s", cpp["reference_id"]), ("-p", cpp["reads_id"])]:
                        suffix = native["datasets"][dataset]["rel"]
                        assert command[command.index(flag) + 1].endswith("/" + suffix)
                        assert native_command[native_command.index(flag) + 1].endswith("/" + suffix)
                row[f"{mode}_wall_s"] = statistics.median(item["wall_s"] for item in group)
                row[f"{mode}_speedup"] = row["cpp_wall_s"] / row[f"{mode}_wall_s"]
            row["cpp_source"] = str((source / "results.tsv").relative_to(ROOT))
            row["cpp_command"] = cpp["cmd"]
            comparisons.append(row)
    limit = math.ceil(max(row[f"{mode}_speedup"] for row in comparisons for mode in ["default", "adaptive"]))
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="790" viewBox="0 0 1000 790" role="img" aria-labelledby="title desc">',
           '<title id="title">Rust versus historical C++ shmap: whole-run speedup</title>',
           '<desc id="desc">Same host, Containment, one mapping worker. C++ is the 1x baseline. Historical timings, not a fresh head-to-head run.</desc>',
           '<rect width="1000" height="790" fill="#ffffff"/>',
           '<g font-family="sans-serif" fill="#20262b">',
           '<text x="32" y="35" font-size="23" font-weight="bold">Rust versus historical C++ shmap</text>',
           '<text x="32" y="62" font-size="15">Whole-run speedup; larger is faster. Containment, one mapping worker.</text>',
           '<rect x="32" y="81" width="18" height="14" fill="#087e8b"/><text x="58" y="94" font-size="14">Rust default</text>',
           '<rect x="220" y="81" width="18" height="14" fill="#c43c59"/><text x="246" y="94" font-size="14">Rust adaptive (experimental; MAPQ unavailable)</text>']
    labels = {"B01": "HiFi 23 kb", "B02": "Simulated 24 kb", "B03": "HiFi 1x", "B04": "HiFi 10x", "B05": "ONT 24 kb"}
    for panel, host in enumerate(["a2", "galaxy"]):
        left = 32 + panel * 492
        start = left + 150
        scale = 268 / limit
        svg.append(f'<text x="{left}" y="134" font-size="20" font-weight="bold">{host}</text>')
        for tick in range(limit + 1):
            position = start + tick * scale
            svg.append(f'<path d="M {position:.2f} 158 V 660" stroke="{"#555555" if tick == 1 else "#e3e7e9"}" stroke-dasharray="4 4"/>')
            svg.append(f'<text x="{position:.2f}" y="681" text-anchor="middle" font-size="12">{tick}x</text>')
        for index, row in enumerate(item for item in comparisons if item["host"] == host):
            top = 172 + index * 98
            svg.append(f'<text x="{left}" y="{top + 13}" font-size="14">{row["benchmark"]}: {labels[row["benchmark"]]}</text>')
            svg.append(f'<text x="{left}" y="{top + 34}" font-size="12">C++ {row["cpp_wall_s"]:.2f} s</text>')
            for offset, mode, color in [(0, "default", "#087e8b"), (30, "adaptive", "#c43c59")]:
                width = row[f"{mode}_speedup"] * scale
                svg.append(f'<rect x="{start}" y="{top + offset}" width="{width:.2f}" height="22" fill="{color}"/>')
                svg.append(f'<text x="{start + width + 5:.2f}" y="{top + offset + 16}" font-size="12">{row[f"{mode}_speedup"]:.2f}x</text>')
    svg += ['<text x="32" y="719" font-size="13">Rust: 6e33e5c, median of three runs. C++: archived median-of-three timings.</text>',
            '<text x="32" y="742" font-size="13">C++ B01/B03/B04: Aug 10 (a2), Aug 9 (galaxy); B02/B05: Sep 12, 2026.</text>',
            '<text x="32" y="765" font-size="13">Not contemporaneous; no new C++ accuracy or output-equivalence claim. Index construction included.</text>',
            '</g></svg>']
    table = io.StringIO()
    writer = csv.DictWriter(table, fieldnames=list(comparisons[0]), delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(comparisons)
    return {ARCHIVE / "versus-cpp.svg": "\n".join(svg) + "\n", ARCHIVE / "versus-cpp.tsv": table.getvalue()}


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
    lines += ["## Historical C++ Comparison", "",
              "![Same-host Rust versus historical C++ shmap timing ratios](versus-cpp.svg)", "",
              "[Chart data and exact C++ commands](versus-cpp.tsv). Both implementations use",
              "Containment and one mapping worker; default Rust is the compatibility mode.",
              "Adaptive Rust is experimental and has different mapping semantics. Indexing",
              "is included and each value is a median of three runs. C++ was not rerun:",
              "B01/B03/B04 are from August 10 (a2) and August 9 (galaxy), and B02/B05",
              "from September 12, 2026. Dates follow the original row commands, not the",
              "carry-forward manifest date. This historical comparison is subject to host",
              "drift and is not a new C++ accuracy, parity, or maintained-suite gate.", ""]
    return {ARCHIVE / "README.md": "\n".join(lines), ARCHIVE / "results.tsv": table.getvalue(), **cpp_comparison()}


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