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


class _PafMatch:
    """Adapter so a PAF row can stand in for a ground-truth header match."""

    def __init__(self, t):
        self._t = t

    def group(self, i):
        return (None, self._t[0], str(self._t[1]), str(self._t[2]), self._t[3])[i]


HDR = re.compile(r"^S\d+_\d+!([^!]+)!(\d+)!(\d+)!([+-])")


def read_fasta(path):
    name, parts = None, []
    with open(path) as f:
        for line in f:
            if line.startswith(">"):
                if name is not None:
                    yield name, "".join(parts)
                # A header can be bare ">" or ">  " in the wild; splitting it
                # blind raises IndexError partway through a 2 GB file.
                fields = line[1:].split()
                name, parts = (fields[0] if fields else ""), []
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
    ap.add_argument("--from-paf",
                    help="reads have no truth header: take each read's own confident mapping "
                         "(mapq>=60) from this PAF as approximate truth. Biased — it can only "
                         "see reads good enough to map confidently — so it UNDERSTATES the "
                         "dataset's error rate. Use it as an operating point, not a measurement.")
    a = ap.parse_args()

    rng = random.Random(a.seed)

    # With --from-paf, the "truth" is the read's own confident placement.
    paf_truth = {}
    if a.from_paf:
        for line in open(a.from_paf):
            c = line.rstrip("\n").split("\t")
            if len(c) < 12 or "tp:A:S" in c[12:]:
                continue
            try:
                if int(c[11]) < 60:
                    continue
                paf_truth[c[0]] = (c[5], int(c[7]), int(c[8]), c[4])
            except ValueError:
                pass
        if not paf_truth:
            sys.exit("no mapq-60 records in " + a.from_paf)

    picked = []
    for name, seq in read_fasta(a.reads):
        if a.from_paf:
            t = paf_truth.get(name)
            if t is None:
                continue
            m = _PafMatch(t)
        else:
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
    if a.from_paf:
        # The "truth" interval is shmap-rs's own reported window, which is sized
        # by the read's k-mer count rather than by an alignment. Read length
        # minus that window measures the mapper's windowing, not the read's
        # indel content, so the column is suppressed rather than misread.
        print(f"mean length delta         n/a (--from-paf: the interval is a mapping "
              f"window, not an alignment)")
    else:
        print(f"mean length delta         {mean_dl*100:+.4f}%   (net indel rate; + = insertions)")
    print(f"mean {a.k}-mer survival      {mean_s*100:.2f}%")
    print(f"implied total error rate  {est*100:.3f}% per base")
    print()
    if a.from_paf:
        print("Estimated from confidently-mapped reads only, so it UNDERSTATES the dataset's")
        print("error rate: reads too damaged to reach mapq 60 are invisible to it.")
    elif abs(mean_dl) < 1e-4:
        print("Length is preserved to within 0.01%, so errors are substitutions")
        print("(or exactly balanced indels, which no simulator produces by accident).")
    else:
        print(f"Length is not preserved, so indels are present at ~{abs(mean_dl)*100:.3f}% net.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
