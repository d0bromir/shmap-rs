#!/usr/bin/env python3
"""The cumulative optimization ladder: what each change is worth, in order.

  ablation.py --measure           run the ladder here and write a result set
  ablation.py --reconcile         measure the C++, the baseline and the shipped
                                  mapper together, to show why the ladder's
                                  end-to-end ratio is not the paper's speedup
  ablation.py                     build paper/generated/figure_ablation.{tex,tsv}
  ablation.py --check             exit 1 if the figure would change
  ablation.py --list              the rungs, in order, with the switch each adds

---------------------------------------------------------------------------
What this measures, and why it is not the table
---------------------------------------------------------------------------
`PORT_CHANGES.md` records what each optimization was worth *against the build
it landed on*, months apart, on different inputs. Those figures are real and
deliberately not refreshed, but they cannot be put on one axis: they do not
share a baseline, a machine, a compiler or a read set, and they do not sum.
Table 1 of the companion note says so in as many words.

This is the other measurement, the one a reader actually wants: one binary,
one machine, one input, one compiler, and a *ladder*. Rung 0 has every
ablatable optimization switched off; each rung to the right switches exactly
one more back on; the last rung is the shipped build. Adjacent rungs differ by
one change and nothing else, so the step between them is that change's worth
*in the presence of the ones before it* — which is the only sense in which the
question has an answer, because these changes interact. (Row 1 removes a
per-worker genome-sized array; how much rows 5 and 6 are then worth depends
entirely on whether that array is still there.)

The switches are one branch, not nine builds: a ladder assembled from nine
separate compilations would measure nine compilations as much as nine changes.
But they are also *not in the shipped mapper*. Instrumenting a hot loop
permanently so that a figure can be drawn is a bad trade -- the mapper is
complex enough, and every branch and buffer that exists only for measurement
is carried by every user forever. So the switches live on
`ARCHIVE_BRANCH`, which is never merged, and this script refuses to run
against a binary that does not have them (see `check_instrumented`). What
reaches `main` is the harness, the recorded ladder and the figure -- the
evidence, not the scaffolding.

---------------------------------------------------------------------------
The order of the rungs
---------------------------------------------------------------------------
The companion note's own layer order — data structure, then the algorithmic
identities, then the parallel decomposition, then the code — which is the
order it argues the changes had to be discovered in. Not the order of largest
effect: choosing that order after seeing the numbers is how a cumulative
ladder is made to say whatever its author wants.

Two of the nine are not rungs, and both absences are reported rather than
estimated:

  * Row 4 (threaded read mapping) is a capability, not a branch. It is the
    difference between the two *series* — the whole ladder is measured at one
    worker and again at `-@N` — so the figure shows it as the gap between the
    curves instead of as a step in one.
  * Row 8 (`PMatches` inline, `Match` borrowing its `Seed`, `lto = "fat"`) is
    type- and build-level. Reversing it is a different binary, so it cannot be
    a switch, and it is stated as not ablated.

---------------------------------------------------------------------------
What makes the numbers checkable
---------------------------------------------------------------------------
1. **Every rung's output is compared byte for byte.** All nine changes claim
   to preserve the mapping exactly; if a rung's PAF differs from rung 0's, the
   ladder is not measuring what it says and the run fails rather than
   reporting. This is a stronger check than the suite's, because it holds the
   input fixed and varies only the optimization.
2. **Repeats are round-robin, reduced by median.** The whole ladder runs, then
   runs again. Measuring one rung five times in a row and then the next would
   let a thermal or scheduler drift over the run land entirely on one rung and
   be read as that optimization's effect.
3. **The inputs are pinned by identity.** Reference and reads are checked
   against `benchmarks/data/datasets.tsv` (bytes, records, bases) before
   anything is measured, exactly as `run.py` does.
4. **The result set is committed.** The figure is regenerated from
   `ladder.tsv` by a pure function — no timestamps, sorted iteration — so
   `--check` is a real equality test and a reader can read the numbers the
   bars are drawn from without a LaTeX toolchain.

Wall time and peak RSS are the whole process, `/usr/bin/time -v`, which is
what a user pays. Indexing is included: two of the rungs are index-side
changes and hiding them behind a mapping-only figure would understate them.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shlex
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout import REPO, RESULTS, arch, resolve_dataset  # noqa: E402
from run import load_registry, load_suite, parse_time_v, rustc_version  # noqa: E402

OUT = REPO / "paper" / "generated"
NAME = "figure_ablation"
# One directory per architecture, for the reason layout.py separates result
# sets: a wall-clock second is a property of the machine that spent it.
SET_ROOT = RESULTS / "ablation"

# Where the run-time switches live. Deliberately not on any branch that merges:
# they exist to be measured once and cited, not to be carried by every user of
# the mapper forever.
ARCHIVE_BRANCH = "archive/ablation-instrumentation"
ARCHIVE_ZIP = ("https://github.com/d0bromir/shmap-rs/archive/refs/heads/"
               "archive/ablation-instrumentation.zip")
ENV = "SHMAP_ABLATE"

# The parameter set the ladder is measured at, and why it is not the paper's:
# see the comment above `[params.ablation]` in suite.toml.
PARAM_SET = "ablation"
METRIC = "Containment"

# `(rung label, SHMAP_ABLATE switch this rung turns back on, PORT_CHANGES row,
#   the `-x` timer that row's "What it optimizes" column names)` in the
# companion note's layer order. Rung 0 is everything off; each entry below adds
# exactly one switch to the ones above it.
#
# The targeted timer matters because at chromosome scale a change worth 40% of
# its own stage can be worth 2% of the wall, which is inside the noise of any
# laptop. The stage is the claim PORT_CHANGES.md actually makes for the row;
# the wall is what a user pays. The ladder reports both and lets neither stand
# in for the other.
LADDER: list[tuple[str, str, int, str]] = [
    ("read-sized buckets", "bucket-array", 1, "bucket_merge"),
    ("streamed seeds", "stream-seeds", 2, "match_seeds"),
    ("refine memo", "refine-memo", 3, "match_rest"),
    ("packed sort key", "packed-sort", 9, "bucket_merge"),
    ("parallel index", "parallel-index", 5, "indexing"),
    ("parallel FASTA", "parallel-fasta", 6, "index_reading"),
    ("sketch loop", "sketch-loop", 7, "index_sketching"),
]
BASELINE = "pre-optimization"

# Every timer kept in the TSV, so a reader can check a row against a stage the
# figure does not draw. `indexing` and `mapping` are wall; the rest are CPU
# summed across workers and must never be divided into them — the same rule
# profiles.tsv states in its own header.
STAGES = ["indexing", "mapping", "index_reading", "index_sketching",
          "index_collecting", "index_finalizing", "query_mapping", "sketching",
          "prepare", "seeding", "match_seeds", "bucket_merge", "match_rest",
          "refine", "match_rest_for_best2"]

# Rows that exist but cannot be a rung, and why. Printed in the provenance and
# the caption: an optimization missing from a figure that claims to account
# for all of them must be visibly missing, not quietly.
NOT_ABLATED: dict[int, str] = {
    4: "threaded read mapping is the gap between the two series, not a step in one",
    8: "type- and build-level (PMatches inline, Match borrows its Seed, lto=fat) — "
       "reversing it is a different binary, not a different branch",
}


def switches_at(i: int) -> list[str]:
    """The switches still ablated at rung `i` (0 = baseline, everything off)."""
    return [s for _, s, _, _ in LADDER[i:]]


def rung_label(i: int) -> str:
    return BASELINE if i == 0 else "+ " + LADDER[i - 1][0]


def rung_row(i: int) -> str:
    return "" if i == 0 else str(LADDER[i - 1][2])


def rung_stage(i: int) -> str:
    return "" if i == 0 else LADDER[i - 1][3]


# ---------------------------------------------------------------------------
# measuring
# ---------------------------------------------------------------------------

def verify_dataset(reg: dict, ds_id: str, full: bool) -> Path:
    """The registry's path for `ds_id`, or exit. Same rule as run.py.

    Benchmarking a quietly regenerated file and attributing the difference to
    code is the failure this system most needs to be incapable of, and it is
    silent. Size always; records and bases on request, because counting them
    costs a full pass.
    """
    if ds_id not in reg:
        sys.exit(f"dataset {ds_id} is not in datasets.tsv")
    e = reg[ds_id]
    p = Path(e["path"])
    if not p.exists():
        sys.exit(f"dataset {ds_id} missing at {p}")
    if str(p.stat().st_size) != e["bytes"]:
        sys.exit(f"dataset {ds_id} changed: registry says {e['bytes']} bytes, "
                 f"file is {p.stat().st_size}. Register a NEW id rather than "
                 f"editing the row — see VERSIONING.md §3.")
    if full:
        records = bases = 0
        with open(p) as fh:
            for line in fh:
                if line.startswith(">"):
                    records += 1
                else:
                    bases += len(line.strip())
        if (str(records), str(bases)) != (e["records"], e["bases"]):
            sys.exit(f"dataset {ds_id}: registry says {e['records']} records / "
                     f"{e['bases']} bases, file has {records} / {bases}")
    return p


def measure(binary: Path, ref: Path, reads: Path, params: list[str],
            threads: int, ablate: list[str], workdir: Path, tag: str) -> dict:
    """One invocation: wall seconds, peak RSS, stage timers, mapping digest.

    `-x` is passed on every rung, never on some — the instrumentation costs a
    little wall clock, and a ladder that paid it on one rung and not the next
    would report that difference as an optimization.
    """
    paf, tf = workdir / f"{tag}.paf", workdir / f"{tag}.time"
    prof = workdir / f"{tag}.json"
    cmd = ["/usr/bin/time", "-v", "-o", str(tf), str(binary),
           "-s", str(ref), "-p", str(reads), *params, "-m", METRIC, "-@", str(threads),
           "-x", "--profile-log", str(prof)]
    env = dict(os.environ)
    if ablate:
        env["SHMAP_ABLATE"] = ",".join(ablate)
    else:
        env.pop("SHMAP_ABLATE", None)
    with open(paf, "w") as fo:
        rc = subprocess.run(cmd, stdout=fo, stderr=subprocess.DEVNULL, env=env).returncode
    if rc != 0:
        sys.exit(f"{tag} exited {rc}: {shlex.join(cmd)}")
    wall, rss = parse_time_v(tf)
    timers = json.loads(prof.read_text())["global"]["timers_secs"]

    # Columns 1-12 only: column 13 is the per-read wall clock, which differs
    # between two runs of the *same* build and would defeat the check this
    # digest exists for.
    h, mapped = hashlib.sha256(), 0
    with open(paf) as fh:
        for line in fh:
            mapped += 1
            h.update("\t".join(line.rstrip("\n").split("\t")[:12]).encode())
            h.update(b"\n")
    for p in (paf, tf, prof):
        p.unlink()
    return dict(wall_s=wall, peak_rss_kb=rss, mapped=mapped, paf_sha=h.hexdigest()[:16],
                stages={s: float(timers.get(s, 0.0)) for s in STAGES})


def check_instrumented(binary: Path, ref: Path, reads: Path) -> None:
    """Refuse a binary whose switches do nothing.

    This is the failure this script most needs to be incapable of. `main`'s
    mapper does not know `SHMAP_ABLATE`, and an unknown environment variable is
    not an error -- it is ignored. So a ladder run against a stock build would
    measure the *same binary* at every rung, succeed, agree byte for byte with
    itself, and draw a flat figure that looks like a finding. Nothing
    downstream could tell that apart from a real result.

    The instrumented build exits 2 on an unknown switch name, so a deliberately
    bogus one separates the two cases in one cheap invocation.
    """
    r = subprocess.run([str(binary), "-s", str(ref), "-p", str(reads), "-k", "25"],
                       capture_output=True, text=True,
                       env={**os.environ, ENV: "__not_a_switch__"})
    if r.returncode == 2 and "unknown switch" in r.stderr:
        return
    sys.exit(
        f"{binary} is not an instrumented build: it ignored {ENV} instead of "
        f"rejecting an unknown switch.\n"
        f"  Every rung would then measure the same binary, agree with itself, and "
        f"draw a flat ladder that looks like a result.\n"
        f"  The switches are deliberately not in the shipped mapper. Build from "
        f"{ARCHIVE_BRANCH}:\n"
        f"    git worktree add /tmp/abl {ARCHIVE_BRANCH} && cargo build --release "
        f"--manifest-path /tmp/abl/Cargo.toml\n"
        f"    python3 benchmarks/scripts/ablation.py --measure "
        f"--binary /tmp/abl/target/release/shmap\n"
        f"  or unpack {ARCHIVE_ZIP}")


def run_ladder(a: argparse.Namespace) -> int:
    suite = load_suite()
    reg = load_registry()
    ref = verify_dataset(reg, a.reference, a.verify_full)
    reads = verify_dataset(reg, a.reads, a.verify_full)
    binary = Path(a.binary) if a.binary else REPO / "target" / "release" / "shmap"
    if not binary.exists():
        sys.exit(f"no binary at {binary}; build one from {ARCHIVE_BRANCH} — see --help")
    check_instrumented(binary, ref, reads)

    p = suite["params"][PARAM_SET]
    params = ["-k", str(p["k"]), "-r", str(p["hashratio"]), "-t", str(p["threshold"]),
              "-d", str(p["min_diff"]), "-o", str(p["max_overlap"])]
    thread_counts = [int(t) for t in a.threads.split(",")]
    workdir = Path(a.workdir).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)

    # Read both inputs once so the first rung does not pay for a cold page
    # cache that every later rung gets for free.
    for f in (ref, reads):
        subprocess.run(["bash", "-c", f"cat {shlex.quote(str(f))} > /dev/null"], check=True)

    t0 = time.time()
    raw: list[dict] = []
    for rep in range(a.repeats):
        for threads in thread_counts:
            for i in range(len(LADDER) + 1):
                off = switches_at(i)
                r = measure(binary, ref, reads, params, threads, off, workdir,
                            f"t{threads}_r{i}_rep{rep}")
                raw.append(dict(threads=threads, rung=i, repeat=rep, **r))
                print(f"  rep{rep} -@{threads} rung {i} {rung_label(i):<22} "
                      f"{r['wall_s']:7.2f}s {r['peak_rss_kb']/1024:8.1f} MB "
                      f"mapped={r['mapped']} {r['paf_sha']}", flush=True)

    # Every rung must produce the same mapping. This is the claim all nine
    # changes make, and the ladder is only a measurement of them if it holds.
    digests = {r["paf_sha"] for r in raw}
    if len(digests) != 1:
        by_rung = {}
        for r in raw:
            by_rung.setdefault(r["paf_sha"], []).append(f"-@{r['threads']} rung {r['rung']}")
        detail = "; ".join(f"{d}: {', '.join(sorted(set(v)))}" for d, v in sorted(by_rung.items()))
        sys.exit(f"OUTPUT CHANGED ACROSS THE LADDER — {len(digests)} distinct mappings.\n"
                 f"  {detail}\n"
                 f"Every switch is supposed to be output-preserving in both positions. "
                 f"Either a switch is wrong or an optimization is; do not publish this.")

    rows = []
    for threads in thread_counts:
        for i in range(len(LADDER) + 1):
            got = [r for r in raw if r["threads"] == threads and r["rung"] == i]
            rows.append(dict(
                threads=threads, rung=i, label=rung_label(i), row=rung_row(i),
                switch="" if i == 0 else LADDER[i - 1][1], stage=rung_stage(i),
                ablated=",".join(switches_at(i)),
                wall_s=round(statistics.median(g["wall_s"] for g in got), 3),
                wall_min_s=round(min(g["wall_s"] for g in got), 3),
                wall_max_s=round(max(g["wall_s"] for g in got), 3),
                peak_rss_kb=int(statistics.median(g["peak_rss_kb"] for g in got)),
                mapped=got[0]["mapped"], paf_sha=got[0]["paf_sha"],
                **{f"s_{s}": round(statistics.median(g["stages"][s] for g in got), 3)
                   for s in STAGES}))

    outdir = SET_ROOT / arch() / "current"
    outdir.mkdir(parents=True, exist_ok=True)
    cols = (["threads", "rung", "label", "row", "switch", "stage", "ablated",
             "wall_s", "wall_min_s", "wall_max_s", "peak_rss_kb", "mapped", "paf_sha"]
            + [f"s_{s}" for s in STAGES])
    with open(outdir / "ladder.tsv", "w") as fo:
        fo.write("# Cumulative optimization ladder — GENERATED by benchmarks/scripts/ablation.py\n")
        fo.write("# rung 0 has every ablatable optimization off; each rung adds one back.\n")
        fo.write("# wall_s, peak_rss_kb and every s_* are medians over the repeats; paf_sha is\n")
        fo.write("# identical on every row by construction (the run fails otherwise).\n")
        fo.write("# s_indexing and s_mapping are WALL; every other s_* is CPU summed across\n")
        fo.write("# workers and will exceed the wall at -@N. Never divide one by the other.\n")
        fo.write(f"# measured with the run-time switches from {ARCHIVE_BRANCH}, which is never\n")
        fo.write("# merged: the shipped mapper carries no ablation code. See manifest.json.\n")
        fo.write("\t".join(cols) + "\n")
        for r in rows:
            fo.write("\t".join(str(r[c]) for c in cols) + "\n")

    git = lambda *xs: subprocess.run(["git", "-C", str(REPO), *xs], capture_output=True,
                                     text=True).stdout.strip()
    manifest = dict(
        schema=1,
        kind="ablation-ladder",
        instrumentation=dict(
            branch=ARCHIVE_BRANCH,
            commit=git("rev-parse", ARCHIVE_BRANCH) or git("rev-parse", f"origin/{ARCHIVE_BRANCH}"),
            zip=ARCHIVE_ZIP,
            note="the run-time switches are not in the shipped mapper; this names the "
                 "never-merged branch the measured binary was built from",
        ),
        host=platform.node(), arch=arch(),
        cpu_model=cpu_model(), cores=os.cpu_count(),
        commit=git("rev-parse", "HEAD"),
        dirty=bool(git("status", "--porcelain")),
        rustc=rustc_version(),
        suite_version=str(suite["suite_version"]),
        params=PARAM_SET, metric=METRIC, param_flags=params,
        threads=thread_counts, repeats=a.repeats, reduce="median",
        datasets={a.reference: reg[a.reference], a.reads: reg[a.reads]},
        identity_verified="bytes+records+bases" if a.verify_full else "bytes",
        binary=subprocess.run([str(binary), "--version"], capture_output=True,
                              text=True).stdout.strip(),
        started=datetime.fromtimestamp(t0, timezone.utc).isoformat(),
        finished=datetime.now(timezone.utc).isoformat(),
        duration_s=round(time.time() - t0, 1),
        invocations=len(raw),
        paf_sha=sorted(digests)[0],
        not_ablated={str(k): v for k, v in sorted(NOT_ABLATED.items())},
    )
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nwrote {outdir}/ladder.tsv ({len(rows)} rows, {len(raw)} invocations, "
          f"{manifest['duration_s']:.0f}s)")
    return 0


def cpu_model() -> str:
    """The CPU as /proc names it — the machine is part of the measurement."""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


# ---------------------------------------------------------------------------
# reconciling the ladder with the C++ comparison
# ---------------------------------------------------------------------------

# The four measurements that explain why the ladder's end-to-end ratio is
# smaller than the paper's headline speedup over the C++. Order is the order
# they are divided into each other.
RECONCILE = [
    ("cpp", "the C++ at the pinned commit"),
    ("baseline", "the port with every ablatable optimization switched off"),
    ("instrumented", "the same build with none switched off"),
    ("shipped", "the released mapper, which carries no ablation code at all"),
    ("shipped_x", "the released mapper again, with -x, as the ladder runs it"),
]

# Optional fifth row: the released source built with Cargo's stock release
# profile instead of this one's `lto = "fat", codegen-units = 1`. It answers
# the obvious cheap explanation for the port step -- "the Rust is just built
# harder" -- with a number instead of an opinion. Optional because it needs a
# second build of the same source, which only matters when someone asks.
UNTUNED = ("untuned", "the released source at Cargo's default release profile")


def run_reconcile(a: argparse.Namespace) -> int:
    """Measure the C++, the ladder's baseline and the shipped mapper together.

    A reader who takes the note at its word hits an apparent contradiction:
    the abstract claims the port is 2-3x the C++, and the ladder is worth well
    under that end to end. Both are right, and the reason is that the ladder's
    baseline was never the C++ -- it is the *port* with seven changes switched
    off, still carrying row 8 and every port-level difference that is not a
    numbered optimization at all.

    So the two figures compose rather than compete, and they compose by
    multiplication, not addition:

        C++/shipped  =  C++/baseline  x  baseline/shipped

    the second factor being the whole ladder. Measuring all of it here, on one
    input in one sitting, is what turns that from an excuse into an arithmetic
    identity a reviewer can check -- the product has to come back to the
    directly measured total, and it is printed so that it visibly does.

    `-x` is passed to none of them. The ladder uses it on every rung, where it
    cancels; the C++ has no equivalent, so including it on one side of *this*
    comparison would be measuring the instrumentation.

    `instrumented` exists to price the scaffolding itself: it is the ablation
    build with no switch set, so `instrumented/shipped` is what merely carrying
    the switches costs. If that were not ~1 the ladder's rungs would all be
    tilted and the whole exercise would be suspect.
    """
    suite = load_suite()
    reg = load_registry()
    ref = verify_dataset(reg, a.reference, a.verify_full)
    reads = verify_dataset(reg, a.reads, a.verify_full)

    cpp = Path(os.path.expanduser(a.cpp))
    instrumented = Path(a.binary) if a.binary else Path("/tmp/abl/target/release/shmap")
    shipped = Path(a.shipped) if a.shipped else REPO / "target" / "release" / "shmap"
    for label, p in (("--cpp", cpp), ("--binary", instrumented), ("--shipped", shipped)):
        if not p.exists():
            sys.exit(f"{label}: no binary at {p}")
    check_instrumented(instrumented, ref, reads)
    check_no_tracy(cpp)

    p = suite["params"][PARAM_SET]
    params = ["-k", str(p["k"]), "-r", str(p["hashratio"]), "-t", str(p["threshold"]),
              "-d", str(p["min_diff"]), "-o", str(p["max_overlap"]), "-m", METRIC]
    workdir = Path(a.workdir).expanduser()
    workdir.mkdir(parents=True, exist_ok=True)
    for f in (ref, reads):
        subprocess.run(["bash", "-c", f"cat {shlex.quote(str(f))} > /dev/null"], check=True)

    # The C++ is single-threaded by design, so every row here is one worker.
    plan = {
        "cpp": (cpp, None, []),
        "baseline": (instrumented, ",".join(switches_at(0)), ["-@", "1"]),
        "instrumented": (instrumented, None, ["-@", "1"]),
        "shipped": (shipped, None, ["-@", "1"]),
        # The ladder runs every rung under `-x`; the C++ cannot. Measuring the
        # same binary both ways prices that difference, because the figure
        # puts a C++ bar with no profiling next to rungs that have it.
        "shipped_x": (shipped, None, ["-@", "1", "-x", "--profile-log",
                                     str(Path(a.workdir).expanduser() / "rec_x.json")]),
    }
    order = list(RECONCILE)
    if a.untuned:
        untuned = Path(a.untuned)
        if not untuned.exists():
            sys.exit(f"--untuned: no binary at {untuned}")
        plan["untuned"] = (untuned, None, ["-@", "1"])
        order.append(UNTUNED)
    raw: dict[str, list[tuple[float, int, int]]] = {k: [] for k in plan}
    for rep in range(a.repeats):
        for key, (binary, ablate, extra) in plan.items():
            tf = workdir / f"rec_{key}_{rep}.time"
            paf = workdir / f"rec_{key}_{rep}.paf"
            env = dict(os.environ)
            env.pop(ENV, None)
            if ablate:
                env[ENV] = ablate
            with open(paf, "w") as fo:
                rc = subprocess.run(["/usr/bin/time", "-v", "-o", str(tf), str(binary),
                                     "-s", str(ref), "-p", str(reads), *params, *extra],
                                    stdout=fo, stderr=subprocess.DEVNULL, env=env).returncode
            if rc != 0:
                sys.exit(f"{key} exited {rc}")
            wall, rss = parse_time_v(tf)
            mapped = sum(1 for _ in open(paf))
            raw[key].append((wall, rss, mapped))
            paf.unlink(); tf.unlink()
            print(f"  rep{rep} {key:13} {wall:7.2f}s {rss/1024:8.1f} MB mapped={mapped}",
                  flush=True)

    med = {k: (statistics.median(w for w, _, _ in v),
               statistics.median(r for _, r, _ in v),
               v[0][2]) for k, v in raw.items()}
    total = med["cpp"][0] / med["shipped"][0]
    port = med["cpp"][0] / med["baseline"][0]
    ladder = med["baseline"][0] / med["shipped"][0]
    overhead = med["instrumented"][0] / med["shipped"][0]
    profiling = med["shipped_x"][0] / med["shipped"][0]
    tuning = med["untuned"][0] / med["shipped"][0] if "untuned" in med else None

    outdir = SET_ROOT / arch() / "current"
    outdir.mkdir(parents=True, exist_ok=True)
    cols = ["which", "what", "wall_s", "wall_min_s", "wall_max_s", "peak_rss_mb", "mapped"]
    with open(outdir / "reconcile.tsv", "w") as fo:
        fo.write("# Why the ladder's end-to-end ratio is not the paper's C++ speedup.\n")
        fo.write("# GENERATED by benchmarks/scripts/ablation.py --reconcile\n")
        fo.write("# One worker throughout (the C++ is single-threaded by design). No -x on the\n")
        fo.write("# rows the ratios are built from: the C++ has no equivalent, so profiling one\n")
        fo.write("# side would measure it. `shipped_x` is the exception and exists only to price\n")
        fo.write("# that difference, because the figure puts an unprofiled C++ bar next to rungs\n")
        fo.write("# the ladder does profile.\n")
        fo.write(f"# C++/shipped = {total:.3f} = (C++/baseline = {port:.3f}) x "
                 f"(baseline/shipped = {ladder:.3f}), the second factor being the whole ladder.\n")
        fo.write(f"# instrumented/shipped = {overhead:.3f} — what merely carrying the switches costs.\n")
        fo.write("\t".join(cols) + "\n")
        for key, what in order:
            w, r, m = med[key]
            fo.write(f"{key}\t{what}\t{w:.3f}\t{min(x for x, _, _ in raw[key]):.3f}\t"
                     f"{max(x for x, _, _ in raw[key]):.3f}\t{r/1024:.1f}\t{m}\n")

    (outdir / "reconcile.json").write_text(json.dumps(dict(
        schema=1, kind="ablation-reconciliation",
        host=platform.node(), arch=arch(), cpu_model=cpu_model(),
        repeats=a.repeats, reduce="median", threads=1, profiling=False,
        params=PARAM_SET, param_flags=params,
        datasets={a.reference: reg[a.reference], a.reads: reg[a.reads]},
        binaries={"cpp": str(cpp), "cpp_sha256": sha256_file(cpp),
                  "instrumented": str(instrumented), "shipped": str(shipped)},
        ratios=dict(total=round(total, 4), port=round(port, 4), ladder=round(ladder, 4),
                    instrumentation_overhead=round(overhead, 4),
                    profiling_overhead=round(profiling, 4),
                    **({"build_tuning": round(tuning, 4)} if tuning else {})),
        rows=[dict(which=k, what=w, wall_s=round(med[k][0], 3),
                   peak_rss_mb=round(med[k][1] / 1024, 1), mapped=med[k][2])
              for k, w in order],
        measured=datetime.now(timezone.utc).isoformat(),
    ), indent=2) + "\n")

    print(f"\n  C++ / shipped   = {total:.2f}x   (measured directly)")
    print(f"  C++ / baseline  = {port:.2f}x   (everything the ladder cannot switch off)")
    print(f"  baseline / ship = {ladder:.2f}x   (the whole ladder)")
    print(f"  product         = {port * ladder:.2f}x   (must equal the first line)")
    print(f"  instrumentation overhead = {overhead:.2f}x")
    print(f"  profiling (-x) overhead  = {profiling:.2f}x")
    if tuning:
        print(f"  build tuning (lto/codegen-units) = {tuning:.2f}x")
    print(f"\nwrote {outdir}/reconcile.tsv")
    return 0


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def check_no_tracy(binary: Path) -> None:
    """Refuse a C++ built with Tracy, as run.py does.

    Upstream's Makefile adds it by default and it costs ~8.8%, which would
    flatter every ratio here.
    """
    out = subprocess.run(["bash", "-c",
                          f"strings {shlex.quote(str(binary))} | "
                          f"grep -cE 'TracyClient|Tracy Profiler|tracy_[a-z]|__tracy'"],
                         capture_output=True, text=True)
    if int(out.stdout.strip() or 0) > 10:
        sys.exit(f"{binary} has live Tracy symbols — rebuild without -DTRACY_ENABLE")


# ---------------------------------------------------------------------------
# the figure
# ---------------------------------------------------------------------------

def committed_arches() -> list[str]:
    """Architectures that have a committed ladder, in a stable order."""
    if not SET_ROOT.is_dir():
        return []
    return sorted(p.name for p in SET_ROOT.iterdir()
                  if (p / "current" / "ladder.tsv").exists())


def load_ladder(a: str | None = None) -> tuple[list[dict], dict, Path]:
    """The committed ladder, which is one measurement rather than one per host.

    Result sets are filed per architecture because a wall-clock second belongs
    to the machine that spent it. But the paper carries *one* ablation figure,
    and everything derived from it -- the figure, `--check`, the self-test --
    has to give the same answer wherever it runs. Resolving on `arch()` alone
    made all three fail on any machine that had not measured a ladder itself,
    which is how a green x86_64 job and a red aarch64 job could disagree about
    a file neither of them changed.

    So: the architecture asked for, else this machine's if it has one, else the
    only one there is. Two committed ladders and no `--arch` is genuinely
    ambiguous and says so rather than picking.
    """
    have = committed_arches()
    if a is None:
        if arch() in have:
            a = arch()
        elif len(have) == 1:
            a = have[0]
        elif have:
            sys.exit(f"several committed ablation ladders ({', '.join(have)}); "
                     f"name one with --arch")
    d = SET_ROOT / (a or arch()) / "current"
    t, m = d / "ladder.tsv", d / "manifest.json"
    if not t.exists() or not m.exists():
        sys.exit(f"no ablation result set in {d}; measure one with:\n"
                 f"  python3 benchmarks/scripts/ablation.py --measure")
    with open(t) as fh:
        rows = list(csv.DictReader((l for l in fh if not l.startswith("#")), delimiter="\t"))
    for r in rows:
        for k in ("threads", "rung", "peak_rss_kb", "mapped"):
            r[k] = int(r[k])
        for k in ("wall_s", "wall_min_s", "wall_max_s"):
            r[k] = float(r[k])
        for k in [f"s_{s}" for s in STAGES]:
            r[k] = float(r.get(k, 0.0) or 0.0)
    return rows, json.loads(m.read_text()), d


def digest(d: Path) -> str:
    h = hashlib.sha256()
    for name in ("ladder.tsv", "manifest.json"):
        h.update(name.encode())
        h.update((d / name).read_bytes())
    return h.hexdigest()[:16]


def reconciliation(a: str | None = None) -> dict | None:
    """The C++-vs-ladder decomposition, if one has been measured.

    Optional on purpose: it needs a C++ binary, which most machines running
    this will not have. Absent, the figure and its provenance simply do not
    make the claim.
    """
    have = committed_arches()
    if a is None:
        a = arch() if arch() in have else (have[0] if len(have) == 1 else arch())
    p = SET_ROOT / a / "current" / "reconcile.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def tex_escape(s: str) -> str:
    for a, b in (("\\", ""), ("&", r"\&"), ("#", r"\#"), ("%", r"\%"),
                 ("$", r"\$"), ("_", r"\_"), ("{", r"\{"), ("}", r"\}")):
        s = s.replace(a, b)
    return s


def header(man: dict, dig: str, comment: str) -> list[str]:
    return [
        f"{comment} GENERATED by benchmarks/scripts/ablation.py -- do not edit.",
        f"{comment} artifact:   {NAME} (figure)",
        f"{comment} result set: benchmarks/results/ablation/{man['arch']}/current",
        f"{comment} commit:     {man.get('commit', '?')[:12]}"
        + ("  (working tree dirty)" if man.get("dirty") else ""),
        f"{comment} host:       {man.get('host', '?')} ({man.get('cores', '?')} cores)",
        f"{comment} measured:   {man.get('finished', '?')[:10]}",
        f"{comment} inputs:     sha256:{dig}",
        f"{comment} provenance: paper/generated/ABLATION.md",
    ]


# Two series, one per thread count. Distinguished by fill density as well as
# hue, because the note is printed in greyscale as often as not and two bars
# that differ only in colour are then the same bar.
SERIES = [("blue!70!black", 25), ("orange!85!black", 70)]

# The C++ bar. A third hue, because it is not a rung of the ladder but the
# thing the ladder is measured away from.
CPP_COLOUR = "black!55"
CPP_LABEL = "the C++"


def decade_ticks(lo: float, hi: float) -> list[str]:
    """A 1-2-5 tick sequence covering `[lo, hi]`, as plain integers.

    pgfplots' own log ticks read `10^{2.5}`, which is unreadable as a number
    of megabytes. These are the values a reader would write down.
    """
    out: list[str] = []
    mag = 1.0
    while mag <= hi * 10:
        for m in (1, 2, 5):
            v = mag * m
            if lo * 0.75 <= v <= hi * 1.4:
                out.append(f"{v:g}")
        mag *= 10
    return out or [f"{lo:g}", f"{hi:g}"]


def axis(rows: list[dict], threads: list[int], key: str, scale: float,
         ylabel: str, spread: bool = False, log: bool = False,
         lead: tuple[str, float] | None = None) -> list[str]:
    """One panel, as its own tikzpicture: rung on x, `key` on y, one bar per
    thread count.

    `spread` draws each bar's min..max over the repeats as an error bar. It is
    on for wall clock and off for peak RSS because only one of the two is
    noisy: a step whose bar overlaps its neighbour's whiskers has not been
    resolved by this host, and a reader has to be able to see that rather than
    be told the median and left to assume it.

    `log` is for peak RSS, where the two series differ by an order of
    magnitude at the baseline and by a few percent everywhere else. On a
    linear axis the one-worker series is a flat line under the other's first
    bar, which reads as "row 1 does nothing at one worker" -- the opposite of
    what was measured. A log axis is read as ratios, which is what this panel
    is about.

    `lead` is the C++ itself, drawn at x = -1 as the bar the whole ladder
    starts from. Without it the figure silently begins at the *port* and the
    largest single step in the comparison -- getting there -- is off the left
    edge. It joins the narrow series only: the C++ has no thread count.
    """
    labels = ([tex_escape(lead[0])] if lead else []) + \
             [tex_escape(r["label"]) for r in rows if r["threads"] == threads[0]]
    vals = [r[key] * scale for r in rows] + ([lead[1]] if lead else [])
    # Explicit rather than `xtick=data`: the C++ is a one-point plot, and
    # taking tick positions from it leaves every rung unlabelled.
    ticks = list(range(-1 if lead else 0, len(labels) - (1 if lead else 0)))
    out = [
        r"\begin{tikzpicture}",
        r"\begin{axis}[",
        r"  width=0.50\textwidth, height=3.6cm,",
        r"  ybar, bar width=4pt,",
        *((rf"  ymode=log, log origin=infty, ymin={min(vals) * 0.55:.4g}, "
           rf"ymax={max(vals) * 2.2:.4g},",
           r"  ytick={" + ",".join(decade_ticks(min(vals), max(vals))) + r"},",
           r"  yticklabels={" + ",".join(decade_ticks(min(vals), max(vals))) + r"},")
          if log else (r"  ymin=0,",)),
        r"  xtick={" + ",".join(str(t) for t in ticks) + "},",
        r"  xticklabels={" + ",".join("{" + l + "}" for l in labels) + "},",
        r"  x tick label style={rotate=35, anchor=east, font=\tiny},",
        r"  y tick label style={font=\tiny}, ylabel style={font=\scriptsize},",
        r"  ylabel={" + ylabel + "},",
        r"  enlarge x limits=0.07, ymajorgrids, grid style={gray!25},",
        r"  legend style={font=\tiny, draw=none, fill=none, at={(0.97,0.97)},",
        r"                anchor=north east, legend columns=1},",
        r"]",
    ]
    if lead:
        out += [rf"\addplot+[draw={CPP_COLOUR}, fill={CPP_COLOUR}!45] "
                rf"coordinates {{(-1,{lead[1]:.4g})}};",
                r"\addlegendentry{the C++}"]
    for (colour, density), t in zip(SERIES, threads):
        series = [r for r in rows if r["threads"] == t]
        if spread:
            pts = " ".join(
                f"({r['rung']},{r[key] * scale:.4g}) "
                f"+- (0,{max(0.0, r['wall_max_s'] - r[key]) * scale:.4g}) "
                f"-= (0,{max(0.0, r[key] - r['wall_min_s']) * scale:.4g})"
                for r in series)
            style = (rf"draw={colour}, fill={colour}!{density}, "
                     r"error bars/.cd, y dir=both, y explicit, "
                     r"error bar style={gray!60, line width=0.4pt}")
        else:
            pts = " ".join(f"({r['rung']},{r[key] * scale:.4g})" for r in series)
            style = rf"draw={colour}, fill={colour}!{density}"
        out += [rf"\addplot+[{style}] coordinates {{{pts}}};",
                rf"\addlegendentry{{\texttt{{-@{t}}}}}"]
    out += [r"\end{axis}", r"\end{tikzpicture}"]
    return out


def build(rows: list[dict], man: dict) -> tuple[str, list[str], list[list]]:
    threads = sorted({r["threads"] for r in rows})
    ordered = sorted(rows, key=lambda r: (r["threads"], r["rung"]))

    # The C++ bar, when a reconciliation has been measured. The ladder starts
    # at the port, so without this the figure omits the largest step in the
    # whole comparison -- which is the one a reader most wants to see.
    rec = reconciliation(man.get("arch"))
    lead_wall = lead_rss = None
    if rec:
        cpp = next((r for r in rec.get("rows", []) if r["which"] == "cpp"), None)
        if cpp:
            lead_wall = (CPP_LABEL, cpp["wall_s"])
            lead_rss = (CPP_LABEL, cpp["peak_rss_mb"])

    body = axis(ordered, threads, "wall_s", 1.0, r"wall (s)", spread=True, lead=lead_wall)
    body += [r"\hfill"]
    body += axis(ordered, threads, "peak_rss_kb", 1 / 1024.0,
                 r"peak RSS (MB, log)", log=True, lead=lead_rss)

    cols = (["threads", "rung", "label", "port_changes_row", "switch_enabled",
             "still_ablated", "wall_s", "wall_min_s", "wall_max_s", "peak_rss_mb",
             "speedup_vs_rung0", "rss_ratio_vs_rung0", "step_wall_pct",
             "targeted_stage", "stage_s_prev_rung", "stage_s", "step_stage_pct",
             "mapped", "paf_sha"]
            + [f"s_{s}" for s in STAGES])
    data: list[list] = []
    for t in threads:
        series = [r for r in ordered if r["threads"] == t]
        base = series[0]
        for i, r in enumerate(series):
            prev = series[i - 1] if i else None
            stage = r["stage"]
            sk = f"s_{stage}" if stage else ""
            data.append([
                t, r["rung"], r["label"], r["row"] or "", r["switch"], r["ablated"],
                f"{r['wall_s']:.3f}", f"{r['wall_min_s']:.3f}", f"{r['wall_max_s']:.3f}",
                f"{r['peak_rss_kb'] / 1024:.1f}",
                f"{base['wall_s'] / r['wall_s']:.3f}" if r["wall_s"] else "",
                f"{base['peak_rss_kb'] / r['peak_rss_kb']:.3f}" if r["peak_rss_kb"] else "",
                "" if prev is None else f"{100.0 * (prev['wall_s'] - r['wall_s']) / prev['wall_s']:.1f}",
                stage,
                "" if prev is None or not sk else f"{prev[sk]:.3f}",
                "" if not sk else f"{r[sk]:.3f}",
                "" if prev is None or not sk or not prev[sk]
                else f"{100.0 * (prev[sk] - r[sk]) / prev[sk]:.1f}",
                r["mapped"], r["paf_sha"],
                *[f"{r[f's_{s}']:.3f}" for s in STAGES],
            ])
    return "\n".join(body), cols, data


def caption(rows: list[dict], man: dict) -> str:
    threads = sorted({r["threads"] for r in rows})
    rec = reconciliation(man.get("arch"))
    return (
        r"Every optimization, put back one at a time. The leftmost bar is the C++ itself; "
        r"\emph{" + tex_escape(BASELINE) + r"} is this port with every ablatable change "
        r"switched off (\texttt{SHMAP\_ABLATE}), so the step between those two is the port "
        r"and nothing else --- the largest step in the figure, and the one the layered story "
        r"does not cover. Each rung to the right switches exactly one more change back on, so "
        r"adjacent rungs differ by one change and nothing else. The two series are the two "
        r"thread counts, and the gap between them is row~4; row~8 is not ablatable, and the "
        r"C++ is single-threaded so it has no \texttt{-@N} counterpart. Left: wall clock, "
        r"median over " + str(man.get("repeats", "?")) + r" round-robin runs of the whole "
        r"ladder, whiskers at min--max, so an unresolved step is visible as one. Right: peak "
        r"resident set, log scale --- one worker's genome-sized accumulator hides under "
        r"the index-build peak, \texttt{-@" + str(threads[-1]) + r"} workers' copies do "
        r"not. Every rung's mapping is byte-identical; the run fails otherwise. The C++ bar "
        r"carries no \texttt{-x} (the C++ has no equivalent) while the rungs do, costing "
        + (f"{rec['ratios']['profiling_overhead']:.2f}" if rec else "a few percent")
        + r"$\times$, so the first step is understated rather than flattered. Numbers and "
        r"per-stage timers in \texttt{" + tex_escape(NAME) + r".tsv}; the decomposition "
        r"against the C++ in \texttt{reconcile.tsv}."
    )


def render(rows: list[dict], man: dict, dig: str) -> dict[str, str]:
    body, cols, data = build(rows, man)
    tex = "\n".join([
        *header(man, dig, "%"), "%",
        r"\begin{figure*}[!t]", r"\centering", body,
        r"\caption{" + caption(rows, man) + "}",
        r"\label{fig:ablation}", r"\end{figure*}", "",
    ])
    tsv = [*header(man, dig, "#"), "\t".join(cols)]
    for row in data:
        tsv.append("\t".join(str(v) for v in row))
    return {f"{NAME}.tex": tex, f"{NAME}.tsv": "\n".join(tsv) + "\n"}


def provenance(rows: list[dict], man: dict, dig: str) -> str:
    threads = sorted({r["threads"] for r in rows})
    ds = man.get("datasets", {})
    out = [
        "# Provenance of the cumulative ablation ladder",
        "",
        "GENERATED by `benchmarks/scripts/ablation.py` — do not edit.",
        "",
        "| | |",
        "|---|---|",
        f"| result set | `benchmarks/results/ablation/{man['arch']}/current` |",
        f"| host | `{man.get('host')}` — {man.get('cpu_model')}, {man.get('cores')} cores |",
        f"| commit | `{man.get('commit', '?')[:12]}`"
        + (" **(working tree dirty)**" if man.get("dirty") else "") + " |",
        f"| rustc | `{man.get('rustc')}` |",
        f"| measured | {man.get('finished', '?')[:10]} |",
        f"| parameters | suite.toml `params.{man.get('params')}`: "
        f"`{' '.join(man.get('param_flags', []))}`, metric `{man.get('metric')}` |",
        f"| thread counts | {', '.join(f'`-@{t}`' for t in threads)} |",
        f"| repeats | {man.get('repeats')}, round-robin over the whole ladder, reduced by {man.get('reduce')} |",
        f"| inputs verified | {man.get('identity_verified')} against `benchmarks/data/datasets.tsv` |",
        f"| mapping digest | `{man.get('paf_sha')}` — identical on every rung |",
        f"| input digest | `sha256:{dig}` |",
        "",
        "## Inputs",
        "",
        "| id | rel path | bytes | records | bases |",
        "|---|---|---|---|---|",
    ]
    for k in sorted(ds):
        e = ds[k]
        out.append(f"| `{k}` | `{e.get('rel', '')}` | {e.get('bytes')} | "
                   f"{e.get('records')} | {e.get('bases')} |")
    out += [
        "",
        "## Rungs",
        "",
        "Cumulative and left to right: rung 0 has every ablatable optimization off, and each",
        "rung switches exactly one more back on.",
        "",
        "| rung | label | PORT_CHANGES row | switch turned back on | stage that row targets |",
        "|---|---|---|---|---|",
        "| 0 | " + BASELINE + " | — | — (all off) | — |",
    ]
    for i, (label, sw, row, stage) in enumerate(LADDER, 1):
        out.append(f"| {i} | + {label} | {row} | `{sw}` | `{stage}` |")
    out += [
        "",
        "## Not on the ladder",
        "",
        "An optimization missing from a figure that claims to account for all of them has to be",
        "visibly missing rather than quietly.",
        "",
    ]
    for row, why in sorted(NOT_ABLATED.items()):
        out.append(f"- **Row {row}** — {why}.")

    rec = reconciliation()
    if rec:
        r = rec["ratios"]
        out += [
            "",
            "## Why this is not the paper's speedup over the C++",
            "",
            "The ladder is worth less end to end than the note's headline comparison against the",
            "C++, and the reason is that **the ladder's baseline was never the C++**: it is the",
            "*port* with the ablatable changes switched off. The C++ is the leftmost bar of the",
            "figure, and the step from it to that baseline is the single largest one in the whole",
            "comparison -- larger than the entire ladder to its right. The two compose by",
            "multiplication:",
            "",
            "```",
            f"  C++ / shipped   = {r['total']:.2f}x      measured directly",
            f"  C++ / baseline  = {r['port']:.2f}x      the port itself: row 8 plus the rewrite",
            f"  baseline / ship = {r['ladder']:.2f}x      the whole ladder",
            f"                    {r['port']:.2f} x {r['ladder']:.2f} = {r['port'] * r['ladder']:.2f}x",
            "```",
            "",
            "Measured together, on one input in one sitting, by `ablation.py --reconcile`; the",
            "numbers are in `reconcile.tsv` beside the ladder. The product has to come back to the",
            "directly measured total, which is what makes this an arithmetic identity a reviewer",
            "can check rather than an excuse.",
            "",
            "**What is in that first step, and what is not.** Row 8 -- matches borrowing their",
            "seeds, the single-occurrence case stored inline, `lto`, the bounded read-ahead -- is a",
            "numbered change but a type- and build-level one, so it cannot be a rung. The rest is",
            "the port as such: a different language, allocator and standard library. This does not",
            "separate the two, and doing so would need type surgery rather than a switch. What it",
            "does rule out is the cheap explanation:",
            "",
            f"- build settings are worth {r.get('build_tuning', float('nan')):.2f}x"
            if "build_tuning" in r else
            "- build settings were not measured on this run",
            "  (`untuned`: the same source at Cargo's stock release profile, without fat LTO or",
            "  single-codegen builds), so the first bar is not the Rust merely being built harder.",
            "",
            f"Carrying the switches at all costs {r['instrumentation_overhead']:.2f}x",
            "(`instrumented` over `shipped`). That is small enough that the rungs are not",
            "meaningfully tilted by the instrumentation, and large enough to be one more reason",
            "the switches are not in the shipped mapper.",
            "",
            "One worker throughout, because the C++ is single-threaded by design. No `-x` on the",
            "rows the ratios are built from -- the C++ has no equivalent, so profiling one side",
            "would be measuring the instrumentation instead of the program. The ladder itself does",
            "use `-x` on every rung, where it cancels; `shipped_x` prices exactly that difference,",
            f"at {r.get('profiling_overhead', float('nan')):.2f}x, because the figure puts an",
            "unprofiled C++ bar next to rungs that are profiled. It biases the first step",
            "*downwards*, so the port is understated there rather than flattered.",
        ]
    out += [
        "",
        "## Read with",
        "",
        "- A step is the change's worth **given every change to its left**, not in isolation.",
        "  These interact: how much the parallel index is worth depends on whether each worker",
        "  is still carrying a genome-sized accumulator. Steps therefore do not commute and do",
        "  not sum to the end-to-end ratio in any meaningful way beyond arithmetic.",
        "- Not comparable with `PORT_CHANGES.md`'s per-row effects, which were each measured",
        "  against the build that change landed on, months apart and on other inputs. That is",
        "  the reason this exists.",
        "- **The wall column and the stage column answer different questions.** A change that",
        "  removes 40% of the stage it targets can be worth 2% of a run that stage is 5% of.",
        "  Both are in the `.tsv` (`step_wall_pct`, `step_stage_pct`); the first is what a user",
        "  pays and the second is the claim `PORT_CHANGES.md` makes for the row. Neither stands",
        "  in for the other, and a step whose wall column is inside `wall_min_s`..`wall_max_s`",
        "  of its neighbour has not been resolved by this host and should be read from the stage.",
        "- **The two parallelism rungs are no-ops in the one-worker series, by construction.**",
        "  `parallel-index` and `parallel-fasta` both take the serial path at `-@1` whether the",
        "  switch is on or off, so those two steps measure nothing there and their sign is the",
        "  host's noise. They are kept in that series rather than blanked so that both series",
        "  have the same rungs in the same order, which is what makes them comparable.",
        "- `s_indexing` and `s_mapping` are wall clock; every other `s_*` is CPU summed across",
        "  workers, so at `-@N` they exceed the wall. Never divide one by the other.",
        "- Peak RSS is the whole process, so at one worker the accumulator sits under the",
        "  index-build peak and row 1 looks free. It is not: the array is per worker, which is",
        f"  what the `-@{threads[-1]}` series shows.",
        "- This is a chromosome-scale reference on a laptop, not the whole-genome suite, and at",
        "  a higher sampling rate than the paper's headline numbers (see `[params.ablation]` in",
        "  suite.toml for why). It measures the *relative* worth of the changes on one input",
        "  under a single-variable protocol; the absolute end-to-end figures the paper quotes",
        "  come from the promoted suite result sets on the benchmark hosts.",
        "",
    ]
    return "\n".join(out)


def build_all(a: str | None = None) -> tuple[dict[str, str], dict]:
    rows, man, d = load_ladder(a)
    dig = digest(d)
    files = render(rows, man, dig)
    files["ABLATION.md"] = provenance(rows, man, dig)
    return files, man


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--measure", action="store_true", help="run the ladder and write a result set")
    ap.add_argument("--reconcile", action="store_true",
                    help="measure C++ vs the ladder's baseline vs the shipped mapper")
    ap.add_argument("--cpp", default="~/Pesho/shmap/release/shmap",
                    help="the C++ binary, for --reconcile")
    ap.add_argument("--shipped", help="the released mapper, for --reconcile "
                                     "(default: target/release/shmap)")
    ap.add_argument("--untuned", help="the same source at Cargo's default release profile, "
                                     "to price this repo's lto/codegen-units settings")
    ap.add_argument("--check", action="store_true", help="exit 1 if the figure would change")
    ap.add_argument("--list", action="store_true", help="print the rungs and stop")
    ap.add_argument("--reference", default="REF-CHR21", help="dataset id of the reference")
    ap.add_argument("--reads", default="LOC-CHR21SIM12K", help="dataset id of the reads")
    ap.add_argument("--threads", default="1,8", help="comma-separated thread counts (two series)")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--binary", help="mapper to measure (default: target/release/shmap)")
    ap.add_argument("--workdir", default="~/ablation-work", help="scratch for PAFs, deleted as it goes")
    ap.add_argument("--verify-full", action="store_true",
                    help="check records and bases too, not just size")
    ap.add_argument("--arch", help="which committed ladder to draw (default: this machine's, "
                                  "or the only one committed)")
    ap.add_argument("--out", help=f"output directory (default: {OUT})")
    a = ap.parse_args()

    if a.list:
        print(f"rung 0  {BASELINE:<24} (every ablatable optimization off)")
        for i, (label, sw, row, stage) in enumerate(LADDER, 1):
            print(f"rung {i}  + {label:<22} row {row}, switch {sw!r}, stage {stage!r}")
        for row, why in sorted(NOT_ABLATED.items()):
            print(f"not a rung: row {row} — {why}")
        return 0

    if a.measure:
        return run_ladder(a)

    if a.reconcile:
        return run_reconcile(a)

    files, man = build_all(a.arch)
    out = Path(a.out) if a.out else OUT

    if a.check:
        stale = [n for n, body in sorted(files.items())
                 if not (out / n).exists() or (out / n).read_text() != body]
        if stale:
            print(f"ablation figure out of date in {out}: {', '.join(stale)}\n"
                  f"regenerate with: python3 benchmarks/scripts/ablation.py", file=sys.stderr)
            return 1
        print(f"ablation figure is current with {man['host']} ({man['arch']})")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    for n, body in sorted(files.items()):
        (out / n).write_text(body)
    print(f"wrote {NAME}.{{tex,tsv}} and ABLATION.md to {out} "
          f"from {man['host']} ({man['arch']}, {man['finished'][:10]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
