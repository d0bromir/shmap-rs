#!/usr/bin/env python3
"""Copy the benchmark corpus to another host, and prove it arrived intact.

  sync_data.py --to galaxy              copy what the suite actually uses
  sync_data.py --to galaxy --dry-run    show what would move, move nothing
  sync_data.py --to galaxy --all        every dataset in the registry, not just
                                        the ones suite.toml references
  sync_data.py --to galaxy --verify     verify what is already there, copy nothing

---------------------------------------------------------------------------
Why a copy and not a share
---------------------------------------------------------------------------
The two hosts are meant to benchmark at the same time. Serving one host's
corpus to the other over the network would put both measurements on the same
disks and the same link: the serving host would be doing I/O it is not
accounting for, and the reading host would be network-bound rather than page
-cache-bound. `suite.toml` sets `warm_cache = true` precisely so every
benchmark reads from a warm local page cache; that assumption does not
survive a network filesystem. Two independent copies is the only arrangement
in which both hosts measure the thing they claim to measure.

---------------------------------------------------------------------------
Same structure everywhere
---------------------------------------------------------------------------
`datasets.tsv` holds paths relative to the data root, so this copies each one
to the *same relative location* under the target's root. That is what lets
the benchmark scripts run unmodified on either host: nothing resolves a
hostname, it just resolves `data/files/<relative path>`.

---------------------------------------------------------------------------
Verification
---------------------------------------------------------------------------
Every file is size-checked on the far side against the registry after
transfer -- the same check `run.py:verify_datasets` makes before it measures
anything, so a short or truncated copy fails here rather than silently
becoming a benchmark of a different file. `--deep` additionally recounts
records and bases remotely, which is exact but reads every byte (minutes for
the 31 GB set), so it is opt-in.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout import DATASETS_TSV, HOSTS_TOML, SUITE_TOML  # noqa: E402
from run import load_registry, load_suite  # noqa: E402


def hosts() -> dict:
    return tomllib.load(open(HOSTS_TOML, "rb"))


def target_spec(name: str) -> tuple[str, str]:
    """(ssh destination, data root) for a host named in hosts.toml."""
    h = hosts()
    if name not in h or name == "schema":
        known = [k for k in h if k != "schema"]
        sys.exit(f"unknown host {name!r}; hosts.toml knows: {', '.join(known)}")
    e = h[name]
    return f"{e['user']}@{e['address']}", e["data_root"].rstrip("/")


def needed(all_datasets: bool) -> list[str]:
    if all_datasets:
        return sorted(load_registry())
    suite = load_suite()
    return sorted({b[k] for b in suite["benchmark"] for k in ("reference", "reads")})


def remote(dest: str, cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", dest, cmd],
                          capture_output=True, text=True)


def human(n: float) -> str:
    return f"{n/1e9:.2f} GB"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True, help="target host, as named in data/hosts.toml")
    ap.add_argument("--all", action="store_true", help="every registry entry, not just the suite's")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true", help="verify only; copy nothing")
    ap.add_argument("--deep", action="store_true", help="also recount records and bases remotely")
    args = ap.parse_args()

    dest, root = target_spec(args.to)
    reg = load_registry()
    ids = needed(args.all)

    plan, missing_local = [], []
    for ds in ids:
        e = reg[ds]
        src = Path(e["path"])
        if not src.exists():
            missing_local.append(f"{ds} ({src})")
            continue
        rel = e.get("rel", "")
        if not rel or rel.startswith("/"):
            # Absolute registry entries belong to a specific host and have no
            # place in the shared tree; skip rather than invent a location.
            print(f"  skip {ds}: registry path is absolute ({e['path']})")
            continue
        plan.append((ds, src, rel, int(e["bytes"])))

    if missing_local:
        print("not present on this host, cannot copy:")
        for m in missing_local:
            print(f"  {m}")

    total = sum(b for _, _, _, b in plan)
    print(f"\n{len(plan)} datasets, {human(total)} -> {dest}:{root}/\n")
    for ds, src, rel, b in plan:
        print(f"  {ds:<14} {human(b):>10}  {rel}")

    if args.dry_run:
        print("\ndry run; nothing copied")
        return 0

    if not args.verify:
        print()
        for i, (ds, src, rel, b) in enumerate(plan, 1):
            target = f"{root}/{rel}"
            print(f"[{i}/{len(plan)}] {ds} -> {target}")
            mk = remote(dest, f"mkdir -p {Path(target).parent}")
            if mk.returncode != 0:
                print(f"  !! mkdir failed: {mk.stderr.strip()}")
                return 1
            # --partial so an interrupted 31 GB transfer resumes instead of
            # restarting; --inplace keeps peak disk at one copy, not two.
            r = subprocess.run(["rsync", "-a", "--partial", "--inplace",
                                "--info=progress2", str(src), f"{dest}:{target}"])
            if r.returncode != 0:
                print(f"  !! rsync failed for {ds} (exit {r.returncode})")
                return 1

    # ---- verify ----------------------------------------------------------
    print("\nverifying against the registry:")
    bad = 0
    for ds, src, rel, b in plan:
        target = f"{root}/{rel}"
        out = remote(dest, f"stat -c %s {target} 2>/dev/null || echo MISSING")
        got = out.stdout.strip()
        ok = got == str(b)
        print(f"  [{'ok  ' if ok else 'FAIL'}] {ds:<14} {b} bytes" + ("" if ok else f" — remote says {got}"))
        if not ok:
            bad += 1
            continue
        if args.deep:
            cmd = (f"awk '/^>/{{r++;next}}{{n+=length($0)}}END{{print r, n}}' {target}")
            d = remote(dest, cmd)
            recs, bases = (d.stdout.split() + ["", ""])[:2]
            e = reg[ds]
            deep_ok = recs == e["records"] and bases == e["bases"]
            print(f"        {'ok  ' if deep_ok else 'FAIL'} records={recs} bases={bases}"
                  + ("" if deep_ok else f" — registry says {e['records']}/{e['bases']}"))
            if not deep_ok:
                bad += 1

    if bad:
        print(f"\n{bad} dataset(s) did not verify — do NOT benchmark against this host yet")
        return 1
    print(f"\nall {len(plan)} datasets verified on {args.to}")
    print(f"point its corpus symlink at the root:\n"
          f"  ssh {dest} 'ln -sfn {root} ~/shmap-rs/benchmarks/data/files'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
