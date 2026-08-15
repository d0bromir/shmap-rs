#!/usr/bin/env python3
"""Check suite.toml is internally consistent and resolves against datasets.tsv.

Run this before committing a suite change. It is cheap and catches the errors
that would otherwise only surface an hour into a benchmark run: a dataset id
that does not exist, one that lives on a different host, or a check name that
no [checks.*] section defines.
"""
import sys, tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout import DATASETS_TSV, SUITE_TOML, arch, resolve_dataset  # noqa: E402

suite = tomllib.load(open(SUITE_TOML, "rb"))

reg = {}
for line in open(DATASETS_TSV):
    if line.startswith("#") or not line.strip():
        continue
    f = line.rstrip("\n").split("\t")
    if f[0] == "id":
        continue
    reg[f[0]] = dict(zip(("kind", "host", "path", "bytes", "records", "bases"), f[1:7]))

# Presence of the corpus is a property of one machine, not of the repository.
# This script's job is that suite.toml is coherent, and that must hold on a CI
# runner too -- which has no corpus. So a missing file is a note unless asked
# for explicitly. `run.py:verify_datasets` is the real gate and refuses to
# measure anything whose size does not match the registry.
require_data = "--require-data" in sys.argv

errs = []
absent = []
for b in suite["benchmark"]:
    for key in ("reference", "reads"):
        ds = b[key]
        if ds not in reg:
            errs.append(f"{b['id']}: dataset '{ds}' not in datasets.tsv")
        elif not resolve_dataset(reg[ds]["path"]).exists():
            # The registry's `host` column is provenance -- where a dataset was
            # first registered -- not a gate on where it may be used.
            where = f"{ds} at {resolve_dataset(reg[ds]['path'])}"
            (errs if require_data else absent).append(
                f"{b['id']}: dataset '{where}' not present on this host"
                + ("" if require_data else " (note)"))
        elif reg[ds]["bytes"] == "MISSING":
            errs.append(f"{b['id']}: dataset '{ds}' is marked MISSING in the registry")
    if b["params"] not in suite["params"]:
        errs.append(f"{b['id']}: unknown params '{b['params']}'")
    elif suite["params"][b["params"]].get("enabled") is False:
        errs.append(f"{b['id']}: params '{b['params']}' is disabled")
    for c in b["checks"]:
        if c not in suite["checks"]:
            errs.append(f"{b['id']}: unknown check '{c}'")
    for i in b["impls"]:
        if i not in suite["impl"]:
            errs.append(f"{b['id']}: unknown impl '{i}'")

ids = [b["id"] for b in suite["benchmark"]]
if len(ids) != len(set(ids)):
    errs.append("duplicate benchmark ids")

# Every benchmark the external corpus will actually attempt needs a preset,
# because reference_mappers.py exits rather than guess one. A benchmark every
# mapper skips needs none.
ext = suite.get("external", {})
if ext.get("enabled"):
    ext_mappers = {k: v for k, v in ext.items()
                   if isinstance(v, dict) and k != "presets"}
    for b in suite["benchmark"]:
        runs = [m for m, spec in ext_mappers.items()
                if b["id"] not in spec.get("skip_benchmarks", [])]
        if runs and b["id"] not in ext.get("presets", {}):
            errs.append(f"{b['id']}: no [external.presets] entry, but "
                        f"{', '.join(sorted(runs))} would run on it")

# Counted per tier and per implementation, because the two now differ by
# orders of magnitude: the pr tier is the gate every pull request pays, and
# reporting one total for both invites reading the paper tier's hours as if
# they were part of it. `impls` is respected because a benchmark that does not
# list the reference implementation does not invoke it, and counting it anyway
# overstated the reference column for every benchmark added since.
refrep = suite["run"]["reference_impl"]["repeats"]
ref_impls = {n for n, s in suite["impl"].items() if s.get("role") == "reference"}
tiers: dict[str, list[int]] = {}
for b in suite["benchmark"]:
    t = b.get("tier", "pr")
    subj = len(b["metrics"]) * len(b["threads"])
    ref = (len(b["metrics"]) * len(b["reference_impl_threads"]) * refrep
           if ref_impls & set(b["impls"]) else 0)
    acc = tiers.setdefault(t, [0, 0, 0])
    acc[0] += subj
    acc[1] += ref
    acc[2] += 1

import platform
if absent:
    print(f"\n{len(absent)} dataset(s) referenced by the suite are not on this machine.")
    print("That is expected off a benchmark host — see benchmarks/data/README.md.")
    print("Use --require-data to make it an error (a benchmark host should).")
    for a_ in absent:
        print(f"  {a_}")
    print()

print(f"suite_version {suite['suite_version']}  dataset_version "
      f"{suite['dataset_version']}  running on {platform.node()} ({arch()})")
print(f"benchmarks: {len(ids)}  ({', '.join(ids)})")
for t in sorted(tiers, key=lambda x: (x != "pr", x)):
    subj, ref, n = tiers[t]
    gate = " — the per-PR gate" if t == "pr" else " — run.py --tier " + t
    print(f"tier {t}: {n} benchmark(s), {subj} subject + {ref} reference "
          f"= {subj + ref} invocations{gate}")
print(f"blocking checks: {', '.join(k for k, v in suite['checks'].items() if v.get('blocking'))}")

if errs:
    print("\nERRORS:")
    for e in errs:
        print("  " + e)
    sys.exit(1)
print("\nOK — suite.toml is consistent and every dataset resolves")
