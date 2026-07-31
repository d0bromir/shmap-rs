#!/usr/bin/env python3
"""Adjudicate shmap-rs vs a reference mapper where ground truth exists.

  adjudicate_disagreements.py <shmap.paf> <reference.paf> [--truth] [--dump FILE]

On a real read set, a disagreement tells you nothing about who is wrong. On a
SIMULATED set the read header carries the true position, so every disagreement
can be scored:

    S1_1!chr10!6713774!6737773!-
         ^target ^start   ^end

Use this before trying to "fix" disagreements: a chunk of them are usually the
reference mapper being wrong, and chasing those makes shmap-rs worse.

Without --truth it still splits disagreements by target and reports the
chromosome pairs involved, which is what is available on a real read set.
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "benchmarks"))
from concordance import overlaps, parse_paf  # noqa: E402

TRUTH_RE = re.compile(r"^S\d+_\d+!([^!]+)!(\d+)!(\d+)!([+-])")


def truth_of(read: str):
    m = TRUTH_RE.match(read)
    return (m.group(1), int(m.group(2)), int(m.group(3))) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("subject")
    ap.add_argument("reference")
    ap.add_argument("--min-overlap", type=float, default=0.1)
    ap.add_argument("--truth", action="store_true", help="read names carry true positions")
    ap.add_argument("--dump", help="write the disagreeing read names here")
    a = ap.parse_args()

    subj, ref = parse_paf(a.subject), parse_paf(a.reference)
    T = a.min_overlap

    same_target, other_target, dump = [], [], []
    for read, r in ref.items():
        s = subj.get(read)
        if s is None or overlaps(s, r, T):
            continue
        (other_target if s[0] != r[0] else same_target).append((read, s, r))
        dump.append(read)

    print(f"reference mappings   {len(ref)}")
    print(f"disagreements        {len(same_target) + len(other_target)}")
    print(f"  same target        {len(same_target)}")
    print(f"  different target   {len(other_target)}")

    if a.dump:
        Path(a.dump).write_text("\n".join(dump) + "\n")
        print(f"wrote {len(dump)} read names to {a.dump}")

    if not a.truth:
        pairs = collections.Counter((r[0], s[0]) for _, s, r in other_target)
        print("\ntop reference->shmap-rs target pairs (no ground truth available):")
        for (rt, st), n in pairs.most_common(15):
            print(f"  {n:6}  {rt} -> {st}")
        return 0

    # --- with ground truth, score every disagreement -----------------------
    verdict = collections.Counter()
    wrong_pairs = collections.Counter()
    examples: dict[str, list] = collections.defaultdict(list)

    for read, s, r in other_target + same_target:
        t = truth_of(read)
        if t is None:
            verdict["unparseable header"] += 1
            continue
        s_ok = overlaps(s, t, T)
        r_ok = overlaps(r, t, T)
        cat = ("both right (disagree but both overlap truth)" if s_ok and r_ok else
               "shmap-rs RIGHT, reference wrong" if s_ok else
               "shmap-rs WRONG, reference right" if r_ok else
               "both wrong")
        verdict[cat] += 1
        if not s_ok:
            wrong_pairs[(t[0], s[0])] += 1
            if len(examples[cat]) < 5:
                examples[cat].append((read, t, s, r))

    total = sum(verdict.values())
    print(f"\nadjudicated against ground truth ({total} disagreements):")
    for cat, n in verdict.most_common():
        print(f"  {n:6}  {n/total*100:5.1f}%  {cat}")

    if wrong_pairs:
        print("\nwhere shmap-rs actually goes wrong — true target -> reported target:")
        for (tt, st), n in wrong_pairs.most_common(15):
            tag = "  (same target, wrong position)" if tt == st else ""
            print(f"  {n:6}  {tt} -> {st}{tag}")

    for cat in ("shmap-rs WRONG, reference right", "both wrong"):
        if examples[cat]:
            print(f"\nexamples — {cat}:")
            for read, t, s, r in examples[cat]:
                print(f"  {read}")
                print(f"    truth      {t[0]}:{t[1]}-{t[2]}")
                print(f"    shmap-rs   {s[0]}:{s[1]}-{s[2]}")
                print(f"    reference  {r[0]}:{r[1]}-{r[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
