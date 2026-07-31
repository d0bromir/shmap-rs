#!/usr/bin/env python3
"""How does shmap-rs degrade as substitution and indel rates rise?

  sweep_error_rates.py --ref hs1.fa --binary target/release/shmap [--n 20000]

The published accuracy numbers rest on D2-SIM24K, which measures 0.498% error
with a length delta of +0.004% — essentially pure substitutions, no indels. So
every accuracy claim here is a substitution-only claim, and this sweep is what
tests whether that generalises.

The hypothesis worth testing is that indels are *not* equivalent to
substitutions at the same rate, even though both destroy about `k` k-mers per
event. shmap scores a bucket over a window bounded by the read's own k-mer
count, so it assumes the read and its reference interval have nearly the same
length. Substitutions preserve that; indels do not. If the assumption is what
matters, indels should hurt disproportionately, and the damage should show up in
*span* before it shows up in k-mer survival.

The reference is loaded once and reused for every parameter set — parsing 3.1
Gbp in Python is minutes, and doing it per row would dominate the run.
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
from simulate_reads import COMP, mutate, read_fasta  # noqa: E402

import random  # noqa: E402

HDR = re.compile(r"^S\d+_\d+!([^!]+)!(\d+)!(\d+)!([+-])")

# (label, sub, ins, del). Chosen so substitution-only and indel-only rows can be
# read against each other at equal TOTAL error, which is the comparison that
# answers the question.
GRID = [
    ("clean",            0.000,  0.0,    0.0),
    ("sub 0.5% (=D2)",   0.005,  0.0,    0.0),
    ("sub 1%",           0.010,  0.0,    0.0),
    ("sub 2%",           0.020,  0.0,    0.0),
    ("sub 5%",           0.050,  0.0,    0.0),
    ("indel 0.5%",       0.000,  0.0025, 0.0025),
    ("indel 1%",         0.000,  0.005,  0.005),
    ("indel 2%",         0.000,  0.010,  0.010),
    ("del-only 1%",      0.000,  0.0,    0.010),
    ("ins-only 1%",      0.000,  0.010,  0.0),
    ("HiFi-like",        0.004,  0.0005, 0.0005),
    ("ONT-like 5%",      0.030,  0.010,  0.010),
]


def gen(seqs, total, cum, n, length, sub, ins, dele, seed, out):
    rng = random.Random(seed)
    written = 0
    with open(out, "w") as fo:
        i = 0
        attempts = 0
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
            fo.write(f">S{i}_1!{name}!{start}!{start+length}!{strand}\n")
            fo.write(mutate(frag, sub, ins, dele, rng) + "\n")
            written += 1
    return written


def score(paf: str) -> tuple[int, int, float]:
    """(mapped, correct, mean |span - read length| / read length)"""
    mapped = correct = 0
    span_err = []
    for line in open(paf):
        c = line.split("\t")
        if len(c) < 12:
            continue
        m = HDR.match(c[0])
        if not m:
            continue
        mapped += 1
        ts, te = int(m.group(2)), int(m.group(3))
        gs, ge = int(c[7]), int(c[8])
        span_err.append(abs((ge - gs) - (te - ts)) / (te - ts))
        if c[5] != m.group(1):
            continue
        lo, hi = max(ts, gs), min(te, ge)
        if hi > lo and (hi - lo) / (max(te, ge) - min(ts, gs)) > 0.1:
            correct += 1
    return mapped, correct, (sum(span_err) / len(span_err) if span_err else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--len", type=int, default=24000, dest="length")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--k", type=int, default=25)
    ap.add_argument("--hashratio", type=float, default=0.01)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keep", help="directory to keep generated read sets in")
    a = ap.parse_args()

    print(f"loading {a.ref} ...", file=sys.stderr)
    t0 = time.time()
    seqs = [(n, s) for n, s in read_fasta(a.ref) if len(s) > a.length * 2]
    total, acc, cum = 0, 0, []
    for _, s in seqs:
        acc += len(s)
        cum.append(acc)
    total = acc
    print(f"  {len(seqs)} contigs, {total/1e9:.3f} Gbp, {time.time()-t0:.0f}s",
          file=sys.stderr)

    work = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="errsweep-"))
    work.mkdir(parents=True, exist_ok=True)

    hdr = (f'{"profile":16}{"sub%":>7}{"indel%":>8}{"mapped":>9}{"correct":>9}'
           f'{"of_n":>9}{"span_err":>10}{"wall_s":>8}')
    print(hdr)
    print("-" * len(hdr))
    for label, sub, ins, dele in GRID:
        reads = work / f"{label.replace(' ', '_').replace('%','')}.fa"
        n = gen(seqs, total, cum, a.n, a.length, sub, ins, dele, a.seed, reads)
        paf = str(reads) + ".paf"
        t = time.time()
        with open(paf, "w") as fo:
            subprocess.run([a.binary, "-s", a.ref, "-p", str(reads),
                            "-k", str(a.k), "-r", str(a.hashratio), "-t", "0.4",
                            "-d", "0.075", "-o", "0.3", "-m", "Containment",
                            "-@", str(a.threads)],
                           stdout=fo, stderr=subprocess.DEVNULL)
        wall = time.time() - t
        mapped, correct, span = score(paf)
        print(f"{label:16}{sub*100:>7.2f}{(ins+dele)*100:>8.2f}{mapped:>9}{correct:>9}"
              f"{correct/n*100:>8.2f}%{span*100:>9.2f}%{wall:>8.1f}")
        if not a.keep:
            Path(paf).unlink(missing_ok=True)
            reads.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
