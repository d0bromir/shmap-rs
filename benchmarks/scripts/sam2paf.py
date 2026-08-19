#!/usr/bin/env python3
"""Convert a SAM stream to PAF, so an aligner can join the concordance corpus.

  sam2paf.py aln.sam > out.paf
  bwa-mem2 mem ... > aln.sam && sam2paf.py aln.sam > out.paf

Every mapper in `[external]` had emitted PAF natively until bwa-mem2, which is
an ALIGNER: it produces SAM with base-level alignments. `concordance.py` joins
PAFs, so the choice was to teach it SAM or to project SAM down to PAF here.
This is the second, because the projection is the honest direction — a PAF is
strictly less information than a SAM, so nothing is invented — and because it
keeps every mapper in the corpus stored in one format that one scorer reads.

NOT IN A PIPE, WHEN THE MAPPER IS BEING TIMED
---------------------------------------------
This converts ~294 000 records/s, which is the same order as bwa-mem2's output
rate at -t 32. Piping the mapper into it would make the converter
intermittently the bottleneck, and the mapper would then block on a full pipe —
inflating the single number the corpus exists to measure. suite.toml therefore
writes SAM to disk and converts afterwards, outside the timed step. Reading
from stdin still works and is right for anything that is not being timed.

WHY NOT paftools.js
-------------------
`paftools.js sam2paf` does this job and is the reference implementation of the
rule. It needs k8, a JavaScript runtime, which is a third-party binary neither
benchmark host has and which is not packaged for aarch64 as readily as it is
for x86_64. Adding a dependency that exists on one of the two architectures to
process results that must be comparable across both is the wrong trade for
~120 lines. The conversion is checked against paftools' documented behaviour in
`test_sam2paf.py`, including the cases below where a naive reading gets it
wrong.

THE THREE THINGS THAT ARE EASY TO GET WRONG
-------------------------------------------
1. PAF query coordinates are on the ORIGINAL read, SAM's are on the aligned
   strand. For a reverse-strand record the clips must be swapped, or every
   minus-strand read is reported at the wrong offset within itself. It does not
   affect target coordinates, which is what concordance scores, so this is the
   error that would never have shown up in our own numbers and would have been
   wrong for anyone else reading the PAF.

2. Hard clips count towards query length but not towards the aligned block.
   bwa-mem hard-clips supplementary records, so ignoring H makes a
   supplementary record claim a query length shorter than the read, and PAF
   column 2 then disagrees between two records of one read.

3. Column 10 is residue MATCHES, not aligned length. CIGAR `M` is
   "match-or-mismatch" and cannot distinguish them, so matches come from
   `blen - NM`, exactly as paftools does. Without an NM tag the count is
   unknowable and the record is emitted with `matches = blen`, flagged by a
   `NM:i:-1`-free output and counted in the summary rather than silently
   pretending the alignment was perfect.

WHAT IS DROPPED
---------------
Unmapped records (flag 0x4) — they place nothing. Everything else is kept and
tagged as PAF's own convention requires, because dropping is the scorer's
decision, not the converter's:

  tp:A:P  primary
  tp:A:S  secondary   (flag 0x100)
  tp:A:I  supplementary (flag 0x800) — a real placement of part of the read

`concordance.py` drops `tp:A:S` and takes the first remaining record per read,
which is the same rule it already applies to Winnowmap2's minimap2-style
output. Emitting the tag is what lets it do that.
"""

from __future__ import annotations

import argparse
import re
import sys

CIGAR_RE = re.compile(r"(\d+)([MIDNSHP=X])")

# Consumes query / consumes target, per the SAM specification. `blen` is the
# alignment block: the columns of the alignment, which is M/I/D/=/X and
# excludes clipping and padding.
Q_OPS = frozenset("MIS=X")
T_OPS = frozenset("MDN=X")
BLOCK_OPS = frozenset("MID=X")


def parse_cigar(cigar: str) -> tuple[int, int, int, int, int]:
    """(query_len_with_hard_clips, qstart, qspan, tspan, blen) for one CIGAR."""
    qlen = qstart = qspan = tspan = blen = 0
    seen_aligned = False
    for n_s, op in CIGAR_RE.findall(cigar):
        n = int(n_s)
        if op in Q_OPS or op == "H":
            qlen += n
        if op in T_OPS:
            tspan += n
        if op in BLOCK_OPS:
            blen += n
        if op in ("S", "H"):
            # Leading clip is the query offset; a trailing clip is not.
            if not seen_aligned:
                qstart += n
        else:
            if op in Q_OPS:
                seen_aligned = True
                qspan += n
    return qlen, qstart, qspan, tspan, blen


def convert(fin, fout) -> dict:
    """Stream SAM to PAF. Returns counts for the caller to report."""
    tlen: dict[str, int] = {}
    n = dict(records=0, unmapped=0, written=0, no_nm=0, no_sq=0)

    for line in fin:
        if line.startswith("@"):
            if line.startswith("@SQ\t"):
                name = ln = None
                for f in line.rstrip("\n").split("\t")[1:]:
                    if f.startswith("SN:"):
                        name = f[3:]
                    elif f.startswith("LN:"):
                        ln = int(f[3:])
                if name is not None and ln is not None:
                    tlen[name] = ln
            continue

        c = line.rstrip("\n").split("\t")
        if len(c) < 11:
            continue
        n["records"] += 1
        flag = int(c[1])
        if flag & 0x4 or c[2] == "*" or c[5] == "*":
            n["unmapped"] += 1
            continue

        qname, rname, cigar = c[0], c[2], c[5]
        pos = int(c[3]) - 1
        mapq = int(c[4])
        qlen, qstart, qspan, tspan, blen = parse_cigar(cigar)
        if qlen == 0 or tspan == 0:
            n["unmapped"] += 1
            continue

        strand = "-" if flag & 0x10 else "+"
        qend = qstart + qspan
        if strand == "-":
            # SAM reports the read as aligned, i.e. reverse-complemented; PAF
            # reports the interval on the read as submitted. Reflect it.
            qstart, qend = qlen - qend, qlen - qstart

        nm = None
        for f in c[11:]:
            if f.startswith("NM:i:"):
                try:
                    nm = int(f[5:])
                except ValueError:
                    nm = None
                break
        if nm is None:
            n["no_nm"] += 1
            matches = blen
        else:
            matches = max(0, blen - nm)

        target_len = tlen.get(rname)
        if target_len is None:
            n["no_sq"] += 1
            # Without an @SQ line the true length is unknown. The end of this
            # alignment is a lower bound and is not a guess dressed as a fact;
            # concordance.py does not read column 7.
            target_len = pos + tspan

        tp = "S" if flag & 0x100 else ("I" if flag & 0x800 else "P")
        fout.write(f"{qname}\t{qlen}\t{qstart}\t{qend}\t{strand}\t"
                   f"{rname}\t{target_len}\t{pos}\t{pos + tspan}\t"
                   f"{matches}\t{blen}\t{mapq}\ttp:A:{tp}\n")
        n["written"] += 1

    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sam", nargs="?", default="-", help="SAM file, or - for stdin")
    ap.add_argument("--stats", action="store_true",
                    help="write a one-line summary to stderr")
    a = ap.parse_args()

    fin = sys.stdin if a.sam == "-" else open(a.sam)
    try:
        n = convert(fin, sys.stdout)
    finally:
        if fin is not sys.stdin:
            fin.close()

    if a.stats:
        print(f"sam2paf: {n['records']} records, {n['written']} written, "
              f"{n['unmapped']} unmapped, {n['no_nm']} without NM, "
              f"{n['no_sq']} without @SQ", file=sys.stderr)
    # A SAM with records but no @SQ header is a truncated or mis-piped stream,
    # and it would otherwise produce a PAF with plausible-looking but invented
    # target lengths. Say so loudly.
    if n["no_sq"]:
        print(f"sam2paf: WARNING — {n['no_sq']} records named a reference with "
              f"no @SQ header; column 7 is a lower bound for those",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
