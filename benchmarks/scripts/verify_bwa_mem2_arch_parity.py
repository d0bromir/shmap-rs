#!/usr/bin/env python3
"""Order-independent signature of a PAF, for comparing two hosts' corpora.

  verify_bwa_mem2_arch_parity.py <file.paf> [...]

Run it on each host and compare the signatures. Identical signature means the
two files contain exactly the same multiset of records.

WHY THIS EXISTS
---------------
bwa-mem2's x86_64 and aarch64 builds are not the same artefact. bioconda builds
x86_64 with upstream's five SIMD variants and a dispatcher (a2 selects
avx512bw), and aarch64 by grafting in sse2neon and compiling for baseline
ARMv8. sse2neon is intended to be semantics-preserving, so the two corpora
should hold identical alignments and differ only in wall time and peak RSS.

"Intended to be" is not evidence, and this corpus has already been bitten once
by assuming a portability switch was harmless: suite.toml records that
mapquik's --nosimd path, which looked like the way to get an aarch64 build,
changes 26.8% of records against its own default path. That was caught by
comparing outputs. This does the same comparison for bwa-mem2, and the answer
reaches the paper either way — a cross-architecture speed ratio between two
binaries that disagree about where reads go is not a speed ratio.

WHY NOT `sort | sha256sum`
--------------------------
Record order is not stable: bwa-mem2 writes from 32 threads, so two runs of one
binary already differ in order while containing the same records. The
comparison therefore has to be order-independent. Sorting would do it, but
sorting B08's 16 GB PAF costs a temp file as large as the input on both hosts,
for a question that needs one streaming pass.

Instead each line is hashed and the hashes are SUMMED. Addition is commutative,
so the total does not depend on order, and the sum is taken over 128 bits, wide
enough that an accidental collision between two genuinely different corpora is
not a thing that happens. It is not a cryptographic commitment — an adversary
could construct a collision — but nothing here is adversarial.

DUPLICATES ARE NOT COLLAPSED, deliberately: a multiset, not a set. If one build
emitted a record twice and the other once, that is a difference worth failing
on, and a set comparison would hide it.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

MOD = 1 << 128


def signature(path: Path) -> tuple[int, int, str]:
    """(records, bases_spanned, hex digest) for one PAF."""
    total = 0
    n = 0
    span = 0
    with open(path, "rb") as f:
        for line in f:
            if not line.strip():
                continue
            n += 1
            total = (total + int(hashlib.md5(line).hexdigest(), 16)) % MOD
            # A second, independent quantity: if the digests differ, this says
            # whether the difference is a few records or a wholesale
            # disagreement about placement.
            c = line.split(b"\t")
            if len(c) > 8:
                try:
                    span += int(c[8]) - int(c[7])
                except ValueError:
                    pass
    return n, span, f"{total:032x}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paf", nargs="+")
    a = ap.parse_args()

    import platform
    print(f"host {platform.node()} ({platform.machine()})")
    print(f"{'file':28} {'records':>12} {'target bases':>16}  signature")
    for p in a.paf:
        path = Path(p)
        if not path.exists():
            print(f"{path.name:28} {'MISSING':>12}")
            continue
        n, span, sig = signature(path)
        print(f"{path.name:28} {n:>12,} {span:>16,}  {sig}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
