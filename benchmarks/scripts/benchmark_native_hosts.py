#!/usr/bin/env python3
"""Native-only WGS mode comparison, separate from the maintained suite gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import statistics
import subprocess
import sys
from pathlib import Path

import run as runner
from benchmark_adaptive import digest, warm

MODES = {
    "default": [],
    "adaptive": ["--compact-index", "--adaptive", "--read-batch-size", "64"],
    "dense": ["--compact-index", "--adaptive", "--adaptive-dense", "--read-batch-size", "64"],
    "parsing": ["--compact-index", "--adaptive", "--read-batch-size", "64", "--reader-threads", "2"],
}
TRUTH = re.compile(r"^S\d+_\d+!([^!]+)!(\d+)!(\d+)!([+-])")


def assess(path: Path) -> dict:
    counts = dict(mapped=0, invalid=0, fast=0, dense=0, mapq_unavailable=0,
                  truth_mapped=0, truth_overlap_correct=0, endpoints_1kb=0, wrong_mapq60=0)
    fingerprint = hashlib.sha256()
    with path.open() as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            fingerprint.update(("\t".join(field for field in fields if not field.startswith("t:f:")) + "\n").encode())
            counts["mapped"] += 1
            try:
                query_length, query_start, query_end = map(int, fields[1:4])
                target_length, start, end, matches, block, mapq = map(int, fields[6:12])
                valid = (0 <= query_start < query_end <= query_length
                         and 0 <= start < end <= target_length and 0 <= matches <= block
                         and fields[4] in ("+", "-") and (0 <= mapq <= 60 or mapq == 255))
            except (ValueError, IndexError):
                counts["invalid"] += 1
                continue
            counts["invalid"] += not valid
            counts["mapq_unavailable"] += mapq == 255
            counts["fast"] += any(field.startswith("am:Z:adaptive") for field in fields[12:])
            counts["dense"] += "am:Z:adaptive-dense-v1" in fields[12:]
            truth = TRUTH.match(fields[0])
            if truth:
                target, true_start, true_end, strand = truth.groups()
                true_start, true_end = int(true_start), int(true_end)
                same_locus = valid and target == fields[5] and strand == fields[4]
                intersection = max(0, min(end, true_end) - max(start, true_start))
                union = max(end, true_end) - min(start, true_start)
                correct = same_locus and union > 0 and intersection / union > 0.1
                counts["truth_mapped"] += 1
                counts["truth_overlap_correct"] += correct
                counts["endpoints_1kb"] += same_locus and max(abs(start - true_start), abs(end - true_end)) <= 1000
                counts["wrong_mapq60"] += not correct and mapq == 60
    return dict(**counts, paf_without_timing_sha256=fingerprint.hexdigest())


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="B01,B02,B03,B04,B05")
    parser.add_argument("--threads", default="1,16,64")
    parser.add_argument("--repeats", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("refusing to run as root")
    threads = [int(value) for value in args.threads.split(",")]
    cap = runner.host_config().get("thread_cap", 64)
    if not threads or min(threads) < 1 or max(threads) > cap or (args.repeats is not None and args.repeats < 1):
        parser.error("invalid threads or repeats")
    suite, registry = runner.load_suite(), runner.load_registry()
    selected = args.only.split(",")
    if not set(selected) <= {f"B{number:02}" for number in range(1, 6)}:
        parser.error("only B01-B05 are supported")
    suite["benchmark"] = [bench for bench in suite["benchmark"] if bench["id"] in selected]
    suite["run"]["drift_probe"]["enabled"] = False
    for bench in suite["benchmark"]:
        bench["metrics"], bench["threads"], bench["impls"] = ["Containment"], threads, ["shmap-rs"]
    repeats = runner.subject_repeats(suite, args.repeats)
    jobs = runner.plan(suite, registry, ["shmap-rs"], repeats)
    print(f"native-only: {len(jobs) * len(MODES)} measurements; modes={list(MODES)} threads={threads} repeats={repeats}")
    print("parsing uses two additional reader threads; other modes use the default reader; no cache reuse")
    if args.dry_run:
        return
    commit = subprocess.check_output(["git", "-C", str(runner.REPO), "rev-parse", "--verify", args.commit + "^{commit}"], text=True).strip()
    args.out = args.out.expanduser().resolve()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("GH_", "GITHUB_", "SHMAP_"))}
    with runner.HostLock() as lock:
        lock.note(f"native commit={commit[:12]} preparing")
        runner.verify_datasets(suite, registry)
        if (runner.WORKROOT / commit[:12]).exists():
            raise RuntimeError("build worktree already exists; refusing to replace it")
        args.out.mkdir(parents=True, exist_ok=False)
        raw = args.out / "raw"
        raw.mkdir()
        worktree = runner.prepare_worktree(commit)
        binary = worktree / "target/release/shmap"
        rows, hashes = [], {}
        manifest = dict(scope="native-only WGS ablation, not the maintained suite gate",
                        commit=commit, host=platform.node(), arch=platform.machine(),
                        driver_sha256=digest(Path(__file__)), rustc=runner.rustc_version(),
                        suite_version=suite["suite_version"], dataset_version=suite["dataset_version"],
                        binary_sha256=digest(binary), repeats=repeats, threads=threads, modes=MODES,
                        accuracy="IoU > 0.1 on true segment AND strand; endpoints_1kb requires both endpoints; MAPQ 255 is unavailable",
                        datasets={key: value for key, value in registry.items() if key in {job[field] for job in jobs for field in ("reference_id", "reads_id")}},
                        status="running", rows=rows)
        report = args.out / "report.json"
        report.write_text(json.dumps(manifest, indent=2) + "\n")
        try:
            for job in jobs:
                names = list(MODES)
                offset = job["repeat"] % len(names)
                for mode in names[offset:] + names[:offset]:
                    runner.check_disk_space(args.out)
                    tag = f"{job['benchmark']}_{mode}_t{job['threads']}_r{job['repeat']}"
                    lock.note(f"native commit={commit[:12]} {tag}")
                    print(f"starting {tag}")
                    for field in ("reference", "reads"):
                        warm(Path(job[field]))
                    prefix = raw / tag
                    paf, timing, profile = (prefix.with_suffix(suffix) for suffix in (".paf", ".time", ".json"))
                    command = ["/usr/bin/time", "-f", "%e\t%U\t%S\t%M", "-o", str(timing), str(binary),
                               "-s", job["reference"], "-p", job["reads"], *job["base"], "-m", "Containment",
                               "-@", str(job["threads"]), "-x", "--profile-log", str(profile), *MODES[mode]]
                    with paf.open("w") as output, prefix.with_suffix(".stderr").open("w") as errors:
                        subprocess.run(command, stdout=output, stderr=errors, env=environment, check=True)
                    wall, user, system, rss = map(float, timing.read_text().split())
                    timers = json.loads(profile.read_text())["global"]["timers_secs"]
                    row = dict(benchmark=job["benchmark"], mode=mode, threads=job["threads"], repeat=job["repeat"],
                               wall_s=wall, cpu_s=user + system, peak_rss_kb=rss,
                               mapping_s=timers.get("mapping"), indexing_s=timers.get("indexing"),
                               repeat_indexing_s=timers.get("repeat_indexing", 0), command=command, **assess(paf))
                    key = (job["benchmark"], mode)
                    previous = hashes.setdefault(key, row["paf_without_timing_sha256"])
                    row["deterministic"] = previous == row["paf_without_timing_sha256"]
                    rows.append(row)
                    report.write_text(json.dumps(manifest, indent=2) + "\n")
                    print(f"finished {tag}: wall={wall:.2f}s mapping={row['mapping_s']} mapped={row['mapped']} fast={row['fast']} invalid={row['invalid']} deterministic={row['deterministic']}")
                    if row["invalid"] or not row["deterministic"]:
                        raise RuntimeError(f"invalid or nondeterministic output: {tag}")
            summary = []
            for benchmark in selected:
                for thread_count in threads:
                    baseline = statistics.median(row["wall_s"] for row in rows if row["benchmark"] == benchmark and row["threads"] == thread_count and row["mode"] == "default")
                    for mode in MODES:
                        group = [row for row in rows if row["benchmark"] == benchmark and row["threads"] == thread_count and row["mode"] == mode]
                        wall = statistics.median(row["wall_s"] for row in group)
                        summary.append(dict(benchmark=benchmark, threads=thread_count, mode=mode, wall_s=wall,
                                            mapping_s=statistics.median(row["mapping_s"] for row in group), speedup=baseline / wall))
            manifest.update(status="complete", summary=summary)
        except BaseException as error:
            manifest.update(status="failed", error=str(error))
            raise
        finally:
            report.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"complete: {report}")


if __name__ == "__main__":
    main()