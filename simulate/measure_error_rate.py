#!/usr/bin/env python3
"""Measure the actual error rate of a ground-truth-encoded read set.

  measure_error_rate.py --ref hs1.fa --reads reads.fa [--sample 300] [--k 25]

Reads carrying `!chr!start!end!strand` headers can be compared against the exact
reference interval they came from, so their error rate is measurable rather than
taken on trust. This exists because the documented rate for a dataset and its
real rate are different things, and every accuracy number here depends on the
real one.

Two estimates are reported because they fail differently:

  length delta   (len(read) - len(ref interval)) / len, the net indel rate. Signed:
                 positive means insertions dominate. Blind to substitutions, and
                 blind to insertions and deletions that cancel out.

  k-mer survival fraction of the read's k-mers present in the reference interval.
                 Under substitutions only, survival ~ (1-e)^k, which inverts to an
                 error rate. Indels break k-mers the same way, so the inverted
                 figure is a TOTAL error rate, not a substitution rate — the two
                 columns together separate them.

The k-mer estimate saturates: below ~0.1% error nearly everything survives, and
above ~5% almost nothing does, so treat it as a range rather than a measurement
outside roughly 0.2%-4%.
"""

from __future__ import annotations

import argparse
import random
import re
import sys

COMP = str.maketrans("ACGTNacgtn", "TGCANtgcan")
HDR = re.compile(r"^S\d+_\d+!([^!]+)!(\d+)!(\d+)!([+-])")


def read_fasta(path):
    name, parts = None, []
    with open(path) as f:
        for line in f:
            if line.startswith(">"):
                if name is not None:
                    yield name, "".join(parts)
                name, parts = line[1:].split()[0], []
            else:
                parts.append(line.strip())
    if name is not None:
        yield name, "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--reads", required=True)
    ap.add_argument("--sample", type=int, default=300)
    ap.add_argument("--k", type=int, default=25)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    picked = []
    for name, seq in read_fasta(a.reads):
        m = HDR.match(name)
        if not m:
            continue
        if len(picked) < a.sample:
            picked.append((m, seq))
        else:
            j = rng.randrange(len(picked) + 1)      # reservoir, so it is not just the head
            if j < a.sample:
                picked[j] = (m, seq)
    if not picked:
        sys.exit("no ground-truth-encoded reads found")

    want = {m.group(1) for m, _ in picked}
    ref = {n: s for n, s in read_fasta(a.ref) if n in want}

    dl, surv, n = [], [], 0
    for m, seq in picked:
        chrom, start, end, strand = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        s = ref.get(chrom)
        if s is None or end > len(s):
            continue
        frag = s[start:end]
        if strand == "-":
            frag = frag.translate(COMP)[::-1]
        if not frag:
            continue
        n += 1
        dl.append((len(seq) - len(frag)) / len(frag))
        rk = {frag[i:i + a.k].upper() for i in range(len(frag) - a.k + 1)}
        qk = [seq[i:i + a.k].upper() for i in range(len(seq) - a.k + 1)]
        if qk:
            surv.append(sum(1 for x in qk if x in rk) / len(qk))

    if not n:
        sys.exit("no read could be matched to its reference interval")

    mean_dl = sum(dl) / len(dl)
    mean_s = sum(surv) / len(surv)
    # survival = (1-e)^k  ->  e = 1 - survival^(1/k)
    est = 1 - mean_s ** (1 / a.k) if mean_s > 0 else float("nan")

    print(f"reads compared            {n}")
    print(f"mean length delta         {mean_dl*100:+.4f}%   (net indel rate; + = insertions)")
    print(f"mean {a.k}-mer survival      {mean_s*100:.2f}%")
    print(f"implied total error rate  {est*100:.3f}% per base")
    print()
    if abs(mean_dl) < 1e-4:
        print("Length is preserved to within 0.01%, so errors are substitutions")
        print("(or exactly balanced indels, which no simulator produces by accident).")
    else:
        print(f"Length is not preserved, so indels are present at ~{abs(mean_dl)*100:.3f}% net.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
