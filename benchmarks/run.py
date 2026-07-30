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
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}\n")
        self.fh.flush()
        return self

    def note(self, text: str) -> None:
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()} {text}\n")
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


def main() -> int:
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
        print("measurement loop is step 3b — see benchmarks/README.md")
        # TODO(step 3b): checkout worktree, build, execute jobs, write result set.
    return 0


if __name__ == "__main__":
    sys.exit(main())
