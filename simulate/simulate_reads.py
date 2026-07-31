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

So this generates reads with exact, independent rates.

Two things make it more than a toy, and both were added after a uniform version
gave a badly misleading answer:

  --error-sd   spreads the error rate ACROSS reads. Every read carrying exactly
               the mean is the most misleading simplification available here,
               because shmap maps a read when (1-e)^k > t — a threshold on that
               read's own rate. At a mean near the threshold the mean predicts
               almost nothing: real ONT maps ~43% at k=25 where a uniform
               simulation at the same mean mapped 0.08%.

  --hp-bias    concentrates indels in homopolymer runs, which is where real
               long-read indels overwhelmingly fall. Clustered damage leaves
               more intact k-mers than the same count spread evenly.

It is still not a sequencer model — error is not position-independent, and
context effects beyond homopolymers are ignored. Use `pbsim3/` to reproduce the
published datasets; use this to ask controlled questions.

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


def mutate(seq: str, sub: float, ins: float, dele: float, rng: random.Random,
           hp_bias: float = 1.0) -> str:
    """Apply per-base substitution, insertion and deletion rates.

    Rates are per reference base and independent, so `--sub 0.01 --ins 0.01`
    gives roughly 1% of each rather than 1% total. Insertions are emitted
    before the base so a run can grow, and a deleted base emits nothing.

    `hp_bias` multiplies the indel rate inside homopolymer runs (a base equal to
    the one before it). Real long-read indels are overwhelmingly homopolymer
    length errors rather than uniformly scattered, and the difference matters
    for a k-mer method: clustering the damage into runs leaves more intact
    k-mers than the same number of errors spread evenly. 1.0 disables it.
    """
    out = []
    prev = ""
    for b in seq:
        in_hp = b.upper() == prev
        prev = b.upper()
        i_rate = ins * (hp_bias if in_hp else 1.0)
        d_rate = dele * (hp_bias if in_hp else 1.0)
        if i_rate and rng.random() < i_rate:
            # A homopolymer insertion extends the run; elsewhere it is random.
            out.append(prev if in_hp else rng.choice(BASES))
        r = rng.random()
        if d_rate and r < d_rate:
            continue
        if sub and r < d_rate + sub:
            out.append(rng.choice([x for x in BASES if x != b.upper()]))
        else:
            out.append(b)
    return "".join(out)


def read_error_scale(rng: random.Random, sd: float) -> float:
    """Per-read multiplier on the error rates, mean 1.

    Every read having exactly the mean error rate is the single most misleading
    thing a simulator can do here. shmap maps a read when `(1-e)^k > t`, which
    is a threshold on that read's OWN error rate — so at a mean near the
    threshold, what maps is the low-error tail and the mean predicts almost
    nothing. Measured: real ONT maps ~43% at k=25, while a uniform simulation at
    the same mean maps 0.08%.

    Drawn from a gamma with unit mean (shape 1/sd^2), which is positive by
    construction, right-skewed like real accuracy distributions, and collapses
    to exactly 1.0 when sd is 0 — so the default reproduces the old behaviour.
    """
    if sd <= 0:
        return 1.0
    shape = 1.0 / (sd * sd)
    return rng.gammavariate(shape, 1.0 / shape)


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
    ap.add_argument("--error-sd", type=float, default=0.0,
                    help="relative spread of the per-read error rate (0 = every read identical, "
                         "which is unrealistic; ~0.5 is long-read-like)")
    ap.add_argument("--hp-bias", type=float, default=1.0,
                    help="multiply indel rates inside homopolymer runs (1 = off, ~5 is HiFi-like)")
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
            scale = read_error_scale(rng, a.error_sd)
            fo.write(f">S{i}_1!{name}!{start}!{start + L}!{strand}\n")
            fo.write(mutate(frag, a.sub * scale, a.ins * scale, a.dele * scale,
                            rng, a.hp_bias) + "\n")
            written += 1

    print(f"wrote {written} reads to {a.out} "
          f"(sub={a.sub} ins={a.ins} del={a.dele} error_sd={a.error_sd} "
          f"hp_bias={a.hp_bias} len={a.length} seed={a.seed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
