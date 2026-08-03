#!/usr/bin/env python3
"""Run the shmap-rs benchmark suite on the benchmark host.

Executes on `a2`, never on a maintainer's laptop and never as a GitHub
self-hosted runner — see ../SECURITY.md for why.

  run.py --commit <sha>      measure a commit already trusted (e.g. main)
  run.py --pr <n>            measure a pull request, subject to authorization
  run.py --status            report whether a run is in progress
  run.py --dry-run           print the planned invocations and exit

Guarantees:
  * At most one run at a time, host-wide, via an exclusive flock held for the
    whole run. Concurrent callers queue unless --no-wait.
  * A PR is built only if its author has push/admin, or a push/admin user
    labelled it `bench-approved` AND the approved head SHA still matches.
  * Every dataset's identity triple is verified against the registry before
    anything is measured.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
LOCKFILE = Path.home() / ".shmap-bench.lock"
WORKROOT = Path.home() / "bench-work"
# Where result sets are written on the benchmark host — deliberately not
# inside the checkout; see the comment at `default_out`.
RESULTS_ROOT = Path(os.environ.get("SHMAP_BENCH_RESULTS",
                                   str(Path.home() / "bench-results")))
APPROVAL_LABEL = "bench-approved"
GH = os.environ.get("GH_BIN", "gh")


# --------------------------------------------------------------------------
# concurrency
# --------------------------------------------------------------------------

class HostLock:
    """Exclusive, host-wide, released by the kernel if we die.

    Two benchmarks sharing 64 cores would contaminate each other's timings and
    produce results that are wrong rather than obviously broken, so this is a
    correctness mechanism, not just hygiene.
    """

    def __init__(self, wait: bool = True):
        self.wait = wait
        self.fh = None
        self.started = None

    def __enter__(self):
        self.fh = open(LOCKFILE, "a+")
        flags = fcntl.LOCK_EX | (0 if self.wait else fcntl.LOCK_NB)
        if self.wait:
            self.fh.seek(0)
            holder = self.fh.read().strip()
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(f"another run holds the lock ({holder or 'unknown'}); queueing", flush=True)
                fcntl.flock(self.fh, fcntl.LOCK_EX)
        else:
            try:
                fcntl.flock(self.fh, flags)
            except BlockingIOError:
                self.fh.seek(0)
                sys.exit(f"benchmark already running: {self.fh.read().strip()}")
        self.started = datetime.now(timezone.utc).isoformat()
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"pid={os.getpid()} started={self.started}\n")
        self.fh.flush()
        return self

    def note(self, text: str) -> None:
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"pid={os.getpid()} started={self.started} {text}\n")
        self.fh.flush()

    def __exit__(self, *exc):
        # Kernel releases the lock on close/exit; truncate so --status is clean.
        try:
            self.fh.seek(0)
            self.fh.truncate()
            self.fh.flush()
        finally:
            self.fh.close()


def show_status() -> int:
    if not LOCKFILE.exists():
        print("idle")
        return 0
    with open(LOCKFILE, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            print("idle")
        except BlockingIOError:
            fh.seek(0)
            print("running: " + (fh.read().strip() or "unknown"))
    return 0


# --------------------------------------------------------------------------
# authorization
# --------------------------------------------------------------------------

def gh_json(*args: str):
    out = subprocess.run([GH, *args], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"gh {' '.join(args)} failed: {out.stderr.strip()}")
    return json.loads(out.stdout) if out.stdout.strip() else None


def has_write(login: str, repo: str) -> bool:
    try:
        perm = gh_json("api", f"repos/{repo}/collaborators/{login}/permission")
    except SystemExit:
        return False
    return perm and perm.get("permission") in ("write", "admin", "maintain")


def authorize_pr(pr: int, repo: str) -> tuple[str, str]:
    """Return (head_sha, who_authorized) or exit.

    A label alone is not enough: it must have been applied by someone with write
    access, and the PR must not have moved since. Otherwise "get approved, then
    push" is a trivial bypass.
    """
    info = gh_json("api", f"repos/{repo}/pulls/{pr}",
                   "--jq", '{head: .head.sha, author: .user.login, labels: [.labels[].name]}')
    head, author, labels = info["head"], info["author"], info["labels"]

    if has_write(author, repo):
        return head, f"author:{author}"

    if APPROVAL_LABEL not in labels:
        sys.exit(
            f"PR #{pr} is from '{author}', who does not have write access, and is not labelled "
            f"'{APPROVAL_LABEL}'.\nA maintainer must review the diff and apply that label before "
            f"its code is built or run on this host. See SECURITY.md."
        )

    events = gh_json("api", f"repos/{repo}/issues/{pr}/events", "--paginate",
                     "--jq", f'[.[] | select(.event=="labeled" and .label.name=="{APPROVAL_LABEL}")] | last')
    if not events:
        sys.exit(f"cannot determine who applied '{APPROVAL_LABEL}' to PR #{pr}")
    labeller = events["actor"]["login"]
    if not has_write(labeller, repo):
        sys.exit(f"'{APPROVAL_LABEL}' on PR #{pr} was applied by '{labeller}', who lacks write access")

    labelled_at = events["created_at"]
    commits = gh_json("api", f"repos/{repo}/pulls/{pr}/commits", "--paginate",
                      "--jq", '[.[] | {sha: .sha, date: .commit.committer.date}] | last')
    if commits and commits["date"] > labelled_at:
        sys.exit(
            f"PR #{pr} was pushed to after '{APPROVAL_LABEL}' was applied "
            f"(label {labelled_at}, last commit {commits['date']}).\n"
            f"A maintainer must re-apply the label after reviewing the new commits."
        )
    return head, f"label:{labeller}"


# --------------------------------------------------------------------------
# suite + registry
# --------------------------------------------------------------------------

def load_suite() -> dict:
    return tomllib.load(open(HERE / "suite.toml", "rb"))


def load_registry() -> dict:
    reg = {}
    for line in open(HERE / "datasets.tsv"):
        if line.startswith("#") or not line.strip():
            continue
        f = line.rstrip("\n").split("\t")
        if f[0] == "id":
            continue
        reg[f[0]] = dict(kind=f[1], host=f[2], path=os.path.expanduser(f[3]),
                         bytes=f[4], records=f[5], bases=f[6])
    return reg


def verify_datasets(suite: dict, reg: dict) -> None:
    """Fail before measuring if an input is not what the registry says.

    Benchmarking a quietly regenerated or truncated file and attributing the
    difference to code is the most damaging failure this system can have, and
    it is silent. Size is checked always; the full triple only on request,
    because counting bases in 31 GB costs minutes.
    """
    need = {b[k] for b in suite["benchmark"] for k in ("reference", "reads")}
    for ds in sorted(need):
        e = reg[ds]
        p = Path(e["path"])
        if not p.exists():
            sys.exit(f"dataset {ds} missing at {p}")
        actual = p.stat().st_size
        if str(actual) != e["bytes"]:
            sys.exit(
                f"dataset {ds} changed: registry says {e['bytes']} bytes, file is {actual}.\n"
                f"Register a NEW id rather than editing the row — historical results must keep "
                f"resolving to what they measured. See VERSIONING.md §3."
            )
    print(f"verified {len(need)} datasets against the registry")


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def plan(suite: dict, reg: dict, impls: list[str]) -> list[dict]:
    jobs = []
    for b in suite["benchmark"]:
        params = suite["params"][b["params"]]
        base = [
            "-k", str(params["k"]),
            "-r", str(params["hashratio"]),
            "-t", str(params["threshold"]),
            "-d", str(params["min_diff"]),
            "-o", str(params["max_overlap"]),
        ]
        for metric in b["metrics"]:
            for impl in b["impls"]:
                if impl not in impls:
                    continue
                spec = suite["impl"][impl]
                threads = b["threads"] if spec.get("supports_threads") else b["reference_impl_threads"]
                repeats = suite["run"]["reference_impl"]["repeats"] if spec["role"] == "reference" else suite["run"]["repeats"]
                for t in threads:
                    for rep in range(repeats):
                        jobs.append(dict(
                            benchmark=b["id"], impl=impl, metric=metric, threads=t, repeat=rep,
                            reference=reg[b["reference"]]["path"], reads=reg[b["reads"]]["path"],
                            reference_id=b["reference"], reads_id=b["reads"],
                            params_id=b["params"], base=base,
                        ))
    return jobs


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def parse_time_v(path: Path) -> tuple[float, int]:
    """Wall seconds and peak RSS in kB from /usr/bin/time -v output."""
    wall, rss = None, None
    for line in path.read_text().splitlines():
        if "Elapsed (wall clock) time" in line:
            t = line.split(": ")[-1].strip().split(":")
            wall = float(t[-1]) + (float(t[-2]) * 60 if len(t) > 1 else 0) + (float(t[-3]) * 3600 if len(t) > 2 else 0)
        elif "Maximum resident set size" in line:
            rss = int(line.split(":")[-1])
    return wall, rss


def strip_time_tag(src: Path, dst: Path) -> None:
    """Drop the wall-clock t:f: PAF tag, which varies run to run."""
    with open(src) as fi, open(dst, "w") as fo:
        for line in fi:
            i = line.find("\tt:f:")
            if i >= 0:
                j = line.find("\t", i + 1)
                line = line[:i] + (line[j:] if j > 0 else "\n")
            fo.write(line)


def prepare_worktree(commit: str) -> Path:
    """Isolated checkout at `commit`, built fresh. Never a maintainer checkout."""
    WORKROOT.mkdir(exist_ok=True)
    wt = WORKROOT / commit[:12]
    if wt.exists():
        sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])
    r = sh(["git", "-C", str(REPO), "worktree", "add", "--detach", str(wt), commit])
    if r.returncode != 0:
        sys.exit(f"worktree failed: {r.stderr.strip()}")
    print(f"building {commit[:12]} in {wt}")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GH_", "GITHUB_"))}
    b = subprocess.run(["cargo", "build", "--release", "--locked"], cwd=wt, env=env,
                       capture_output=True, text=True)
    if b.returncode != 0:
        sys.exit("build failed:\n" + b.stderr[-3000:])
    return wt


def check_reference_binary(spec: dict) -> str:
    """Refuse a C++ built with Tracy: upstream's Makefile adds it by default and
    it costs ~8.8%, which would silently flatter every ratio we publish."""
    binary = os.path.expanduser(spec["binary"])
    if not Path(binary).exists():
        sys.exit(f"reference binary missing: {binary}")
    out = sh(["bash", "-c", f"strings {shlex.quote(binary)} | grep -cE 'TracyClient|Tracy Profiler|tracy_[a-z]|__tracy'"])
    n = int(out.stdout.strip() or 0)
    if n > 10:
        sys.exit(f"reference binary has {n} live Tracy symbols — rebuild without -DTRACY_ENABLE "
                 f"(see SECURITY.md / suite.toml forbid_build_flag)")
    return binary


def one_read_fasta(reads_path: str, cache_dir: Path) -> Path:
    """A cached one-record slice of `reads_path`'s first read.

    Used by `measure`'s indexing/mapping split for a tool with no native
    phase report (see there for the method, from Pesho): indexing does not
    depend on the read set, so a run against just this file isolates it.
    Cached per result set (`cache_dir` is the run's own `raw/`, so this
    never reads a stale file across separate `run.py` invocations) and
    keyed on the reads file's own name — regenerating costs one streamed
    pass that stops at the second `>` record, not a full read of what can
    be a multi-GB file.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / f"{Path(reads_path).name}.one_read.fa"
    if dest.exists():
        return dest
    with open(reads_path) as fi, open(dest, "w") as fo:
        seen_header = False
        for line in fi:
            if line.startswith(">"):
                if seen_header:
                    break
                seen_header = True
            fo.write(line)
    return dest


def measure(job: dict, binary: str, outdir: Path, suite: dict) -> dict:
    tag = f"{job['benchmark']}_{job['impl']}_{job['metric']}_t{job['threads']}_r{job['repeat']}"
    paf, tf = outdir / f"{tag}.paf", outdir / f"{tag}.time"
    cmd = ["/usr/bin/time", "-v", "-o", str(tf), binary,
           "-s", job["reference"], "-p", job["reads"], *job["base"], "-m", job["metric"]]
    spec = suite["impl"][job["impl"]]
    if spec.get("supports_threads"):
        cmd += ["-@", str(job["threads"])]
        jsonp = outdir / f"{tag}.json"
        cmd += ["-x", "--profile-log", str(jsonp)]
    with open(paf, "w") as fo:
        rc = subprocess.run(cmd, stdout=fo, stderr=subprocess.DEVNULL).returncode
    wall, rss = parse_time_v(tf)
    mapped = sum(1 for _ in open(paf))
    q60 = sum(1 for l in open(paf) if l.split("\t")[11:12] == ["60"])

    # Split the wall into its two phases. Indexing is a fixed cost that does not
    # depend on the read set, and it is largely serial; mapping is what scales
    # and what a change to the mapper actually moves. Reporting only the total
    # mixes them, which flatters or damns a change depending on how much of the
    # run the index happened to be — at -@16 on a 1x read set it is over half.
    #
    # These come from the -x report's own timers, which are WALL for `indexing`
    # and `mapping` (the per-stage timers below them are CPU summed across
    # threads, and must never be compared against these). The C++ emits no such
    # report, so its rows carry only the total.
    index_s = map_s = ""
    if spec.get("supports_threads"):
        try:
            t = json.loads(Path(f"{outdir}/{tag}.json").read_text())["global"]["timers_secs"]
            index_s, map_s = round(t.get("indexing", 0.0), 3), round(t.get("mapping", 0.0), 3)
        except (OSError, ValueError, KeyError):
            pass
    else:
        # No native phase report to read (the C++ reference emits none, and
        # this is the general fallback for any future impl in the same
        # position). Pesho's method: re-run the identical command with the
        # read set swapped for a single read. Indexing does not depend on
        # the read set, so that run's wall time is (almost) entirely
        # indexing — subtracting it from the real run's wall time isolates
        # mapping without needing the tool's own cooperation. Re-run once
        # per `measure` call (not cached at the value level) rather than
        # once total, so it inherits the same repeat-and-median treatment
        # as everything else here — see the "C++ varies ~8% run-to-run"
        # comment on `[run.reference_impl]` in suite.toml.
        one_read = one_read_fasta(job["reads"], outdir)
        idx_tf = outdir / f"{tag}.index_only.time"
        idx_cmd = ["/usr/bin/time", "-v", "-o", str(idx_tf), binary,
                   "-s", job["reference"], "-p", str(one_read), *job["base"], "-m", job["metric"]]
        with open(outdir / f"{tag}.index_only.paf", "w") as idx_fo:
            subprocess.run(idx_cmd, stdout=idx_fo, stderr=subprocess.DEVNULL)
        index_wall, _ = parse_time_v(idx_tf)
        if index_wall is not None:
            index_s = round(index_wall, 3)
            map_s = round(wall - index_wall, 3)

    return dict(**{k: job[k] for k in ("benchmark", "impl", "metric", "threads", "repeat",
                                       "reference_id", "reads_id", "params_id")},
                rc=rc, wall_s=wall, index_s=index_s, map_s=map_s,
                peak_rss_kb=rss, mapped=mapped, mapq60=q60,
                cmd=" ".join(shlex.quote(c) for c in cmd), paf=str(paf))


def ground_truth_floor(suite: dict, metric: str) -> float:
    """Per-metric floor, falling back to the shared one.

    Not every metric can be equally coordinate-precise — see the comment on
    `[checks.ground_truth]` in suite.toml for why bucket_SH has its own.
    """
    c = suite["checks"]["ground_truth"]
    return c.get("min_fraction_by_metric", {}).get(metric, c["min_fraction"])


def run_checks(bench: dict, metric: str, rows: list[dict], outdir: Path, suite: dict) -> list[dict]:
    """Every blocking check that failed here must block the merge."""
    res = []
    rs = [r for r in rows if r["impl"] == "shmap-rs"]
    cpp = [r for r in rows if r["impl"] != "shmap-rs"]

    if "thread_determinism" in bench["checks"] and len(rs) > 1:
        base = outdir / "det_base.paf"
        strip_time_tag(Path(rs[0]["paf"]), base)
        bad = []
        for r in rs[1:]:
            other = outdir / "det_other.paf"
            strip_time_tag(Path(r["paf"]), other)
            if sh(["cmp", "-s", str(base), str(other)]).returncode != 0:
                bad.append(r["threads"])
        res.append(dict(check="thread_determinism", benchmark=bench["id"], metric=metric,
                        passed=not bad, detail=f"differs at -@{bad}" if bad else "identical across all thread counts"))

    if "validate_paf" in bench["checks"] and rs:
        v = sh([sys.executable, str(REPO / "profiling" / "validate_paf.py"), rs[0]["paf"]])
        res.append(dict(check="validate_paf", benchmark=bench["id"], metric=metric,
                        passed=v.returncode == 0, detail=v.stdout.strip().splitlines()[-1] if v.stdout else v.stderr[:200]))

    if "ground_truth" in bench["checks"] and rs:
        v = sh([sys.executable, str(REPO / "profiling" / "validate_paf.py"), rs[0]["paf"], "--truth"])
        line = next((l for l in v.stdout.splitlines() if l.startswith("ground truth")), "")
        # Take the exact counts, not the printed percentage. validate_paf.py
        # rounds to 2dp, so a true 0.98996 prints as "99.00%" and would pass a
        # 0.99 gate it actually fails — and a value sitting on the boundary
        # would flip between runs of identical output.
        mo = re.search(r"ground truth: (\d+)/(\d+)", line)
        ok, tot = (int(mo.group(1)), int(mo.group(2))) if mo else (0, 0)
        frac = ok / tot if tot else 0.0
        need = ground_truth_floor(suite, metric)
        res.append(dict(check="ground_truth", benchmark=bench["id"], metric=metric,
                        passed=frac >= need, detail=f"{ok}/{tot} = {frac:.6f} (need {need})"))

        # Same PAF, same subprocess output: reads placed wrongly while claiming
        # mapq 60. Recorded rather than gated — see [checks.wrong_q60] in
        # suite.toml for why an absolute threshold cannot work here.
        wq = re.search(r"wrong at mapq 60: (\d+)/(\d+)",
                       next((l for l in v.stdout.splitlines()
                             if l.startswith("wrong at mapq 60")), ""))
        if wq:
            nbad, nq60 = int(wq.group(1)), int(wq.group(2))
            res.append(dict(check="wrong_q60", benchmark=bench["id"], metric=metric,
                            passed=True,
                            detail=f"{nbad}/{nq60} = {nbad / nq60 if nq60 else 0.0:.6f}"))

    # Concordance against the cached external mappers. These are never run
    # here — reference_mappers.py built their PAFs once, and this is a join.
    # A missing cache entry is reported, not fatal: the corpus is optional and
    # takes hours to build.
    ext = suite.get("external", {})
    if ext.get("enabled") and rs:
        cache = Path(os.path.expanduser(ext["cache_dir"]))
        for mapper in sorted(k for k, v in ext.items()
                             if isinstance(v, dict) and k != "presets"):
            if bench["id"] in ext[mapper].get("skip_benchmarks", []):
                continue
            ref_paf = cache / mapper / f"{bench['id']}.paf"
            if not ref_paf.exists():
                continue
            v = sh([sys.executable, str(HERE / "concordance.py"), rs[0]["paf"], str(ref_paf),
                    "--min-overlap", str(ext.get("concordance_min_overlap", 0.1)), "--json"])
            try:
                c = json.loads(v.stdout)
            except json.JSONDecodeError:
                continue
            res.append(dict(check=f"concordance_{mapper}", benchmark=bench["id"], metric=metric,
                            passed=True,
                            detail=f"good={c['good']:.4f} recall={c['recall']:.4f} "
                                   f"agreement={c['agreement']:.4f} ref={c['reference_mapped']}"))

    if "impl_agreement" in bench["checks"] and rs and cpp:
        a = sh(["bash", "-c",
                f"comm -12 <(cut -f1-12 {shlex.quote(rs[0]['paf'])}|sort) "
                f"<(cut -f1-12 {shlex.quote(cpp[0]['paf'])}|sort)|wc -l"])
        n = int(a.stdout.strip() or 0)
        tot = rs[0]["mapped"] or 1
        res.append(dict(check="impl_agreement", benchmark=bench["id"], metric=metric,
                        passed=True, detail=f"{n}/{tot} = {n/tot:.4f}"))
    return res


def recheck(outdir: Path, suite: dict) -> int:
    """Re-evaluate a finished result set's checks from its retained PAFs.

    The checks are deterministic functions of the output, so when a *threshold*
    turns out to be wrong the verdict can be corrected without spending hours
    re-measuring. No timing, memory or mapping figure is touched — only the
    check outcomes, and the manifest records that this happened.

    Limits, deliberately visible rather than papered over: only the checks that
    read a single PAF can be redone here. `execute` deletes every PAF but the
    first in each group (B04's are ~600 MB each), so `thread_determinism` and
    `impl_agreement` no longer have their inputs. Their recorded outcomes are
    preserved untouched, and a threshold change affecting those two needs a
    real re-run.
    """
    outdir = Path(outdir)
    raw, man_p = outdir / "raw", outdir / "manifest.json"
    for p in (man_p, outdir / "results.tsv", outdir / "checks.tsv"):
        if not p.exists():
            sys.exit(f"not a result set: missing {p}")

    with open(outdir / "results.tsv") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    with open(outdir / "checks.tsv") as f:
        old = list(csv.DictReader(f, delimiter="\t"))

    # Concordance belongs here too: it is a join between our retained PAF and a
    # cached external one, so rebuilding the corpus (a new mapper version, or a
    # corrected reference) can be reflected without re-measuring anything.
    # Matched by prefix because the checks are named concordance_<mapper>.
    REDOABLE = {"validate_paf", "ground_truth", "wrong_q60"}
    REDOABLE_PREFIXES = ("concordance_",)

    def redoable(name: str) -> bool:
        return name in REDOABLE or name.startswith(REDOABLE_PREFIXES)
    redone: dict[tuple, dict] = {}
    for bench in suite["benchmark"]:
        bid = bench["id"]
        # `checks` lists validate_paf/ground_truth by name; concordance is
        # driven by [external] rather than per-benchmark, so it is always
        # attempted and simply finds no cache entry when there is none.
        if not (REDOABLE & set(bench["checks"])) and not suite.get("external", {}).get("enabled"):
            continue
        for metric in bench["metrics"]:
            pafs = sorted(raw.glob(f"{bid}_shmap-rs_{metric}_t*_r0.paf"))
            if not pafs:
                continue
            src = next((r for r in rows if r["benchmark"] == bid and r["impl"] == "shmap-rs"
                        and r["metric"] == metric), None)
            # A single row with no C++ counterpart: run_checks then evaluates
            # exactly the single-PAF checks and skips the other two by itself.
            stub = dict(impl="shmap-rs", paf=str(pafs[0]),
                        mapped=int(src["mapped"]) if src else 0, threads=0)
            for c in run_checks(bench, metric, [stub], raw, suite):
                if redoable(c["check"]):
                    redone[(c["check"], bid, metric)] = c

    merged, changed = [], []
    for c in old:
        k = (c["check"], c["benchmark"], c["metric"])
        if k in redone:
            new = redone.pop(k)
            was = c["passed"].strip().lower() == "true"
            if was != new["passed"] or c["detail"] != new["detail"]:
                changed.append((k, c["passed"], c["detail"], new["passed"], new["detail"]))
            merged.append(dict(check=k[0], benchmark=k[1], metric=k[2],
                               passed=new["passed"], detail=new["detail"]))
        else:
            merged.append(dict(check=c["check"], benchmark=c["benchmark"], metric=c["metric"],
                               passed=c["passed"].strip().lower() == "true", detail=c["detail"]))
    for k, c in redone.items():          # checks that did not exist before
        merged.append(dict(check=k[0], benchmark=k[1], metric=k[2],
                           passed=c["passed"], detail=c["detail"]))
        changed.append((k, "-", "(absent)", c["passed"], c["detail"]))

    for k, wasp, wasd, nowp, nowd in changed:
        print(f"  {k[0]} {k[1]}/{k[2]}: {wasp} [{wasd}] -> {nowp} [{nowd}]")
    if not changed:
        print("  no check outcome changed")

    # Rebuilt from the same retained JSON, so a recheck also picks up a change
    # to PROFILE_COUNTERS without re-measuring. `rows` is what results.tsv
    # holds — already median-reduced — so this reproduces the file byte for
    # byte when the extraction has not changed. (It read an undefined `reduced`
    # until 2026-08-02, copied from `execute`, which made --recheck crash after
    # printing its verdict and before writing checks.tsv.)
    write_profiles_tsv(outdir, rows)

    with open(outdir / "checks.tsv", "w") as fo:
        fo.write("check\tbenchmark\tmetric\tpassed\tdetail\n")
        for c in merged:
            fo.write(f"{c['check']}\t{c['benchmark']}\t{c['metric']}\t{c['passed']}\t{c['detail']}\n")

    man = json.loads(man_p.read_text())
    bad_rc = sum(1 for r in rows if int(r["rc"]) != 0)
    failed_blocking = sum(1 for c in merged
                          if not c["passed"]
                          and suite["checks"].get(c["check"], {}).get("blocking"))
    man["failures"] = bad_rc + failed_blocking
    man.setdefault("rechecks", []).append(dict(
        at=datetime.now(timezone.utc).isoformat(),
        suite_version=suite["suite_version"],
        re_evaluated=sorted(REDOABLE) + ["concordance_*"],
        preserved=["thread_determinism", "impl_agreement"],
        changed=[f"{k[0]} {k[1]}/{k[2]}" for k, *_ in changed]))
    man_p.write_text(json.dumps(man, indent=2) + "\n")

    # A recheck rewrites checks.tsv, which the comparison table reads, so the
    # artifacts beside the set would otherwise describe the checks it used to
    # have. Same non-fatal treatment as in execute().
    rc = sh([sys.executable, str(HERE / "paper.py"), str(outdir),
             "--out", str(outdir / "paper")])
    print((rc.stdout or rc.stderr).strip() or "paper artifacts: no output")
    rc = sh([sys.executable, str(HERE / "build_pdf.py"), "--dir", str(outdir / "paper")])
    print((rc.stdout or rc.stderr).strip() or "artifacts PDF: no output")

    print(f"{len(changed)} change(s); {man['failures']} failure(s) remain in {outdir}")
    return 1 if man["failures"] else 0


# Stage timers worth a column, in the order a run performs them. `total`,
# `indexing` and `mapping` are WALL; everything below them is CPU summed across
# threads, which is why the two groups are labelled apart and must not be
# divided into each other.
PROFILE_WALL = ["total", "indexing", "mapping"]
PROFILE_INDEX = ["index_reading", "index_sketching", "index_collecting", "index_finalizing"]
PROFILE_MAP = ["query_mapping", "sketching", "seeding", "prepare", "collect_kmer_info",
               "match_seeds", "match_rest", "refine", "bucket_merge"]
PROFILE_COUNTERS = ["seeds", "matches", "seeded_buckets", "refined_buckets",
                    "refine_memo_hits", "final_buckets", "mapped_reads", "mapq60"]


def write_profiles_tsv(outdir: Path, rows: list[dict]) -> None:
    """One row per invocation, one column per stage — the readable form of the
    `-x` JSON reports."""
    raw = outdir / "raw"
    cols = (["benchmark", "metric", "threads"]
            + [f"wall_{c}" for c in PROFILE_WALL]
            + [f"cpu_{c}" for c in PROFILE_INDEX + PROFILE_MAP]
            + [f"n_{c}" for c in PROFILE_COUNTERS])
    out = []
    for r in rows:
        if r["impl"] != "shmap-rs":
            continue
        f = raw / f"{r['benchmark']}_{r['impl']}_{r['metric']}_t{r['threads']}_r0.json"
        if not f.exists():
            continue
        try:
            j = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        t = j.get("global", {}).get("timers_secs", {})
        c = j.get("global", {}).get("counters", {})
        row = [r["benchmark"], r["metric"], str(r["threads"])]
        row += [f"{t.get(k, 0.0):.3f}" for k in PROFILE_WALL]
        row += [f"{t.get(k, 0.0):.3f}" for k in PROFILE_INDEX + PROFILE_MAP]
        row += [str(c.get(k, "")) for k in PROFILE_COUNTERS]
        out.append(row)
    out.sort(key=lambda r: (r[0], r[1], int(r[2])))
    with open(outdir / "profiles.tsv", "w") as fo:
        fo.write("# wall_* are wall-clock; cpu_* are summed across threads and will\n")
        fo.write("# exceed wall_total at high thread counts. Never divide one by the other.\n")
        fo.write("\t".join(cols) + "\n")
        for r in out:
            fo.write("\t".join(r) + "\n")


def collect_per_read_stats(suite: dict, reg: dict, outdir: Path, binary: str, lock) -> list[str]:
    """Per-read time and match counts, for the scaling scatter.

    A separate invocation rather than a flag on a measured one, deliberately.
    The instrumentation writes a row per read, and while that does not change
    the mapping it does change the wall clock of the run that carries it --
    which is the one number the benchmark exists to report. Contaminating a
    timing row to save a couple of minutes would be a bad trade.

    Off unless `[per_read_stats] enabled = true`; the benchmarks, metric,
    thread count and sampling interval all come from suite.toml so this file
    does not encode a policy about which datasets are worth sampling.
    """
    cfg = suite.get("per_read_stats", {})
    if not cfg.get("enabled"):
        return []
    metric = cfg.get("metric", "Containment")
    threads = int(cfg.get("threads", 1))
    sample = int(cfg.get("sample", 1))
    wanted = cfg.get("benchmarks") or [b["id"] for b in suite["benchmark"]]

    written = []
    for bid in wanted:
        bench = next((b for b in suite["benchmark"] if b["id"] == bid), None)
        if not bench or metric not in bench["metrics"]:
            print(f"  per-read stats: skipping {bid} (no {metric} in this benchmark)")
            continue
        params = suite["params"][bench["params"]]
        out = outdir / f"per-read-{bid}-{metric}.tsv"
        cmd = [binary,
               "-s", reg[bench["reference"]]["path"],
               "-p", reg[bench["reads"]]["path"],
               "-k", str(params["k"]), "-r", str(params["hashratio"]),
               "-t", str(params["threshold"]), "-d", str(params["min_diff"]),
               "-o", str(params["max_overlap"]),
               "-m", metric, "-@", str(threads),
               "--per-read-stats", str(out),
               "--per-read-stats-sample", str(sample)]
        lock.note(f"per-read stats {bid}/{metric}")
        t0 = time.time()
        # stdout is the PAF and is not wanted here: this run exists for the
        # side file, and B04's PAF alone is ~600 MB.
        r = sh(["bash", "-c", f"{shlex.join(cmd)} > /dev/null"])
        if r.returncode != 0 or not out.exists():
            print(f"  !! per-read stats {bid} failed rc={r.returncode}: {r.stderr[-200:]}")
            continue
        n = sum(1 for _ in out.open()) - 1
        print(f"  per-read stats {bid:5} {metric:12} {n} rows in {time.time()-t0:.1f}s "
              f"(every {sample} read{'s' if sample > 1 else ''})")
        written.append(out.name)
    return written


def add_per_read_stats(outdir: Path, suite: dict, reg: dict, lock) -> int:
    """Add per-read stats to a result set measured before the instrumentation.

    The alternative was to fold the files silently into an existing set, which
    would quietly break the one property the manifest is for: that every file
    beside it came from the commit it names. These rows come from a *later*
    binary, so the manifest records that separately, with the commit that
    produced them, and the figure built from them says so.

    Timing figures are not touched and no other file is rewritten.
    """
    man_p = outdir / "manifest.json"
    if not man_p.exists():
        print(f"no manifest at {man_p}", file=sys.stderr)
        return 2
    binary = str(REPO / "target" / "release" / "shmap")
    if not Path(binary).exists():
        print(f"no binary at {binary}; cargo build --release first", file=sys.stderr)
        return 2

    written = collect_per_read_stats(suite, reg, outdir, binary, lock)
    if not written:
        print("nothing written (is [per_read_stats] enabled in suite.toml?)")
        return 1

    man = json.loads(man_p.read_text())
    man["per_read_stats"] = sorted(written)
    man["per_read_stats_provenance"] = dict(
        at=datetime.now(timezone.utc).isoformat(),
        commit=sh(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip(),
        binary=sh([binary, "--version"]).stdout.strip() or binary,
        note="measured after the rest of this result set, by run.py --per-read-stats; "
             "timing and memory rows in results.tsv are untouched",
    )
    man_p.write_text(json.dumps(man, indent=2) + "\n")
    print(f"added {len(written)} per-read file(s) to {outdir}")
    return 0


def execute(jobs: list[dict], suite: dict, reg: dict, commit: str, wt: Path,
            outdir: Path, authorized_by: str, lock) -> int:
    outdir.mkdir(parents=True, exist_ok=True)
    raw = outdir / "raw"; raw.mkdir(exist_ok=True)
    binaries = {"shmap-rs": str(wt / "target" / "release" / "shmap")}
    if any(j["impl"] != "shmap-rs" for j in jobs):
        binaries["cpp-shmap"] = check_reference_binary(suite["impl"]["cpp-shmap"])

    rows, checks, failed = [], [], 0
    groups: dict[tuple, list[dict]] = {}
    for j in jobs:
        groups.setdefault((j["benchmark"], j["metric"]), []).append(j)

    t0 = time.time()
    for n, ((bid, metric), gjobs) in enumerate(groups.items(), 1):
        bench = next(b for b in suite["benchmark"] if b["id"] == bid)
        if suite["run"].get("warm_cache"):
            for p in (gjobs[0]["reference"], gjobs[0]["reads"]):
                sh(["bash", "-c", f"cat {shlex.quote(p)} > /dev/null"])
        grows = []
        for j in gjobs:
            rep = f" rep{j['repeat']+1}/{suite['run']['reference_impl']['repeats']}" if j["impl"] != "shmap-rs" else ""
            lock.note(f"commit={commit[:12]} {bid}/{metric} {j['impl']} -@{j['threads']}{rep} ({n}/{len(groups)})")
            r = measure(j, binaries[j["impl"]], raw, suite)
            if r["rc"] != 0:
                print(f"  !! {bid} {metric} -@{j['threads']} exited {r['rc']}")
                failed += 1
            grows.append(r)
            print(f"  {bid} {j['impl']:10} {metric:12} -@{j['threads']:<3} "
                  f"{r['wall_s']:8.2f}s {r['peak_rss_kb']/1048576:5.2f}GB mapped={r['mapped']}")
        cres = run_checks(bench, metric, grows, raw, suite)
        for c in cres:
            flag = "ok " if c["passed"] else "FAIL"
            print(f"    [{flag}] {c['check']}: {c['detail']}")
            if not c["passed"] and suite["checks"][c["check"]].get("blocking"):
                failed += 1
        checks += cres
        rows += grows
        # PAFs are large (B04 is ~600 MB each); keep only what later steps need.
        for r in grows[1:]:
            Path(r["paf"]).unlink(missing_ok=True)
        for stray in ("det_base.paf", "det_other.paf"):
            (raw / stray).unlink(missing_ok=True)

    # After the measured matrix, so nothing above shares a run with it.
    per_read_files = collect_per_read_stats(suite, reg, outdir, binaries["shmap-rs"], lock)

    # reduce reference-impl repeats by median
    import statistics
    reduced, bykey = [], {}
    for r in rows:
        bykey.setdefault((r["benchmark"], r["impl"], r["metric"], r["threads"]), []).append(r)
    for key, rs_ in bykey.items():
        if len(rs_) == 1:
            reduced.append(rs_[0]); continue
        med = dict(rs_[0])
        med["wall_s"] = statistics.median(x["wall_s"] for x in rs_)
        med["peak_rss_kb"] = statistics.median(x["peak_rss_kb"] for x in rs_)
        for c in ("index_s", "map_s"):
            vals = [x[c] for x in rs_ if isinstance(x[c], (int, float))]
            med[c] = round(statistics.median(vals), 3) if vals else ""
        med["repeat"] = f"median{len(rs_)}"
        reduced.append(med)

    cols = ["benchmark", "impl", "metric", "threads", "repeat", "reference_id", "reads_id",
            "params_id", "rc", "wall_s", "index_s", "map_s", "peak_rss_kb", "mapped",
            "mapq60", "cmd"]
    with open(outdir / "results.tsv", "w") as fo:
        fo.write("\t".join(cols) + "\n")
        for r in sorted(reduced, key=lambda r: (r["benchmark"], r["metric"], r["impl"], r["threads"])):
            fo.write("\t".join(str(r[c]) for c in cols) + "\n")
    # A flat, greppable, git-diffable view of the -x reports. The tarball keeps
    # full fidelity, but nobody reads 105 JSON dumps inside a .gz — and data
    # committed to be read should be readable without unpacking it first.
    write_profiles_tsv(outdir, reduced)

    with open(outdir / "checks.tsv", "w") as fo:
        fo.write("check\tbenchmark\tmetric\tpassed\tdetail\n")
        for c in checks:
            fo.write(f"{c['check']}\t{c['benchmark']}\t{c['metric']}\t{c['passed']}\t{c['detail']}\n")

    manifest = dict(
        schema=1, suite_version=suite["suite_version"], dataset_version=suite["dataset_version"],
        commit=commit, host=suite["run"]["host"], authorized_by=authorized_by,
        started=datetime.fromtimestamp(t0, timezone.utc).isoformat(),
        finished=datetime.now(timezone.utc).isoformat(), duration_s=round(time.time() - t0, 1),
        invocations=len(rows), failures=failed,
        datasets={d: reg[d] for d in sorted({j[k] for j in jobs for k in ("reference_id", "reads_id")})},
        binaries={k: sh([v, "--version"]).stdout.strip() or v for k, v in binaries.items()},
        per_read_stats=sorted(per_read_files),
    )
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    # The paper's tables and figures, built from the set that was just measured
    # and written beside it, so a run is self-describing: the artifacts in
    # outdir/paper/ can only describe outdir. Promoting a set to current/ and
    # running paper.py with no argument is what updates the repo copy.
    #
    # Deliberately non-fatal. A four-hour measurement must not be discarded
    # because a table generator raised on an unexpected column -- the result set
    # is already written and the artifacts can be rebuilt from it at any time.
    try:
        rc = sh([sys.executable, str(HERE / "paper.py"), str(outdir),
                 "--out", str(outdir / "paper")])
        print((rc.stdout or rc.stderr).strip() or "paper artifacts: no output")
        # And typeset them, so the run ends with something a person can open
        # rather than only fragments a LaTeX toolchain could. Exits 0 with an
        # explanation when no engine is installed.
        rc = sh([sys.executable, str(HERE / "build_pdf.py"),
                 "--dir", str(outdir / "paper")])
        print((rc.stdout or rc.stderr).strip() or "artifacts PDF: no output")
    except Exception as e:                                      # noqa: BLE001
        print(f"paper artifacts failed ({e}); rebuild with "
              f"benchmarks/paper.py {outdir} --out {outdir}/paper")

    print(f"\n{len(rows)} invocations in {(time.time()-t0)/60:.1f} min; "
          f"{sum(1 for c in checks if not c['passed'])} check failures; wrote {outdir}")
    return 1 if failed else 0


def main() -> int:
    # Unbuffered: a run is redirected to a log and watched live; buffering
    # left that log empty for the whole 12 minutes during the first smoke test.
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--commit", help="measure an already-trusted commit")
    g.add_argument("--pr", type=int, help="measure a pull request (authorization required)")
    g.add_argument("--status", action="store_true")
    g.add_argument("--recheck", metavar="DIR",
                   help="re-evaluate a finished result set's checks from its retained PAFs, "
                        "without re-measuring (use after correcting a threshold)")
    g.add_argument("--per-read-stats", metavar="DIR",
                   help="add per-read stats to an existing result set, without re-measuring "
                        "anything else (for a set measured before the instrumentation existed)")
    ap.add_argument("--repo", default="d0bromir/shmap-rs")
    ap.add_argument("--no-wait", action="store_true", help="fail instead of queueing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--impls", default="shmap-rs",
                    help="comma-separated; add cpp-shmap to re-measure the reference (~187 min)")
    ap.add_argument("--only", help="comma-separated benchmark ids, e.g. B05")
    ap.add_argument("--out", help="result set directory (default: results/suite-<v>/<commit>-<date>)")
    ap.add_argument("--no-compare", action="store_true",
                    help="skip the comparison against current/ (measure only)")
    ap.add_argument("--post", action="store_true",
                    help="post the comparison to the PR as a comment (needs --pr)")
    args = ap.parse_args()

    if args.status:
        return show_status()
    if os.geteuid() == 0:
        sys.exit("refusing to run as root — see SECURITY.md")

    suite, reg = load_suite(), load_registry()

    if args.recheck:
        # Takes the lock too: validate_paf.py is CPU work, and this host's
        # one-measurement-at-a-time guarantee exists so nothing perturbs a
        # run's timings.
        with HostLock(wait=not args.no_wait) as lock:
            lock.note(f"recheck {args.recheck}")
            return recheck(Path(args.recheck), suite)

    if args.per_read_stats:
        with HostLock(wait=not args.no_wait) as lock:
            return add_per_read_stats(Path(args.per_read_stats), suite, load_registry(), lock)

    impls = args.impls.split(",")

    authorized_by = "n/a"
    if args.pr:
        commit, authorized_by = authorize_pr(args.pr, args.repo)
        print(f"PR #{args.pr} authorized by {authorized_by}, head {commit[:12]}")
    elif args.commit:
        commit = args.commit
    else:
        commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()

    jobs = plan(suite, reg, impls)
    if args.only:
        keep = set(args.only.split(","))
        jobs = [j for j in jobs if j["benchmark"] in keep]
        if not jobs:
            sys.exit(f"--only {args.only} matched no benchmarks")
    if args.dry_run:
        for j in jobs[:10]:
            print(f"  {j['benchmark']} {j['impl']:10} {j['metric']:12} -@{j['threads']:<3} rep{j['repeat']}")
        print(f"  ... {len(jobs)} invocations total")
        return 0

    with HostLock(wait=not args.no_wait) as lock:
        lock.note(f"commit={commit[:12]} pr={args.pr or '-'}")
        verify_datasets(suite, reg)
        print(f"suite {suite['suite_version']}  datasets v{suite['dataset_version']}  "
              f"commit {commit[:12]}  {len(jobs)} invocations")
        wt = prepare_worktree(commit)
        # Outside the checkout on purpose. A run writes ~700 KB of TSVs plus
        # gigabytes of PAFs, and writing them into `benchmarks/results/` left
        # the host's working tree dirty with files that later became tracked
        # upstream — every subsequent `git pull` on the host then aborted.
        # Promotion copies the small files into the repo; the PAFs stay here,
        # where `--recheck` can still find them after a reboot.
        # Version first: a reader looking for "the 1.3.0 numbers" should not
        # have to open a manifest to find which SHA that was. The version is the
        # binary's own `--version`, so it records what actually ran rather than
        # what the tag says.
        ver = sh([str(wt / "target" / "release" / "shmap"), "--version"]).stdout.strip()
        ver = (ver.split()[-1] if ver else "unknown").replace("/", "_")
        default_out = (RESULTS_ROOT /
                       f"{ver}-{commit[:12]}-{datetime.now(timezone.utc):%Y-%m-%d}")
        out = Path(args.out) if args.out else default_out
        try:
            rc = execute(jobs, suite, reg, commit, wt, out, authorized_by, lock)
        finally:
            sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])

    # Measuring and judging used to be two manual steps, which meant the verdict
    # depended on someone remembering to run compare.py against the right
    # baseline. Chaining them is what makes `run.py --pr N` a merge gate rather
    # than a data-collection tool.
    if args.no_compare or args.only:
        # --only produces a partial set; comparing it would report every absent
        # benchmark as an incomplete run.
        if args.only and not args.no_compare:
            print("\n(skipping comparison: --only produces a partial result set)")
        return rc

    baseline = HERE / "results" / f"suite-{suite['suite_version']}" / "current"
    if not baseline.is_dir():
        print(f"\nno baseline at {baseline}; nothing to compare against.\n"
              f"If this run should become the baseline:  cp -r {out} {baseline}")
        return rc

    print("\n" + "=" * 72)
    report = out / "comparison.md"
    v = subprocess.run([sys.executable, str(HERE / "compare.py"), str(out), str(baseline),
                        "--out", str(report)], capture_output=True, text=True)
    print(v.stdout or v.stderr)
    verdict = {0: "ACCEPT", 1: "REVIEW", 2: "BLOCK", 3: "ERROR"}.get(v.returncode, str(v.returncode))
    print(f"verdict: {verdict}   (written to {report})")

    if args.post and args.pr:
        body = f"### Benchmark gate: **{verdict}**\n\n{report.read_text()}"
        p = subprocess.run([GH, "pr", "comment", str(args.pr), "--repo", args.repo,
                            "--body-file", "-"], input=body, capture_output=True, text=True)
        print("posted to PR" if p.returncode == 0 else f"could not post: {p.stderr.strip()[:200]}")

    # A failed check during measurement outranks the comparison: it means the
    # run itself is untrustworthy, not merely worse than the baseline.
    return max(rc, v.returncode)


if __name__ == "__main__":
    sys.exit(main())
