#!/usr/bin/env python3
"""Publish a finished result set to the repository.

  promote.py <result-set-dir>            copy in, regenerate everything, verify
  promote.py <result-set-dir> --commit   ... and commit the result
  promote.py <result-set-dir> --push     ... and push it

---------------------------------------------------------------------------
What this replaces
---------------------------------------------------------------------------
Promotion used to be a sequence someone remembered: copy the set over
`current/`, run `report.py`, run `paper.py`, rebuild the PDF, check nothing
drifted, then commit. Every step is mandatory and skipping one leaves the
repository describing a measurement it no longer contains -- which is exactly
how RESULTS.md previously came to carry contradictory numbers.

The steps in order, and why each is here:

1. Copy the result set over `current/`. This is what every generator reads.
   Reference-implementation rows are carried forward when the incoming set has
   none: `suite.toml` marks the C++ `role = "reference"` and re-measures it
   only when the binary changes, so an ordinary run has none to copy, and
   replacing `current/` wholesale would delete the last copy along with every
   C++ comparison that depends on it.
2. `report.py` -- RESULTS.md and README.md.
3. `paper.py` -- the paper's LaTeX and TSV artifacts.
4. `build_pdf.py` -- the typeset PDF of those artifacts.
5. `optimizations.py` -- the optimization table, from PORT_CHANGES.md.
6. `manuscript.py` -- the macros both notes' prose is written in.
7. `build_paper.py` -- the two-page notes themselves.
5. Verify: both `--check`s must pass afterwards. They compare regenerated
   output against what is now on disk, so a failure here means a generator is
   not deterministic rather than that someone forgot a step -- worth stopping
   for either way.

Nothing is committed unless `--commit` is given, and nothing is pushed unless
`--push` is. A promotion changes every headline number in the repository, so it
should be a decision rather than a side effect.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
from layout import REPO, current_dir  # noqa: E402
sys.path.insert(0, str(HERE))
from run import load_suite  # noqa: E402

# Everything a result set consists of. Named explicitly rather than copying the
# directory wholesale: a run leaves `raw/` behind (PAFs, hundreds of megabytes)
# and that is deliberately not committed.
ARTIFACT_FILES = ["results.tsv", "checks.tsv", "profiles.tsv", "manifest.json",
                  "raw-profiles.tar.gz"]
# `chart-*` are regenerable from profiles.tsv, but they travel with the
# result set anyway: a promoted set should be viewable without anyone
# having to re-derive its pictures first.
ARTIFACT_GLOBS = ["per-read-*.tsv", "chart-*.svg", "chart-index.html"]

# RESULTS.md and README.md carry one running narrative, written about x86_64:
# the C++ comparison, the memory story, the thread-scaling argument. They are
# not a pure function of a result set the way the charts and paper artifacts
# are, so they are regenerated only for this architecture. Promoting another
# updates its own result tree, charts and paper artifacts, all of which are
# per-architecture and cannot collide.
DOC_ARCH = "x86_64"


def reference_impls(suite: dict) -> list[str]:
    """Implementations the suite treats as a reference measurement."""
    return sorted(name for name, spec in suite.get("impl", {}).items()
                  if spec.get("role") == "reference")


def split_rows(tsv: Path, impls: set[str]) -> tuple[list[str], list[str], list[str]]:
    """(header, rows for those impls, rows for everything else)."""
    if not tsv.exists():
        return [], [], []
    lines = tsv.read_text().splitlines(keepends=True)
    if not lines:
        return [], [], []
    head, body = lines[0], lines[1:]
    cols = head.rstrip("\n").split("\t")
    i = cols.index("impl")
    mine = [l for l in body if l.split("\t")[i] in impls]
    rest = [l for l in body if l.split("\t")[i] not in impls]
    return [head], mine, rest


def row_key(line: str, header: list[str]) -> tuple:
    """(benchmark, impl, metric, threads) — what identifies a measured row."""
    f = line.rstrip("\n").split("\t")
    return tuple(f[header.index(c)] for c in ("benchmark", "impl", "metric", "threads"))


def carry_reference_rows(suite: dict, src: Path, dst: Path) -> dict | None:
    """Keep baseline reference rows for the keys the new set did not measure.

    Per key rather than all-or-nothing, because the drift probe measures part
    of the reference every run. An all-or-nothing rule would see those rows,
    conclude the run had measured its own reference, and silently drop every
    benchmark the probe does not cover.

    Returns provenance to record in the manifest, or None if nothing needed
    carrying. Reads `dst` before it is overwritten, so this must be called
    while the previous baseline is still in place.
    """
    impls = set(reference_impls(suite))
    if not impls:
        return None
    ihead, incoming, _ = split_rows(src / "results.tsv", impls)
    ohead, outgoing, _ = split_rows(dst / "results.tsv", impls)
    if not outgoing:
        return None                      # nothing to carry
    ih = ihead[0].rstrip("\n").split("\t") if ihead else []
    oh = ohead[0].rstrip("\n").split("\t") if ohead else []
    have = {row_key(l, ih) for l in incoming} if ih else set()
    carried = [l for l in outgoing if row_key(l, oh) not in have]
    if not carried:
        return None                      # the run covered every key itself
    prev = {}
    man = dst / "manifest.json"
    if man.exists():
        try:
            prev = json.loads(man.read_text())
        except ValueError:
            prev = {}
    return {
        "impls": sorted(impls),
        "rows": len(carried),
        "measured_in_run": len(incoming),
        "from_commit": str(prev.get("commit", "?"))[:12],
        "measured": str(prev.get("finished", "?"))[:10],
        "why": "role=reference in suite.toml: re-measured only when the binary "
               "changes, so the previous measurement stands. The drift probe "
               "covers part of it every run; these are the keys it does not. "
               "Re-measure all of it with run.py --impls shmap-rs,cpp-shmap.",
        "_rows": carried,
    }


def apply_carried_rows(dst: Path, carried: dict) -> None:
    """Append the carried reference rows to the freshly copied results.tsv."""
    tsv = dst / "results.tsv"
    head, _, rest = split_rows(tsv, set())
    body = "".join(head) + "".join(rest) + "".join(carried["_rows"])
    if not body.endswith("\n"):
        body += "\n"
    tsv.write_text(body)


def run(cmd: list[str], what: str) -> None:
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    r = subprocess.run(cmd, cwd=REPO)
    if r.returncode != 0:
        sys.exit(f"{what} failed (exit {r.returncode}); nothing further was done")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result_set")
    ap.add_argument("--commit", action="store_true", help="git commit the promotion")
    ap.add_argument("--push", action="store_true", help="git push after committing")
    ap.add_argument("--message", help="commit subject (default: describes the set)")
    a = ap.parse_args()

    src = Path(a.result_set).resolve()
    if not (src / "manifest.json").exists():
        sys.exit(f"{src} does not look like a result set (no manifest.json)")
    man = json.loads((src / "manifest.json").read_text())

    suite = load_suite()
    # MAJOR, not the full version, for the reason compare.py gates on MAJOR
    # and layout.suite_series() files on it: a MINOR bump adds a benchmark and
    # leaves every existing row comparable (VERSIONING.md §2). A set measured
    # at 1.0 promoted into a 1.1 repo is a baseline with no rows for the new
    # benchmarks, which is precisely "the new row simply has no history" and
    # is what compare.py already handles. Requiring an exact match here meant
    # the first MINOR bump could not be given a baseline at all.
    set_major = str(man.get("suite_version", "")).split(".")[0]
    repo_major = str(suite["suite_version"]).split(".")[0]
    if set_major != repo_major:
        sys.exit(f"suite version mismatch: set is {man.get('suite_version')}, "
                 f"repo is {suite['suite_version']}. Promoting across a MAJOR suite "
                 f"change would compare figures that are not comparable.")
    if man.get("failures"):
        sys.exit(f"{src.name} has {man['failures']} failure(s) recorded; "
                 f"promoting a failed run would publish numbers the gate rejected")

    # Where a result set belongs is a property of the set, not of the machine
    # doing the promoting: take it from the manifest so a galaxy set promoted
    # from a2 still lands under aarch64. Sets measured before the multi-arch
    # split carry no `arch` and were all x86_64.
    set_arch = man.get("arch") or "x86_64"
    dst = current_dir(suite["suite_version"], set_arch)
    dst.mkdir(parents=True, exist_ok=True)

    print(f"promoting {src.name}")
    print(f"  commit   {man.get('commit', '?')[:12]}")
    print(f"  host     {man.get('host', '?')}")
    print(f"  arch     {set_arch}")
    print(f"  measured {man.get('finished', '?')[:10]}")

    # Read before anything is overwritten: an ordinary run does not re-measure
    # the reference implementation, and copying over the baseline would
    # otherwise delete the only copy of those rows.
    carried = carry_reference_rows(suite, src, dst)

    # Stale files from the previous set would otherwise survive and be read by
    # the generators -- per-read files especially, since a set that did not
    # collect them simply has none rather than empty ones.
    for old in dst.glob("per-read-*.tsv"):
        old.unlink()
    copied = []
    for name in ARTIFACT_FILES:
        p = src / name
        if p.exists():
            shutil.copy2(p, dst / name)
            copied.append(name)
    for pattern in ARTIFACT_GLOBS:
        for p in sorted(src.glob(pattern)):
            shutil.copy2(p, dst / p.name)
            copied.append(p.name)
    print(f"  copied {len(copied)} file(s) into {dst.relative_to(REPO)}")

    refs = reference_impls(suite)
    if carried:
        apply_carried_rows(dst, carried)
        prov = {k: v for k, v in carried.items() if k != "_rows"}
        man_p = dst / "manifest.json"
        man_j = json.loads(man_p.read_text())
        man_j["reference_rows_carried_forward"] = prov
        man_p.write_text(json.dumps(man_j, indent=1))
        n_run = carried.get("measured_in_run", 0)
        print(f"  {'/'.join(carried['impls'])}: {n_run} row(s) measured in this run, "
              f"{carried['rows']} carried forward from {carried['from_commit']} "
              f"(measured {carried['measured']}). Without the carry-forward those "
              f"benchmarks would vanish from RESULTS.md and the paper table.")
    elif refs:
        _, have, _ = split_rows(dst / "results.tsv", set(refs))
        if not have:
            print(f"  NOTE: no {'/'.join(refs)} rows in this set and none to carry forward, "
                  f"so {set_arch} has no reference comparison. Its paper table will have no "
                  f"C++ column. Measure one with: run.py --impls shmap-rs,{','.join(refs)}")

    # Charts are regenerated rather than trusted as copied. Each one footers
    # the result-set directory it was drawn from, so a chart copied out of
    # `1.3.1-<sha>-<date>/` still claims that as its source while
    # `charts.py --check` regenerates from `current/` and expects to see
    # `current/` -- which would leave CI failing after every promotion.
    run([sys.executable, "benchmarks/scripts/charts.py", "--arch", set_arch], "charts.py")

    # Paper artifacts are a pure function of one result set and now live in
    # paper/generated/<arch>/, so every architecture gets its own -- they
    # cannot overwrite each other.
    run([sys.executable, "benchmarks/scripts/paper.py", "--arch", set_arch], "paper.py")
    run([sys.executable, "benchmarks/scripts/build_pdf.py", "--arch", set_arch], "build_pdf.py")

    # The cross-architecture document reads every promoted set, so promoting
    # any one of them changes it. No --arch: it is about all of them. With
    # fewer than two promoted it writes nothing and says so, which is why this
    # is unconditional rather than guarded by a count here.
    run([sys.executable, "benchmarks/scripts/crossarch.py", "--pdf"], "crossarch.py")

    # The manuscript, same reason one step further out. Its PROSE quotes numbers
    # from both promoted sets through generated macros, so promoting either one
    # rewrites sentences and not only tables. Regenerating the macros before
    # typesetting is what makes that automatic; the --check below makes it verified.
    # The optimization table is a function of PORT_CHANGES.md rather than of a
    # result set, so a promotion cannot change it -- but the notes will not
    # typeset without it, and regenerating is cheaper than explaining that.
    run([sys.executable, "benchmarks/scripts/optimizations.py"], "optimizations.py")
    run([sys.executable, "benchmarks/scripts/manuscript.py"], "manuscript.py")
    run([sys.executable, "benchmarks/scripts/build_paper.py"], "build_paper.py")

    if set_arch == DOC_ARCH:
        run([sys.executable, "benchmarks/scripts/report.py", "--arch", DOC_ARCH], "report.py")
    else:
        print(f"\n{set_arch} promoted: its result tree, charts and paper artifacts are")
        print("regenerated, and so is the cross-architecture document, which is where")
        print(f"this machine's numbers appear beside {DOC_ARCH}'s. RESULTS.md and README.md")
        print(f"are left alone — they carry one running narrative written about {DOC_ARCH},")
        print("so restating their headline figures from another machine's numbers would")
        print(f"misrepresent them. A section for {set_arch} in that narrative is separate work.")

    # The external-mapper manifest is generated and committed like everything
    # else, but it is generated from a host-local cache rather than from the
    # result set -- so nothing in the cheap tier can see it drift. It did:
    # mapquik's numbers in the paper table were three times too low for months,
    # because the corpus was re-run after a reference fix and never re-exported.
    print("\nchecking the external-mapper manifest against this host's corpus")
    run([sys.executable, "benchmarks/scripts/reference_mappers.py", "--check"],
        "reference_mappers.py --check")

    print("\nverifying that everything regenerates to what is now on disk")
    run([sys.executable, "benchmarks/scripts/charts.py", "--check", "--arch", set_arch],
        "charts.py --check")
    run([sys.executable, "benchmarks/scripts/paper.py", "--check", "--arch", set_arch],
        "paper.py --check")
    run([sys.executable, "benchmarks/scripts/build_pdf.py", "--check", "--arch", set_arch],
        "build_pdf.py --check")
    run([sys.executable, "benchmarks/scripts/crossarch.py", "--check"], "crossarch.py --check")
    run([sys.executable, "benchmarks/scripts/optimizations.py", "--check"],
        "optimizations.py --check")
    run([sys.executable, "benchmarks/scripts/manuscript.py", "--check"],
        "manuscript.py --check")
    run([sys.executable, "benchmarks/scripts/build_paper.py", "--check"],
        "build_paper.py --check")
    if set_arch == DOC_ARCH:
        run([sys.executable, "benchmarks/scripts/report.py", "--check", "--arch", DOC_ARCH],
            "report.py --check")

    if not a.commit and not a.push:
        print("\npromoted. Review `git diff`, then commit; or re-run with --commit.")
        return 0

    subject = a.message or (f"Promote the {man.get('commit', '?')[:12]} result set "
                            f"measured {man.get('finished', '?')[:10]}")
    body = (f"{subject}\n\n"
            f"Result set {src.name}: {man.get('invocations', '?')} invocations on "
            f"{man.get('host', '?')}, {man.get('failures', 0)} failures.\n\n"
            f"RESULTS.md, README.md, the paper artifacts and paper/generated/artifacts.pdf\n"
            f"are all regenerated from it by benchmarks/scripts/promote.py, and every --check\n"
            f"passes against what was written.\n\n"
            f"Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>\n")
    run(["git", "add", "-A",
         "benchmarks/results", "RESULTS.md", "README.md", "paper/generated"], "git add")
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO).returncode
    if staged == 0:
        print("\nnothing changed; the repository already describes this result set")
        return 0
    run(["git", "commit", "-m", body], "git commit")
    if a.push:
        run(["git", "push", "origin", "HEAD"], "git push")
    print("\ndone")
    return 0


if __name__ == "__main__":
    sys.exit(main())
