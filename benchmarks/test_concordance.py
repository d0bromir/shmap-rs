#!/usr/bin/env python3
"""Self-test for concordance.py.

This scorer decides whether shmap-rs is keeping up with Winnowmap2, so a bug in
it either hides a real loss of mappings or invents one. The interval arithmetic
is the fiddly part and is tested directly, including the nested case that
mapquik's intersect_pafs.py gets wrong.

  python3 benchmarks/test_concordance.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from concordance import overlaps, parse_paf, score  # noqa: E402

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:46} got {got!r}")
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")


def approx(name: str, got: float, want: float, tol: float = 1e-9):
    ok = abs(got - want) < tol
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:46} got {got:.6f}")
    if not ok:
        FAIL.append(f"{name}: got {got}, want {want}")


def paf(path: Path, rows) -> str:
    """rows: (read, target, start, end, mapq)"""
    with open(path, "w") as f:
        for read, tgt, s, e, q in rows:
            # 12 mandatory PAF columns
            f.write(f"{read}\t1000\t0\t1000\t+\t{tgt}\t1000000\t{s}\t{e}\t900\t1000\t{q}\n")
    return str(path)


def main() -> int:
    print("interval criterion (threshold 0.1):")
    T = 0.1
    check("identical", overlaps(("c", 0, 100), ("c", 0, 100), T), True)
    check("staggered 50%", overlaps(("c", 0, 100), ("c", 50, 200), T), True)
    check("disjoint", overlaps(("c", 0, 100), ("c", 200, 300), T), False)
    check("touching at a point", overlaps(("c", 0, 100), ("c", 100, 200), T), False)
    check("different target", overlaps(("a", 0, 100), ("b", 0, 100), T), False)
    check("nested (0.4 of union)", overlaps(("c", 0, 100), ("c", 10, 50), T), True)
    check("tiny overlap 9% -> below", overlaps(("c", 0, 100), ("c", 91, 191), T), False)
    check("reversed coordinates", overlaps(("c", 100, 0), ("c", 0, 100), T), True)

    print("\nnested case is not intersect_pafs.py's inflated value:")
    # [0,100] vs [10,50]: intersection 40, union 100 -> 0.40, not 0.50.
    approx("intersection/union for nested", 40 / 100, 0.40)
    check("0.45 threshold rejects (0.40 < 0.45)",
          overlaps(("c", 0, 100), ("c", 10, 50), 0.45), False)
    check("intersect_pafs would have accepted at 0.45",
          0.50 > 0.45, True)

    print("\nscoring:")
    tmp = Path(tempfile.mkdtemp())
    # reference maps 4 reads; subject maps r1 (same), r2 (elsewhere), r3 (missing), r4 (same)
    ref = parse_paf(paf(tmp / "ref.paf", [
        ("r1", "chr1", 1000, 2000, 60), ("r2", "chr1", 5000, 6000, 60),
        ("r3", "chr1", 9000, 10000, 60), ("r4", "chr2", 100, 1100, 60)]))
    sub = parse_paf(paf(tmp / "sub.paf", [
        ("r1", "chr1", 1000, 2000, 60), ("r2", "chr1", 80000, 81000, 60),
        ("r4", "chr2", 150, 1150, 60), ("r9", "chr3", 1, 500, 60)]))
    r = score(sub, ref, T)
    check("reference_mapped", r["reference_mapped"], 4)
    check("subject_mapped", r["subject_mapped"], 4)
    check("both_mapped", r["both_mapped"], 3)
    check("concordant (r1, r4)", r["concordant"], 2)
    check("discordant same target (r2)", r["discordant_same_target"], 1)
    check("missed (r3)", r["missed"], 1)
    approx("recall = 3/4", r["recall"], 0.75)
    approx("agreement = 2/3", r["agreement"], 2 / 3)
    approx("good = 2/4", r["good"], 0.5)
    approx("good == recall * agreement", r["recall"] * r["agreement"], r["good"])

    print("\nheadline number cannot be gamed by mapping less:")
    # A subject that maps only one read, perfectly, scores 1.0 on agreement but
    # 0.25 on `good` — which is why `good` is the number we report.
    lazy = parse_paf(paf(tmp / "lazy.paf", [("r1", "chr1", 1000, 2000, 60)]))
    rl = score(lazy, ref, T)
    approx("lazy mapper agreement (misleading)", rl["agreement"], 1.0)
    approx("lazy mapper good (honest)", rl["good"], 0.25)

    print("\nmapq filter on the reference:")
    refq = parse_paf(paf(tmp / "refq.paf", [
        ("r1", "chr1", 1000, 2000, 60), ("r2", "chr1", 5000, 6000, 3)]), min_mapq=10)
    check("mapq 3 record excluded", len(refq), 1)

    print()
    if FAIL:
        for f in FAIL:
            print(f"  {f}")
        print(f"{len(FAIL)} failure(s)")
        return 1
    print("OK — concordance scoring behaves as documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
