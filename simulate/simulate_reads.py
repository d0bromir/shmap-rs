#!/usr/bin/env python3
"""Simulate reads with independently controlled substitution and indel rates.

  simulate_reads.py --ref hs1.fa --n 20000 --len 24000 --sub 0.005 --ins 0.0 --del 0.0 -o reads.fa

Why this exists alongside `pbsim3/`
-----------------------------------
The PBSIM3 scripts in `pbsim3/` are the provenance record: they are what
actually produced the published datasets, using either a real HiFi read sample
(`--method sample`) or a PacBio error HMM (`--method errhmm`, accuracy 0.99).
Keep using them to reproduce those datasets.

They are the wrong tool for asking *how error rate affects the mapper*, because
an HMM error model couples substitutions and indels: you can ask for 99%
accuracy but not for "1% substitutions and no indels" versus "0.5% substitutions
and 0.5% indels". Separating those is the whole question — k-mer based mapping
should degrade very differently under the two, since one indel shifts every
downstream k-mer while one substitution destroys only the k covering it.

So this generates reads with exact, independent rates. It is not a replacement
for a realistic error model and should not be used to make claims about real
data; it is an instrument for a controlled sweep.

Ground truth
------------
Headers match what `paftools.js pbsim2fq` emits, so every existing tool here
(`profiling/validate_paf.py --truth`, `adjudicate_disagreements.py`) reads them
unchanged:

    >S<n>_1!<chr>!<start>!<end>!<strand>

`start`/`end` are 0-based half-open coordinates of the sampled interval on the
forward strand, before mutation.

Determinism
-----------
Fully determined by `--seed`. The same seed and parameters regenerate the same
file byte-for-byte, so a dataset can be re-derived rather than archived.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

COMP = str.maketrans("ACGTNacgtn", "TGCANtgcan")
BASES = "ACGT"


def read_fasta(path: str) -> list[tuple[str, str]]:
    """Whole-sequence FASTA reader. The reference is held in memory — 3.1 Gbp
    is ~3 GB as a str, which is fine on the benchmark host and keeps sampling
    to an index rather than a seek."""
    seqs: list[tuple[str, str]] = []
    name, parts = None, []
    with open(path) as f:
        for line in f:
            if line.startswith(">"):
                if name is not None:
                    seqs.append((name, "".join(parts)))
                name, parts = line[1:].split()[0], []
            else:
                parts.append(line.strip())
    if name is not None:
        seqs.append((name, "".join(parts)))
    return seqs


def mutate(seq: str, sub: float, ins: float, dele: float, rng: random.Random) -> str:
    """Apply per-base substitution, insertion and deletion rates.

    Rates are per reference base and independent, so `--sub 0.01 --ins 0.01`
    gives roughly 1% of each rather than 1% total. Insertions are emitted
    before the base so a run can grow, and a deleted base emits nothing.
    """
    out = []
    for b in seq:
        if ins and rng.random() < ins:
            out.append(rng.choice(BASES))
        r = rng.random()
        if dele and r < dele:
            continue
        if sub and r < dele + sub:
            out.append(rng.choice([x for x in BASES if x != b.upper()]))
        else:
            out.append(b)
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--n", type=int, help="number of reads (or use --depth)")
    ap.add_argument("--depth", type=float, help="coverage; overrides --n")
    ap.add_argument("--len", type=int, default=24000, dest="length")
    ap.add_argument("--len-sd", type=int, default=0)
    ap.add_argument("--sub", type=float, default=0.0, help="substitutions per base")
    ap.add_argument("--ins", type=float, default=0.0, help="insertions per base")
    ap.add_argument("--del", type=float, default=0.0, dest="dele", help="deletions per base")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-contig", type=int, default=0,
                    help="skip contigs shorter than this (default: skip any shorter than a read)")
    a = ap.parse_args()

    if a.n is None and a.depth is None:
        ap.error("one of --n or --depth is required")

    rng = random.Random(a.seed)
    seqs = [(n, s) for n, s in read_fasta(a.ref)
            if len(s) >= max(a.length * 2, a.min_contig)]
    if not seqs:
        sys.exit("no contig long enough to sample a read from")
    total = sum(len(s) for _, s in seqs)
    n = a.n if a.n is not None else max(1, round(a.depth * total / a.length))

    # Sample position proportional to contig length, so coverage is uniform
    # across the genome rather than across contigs.
    cum, acc = [], 0
    for name, s in seqs:
        acc += len(s)
        cum.append(acc)

    written = 0
    with open(a.out, "w") as fo:
        for i in range(1, n + 1):
            x = rng.randrange(total)
            lo, hi = 0, len(cum) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if cum[mid] <= x:
                    lo = mid + 1
                else:
                    hi = mid
            name, s = seqs[lo]
            L = a.length if not a.len_sd else max(1000, int(rng.gauss(a.length, a.len_sd)))
            if len(s) <= L:
                continue
            start = rng.randrange(0, len(s) - L)
            frag = s[start:start + L]
            if "N" in frag or "n" in frag:
                continue                      # unplaceable; skip rather than emit garbage
            strand = rng.choice("+-")
            if strand == "-":
                frag = frag.translate(COMP)[::-1]
            fo.write(f">S{i}_1!{name}!{start}!{start + L}!{strand}\n")
            fo.write(mutate(frag, a.sub, a.ins, a.dele, rng) + "\n")
            written += 1

    print(f"wrote {written} reads to {a.out} "
          f"(sub={a.sub} ins={a.ins} del={a.dele} len={a.length} seed={a.seed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
