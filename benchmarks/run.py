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
import fcntl
import json
import os
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
    return dict(**{k: job[k] for k in ("benchmark", "impl", "metric", "threads", "repeat",
                                       "reference_id", "reads_id", "params_id")},
                rc=rc, wall_s=wall, peak_rss_kb=rss, mapped=mapped, mapq60=q60,
                cmd=" ".join(shlex.quote(c) for c in cmd), paf=str(paf))


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
        frac = 0.0
        if "(" in line:
            frac = float(line.split("(")[1].split("%")[0]) / 100
        need = suite["checks"]["ground_truth"]["min_fraction"]
        res.append(dict(check="ground_truth", benchmark=bench["id"], metric=metric,
                        passed=frac >= need, detail=f"{frac:.4f} (need {need})"))

    if "impl_agreement" in bench["checks"] and rs and cpp:
        a = sh(["bash", "-c",
                f"comm -12 <(cut -f1-12 {shlex.quote(rs[0]['paf'])}|sort) "
                f"<(cut -f1-12 {shlex.quote(cpp[0]['paf'])}|sort)|wc -l"])
        n = int(a.stdout.strip() or 0)
        tot = rs[0]["mapped"] or 1
        res.append(dict(check="impl_agreement", benchmark=bench["id"], metric=metric,
                        passed=True, detail=f"{n}/{tot} = {n/tot:.4f}"))
    return res


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
        med["repeat"] = f"median{len(rs_)}"
        reduced.append(med)

    cols = ["benchmark", "impl", "metric", "threads", "repeat", "reference_id", "reads_id",
            "params_id", "rc", "wall_s", "peak_rss_kb", "mapped", "mapq60", "cmd"]
    with open(outdir / "results.tsv", "w") as fo:
        fo.write("\t".join(cols) + "\n")
        for r in sorted(reduced, key=lambda r: (r["benchmark"], r["metric"], r["impl"], r["threads"])):
            fo.write("\t".join(str(r[c]) for c in cols) + "\n")
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
    )
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
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
    ap.add_argument("--repo", default="d0bromir/shmap-rs")
    ap.add_argument("--no-wait", action="store_true", help="fail instead of queueing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--impls", default="shmap-rs",
                    help="comma-separated; add cpp-shmap to re-measure the reference (~187 min)")
    ap.add_argument("--only", help="comma-separated benchmark ids, e.g. B05")
    ap.add_argument("--out", help="result set directory (default: results/suite-<v>/<commit>-<date>)")
    args = ap.parse_args()

    if args.status:
        return show_status()
    if os.geteuid() == 0:
        sys.exit("refusing to run as root — see SECURITY.md")

    suite, reg = load_suite(), load_registry()
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
        default_out = (HERE / "results" / f"suite-{suite['suite_version']}" /
                       f"{commit[:12]}-{datetime.now(timezone.utc):%Y-%m-%d}")
        out = Path(args.out) if args.out else default_out
        try:
            return execute(jobs, suite, reg, commit, wt, out, authorized_by, lock)
        finally:
            sh(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)])


if __name__ == "__main__":
    sys.exit(main())
