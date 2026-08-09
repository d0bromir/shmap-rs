#!/usr/bin/env python3
"""Where everything lives, and which architecture we are on.

One module so the layout is stated once. Before this, six call sites each
rebuilt the path to a result set by hand; adding the architecture dimension
to six copies of the same expression is how they drift apart.

    benchmarks/
      data/      datasets.tsv, suite.toml, hosts.toml, and files/ (gitignored)
      scripts/   this file and everything that runs
      results/   suite-<v>/<arch>/{ARCH.md, current/, <ver>-<sha>-<date>/}

---------------------------------------------------------------------------
Architecture
---------------------------------------------------------------------------
`arch()` returns what `uname -m` reports — `x86_64`, `aarch64` — rather than
a prettier label like `arm64`. That is deliberate: the same string is what
Rust names its target triples, so the directory a run writes to is *derived*
from the machine it ran on and cannot be set wrong by hand. There is no
mapping table to keep in sync, and a run physically cannot file itself under
the wrong architecture.

Results are separated by architecture because they are not comparable across
one. `compare.py` already refuses to diff result sets whose manifests name
different hosts; per-architecture baselines are the same rule applied to the
directory tree, so a PR measured on ARM is judged against ARM.

---------------------------------------------------------------------------
Data
---------------------------------------------------------------------------
`datasets.tsv` holds paths relative to `data_root()`, so the same registry
resolves on every host without a per-host branch anywhere in the runner.
`data_root()` is, in order:

  1. $SHMAP_DATA, for a host that keeps its corpus somewhere unusual
  2. benchmarks/data/files — normally a symlink to wherever the disk is

The corpus is ~46 GB for the five suite benchmarks and is never committed;
`data/files` is gitignored. On a host that already has the data laid out,
pointing the symlink at it costs nothing and copies nothing.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

HERE = Path(__file__).resolve().parent      # benchmarks/scripts
BENCH = HERE.parent                         # benchmarks
REPO = BENCH.parent                         # repository root

DATA = BENCH / "data"
SUITE_TOML = DATA / "suite.toml"
DATASETS_TSV = DATA / "datasets.tsv"
HOSTS_TOML = DATA / "hosts.toml"
RESULTS = BENCH / "results"

# `uname -m` values we have actually run on. Anything else still works —
# `arch()` returns it verbatim — but is listed nowhere, so a typo in a
# hand-written path cannot masquerade as a real architecture.
KNOWN_ARCHES = ("x86_64", "aarch64")


def arch() -> str:
    """This machine's architecture, as `uname -m` reports it."""
    return platform.machine()


def data_root() -> Path:
    """Root the relative paths in datasets.tsv resolve against."""
    env = os.environ.get("SHMAP_DATA")
    return Path(env).expanduser() if env else (DATA / "files")


def resolve_dataset(rel: str) -> Path:
    """A dataset's absolute path on this host.

    Absolute entries are honoured as-is: the registry still carries a few
    that predate the shared root, and rewriting them would break the
    append-only rule datasets.tsv documents.
    """
    p = Path(os.path.expanduser(rel))
    return p if p.is_absolute() else data_root() / p


def arch_dir(suite_version: str, a: str | None = None) -> Path:
    """`results/suite-<v>/<arch>/` — the root of one architecture's history."""
    return RESULTS / f"suite-{suite_version}" / (a or arch())


def current_dir(suite_version: str, a: str | None = None) -> Path:
    """The baseline a pull request on this architecture is compared against."""
    return arch_dir(suite_version, a) / "current"


def available_arches(suite_version: str) -> list[str]:
    """Architectures that actually have results checked in, in a stable order."""
    root = RESULTS / f"suite-{suite_version}"
    if not root.is_dir():
        return []
    found = [p.name for p in root.iterdir() if p.is_dir() and (p / "current").is_dir()]
    # Known ones first, in declared order, so output does not depend on
    # readdir order; anything unrecognised follows, sorted.
    known = [a for a in KNOWN_ARCHES if a in found]
    return known + sorted(set(found) - set(known))
