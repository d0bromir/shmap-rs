#!/usr/bin/env python3
"""Accuracy at each benchmark's operating point, by the mapeval criterion.

  accuracy_at_operating_points.py --ref hs1.fa --binary target/release/shmap

WHY THIS IS NOT "ACCURACY ON THE REAL DATASETS"
-----------------------------------------------
Accuracy needs truth. Only B02 has it — its reads carry their true positions,
because they were simulated. B01, B03, B04 and B05 are real reads, and **no
truth exists for them**: that is why RESULTS.md §8 reports concordance against
another mapper for those, which is agreement with an estimate, not accuracy.

What this does instead is measure accuracy at each real dataset's *operating
point*: a simulated set matched to that dataset's read length and measured error
rate, where truth is known by construction. It answers "how accurate is the
mapper on reads like these" and not "how accurate was it on those reads".

Two things that makes it, and one it does not:

  it does     put a number on every benchmark's regime rather than only B02's
  it does     use the same criterion (mapeval overlap) everywhere, so the rows
              are comparable to each other and to published figures
  it does NOT capture real biology — structural variation, heterozygosity,
              cross-individual divergence, or platform error structure beyond a
              rate and a spread. A proxy read is not the read.

ERROR RATES
-----------
Taken from `measure_error_rate.py --from-paf`, which compares each read against
its own confidently-mapped locus. That is biased low — reads too damaged to
reach mapq 60 are invisible to it — so the ONT point in particular is optimistic
and is also run at a higher rate to bracket it.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_reads import COMP, mutate, read_error_scale, read_fasta  # noqa: E402

import random  # noqa: E402

HDR = re.compile(r"^S\d+_\d+!([^!]+)!(\d+)!(\d+)!([+-])")
MIN_OVERLAP = 0.1          # paftools.js mapeval; same constant as concordance.py

# (label, models, read_len, total_error, error_sd, hp_bias)
# Lengths and error rates from benchmarks/datasets.tsv and measure_error_rate.py
# --from-paf. HiFi errors are split ~80/20 substitution/indel; ONT ~60/40, which
# is the usual shape for those platforms.
POINTS = [
    ("B01-like HiFi 23.2kb", "B01",      23189, 0.00435, 0.4, 5.0, 0.8),
    ("B03/B04-like HiFi 12.8kb", "B03/B04", 12838, 0.00380, 0.4, 5.0, 0.8),
    ("B05-like ONT 23.8kb", "B05",       23760, 0.03186, 0.5, 5.0, 0.6),
    ("B05-like ONT, 6% (bracket)", "B05", 23760, 0.06000, 0.5, 5.0, 0.6),
    ("B02 (as generated)", "B02",        24000, 0.00500, 0.0, 1.0, 1.0),
]
METRICS = ["Containment", "Jaccard", "bucket_SH"]


def gen(seqs, total, cum, n, length, sub, ins, dele, esd, hp, seed, out):
    rng = random.Random(seed)
    written, attempts, i = 0, 0, 0
    with open(out, "w") as fo:
        while written < n and attempts < n * 5:
            attempts += 1
            i += 1
            x = rng.randrange(total)
            lo, hi = 0, len(cum) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if cum[mid] <= x:
                    lo = mid + 1
                else:
                    hi = mid
            name, s = seqs[lo]
            if len(s) <= length:
                continue
            start = rng.randrange(0, len(s) - length)
            frag = s[start:start + length]
            if "N" in frag or "n" in frag:
                continue
            strand = rng.choice("+-")
            if strand == "-":
                frag = frag.translate(COMP)[::-1]
            sc = read_error_scale(rng, esd)
            fo.write(f">S{i}_1!{name}!{start}!{start+length}!{strand}\n")
            fo.write(mutate(frag, sub * sc, ins * sc, dele * sc, rng, hp) + "\n")
            written += 1
    return written


def score(paf: str, n: int) -> tuple[int, int, float]:
    """(mapped, correct-by-mapeval-overlap, mapq60 fraction of mapped)"""
    mapped = correct = q60 = 0
    for line in open(paf):
        c = line.split("\t")
        if len(c) < 12:
            continue
        m = HDR.match(c[0])
        if not m:
            continue
        mapped += 1
        if c[11] == "60":
            q60 += 1
        if c[5] != m.group(1):
            continue
        ts, te = int(m.group(2)), int(m.group(3))
        gs, ge = int(c[7]), int(c[8])
        inter = min(te, ge) - max(ts, gs)
        union = max(te, ge) - min(ts, gs)
        if inter > 0 and union > 0 and inter / union > MIN_OVERLAP:
            correct += 1
    return mapped, correct, (q60 / mapped if mapped else 0.0)


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--threads", type=int, default=32)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()

    print(f"loading {a.ref} ...", file=sys.stderr)
    seqs = [(n, s) for n, s in read_fasta(a.ref) if len(s) > 50000]
    acc, cum = 0, []
    for _, s in seqs:
        acc += len(s)
        cum.append(acc)
    total = acc
    work = Path(tempfile.mkdtemp(prefix="acc-"))

    hdr = (f'{"operating point":28}{"like":9}{"err%":>6}{"metric":>12}'
           f'{"mapped":>9}{"correct":>9}{"of_n":>9}{"mapq60":>8}')
    print(hdr)
    print("-" * len(hdr))
    for label, like, length, err, esd, hp, sub_frac in POINTS:
        sub = err * sub_frac
        indel = err * (1 - sub_frac) / 2
        reads = work / (label.replace(" ", "_").replace("/", "-").replace("%", "") + ".fa")
        n = gen(seqs, total, cum, a.n, length, sub, indel, indel, esd, hp, a.seed, reads)
        for metric in METRICS:
            paf = f"{reads}.{metric}.paf"
            with open(paf, "w") as fo:
                subprocess.run([a.binary, "-s", a.ref, "-p", str(reads),
                                "-k", "25", "-r", "0.01", "-t", "0.4", "-d", "0.075",
                                "-o", "0.3", "-m", metric, "-@", str(a.threads)],
                               stdout=fo, stderr=subprocess.DEVNULL)
            mapped, correct, q60 = score(paf, n)
            print(f"{label:28}{like:9}{err*100:>6.2f}{metric:>12}"
                  f"{mapped:>9}{correct:>9}{correct/n*100:>8.2f}%{q60*100:>7.1f}%")
            Path(paf).unlink(missing_ok=True)
        reads.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
