#!/usr/bin/env python3
"""Local, synthetic long-read experiment; not a substitute for the WGS suite."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
from pathlib import Path


def mutate(sequence: str, rng: random.Random, substitution: float, indel: float = 0.001) -> str:
    output = []
    for base in sequence:
        value = rng.random()
        if value < indel / 2:
            continue
        if value < indel:
            output.append(rng.choice("ACGT"))
        output.append(rng.choice("ACGT".replace(base, "")) if rng.random() < substitution else base)
    return "".join(output)


def generate(directory: Path, count: int, seed: int) -> tuple[Path, Path, dict]:
    rng = random.Random(seed)
    unique = "".join(rng.choices("ACGT", k=3_000_000))
    repeat = "".join(rng.choices("ACGT", k=1_000_000))
    copy = mutate(repeat, rng, 0.001, 0.0)
    sequence = unique + repeat + copy
    reference = directory / "reference.fa"
    reference.write_text(">ref\n" + "\n".join(sequence[offset:offset + 80] for offset in range(0, len(sequence), 80)) + "\n")
    reads = directory / "reads.fa"
    truth = {}
    with reads.open("w") as output:
        for number in range(count):
            category = "chimera" if number % 20 == 0 else "noisy" if number % 20 == 1 else "repeat" if number % 20 < 6 else "unique"
            start = rng.randrange(3_000_000, len(sequence) - 24_000) if category == "repeat" else rng.randrange(24_000, len(unique) - 24_000)
            length = rng.choice([12_000, 24_000])
            read = sequence[start:start + length]
            if category == "chimera":
                other = (start + 1_000_000) % (len(unique) - length)
                read = read[:length // 2] + sequence[other:other + length // 2]
            read = mutate(read, rng, 0.06 if category == "noisy" else 0.005)
            strand = "-" if rng.random() < 0.5 else "+"
            if strand == "-":
                read = read.translate(str.maketrans("ACGT", "TGCA"))[::-1]
            name = f"read{number}"
            output.write(f">{name}\n{read}\n")
            truth[name] = {"start": start, "end": start + length, "strand": strand, "category": category, "length": len(read)}
    (directory / "truth.json").write_text(json.dumps(truth))
    return reference, reads, truth


def assess(path: Path, truth: dict) -> dict:
    counts = {"mapped": 0, "correct": 0, "endpoints_1kb": 0, "wrong_confident": 0, "fast": 0, "invalid": 0,
              "mapq_unavailable": 0, "truth_reads": sum(item["category"] != "chimera" for item in truth.values())}
    seen = set()
    categories = {name: {"reads": 0, "mapped": 0, "correct": 0} for name in ["unique", "repeat", "noisy", "chimera"]}
    for item in truth.values():
        categories[item["category"]]["reads"] += 1
    with path.open() as handle:
        for line in handle:
            fields = line.rstrip().split("\t")
            name = fields[0]
            if len(fields) < 12 or name not in truth or name in seen:
                counts["invalid"] += 1
                continue
            seen.add(name)
            item = truth[name]
            counts["mapped"] += 1
            categories[item["category"]]["mapped"] += 1
            query_length, query_start, query_end = map(int, fields[1:4])
            target_length, target_start, target_end, matches, block_length, mapq = map(int, fields[6:12])
            valid = (query_length == item["length"] and 0 <= query_start < query_end <= query_length
                     and 0 <= target_start < target_end <= target_length and 0 <= matches <= block_length
                     and fields[4] in ("+", "-") and 0 <= mapq <= 255)
            counts["invalid"] += not valid
            locus = valid and item["category"] != "chimera" and fields[5] == "ref" and fields[4] == item["strand"]
            start_error = abs(target_start - item["start"])
            end_error = abs(target_end - item["end"])
            correct = locus and max(start_error, end_error) <= item["length"]
            counts["endpoints_1kb"] += locus and max(start_error, end_error) <= 1000
            counts["correct"] += correct
            categories[item["category"]]["correct"] += correct
            counts["wrong_confident"] += item["category"] != "chimera" and not correct and 10 <= mapq < 255
            counts["mapq_unavailable"] += mapq == 255
            counts["fast"] += any(field.startswith("am:Z:adaptive") for field in fields)
    return {**counts, "categories": categories}


def warm(path: Path) -> None:
    with path.open("rb") as handle:
        while handle.read(1 << 20):
            pass


def run(binary: Path, extra: list[str], prefix: Path, reference: Path, reads: Path, threads: int, truth: dict,
    posting_lookups: bool = False) -> dict:
    profile = prefix.with_suffix(".json")
    timing = prefix.with_suffix(".time")
    paf = prefix.with_suffix(".paf")
    command = ["/usr/bin/time", "-f", "%e\t%U\t%S\t%M", "-o", str(timing), str(binary),
               "-s", str(reference), "-p", str(reads), "-k", "25", "-r", "0.01", "-t", "0.4",
               "-d", "0.075", "-o", "0.3", "-@", str(threads), "-x", "--profile-log", str(profile), *extra]
    warm(reference)
    warm(reads)
    environment = os.environ.copy()
    environment.pop("SHMAP_DENSE_POSTING_LOOKUPS", None)
    if posting_lookups:
        environment["SHMAP_DENSE_POSTING_LOOKUPS"] = "1"
    with paf.open("w") as stdout, prefix.with_suffix(".stderr").open("w") as stderr:
        subprocess.run(command, stdout=stdout, stderr=stderr, check=True, env=environment)
    wall, user, system, rss = map(float, timing.read_text().split())
    data = json.loads(profile.read_text())
    timers = data["global"]["timers_secs"]
    counters = data["global"]["counters"]
    return {"wall_s": wall, "cpu_s": user + system, "peak_rss_kb": rss,
            "mapping_s": timers.get("mapping", 0), "indexing_s": timers.get("indexing", 0),
            "query_s": timers.get("query_mapping", 0), "adaptive_s": timers.get("adaptive", 0),
            "adaptive_bases": counters.get("adaptive_bases", 0),
            "adaptive_hits": counters.get("adaptive_hits", 0),
            "adaptive_candidates": counters.get("adaptive_candidates", 0),
            "dense_rescued": counters.get("adaptive_dense", 0),
            "repeat_indexing_s": timers.get("repeat_indexing", 0),
            "posting_lookups": posting_lookups, "command": command, **assess(paf, truth)}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=Path("target/release/shmap"))
    parser.add_argument("--baseline-binary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reads", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dense-lookup-ablation", action="store_true",
                        help="compare candidate-local scans with global posting searches in the same binary")
    args = parser.parse_args()
    if min(args.reads, args.repeats, args.threads) < 1:
        parser.error("reads, repeats, and threads must be positive")
    args.output.mkdir(parents=True, exist_ok=False)
    args.output = args.output.resolve()
    binary = args.binary.resolve()
    baseline = (args.baseline_binary or args.binary).resolve()
    reference, reads, truth = generate(args.output, args.reads, args.seed)
    cache = args.output / "reference.idx"
    configs = {
        "baseline": (baseline, []),
        "compatibility": (binary, []),
        "compact": (binary, ["--compact-index"]),
        "adaptive": (binary, ["--compact-index", "--adaptive"]),
        "adaptive-batched": (binary, ["--compact-index", "--adaptive", "--read-batch-size", "64"]),
        "adaptive-dense": (binary, ["--compact-index", "--adaptive", "--adaptive-dense", "--read-batch-size", "64"]),
        "adaptive-parsing": (binary, ["--compact-index", "--adaptive", "--read-batch-size", "64", "--reader-threads", "2"]),
        "adaptive-cached": (binary, ["--adaptive", "--read-batch-size", "64", "--index-cache", str(cache)]),
    }
    if args.dense_lookup_ablation:
        configs = {"adaptive-dense": configs["adaptive-dense"], "dense-postings": configs["adaptive-dense"]}
    build = run(binary, ["--index-cache", str(cache)], args.output / "cache-build", reference, reads, args.threads, truth)
    rows = []
    names = list(configs)
    for repeat in range(args.repeats):
        for name in names[repeat % len(names):] + names[:repeat % len(names)]:
            executable, extra = configs[name]
            row = {"mode": name, "repeat": repeat, **run(executable, extra, args.output / f"{name}-{repeat}", reference, reads, args.threads, truth,
                                                        posting_lookups=name == "dense-postings")}
            rows.append(row)
            print(f"{name:20} repeat={repeat} wall={row['wall_s']:.3f}s map={row['mapping_s']:.3f}s correct={row['correct']}/{row['truth_reads']} fast={row['fast']}", flush=True)
    summary = {}
    baseline_mode = "dense-postings" if args.dense_lookup_ablation else "baseline"
    base_wall = statistics.median(row["wall_s"] for row in rows if row["mode"] == baseline_mode)
    for name in names:
        group = [row for row in rows if row["mode"] == name]
        wall = statistics.median(row["wall_s"] for row in group)
        summary[name] = {"wall_s": wall, "speedup": base_wall / max(wall, 1e-9),
                         "mapping_s": statistics.median(row["mapping_s"] for row in group),
                         "cpu_s": statistics.median(row["cpu_s"] for row in group),
                         "peak_rss_kb": max(row["peak_rss_kb"] for row in group),
                         **{key: group[0][key] for key in ["mapped", "correct", "endpoints_1kb", "wrong_confident", "fast", "invalid", "mapq_unavailable", "categories", "dense_rescued"]}}
        assert all(row["invalid"] == 0 for row in group), f"invalid PAF in {name}"
        assert len({(row["mapped"], row["correct"], row["fast"]) for row in group}) == 1, f"nondeterministic counts in {name}"
    report = {"scope": "synthetic 5 Mb reference; not WGS; cache-build is excluded only from adaptive-cached",
              "baseline_mode": baseline_mode,
              "accuracy": "correct: both endpoints within one read length on the true strand; endpoints_1kb is stricter; chimeras excluded from both; MAPQ 255 is not confidence",
              "seed": args.seed, "threads": args.threads, "repeats": args.repeats, "host": platform.platform(),
              "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "worktree_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff", "HEAD"])).hexdigest(),
              "binary_sha256": digest(binary), "baseline_sha256": digest(baseline),
              "reference_sha256": digest(reference), "reads_sha256": digest(reads),
              "cache_build": build, "rows": rows, "summary": summary}
    (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    with (args.output / "summary.tsv").open("w") as handle:
        columns = ["mode", "wall_s", "mapping_s", "cpu_s", "speedup", "mapped", "correct", "endpoints_1kb", "wrong_confident", "mapq_unavailable", "fast", "peak_rss_kb"]
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        writer.writerows({"mode": name, **metrics} for name, metrics in summary.items())
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()