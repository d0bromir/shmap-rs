#!/usr/bin/env python3
"""Build and maintain the external-mapper concordance corpus.

  reference_mappers.py --list                  what is cached, what is stale
  reference_mappers.py --run                   run everything missing or stale
  reference_mappers.py --run --only B01,B02
  reference_mappers.py --run --mapper winnowmap2
  reference_mappers.py --run --force           re-run even if cached
  reference_mappers.py --export                write the git-tracked manifest

These mappers are run ONCE per (mapper, benchmark) and their PAFs are cached on
the host. `run.py` never invokes them — a PR run scores shmap-rs against the
cached output, which is a join over two PAFs rather than another mapping pass.
That is the whole point: Winnowmap2 is far too slow to run per PR, and it does
not change between our commits, so re-running it would burn hours to reproduce
a number we already have.

WHAT THESE NUMBERS ARE
----------------------
Concordance, not accuracy. Winnowmap2 is the most accurate long-read mapper
available and it is still an estimate, not truth: where it and shmap-rs
disagree, nothing here says which is right. Accuracy comes from B02, whose
reads carry their true positions in the header. See the commentary on
`[external]` in suite.toml.

STALENESS
---------
A cached PAF is keyed on the mapper version, the full command line, and the
identity of both input files. If any of those change the entry is stale and is
re-run rather than silently reused — benchmarking against a quietly regenerated
input is the most damaging failure this system can have, and it is silent.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import HERE, HostLock, load_registry, load_suite, parse_time_v, sh  # noqa: E402

# Keys under [external] that configure the corpus rather than name a mapper.
NOT_A_MAPPER = {"enabled", "cache_dir", "concordance_min_overlap", "presets"}

EXPORT = HERE / "results" / "reference-mappers" / "manifest.json"


def expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def mappers(suite: dict) -> dict:
    ext = suite.get("external", {})
    return {k: v for k, v in ext.items() if k not in NOT_A_MAPPER and isinstance(v, dict)}


def binary_version(spec: dict) -> str:
    b = expand(spec["binary"])
    if not Path(b).exists():
        return ""
    vc = spec.get("version_cmd", "{binary} --version").format(binary=shlex.quote(b))
    r = sh(["bash", "-c", vc])
    return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr) else "unknown"


def plan_entry(name: str, spec: dict, bench: dict, suite: dict, reg: dict, cache: Path) -> dict | None:
    """The full description of one (mapper, benchmark) job, including the key
    that decides whether the cached copy is still valid."""
    if bench["id"] in spec.get("skip_benchmarks", []):
        return None
    ref_id, reads_id = bench["reference"], bench["reads"]
    ref, reads = reg[ref_id], reg[reads_id]
    preset = suite["external"].get("presets", {}).get(bench["id"])
    if preset is None:
        sys.exit(f"no [external.presets] entry for {bench['id']} — add one; "
                 f"ONT reads must not be mapped with a PacBio preset")

    out_dir = cache / name
    paf = out_dir / f"{bench['id']}.paf"
    # A mapper may need the reference in a different form than the registry's
    # canonical copy — mapquik miscounts a line-wrapped FASTA. The override is
    # a per-mapper path, and run_one refuses to run if it is missing rather
    # than silently falling back to the wrapped one.
    ref_path = ref["path"]
    override = spec.get("reference_override")
    if override:
        ref_path = expand(override.format(reference_id=ref_id))
    fields = dict(
        binary=expand(spec["binary"]),
        reference=ref_path, reads=reads["path"],
        threads=spec.get("threads", 32), preset=preset,
        repetitive=expand(spec.get("repetitive", "").format(reference_id=ref_id)) if spec.get("repetitive") else "",
        out_prefix=str(out_dir / bench["id"]),
    )
    cmd = spec["cmd"].format(**fields)
    return dict(
        mapper=name, benchmark=bench["id"], cmd=cmd, paf=paf,
        out_dir=out_dir, output=spec.get("output", "stdout"),
        role=spec.get("role", "peer"),
        needs=[f for f in (fields["repetitive"], override and ref_path) if f],
        key=dict(mapper=name, benchmark=bench["id"], cmd=cmd,
                 reference_id=ref_id, reads_id=reads_id,
                 reference_identity=[ref["bytes"], ref["records"], ref["bases"]],
                 reads_identity=[reads["bytes"], reads["records"], reads["bases"]]),
    )


def cached_key(entry: dict) -> dict | None:
    j = entry["out_dir"] / f"{entry['benchmark']}.json"
    if not (j.exists() and entry["paf"].exists()):
        return None
    try:
        return json.loads(j.read_text())
    except json.JSONDecodeError:
        return None


def status_of(entry: dict, version: str) -> str:
    got = cached_key(entry)
    if got is None:
        return "missing"
    want = dict(entry["key"], version=version)
    if got.get("key") != want:
        return "stale"
    return "cached"


def run_one(entry: dict, version: str) -> bool:
    entry["out_dir"].mkdir(parents=True, exist_ok=True)
    for need in entry["needs"]:
        if not Path(need).exists():
            print(f"  !! {entry['mapper']} {entry['benchmark']}: missing {need}")
            print("     build it first — see the meryl recipe in suite.toml")
            return False

    tf = entry["out_dir"] / f"{entry['benchmark']}.time"
    wrapped = f"/usr/bin/time -v -o {shlex.quote(str(tf))} {entry['cmd']}"
    t0 = time.time()
    if entry["output"] == "stdout":
        with open(entry["paf"], "w") as fo:
            rc = subprocess.run(["bash", "-c", wrapped], stdout=fo,
                                stderr=subprocess.DEVNULL).returncode
    else:
        rc = subprocess.run(["bash", "-c", wrapped],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    wall = time.time() - t0

    if rc != 0 or not entry["paf"].exists() or entry["paf"].stat().st_size == 0:
        print(f"  !! {entry['mapper']} {entry['benchmark']} failed (rc={rc}), "
              f"cache entry not written")
        entry["paf"].unlink(missing_ok=True)
        return False

    mapped = sum(1 for _ in open(entry["paf"]))
    peak = parse_time_v(tf)[1] if tf.exists() else 0
    (entry["out_dir"] / f"{entry['benchmark']}.json").write_text(json.dumps(dict(
        key=dict(entry["key"], version=version),
        version=version, role=entry["role"], cmd=entry["cmd"],
        mapped=mapped, wall_s=round(wall, 1), peak_rss_kb=peak,
        measured=datetime.now(timezone.utc).isoformat(),
    ), indent=2) + "\n")
    print(f"  {entry['mapper']:12} {entry['benchmark']}  {wall/60:6.1f} min  "
          f"{peak/1048576:5.2f} GB  {mapped} mapped")
    return True


def export_manifest(suite: dict, reg: dict, cache: Path) -> int:
    out = {"schema": 1, "generated": datetime.now(timezone.utc).isoformat(),
           "cache_dir": str(cache), "entries": []}
    for name, spec in sorted(mappers(suite).items()):
        version = binary_version(spec)
        for bench in suite["benchmark"]:
            e = plan_entry(name, spec, bench, suite, reg, cache)
            if e is None:
                continue
            got = cached_key(e)
            row = dict(mapper=name, benchmark=e["benchmark"], role=e["role"],
                       status=status_of(e, version), version=version, cmd=e["cmd"])
            if got:
                row.update({k: got.get(k) for k in ("mapped", "wall_s", "peak_rss_kb", "measured")})
            out["entries"].append(row)
    EXPORT.parent.mkdir(parents=True, exist_ok=True)
    EXPORT.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {EXPORT} ({len(out['entries'])} entries)")
    return 0


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", help="comma-separated benchmark ids")
    ap.add_argument("--mapper", help="comma-separated mapper names")
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args()
    if not (a.list or a.run or a.export):
        ap.error("one of --list, --run or --export is required")

    suite, reg = load_suite(), load_registry()
    if not suite.get("external", {}).get("enabled"):
        sys.exit("[external] is disabled in suite.toml")
    cache = Path(expand(suite["external"]["cache_dir"]))

    want_b = set(a.only.split(",")) if a.only else None
    want_m = set(a.mapper.split(",")) if a.mapper else None

    entries, versions = [], {}
    for name, spec in sorted(mappers(suite).items()):
        if want_m and name not in want_m:
            continue
        versions[name] = binary_version(spec)
        for bench in suite["benchmark"]:
            if want_b and bench["id"] not in want_b:
                continue
            e = plan_entry(name, spec, bench, suite, reg, cache)
            if e:
                entries.append(e)

    if a.export:
        return export_manifest(suite, reg, cache)

    if a.list:
        print(f"cache: {cache}")
        for name, v in versions.items():
            print(f"  {name}: {v or '** BINARY NOT FOUND **'}")
        print()
        print(f"{'mapper':13}{'bench':7}{'status':9}{'mapped':>10}  {'wall':>8}")
        for e in entries:
            st = status_of(e, versions[e["mapper"]])
            got = cached_key(e) or {}
            print(f"{e['mapper']:13}{e['benchmark']:7}{st:9}"
                  f"{got.get('mapped', '-'):>10}  "
                  f"{(str(round(got['wall_s']/60, 1)) + ' min') if got.get('wall_s') else '-':>8}")
        return 0

    todo = [e for e in entries
            if a.force or status_of(e, versions[e["mapper"]]) != "cached"]
    if not todo:
        print("everything is cached and current; nothing to do")
        return 0

    missing_bin = sorted({e["mapper"] for e in todo if not versions[e["mapper"]]})
    if missing_bin:
        sys.exit(f"binary not found for: {', '.join(missing_bin)} — check the paths in suite.toml")

    print(f"{len(todo)} job(s) to run. These are slow; Winnowmap2 on a whole "
          f"genome is measured in hours.")
    # Same host lock as run.py: these are heavy, and letting one land on top of
    # a measured benchmark would corrupt that benchmark's timings.
    with HostLock(wait=not a.no_wait) as lock:
        ok = 0
        for n, e in enumerate(todo, 1):
            lock.note(f"reference_mappers {e['mapper']}/{e['benchmark']} ({n}/{len(todo)})")
            print(f"[{n}/{len(todo)}] {e['mapper']} {e['benchmark']}")
            print(f"  $ {e['cmd']}")
            ok += run_one(e, versions[e["mapper"]])
    print(f"\n{ok}/{len(todo)} succeeded")
    export_manifest(suite, reg, cache)
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
