#!/usr/bin/env python3
"""Find the hashratio a short read needs, by mapping reads of known origin.

  probe_short_read_params.py --reference chrY.fa --binary target/release/shmap

Adding short-read benchmarks raised a question the suite could not answer from
its existing numbers: shmap-rs is a long-read mapper whose sketch size is
LENGTH-PROPORTIONAL, `m ~ r*|read|`, so shrinking the read shrinks the evidence.
At the paper's `r = 0.01` a 150 bp read carries 126 k-mers and keeps ~1.3 of
them. Mapping decisions made from one k-mer are not mapping decisions.

This measures where that breaks rather than arguing about it. Reads are exact
substrings of the reference, so every one of them HAS a correct answer and the
tool is given the easiest possible version of the problem: no sequencing error,
no variants, no adapters. Whatever it cannot do here it cannot do on real data
either, and the failure is attributable to the parameters alone.

The name of each read carries its true offset, so accuracy is checked and not
merely the mapped count -- which is the whole point. At low `r` almost every
read still produces a mapping; those mappings are just placed by one or two
k-mers, and a k-mer that occurs in a thousand places puts the read in one of
them at random. A mapped-rate table would call that a success.

Reported per hashratio:

  records     reads that produced a PAF line at all. A read whose sketch comes
              out EMPTY is skipped silently, and at r=0.01 that is 0.99^126 =
              28% of 150 bp reads before mapping is even attempted.
  placed      reads given a target interval
  correct     placed within one read length of where it came from
  m           mean sketch size actually used, from the PAF's own p:i: tag
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_RATIOS = (0.01, 0.04, 0.1, 0.25, 0.5, 1.0)


def make_reads(reference: Path, out: Path, read_len: int, want: int) -> int:
    """Exact substrings of the reference, named by their true offset."""
    seq = []
    with open(reference) as f:
        for line in f:
            if not line.startswith(">"):
                seq.append(line.strip())
    s = "".join(seq).upper()
    step = max(1, (len(s) - read_len) // (want * 2))
    n = 0
    with open(out, "w") as fo:
        for i in range(0, len(s) - read_len, step):
            r = s[i:i + read_len]
            # An N-containing read has no well-defined k-mers and would
            # measure the reference's gaps rather than the mapper.
            if "N" in r:
                continue
            fo.write(f">probe{n}_{i}\n{r}\n")
            n += 1
            if n >= want:
                break
    return n


def score(paf: Path, read_len: int, total: int) -> tuple[int, int, int, float]:
    records = placed = correct = 0
    ms: list[int] = []
    with open(paf) as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 12:
                continue
            records += 1
            if c[5] == "*":
                continue
            placed += 1
            try:
                true = int(c[0].split("_")[1])
                ts, te = int(c[7]), int(c[8])
            except (IndexError, ValueError):
                continue
            # The mapeval convention this repo already uses: within one read
            # length of the true position.
            if ts - read_len <= true <= te + read_len:
                correct += 1
            for tag in c[12:]:
                if tag.startswith("p:i:"):
                    ms.append(int(tag[4:]))
                    break
    return records, placed, correct, (sum(ms) / len(ms) if ms else 0.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--binary", required=True)
    ap.add_argument("--workdir", default="short-read-probe")
    ap.add_argument("--read-len", type=int, default=150)
    ap.add_argument("--reads", type=int, default=20000)
    ap.add_argument("--k", type=int, default=25)
    ap.add_argument("--threshold", type=float, default=0.4)
    ap.add_argument("--min-diff", type=float, default=0.075)
    ap.add_argument("--max-overlap", type=float, default=0.3)
    ap.add_argument("--metric", default="Containment")
    ap.add_argument("--threads", type=int, default=16)
    ap.add_argument("--ratios", default=",".join(str(r) for r in DEFAULT_RATIOS))
    a = ap.parse_args()

    ref, binary = Path(a.reference).expanduser(), Path(a.binary).expanduser()
    for p in (ref, binary):
        if not p.exists():
            sys.exit(f"not found: {p}")
    work = Path(a.workdir).expanduser()
    work.mkdir(parents=True, exist_ok=True)

    reads = work / f"probe{a.read_len}.fa"
    n = make_reads(ref, reads, a.read_len, a.reads)
    print(f"{n} exact {a.read_len} bp reads from {ref.name}, k={a.k}, "
          f"threshold={a.threshold}, metric={a.metric}\n")

    kmers = a.read_len - a.k + 1
    print(f"{'r':>6} {'exp m':>6} {'records':>8} {'placed':>8} {'correct':>8} "
          f"{'% of all':>9} {'% of placed':>12} {'mean m':>7}")
    rows = []
    for r in [float(x) for x in a.ratios.split(",")]:
        paf = work / f"r{r}.paf"
        cmd = [str(binary), "-p", str(reads), "-s", str(ref), "-k", str(a.k),
               "-r", str(r), "-t", str(a.threshold), "-d", str(a.min_diff),
               "-o", str(a.max_overlap), "-m", a.metric, "-@", str(a.threads)]
        with open(paf, "w") as fo:
            rc = subprocess.run(cmd, stdout=fo, stderr=subprocess.DEVNULL).returncode
        if rc != 0:
            print(f"{r:>6} FAILED (rc={rc})")
            continue
        records, placed, correct, mean_m = score(paf, a.read_len, n)
        print(f"{r:>6} {r * kmers:>6.1f} {records:>8} {placed:>8} {correct:>8} "
              f"{100 * correct / n:>8.2f}% {100 * correct / placed if placed else 0:>11.2f}% "
              f"{mean_m:>7.1f}")
        rows.append((r, records, placed, correct, mean_m))

    print(f"\n{n} reads offered. 'records' below that means reads whose sketch "
          f"came out empty and\nwhich were never attempted: at r, that is "
          f"(1-r)^{kmers} of them.")
    if shutil.which("column"):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
