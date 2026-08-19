#!/usr/bin/env python3
"""Is the short-read PAF defect ours, or inherited from the C++ original?

  probe_short_read_defect.py --reference chrY.fa --rust <bin> [--cpp <bin>]

B06 made `validate_paf` fail for the first time in this suite's history: 172
of 41.8 M records had `qend <= qstart`. Every one came from a read of length
25 or 26 with k = 25.

The arithmetic is not in doubt. At the scoring call site the query size passed
down is `read_length - k`, and the reported query end is that minus one, so

    qend = read_length - k - 1

which is -1 at read_length == k, 0 at k+1, and only positive from k+2 up. PAF
requires `0 <= qstart < qend <= qlen`, so every read of length <= k+1 produces
an invalid record. No previous benchmark could reach it: the shortest read in
the suite before B06 was 12.8 kb.

WHAT THIS SCRIPT DECIDES is provenance, which the arithmetic cannot. shmap-rs
is a port whose value is being a faithful reference for the C++ original, so
"the C++ does this too" and "we introduced it" are very different findings and
belong in different chapters. It feeds both binaries the same reads at the
same parameters and compares the query intervals they report.

Reads are exact substrings of the reference at lengths straddling k, so every
one of them has a correct answer and any difference is the implementations'.

WHAT IT FOUND (chrY, k=25, r=1.0, 200 reads per length, a2)
-----------------------------------------------------------
    read len   sketch m   cpp-shmap        shmap-rs
      24        0         0 records        0 records
      25        1         SIGSEGV          200 records, all `0..-1`  INVALID
      26        2         SIGSEGV          200 records, all `0..0`   INVALID
      27        3         SIGSEGV          200 records, all valid
      28        4         SIGSEGV          200 records, all valid
      29        5         200 records      200 records, all valid
      30+       6+        200 records      200 records, all valid

Two different defects, and the port's is the milder one:

  The C++ SEGFAULTS for any read whose sketch holds fewer than 5 k-mers. 5 is
  MIN_HALFLEN, the bucket-geometry floor, which the algorithm documentation
  describes as rejecting such reads as unmappable "rather than creating
  degenerate buckets". It does not reject them; it dies on them.

  shmap-rs never crashes, and is correct from m=3 upwards. At m=1 and m=2 it
  emits a record whose query interval is empty or inverted, because the
  reported end is `read_length - k - 1`.

So the port already fixes a crash and has a residual coordinate bug in a
narrower range. Neither was reachable by any benchmark before B06: the
shortest read in the suite was 12.8 kb, and 25 bp reads only turn up in real
adapter-trimmed Illumina data.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def make_reads(reference: Path, out: Path, length: int, per: int) -> int:
    """`per` exact substrings of the reference, all of exactly `length` bases."""
    seq = []
    with open(reference) as f:
        for line in f:
            if not line.startswith(">"):
                seq.append(line.strip())
    s = "".join(seq).upper()
    n = 0
    i = 0
    step = max(1, len(s) // (per * 4))
    with open(out, "w") as fo:
        while n < per and i < len(s) - length:
            r = s[i:i + length]
            i += step
            if "N" in r:
                continue
            fo.write(f">len{length}_{n}\n{r}\n")
            n += 1
    return n


def run(binary: Path, reads: Path, reference: Path, k: int, out: Path,
        extra: list[str]) -> int:
    cmd = [str(binary), "-p", str(reads), "-s", str(reference),
           "-k", str(k), "-r", "1.0", "-t", "0.4", "-d", "0.075",
           "-o", "0.3", "-m", "Containment", *extra]
    with open(out, "w") as fo:
        return subprocess.run(cmd, stdout=fo, stderr=subprocess.DEVNULL).returncode


def summarise(paf: Path) -> tuple[int, int, str]:
    """(records, malformed, first malformed interval) for one PAF."""
    records = bad = 0
    example = ""
    if not paf.exists():
        return 0, 0, ""
    with open(paf) as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 12:
                continue
            records += 1
            try:
                qlen, qs, qe = int(c[1]), int(c[2]), int(c[3])
            except ValueError:
                continue
            if not (0 <= qs < qe <= qlen):
                bad += 1
                if not example:
                    example = f"{qs}..{qe}"
    return records, bad, example


def verdict(rc: int, records: int, bad: int, example: str) -> str:
    # 139 from a shell, -11 from subprocess: both are SIGSEGV.
    if rc in (139, -11):
        return "SIGSEGV"
    if rc != 0:
        return f"exit {rc}"
    if records == 0:
        return "0 records"
    if bad:
        return f"{bad}/{records} INVALID {example}"
    return f"{records} ok"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--rust", required=True)
    ap.add_argument("--cpp")
    ap.add_argument("--k", type=int, default=25)
    ap.add_argument("--per", type=int, default=200)
    ap.add_argument("--workdir", default="short-read-defect")
    a = ap.parse_args()

    ref = Path(a.reference).expanduser()
    work = Path(a.workdir).expanduser()
    work.mkdir(parents=True, exist_ok=True)
    # Spans the whole boundary: m = 0 through m = 6, where m = len - k + 1 at
    # r = 1.0. Both defects live inside it and both edges are covered, so a
    # rerun reproduces the table in the docstring rather than a subset of it.
    lengths = [a.k - 1 + d for d in range(0, 8)]

    impls = [("shmap-rs", Path(a.rust).expanduser(), ["-@", "8"])]
    if a.cpp:
        # The C++ has no threading flag; passing one is an error there.
        impls.append(("cpp-shmap", Path(a.cpp).expanduser(), []))
    impls = [(n, b, e) for n, b, e in impls if b.exists()]
    for n, b, _ in impls:
        print(f"{n:10} {b}")
    if not impls:
        sys.exit("no binaries found")

    print(f"\n{a.per} exact reads per length from {ref.name}, k={a.k}, r=1.0\n")
    print(f"{'read len':>8} {'sketch m':>9}  " + "  ".join(f"{n:>26}" for n, _, _ in impls))

    # One length per invocation, deliberately. The C++ segfaults on part of
    # this range, and a single run over all lengths would lose every other
    # length's answer with it — which is exactly what the first version of this
    # script did, and it reported "no records" for lengths that are in fact
    # fine.
    for L in lengths:
        reads = work / f"len{L}.fa"
        n = make_reads(ref, reads, L, a.per)
        cells = []
        for name, binary, extra in impls:
            paf = work / f"{name}_len{L}.paf"
            rc = run(binary, reads, ref, a.k, paf, extra)
            cells.append(f"{verdict(rc, *summarise(paf)):>26}")
        m = max(0, L - a.k + 1)
        mark = "  <- below MIN_HALFLEN" if 0 < m < 5 else ""
        print(f"{L:>8} {m:>9}  " + "  ".join(cells) + mark)

    print(f"\n{a.per} reads offered per length; 'm' is the sketch size at r=1.0, "
          f"i.e. len - k + 1.\nMIN_HALFLEN is 5: a sketch below it cannot form a "
          f"non-degenerate bucket.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
