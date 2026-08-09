#!/usr/bin/env python3
"""Self-test for layout.py.

Every path in the benchmark system now derives from this module, and the
architecture directory a run writes into is derived rather than typed. If
`arch()` disagreed with the machine, a run would file itself under the wrong
architecture and be compared against a baseline from different silicon — a
failure that produces plausible numbers and no error, which is the worst
kind. Pinned here.

  python3 benchmarks/scripts/test_layout.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import layout  # noqa: E402

FAIL: list[str] = []


def check(name: str, got, want):
    ok = got == want
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name:56} got {got!r}")
    if not ok:
        FAIL.append(f"{name}: got {got!r}, want {want!r}")


def main() -> int:
    print("architecture is derived from the machine, not configured:")
    uname_m = subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()
    check("arch() matches uname -m", layout.arch(), uname_m)
    check("and is one we have run on", layout.arch() in layout.KNOWN_ARCHES, True)

    print("\nthe three folders resolve where the layout says:")
    check("BENCH is benchmarks/", layout.BENCH.name, "benchmarks")
    check("scripts/ is where this file lives", layout.HERE.name, "scripts")
    check("REPO contains Cargo.toml", (layout.REPO / "Cargo.toml").exists(), True)
    check("suite.toml is under data/", layout.SUITE_TOML.relative_to(layout.BENCH).as_posix(),
          "data/suite.toml")
    check("datasets.tsv is under data/", layout.DATASETS_TSV.relative_to(layout.BENCH).as_posix(),
          "data/datasets.tsv")
    check("both actually exist", layout.SUITE_TOML.exists() and layout.DATASETS_TSV.exists(), True)

    print("\nresult paths carry the architecture:")
    check("arch_dir places arch under the suite version",
          layout.arch_dir("1.0", "aarch64").relative_to(layout.RESULTS).as_posix(),
          "suite-1.0/aarch64")
    check("current_dir hangs off it",
          layout.current_dir("1.0", "x86_64").relative_to(layout.RESULTS).as_posix(),
          "suite-1.0/x86_64/current")
    check("omitting arch uses this machine's",
          layout.current_dir("1.0"), layout.current_dir("1.0", layout.arch()))
    check("x86_64 results are discoverable",
          "x86_64" in layout.available_arches("1.0"), True)
    check("known arches sort before unknown ones",
          layout.available_arches("1.0")[:1], ["x86_64"])

    print("\ndataset paths resolve against the shared root:")
    check("a relative path joins the root",
          layout.resolve_dataset("hifi_real/x.fa"), layout.data_root() / "hifi_real/x.fa")
    check("an absolute path is left alone (append-only registry)",
          layout.resolve_dataset("/home/dobro/_paper_work/chr21.fa"),
          Path("/home/dobro/_paper_work/chr21.fa"))
    check("~ expands", layout.resolve_dataset("~/x.fa").is_absolute(), True)

    print("\n$SHMAP_DATA overrides the root:")
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("SHMAP_DATA")
        os.environ["SHMAP_DATA"] = tmp
        try:
            check("data_root honours the override", layout.data_root(), Path(tmp))
            check("and relative datasets follow it",
                  layout.resolve_dataset("a/b.fa"), Path(tmp) / "a/b.fa")
        finally:
            if old is None:
                del os.environ["SHMAP_DATA"]
            else:
                os.environ["SHMAP_DATA"] = old
        check("default returns after unsetting", layout.data_root(), layout.DATA / "files")

    print()
    if FAIL:
        for f in FAIL:
            print(f"  {f}")
        print(f"{len(FAIL)} failure(s)")
        return 1
    print("OK — layout resolves paths and architecture as documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
