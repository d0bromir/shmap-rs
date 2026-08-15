#!/usr/bin/env python3
"""Self-test for sam2paf.py.

bwa-mem2 is the only mapper in the corpus that emits SAM, so every one of its
concordance numbers passes through this converter. A bug here is indistinguish-
able from bwa-mem2 disagreeing with shmap-rs, which is exactly the thing the
corpus exists to measure — so the conversion is pinned to worked examples
rather than trusted.

The expected values follow `paftools.js sam2paf`, whose behaviour is the
reference for this format projection.

  python3 benchmarks/scripts/test_sam2paf.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sam2paf import convert, parse_cigar  # noqa: E402

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:52} {got!r}")
    if not ok:
        FAIL.append(f"{name}:\n      got  {got!r}\n      want {want!r}")


HEADER = "@HD\tVN:1.6\tSO:unsorted\n@SQ\tSN:chr1\tLN:248387328\n@SQ\tSN:chr2\tLN:242696752\n"


def run(sam_body: str, header: str = HEADER):
    out = io.StringIO()
    stats = convert(io.StringIO(header + sam_body), out)
    rows = [l.split("\t") for l in out.getvalue().splitlines()]
    return rows, stats


def sam(qname, flag, rname, pos, mapq, cigar, seq="*", extra=("NM:i:0",)):
    return "\t".join([qname, str(flag), rname, str(pos), str(mapq), cigar,
                      "*", "0", "0", seq, "*", *extra]) + "\n"


def main() -> int:
    print("CIGAR arithmetic (qlen, qstart, qspan, tspan, blen):")
    check("plain match", parse_cigar("100M"), (100, 0, 100, 100, 100))
    check("soft clips count towards query length",
          parse_cigar("10S80M10S"), (100, 10, 80, 80, 80))
    check("hard clips count towards query length too",
          parse_cigar("10H80M10H"), (100, 10, 80, 80, 80))
    check("insertion consumes query only",
          parse_cigar("50M5I50M"), (105, 0, 105, 100, 105))
    check("deletion consumes target only",
          parse_cigar("50M5D50M"), (100, 0, 100, 105, 105))
    check("N (skip) consumes target but is not an alignment column",
          parse_cigar("50M5N50M"), (100, 0, 100, 105, 100))
    check("= and X are alignment columns",
          parse_cigar("40=10X50="), (100, 0, 100, 100, 100))

    print("\nforward-strand record:")
    rows, _ = run(sam("readA", 0, "chr1", 1001, 60, "10S80M10S", extra=("NM:i:3",)))
    check("one row", len(rows), 1)
    r = rows[0]
    check("query name / length", (r[0], r[1]), ("readA", "100"))
    check("query interval on the read", (r[2], r[3]), ("10", "90"))
    check("strand", r[4], "+")
    check("target name / length", (r[5], r[6]), ("chr1", "248387328"))
    check("target interval, POS is 1-based and PAF is 0-based",
          (r[7], r[8]), ("1000", "1080"))
    check("matches = blen - NM, not blen", (r[9], r[10]), ("77", "80"))
    check("mapq", r[11], "60")
    check("primary tag", r[12], "tp:A:P")

    print("\nreverse-strand record — clips reflect onto the original read:")
    rows, _ = run(sam("readB", 16, "chr2", 501, 60, "10S80M30S", extra=("NM:i:0",)))
    r = rows[0]
    check("query length", r[1], "120")
    # Aligned-strand interval is [10, 90) of 120; on the original read that is
    # [120-90, 120-10) = [30, 110). Taking the SAM interval unchanged would
    # report [10, 90) and be wrong by 20 bases at both ends.
    check("query interval is reflected", (r[2], r[3]), ("30", "110"))
    check("strand", r[4], "-")
    check("target interval is NOT reflected", (r[7], r[8]), ("500", "580"))

    print("\nrecord flags:")
    rows, _ = run(sam("s", 256, "chr1", 1, 0, "50M")
                  + sam("p", 2048, "chr1", 1, 60, "20H30M")
                  + sam("u", 4, "*", 0, 0, "*"))
    check("secondary tagged S", rows[0][12], "tp:A:S")
    check("supplementary tagged I", rows[1][12], "tp:A:I")
    check("unmapped dropped, two rows survive", len(rows), 2)
    check("hard-clipped supplementary keeps the full read length",
          rows[1][1], "50")

    print("\nmissing metadata is reported, not invented:")
    rows, stats = run(sam("readC", 0, "chr1", 101, 60, "100M", extra=()))
    check("no NM: matches falls back to blen", (rows[0][9], rows[0][10]), ("100", "100"))
    check("and is counted", stats["no_nm"], 1)

    rows, stats = run(sam("readD", 0, "chrUn", 101, 60, "100M"), header="@HD\tVN:1.6\n")
    check("no @SQ: target length is a lower bound", rows[0][6], "200")
    check("and is counted", stats["no_sq"], 1)

    print("\nstats:")
    _, stats = run(sam("a", 0, "chr1", 1, 60, "50M")
                   + sam("b", 4, "*", 0, 0, "*")
                   + sam("c", 16, "chr2", 1, 60, "50M"))
    check("records / written / unmapped",
          (stats["records"], stats["written"], stats["unmapped"]), (3, 2, 1))

    print("\nconcordance.py can read what we write:")
    from concordance import parse_paf  # noqa: E402
    import tempfile
    rows, _ = run(sam("r1", 0, "chr1", 1001, 60, "100M")
                  + sam("r1", 256, "chr2", 5001, 0, "100M")
                  + sam("r2", 16, "chr1", 2001, 60, "100M"))
    with tempfile.NamedTemporaryFile("w", suffix=".paf", delete=False) as f:
        f.write("".join("\t".join(r) + "\n" for r in rows))
        p = f.name
    got = parse_paf(p)
    Path(p).unlink()
    check("secondary dropped, primaries kept with target coordinates",
          got, {"r1": ("chr1", 1000, 1100), "r2": ("chr1", 2000, 2100)})

    if FAIL:
        print(f"\n{len(FAIL)} FAILURE(S):")
        for f in FAIL:
            print("  " + f)
        return 1
    print("\nOK — SAM to PAF conversion matches paftools' documented behaviour")
    return 0


if __name__ == "__main__":
    sys.exit(main())
