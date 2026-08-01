#!/usr/bin/env python3
"""Selective sketch density: raise `-r` only inside repeat regions.

  selective_density.py regions <pass1.paf> <out.bed> [--pad 30000]
  selective_density.py mini-ref <ref.fa> <regions.bed> <out.fa>
  selective_density.py select  <pass1.paf> <reads.fa> <out.fa>
  selective_density.py merge   <pass1.paf> <pass2.paf> <out.paf> [--min-mapq 0]
  selective_density.py score   <paf> [<paf> ...]

RESULTS.md section 8 established that placement errors are an *information*
problem: three scoring changes were tried and all made accuracy worse, while
`-r 0.10` recovered most of the gap -- at 8.7x wall and 7x memory, to repair
errors concentrated in the 6.3% of the genome that is satellite. Section 11
asked whether density could be raised only inside those regions instead.

This drives that experiment end to end using the *stock binary*, so the answer
is known before anything is built into the mapper:

  1. `regions`  - where to densify, from the first pass's own mapq. No ground
                  truth and no external annotation, so it runs on real reads.
  2. `mini-ref` - cut those regions out as `<chrom>:<start>` records. Mapping
                  against this file is the same thing a second, dense index
                  restricted to repeat regions would do.
  3. `select`   - the reads the first pass could not place confidently.
  4. `merge`    - translate back to genome coordinates and substitute.
  5. `score`    - correctness against the ground truth in the read headers.

The meryl repetitive-k-mer set that section 11 suggested for step 1 was tried
first and does not work as a region mask: at k=15 it marks 17% of chr1 at a
30%-per-kilobase threshold and 50% once padded and merged, which is most of the
genome rather than the satellite fraction. It was built to *downweight*
minimizers for Winnowmap2, not to delimit regions, and suite.toml already notes
that k=15 is the pathologically repetitive regime genome-wide.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import re
import sys

TRUTH_RE = re.compile(r"^S\d+_\d+!([^!]+)!(\d+)!(\d+)!([+-])")


def paf_rows(path):
    with open(path) as fh:
        for line in fh:
            if line.strip():
                yield line.rstrip("\n").split("\t")


def mapq(f) -> int:
    return int(f[11]) if len(f) > 11 and f[11].isdigit() else 0


def placed(f) -> bool:
    return f[4] != "*"


def correct(f) -> bool:
    """Placed overlapping the true interval the read header records."""
    m = TRUTH_RE.match(f[0])
    if not m or not placed(f):
        return False
    return (f[5] == m.group(1)
            and int(f[7]) < int(m.group(3))
            and int(f[8]) > int(m.group(2)))


# --------------------------------------------------------------------------


def cmd_regions(a) -> int:
    iv = collections.defaultdict(list)
    seg_len: dict[str, int] = {}
    n_amb = 0
    for f in paf_rows(a.paf):
        if not placed(f):
            continue
        seg_len[f[5]] = int(f[6])
        if mapq(f) >= 60:
            continue
        n_amb += 1
        iv[f[5]].append((int(f[7]), int(f[8])))

    total = 0
    with open(a.out, "w") as fo:
        for chrom, spans in sorted(iv.items()):
            spans.sort()
            merged: list[list[int]] = []
            for s, e in spans:
                s = max(0, s - a.pad)
                e = min(seg_len.get(chrom, e + a.pad), e + a.pad)
                if merged and s <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], e)
                else:
                    merged.append([s, e])
            for s, e in merged:
                fo.write(f"{chrom}\t{s}\t{e}\n")
                total += e - s
    print(f"{n_amb} ambiguous reads -> {total/1e6:.1f} Mbp of dense regions",
          file=sys.stderr)
    return 0


def segments(path: str):
    name, chunks = None, []
    with open(path, "rb") as fh:
        for line in fh:
            if line.startswith(b">"):
                if name is not None:
                    yield name, b"".join(chunks)
                name = line[1:].split()[0].decode()
                chunks = []
            else:
                chunks.append(line.strip())
    if name is not None:
        yield name, b"".join(chunks)


def cmd_mini_ref(a) -> int:
    regions = collections.defaultdict(list)
    with open(a.regions) as fh:
        for line in fh:
            c, s, e = line.split()[:3]
            regions[c].append((int(s), int(e)))

    n = bp = 0
    with open(a.out, "w") as fo:
        for name, seq in segments(a.reference):
            for s, e in regions.get(name, []):
                sub = seq[s:e]
                if not sub:
                    continue
                # One line per record: several tools in this repo's comparison
                # set count newlines as bases on a wrapped FASTA.
                fo.write(f">{name}:{s}\n{sub.decode()}\n")
                n += 1
                bp += len(sub)
    print(f"{n} regions, {bp/1e6:.1f} Mbp", file=sys.stderr)
    return 0


def cmd_select(a) -> int:
    seen, confident = set(), set()
    for f in paf_rows(a.paf):
        seen.add(f[0])
        if mapq(f) >= 60:
            confident.add(f[0])
    want = seen - confident

    n = kept = 0
    keep = False
    with open(a.reads) as fh, open(a.out, "w") as fo:
        for line in fh:
            if line.startswith(">"):
                name = line[1:].split()[0]
                n += 1
                # Absent from the PAF means unmapped, which is also a candidate.
                keep = name in want or name not in seen
                kept += keep
            if keep:
                fo.write(line)
    print(f"selected {kept} of {n} reads ({kept/max(1,n)*100:.2f}%)", file=sys.stderr)
    return 0


def cmd_merge(a) -> int:
    seg_len = {f[5]: f[6] for f in paf_rows(a.pass1) if placed(f)}

    replacement = {}
    for f in paf_rows(a.pass2):
        if not placed(f) or mapq(f) < a.min_mapq or ":" not in f[5]:
            continue
        chrom, off = f[5].rsplit(":", 1)
        if chrom not in seg_len:
            continue
        off = int(off)
        g = list(f)
        g[5], g[6] = chrom, seg_len[chrom]
        g[7], g[8] = str(int(f[7]) + off), str(int(f[8]) + off)
        replacement[f[0]] = g

    n = swapped = 0
    with open(a.out, "w") as fo:
        for f in paf_rows(a.pass1):
            n += 1
            g = replacement.get(f[0])
            if g is not None:
                swapped += 1
                # Pass 1's trailing stat tags are kept: they describe pass 1's
                # sketch, and pass 2's would silently mix two sketch sizes into
                # one column.
                fo.write("\t".join(g[:12] + f[12:]) + "\n")
            else:
                fo.write("\t".join(f) + "\n")
    print(f"merged: {swapped} of {n} records replaced", file=sys.stderr)
    return 0


def cmd_score(a) -> int:
    print(f"{'paf':<28}{'mapped':>9}{'correct':>9}{'of total':>10}{'mapq60':>9}")
    for path in a.pafs:
        rows = list(paf_rows(path))
        tot = len(rows)
        print(f"{path.split('/')[-1]:<28}{sum(placed(f) for f in rows):>9}"
              f"{sum(correct(f) for f in rows):>9}"
              f"{sum(correct(f) for f in rows)/max(1,tot)*100:>9.3f}%"
              f"{sum(mapq(f) >= 60 for f in rows):>9}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("regions"); p.add_argument("paf"); p.add_argument("out")
    p.add_argument("--pad", type=int, default=30000); p.set_defaults(fn=cmd_regions)

    p = sub.add_parser("mini-ref"); p.add_argument("reference")
    p.add_argument("regions"); p.add_argument("out"); p.set_defaults(fn=cmd_mini_ref)

    p = sub.add_parser("select"); p.add_argument("paf"); p.add_argument("reads")
    p.add_argument("out"); p.set_defaults(fn=cmd_select)

    p = sub.add_parser("merge"); p.add_argument("pass1"); p.add_argument("pass2")
    p.add_argument("out"); p.add_argument("--min-mapq", type=int, default=0)
    p.set_defaults(fn=cmd_merge)

    p = sub.add_parser("score"); p.add_argument("pafs", nargs="+")
    p.set_defaults(fn=cmd_score)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
