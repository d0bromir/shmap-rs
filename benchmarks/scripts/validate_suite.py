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

errs = []
host = suite["run"]["host"]
for b in suite["benchmark"]:
    for key in ("reference", "reads"):
        ds = b[key]
        if ds not in reg:
            errs.append(f"{b['id']}: dataset '{ds}' not in datasets.tsv")
        elif not resolve_dataset(reg[ds]["path"]).exists():
            # The registry's `host` column is provenance -- where a dataset was
            # first registered -- not a gate. Whether it is usable *here* is a
            # question for the filesystem, and the corpus resolves through the
            # same relative root on every host, so this is the honest check.
            errs.append(f"{b['id']}: dataset '{ds}' not present on this host at "
                        f"{resolve_dataset(reg[ds]['path'])} — see benchmarks/data/README.md")
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

subject = sum(len(b["metrics"]) * len(b["threads"]) for b in suite["benchmark"])
refrep = suite["run"]["reference_impl"]["repeats"]
reference = sum(len(b["metrics"]) * len(b["reference_impl_threads"]) * refrep for b in suite["benchmark"])

import platform
print(f"suite_version {suite['suite_version']}  dataset_version "
      f"{suite['dataset_version']}  running on {platform.node()} ({arch()})")
print(f"benchmarks: {len(ids)}  ({', '.join(ids)})")
print(f"invocations: {subject} subject + {reference} reference = {subject + reference}")
print(f"blocking checks: {', '.join(k for k, v in suite['checks'].items() if v.get('blocking'))}")

if errs:
    print("\nERRORS:")
    for e in errs:
        print("  " + e)
    sys.exit(1)
print("\nOK — suite.toml is consistent and every dataset resolves")
