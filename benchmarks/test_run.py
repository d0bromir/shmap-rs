#!/usr/bin/env python3
"""Self-test for run.py's one_read_fasta.

Pesho's indexing/mapping split for a tool with no native phase report (the
C++ reference; see `measure`'s doc comment) depends entirely on this
function slicing out exactly the reads file's first record — get that wrong
(truncate mid-sequence, grab zero records, grab two) and the "index-only" run
silently measures the wrong thing, and every index_s/map_s the C++ reports
is wrong with it.

  python3 benchmarks/test_run.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import one_read_fasta  # noqa: E402

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:46} got {got!r}")
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        print("single-record file:")
        src = tmp / "one.fa"
        src.write_text(">r1\nACGT\nACGT\n")
        out = one_read_fasta(str(src), tmp / "cache1")
        check("record count", out.read_text().count(">"), 1)
        check("content unchanged", out.read_text(), ">r1\nACGT\nACGT\n")

        print("\nmulti-record file: only the first is kept")
        src = tmp / "multi.fa"
        src.write_text(">r1\nAAAA\nAAAA\n>r2\nCCCC\n>r3\nGGGG\n")
        out = one_read_fasta(str(src), tmp / "cache2")
        check("record count", out.read_text().count(">"), 1)
        check("first record's header kept", out.read_text().splitlines()[0], ">r1")
        check("second record's header dropped", "r2" in out.read_text(), False)
        check("first record's sequence not truncated", out.read_text(), ">r1\nAAAA\nAAAA\n")

        print("\nwrapped (multi-line) sequence within one record: no lines dropped")
        src = tmp / "wrapped.fa"
        src.write_text(">r1\nAAAA\nCCCC\nGGGG\nTTTT\n>r2\nNNNN\n")
        out = one_read_fasta(str(src), tmp / "cache3")
        check("all four sequence lines kept", out.read_text(), ">r1\nAAAA\nCCCC\nGGGG\nTTTT\n")

        print("\ncached: a second call for the same reads file returns the same content"
              " without re-deriving it from a now-different source")
        src = tmp / "cache_check.fa"
        src.write_text(">r1\nAAAA\n")
        cache_dir = tmp / "cache4"
        out1 = one_read_fasta(str(src), cache_dir)
        src.write_text(">different\nZZZZ\n")  # source changes; cache must not
        out2 = one_read_fasta(str(src), cache_dir)
        check("same cached path returned", out2, out1)
        check("cached content unaffected by the source changing after", out2.read_text(), ">r1\nAAAA\n")

        print()
        if FAIL:
            for f in FAIL:
                print(f"  {f}")
            print(f"{len(FAIL)} failure(s)")
            return 1
        print("OK — one_read_fasta isolates exactly the first record")
        return 0


if __name__ == "__main__":
    sys.exit(main())
