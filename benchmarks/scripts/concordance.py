#!/usr/bin/env python3
"""Score a shmap-rs PAF against a cached external mapper's PAF.

  concordance.py <subject.paf> <reference.paf> [--min-overlap 0.1] [--json]

Reports three numbers, which answer different questions and must not be
conflated:

  recall       of the reads the reference mapped, how many did we map at all?
  agreement    of the reads BOTH mapped, how many did we put in the same place?
  good         of the reads the reference mapped, how many did we map to the
               same place?  (= recall x agreement)

`good` is the headline: it is the fraction of the reference's mappings we
reproduce, and it is the number to drive up. Quoting `agreement` alone is
misleading, because a mapper that maps almost nothing can score ~1.0 on it.

CRITERION
---------
A mapping matches when it is on the same target sequence and

    intersection / (span of the two intervals' extremes) > min_overlap

This is paftools.js mapeval's documented rule ("the intersection between the
true and mapped reference intervals is at least 10% of their union"). Overlap
is used rather than a distance because the tools report intervals of genuinely
different widths — shmap-rs without refinement spans ~30% more than a read —
and an overlap criterion handles that by construction where a distance
threshold needs tuning per metric.

One deliberate deviation. mapquik's `experiments/intersect_pafs.py`, which
implements the same idea, computes the intersection incorrectly when one
interval is NESTED inside the other: for [0,100] against [10,50] it reports
0.500 where intersection/union is 0.400, because it takes `max2 - min1` rather
than clamping both ends. We do not reproduce that. It matters here more than it
would elsewhere, because shmap-rs's intervals are systematically wider than a
read, so the reference's interval sits nested inside ours often rather than
rarely — the exact case the formula inflates. At the default 0.1 threshold the
inflation seldom flips a verdict, but it biases the ratio in the direction that
flatters us, and it would flip verdicts at a stricter threshold.

THIS IS NOT ACCURACY. The reference is a mapper, not truth. Where the two
disagree, this says nothing about which is right. Accuracy comes from reads
that carry their true position (B02); see profiling/validate_paf.py --truth.
"""

from __future__ import annotations

import argparse
import json
import sys


def parse_paf(path: str, min_mapq: int = 0) -> dict[str, tuple[str, int, int]]:
    """read -> (target, start, end), one record per read, primary preferred.

    Minimap2-family mappers (including Winnowmap2) emit several records for one
    read: `tp:A:P` primary, `tp:A:S` secondary, and supplementary rows also
    tagged P. A secondary alignment is deliberately somewhere else, so counting
    it as a placement makes a correct mapper look wrong — Winnowmap2 on B02
    scores 96.57% when every record is counted and 99.65% counted per read,
    which is the difference between "worse than shmap-rs" and "better".

    Secondaries are therefore dropped outright rather than merely deduplicated,
    so the result does not depend on the mapper emitting primary first. shmap-rs
    writes one record per read and no tp: tag, and is unaffected.
    """
    out: dict[str, tuple[str, int, int]] = {}
    with open(path) as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 12:
                continue
            read = c[0]
            if read in out:
                continue
            if "tp:A:S" in c[12:]:
                continue
            try:
                if min_mapq and int(c[11]) < min_mapq:
                    continue
                out[read] = (c[5], int(c[7]), int(c[8]))
            except ValueError:
                continue
    return out


def overlaps(a: tuple[str, int, int], b: tuple[str, int, int], min_overlap: float) -> bool:
    (c1, s1, e1), (c2, s2, e2) = a, b
    if c1 != c2:
        return False
    lo1, hi1 = min(s1, e1), max(s1, e1)
    lo2, hi2 = min(s2, e2), max(s2, e2)
    inter = min(hi1, hi2) - max(lo1, lo2)
    if inter <= 0:
        return False
    span = max(hi1, hi2) - min(lo1, lo2)
    return span > 0 and inter / span > min_overlap


def score(subject: dict, reference: dict, min_overlap: float) -> dict:
    both = concordant = diff_chr = 0
    for read, rcoord in reference.items():
        scoord = subject.get(read)
        if scoord is None:
            continue
        both += 1
        if overlaps(scoord, rcoord, min_overlap):
            concordant += 1
        elif scoord[0] != rcoord[0]:
            diff_chr += 1
    nref = len(reference) or 1
    return dict(
        subject_mapped=len(subject), reference_mapped=len(reference),
        both_mapped=both, concordant=concordant,
        discordant_same_target=both - concordant - diff_chr,
        discordant_other_target=diff_chr,
        missed=len(reference) - both,
        recall=both / nref,
        agreement=concordant / both if both else 0.0,
        good=concordant / nref,
        min_overlap=min_overlap,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("subject", help="shmap-rs PAF")
    ap.add_argument("reference", help="cached external mapper PAF")
    ap.add_argument("--min-overlap", type=float, default=0.1)
    ap.add_argument("--min-mapq", type=int, default=0,
                    help="ignore reference mappings below this mapq (paftools mapeval uses 10)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    subj = parse_paf(a.subject)
    ref = parse_paf(a.reference, a.min_mapq)
    if not ref:
        print(f"reference PAF has no usable records: {a.reference}", file=sys.stderr)
        return 2
    r = score(subj, ref, a.min_overlap)

    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    print(f"reference mapped   {r['reference_mapped']}")
    print(f"subject mapped     {r['subject_mapped']}")
    print(f"both mapped        {r['both_mapped']}")
    print(f"  concordant       {r['concordant']}")
    print(f"  discordant same  {r['discordant_same_target']}")
    print(f"  discordant other {r['discordant_other_target']}")
    print(f"not mapped by us   {r['missed']}")
    print()
    print(f"recall     {r['recall']:.4f}   of the reference's mappings, we mapped at all")
    print(f"agreement  {r['agreement']:.4f}   of shared reads, same place")
    print(f"good       {r['good']:.4f}   of the reference's mappings, reproduced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
