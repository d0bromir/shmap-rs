# Runbook — operating the benchmark host

Practical notes for running benchmarks on `a2`. [`README.md`](README.md) describes the system;
[`../CONTRIBUTING.md`](../CONTRIBUTING.md) describes the process. This file is the set of traps that
have actually cost time, written down so they cost it once.

---

## Launching a run

```bash
ssh a2
cd ~/shmap-rs && git fetch -q origin && git pull -q --ff-only origin main
COMMIT=$(git rev-parse HEAD)                       # AFTER the pull, and check it
setsid nohup python3 benchmarks/scripts/run.py --commit "$COMMIT" \
    --impls shmap-rs,cpp-shmap > ~/run.log 2>&1 < /dev/null &
```

**Verify the commit before launching, not after.** Chaining `git pull && … $(git rev-parse HEAD)`
in one command means a *failed* pull silently launches the previous commit. That happened twice; once
it also left an orphaned `shmap` child, a partial result set and a stray worktree to clean up.

**`git rev-list --count HEAD..origin/main` is worthless without a preceding `fetch`.** It measures
distance to a *cached* ref, so a host three commits behind will report "up to date". Always `fetch`
first when checking staleness.

Cost, measured: ~78 min for shmap-rs alone (the C++ comes from cache), ~4.7 h for the full matrix
with `--impls shmap-rs,cpp-shmap`.

## Things that will bite

**Do not wrap `reference_mappers.py` or `run.py` in `flock`.** They take the host lock themselves,
so an outer `flock` on the same file deadlocks: the wrapper holds it and the script waits forever.
Fourteen minutes at 0% CPU before anyone looked at the process tree. Scripts that do *not* take the
lock — the sweeps in [`../simulate/`](../simulate/) — do need the wrapper, so that nothing contends
with a measured run.

**Redirected Python output is block-buffered.** A watched log stays empty and the run looks hung.
`run.py` and the sweeps call `sys.stdout.reconfigure(line_buffering=True)`; anything new should too.

**Result sets go to `~/bench-results/`, not into the checkout.** They used to be written inside
`benchmarks/results/`, which left the host's tree dirty; once those files were committed from
elsewhere, every subsequent `git pull` on the host aborted. The PAFs (4 GB per run) live there too,
and `--recheck` needs them, so `/tmp` would lose them on reboot.

**`reference_mappers.py --export` writes into the checkout** (`benchmarks/results/reference-mappers/
manifest.json` is tracked). Run `git checkout -- benchmarks/results/reference-mappers/` before
pulling on the host.

## Re-judging without re-measuring

Checks are deterministic functions of the retained PAFs, so a corrected threshold or a rebuilt
external corpus does not need another 4.7 h:

```bash
python3 benchmarks/scripts/run.py --recheck ~/bench-results/<version>-<commit>-<date>
```

Re-evaluates `validate_paf`, `ground_truth` and `concordance_*`. It cannot redo
`thread_determinism` or `impl_agreement` — `execute()` deletes all but the first PAF per group
(B04's are ~600 MB each) — and it says so rather than silently skipping them.

## Host state

| what | where |
|---|---|
| checkout | `~/shmap-rs` |
| result sets, with PAFs | `~/bench-results/<version>-<commit12>-<date>/` |
| external-mapper corpus | `~/bench-refs/` (Winnowmap2, mapquik PAFs; meryl k-mer set) |
| build worktrees | `~/bench-work/<commit>/` — throwaway, removed after each run |
| host lock | `~/.shmap-bench.lock` — kernel flock, released even if the process is killed |

`python3 benchmarks/scripts/run.py --status` says whether anything is running.

## Tools built from source

`a2` has no `sudo` and lacked `zlib`, `libcurl`, `bzip2` and `cmake`. Recorded so nobody re-derives
it:

- **zlib, libcurl** — built static into `~/local`, needed by Winnowmap2.
- **meryl** — from bioconda via `~/tools/bin/micromamba` (env `refmappers`). Its bundled htslib
  wants the whole lzma/bz2/openssl chain for BAM support it does not use, and the prebuilt release
  binary links `libssl.so.10`, which Ubuntu 24.04 does not ship.
- **mapquik needs a one-line reference.** It counts newlines as bases, so a wrapped FASTA yields
  coordinates in file-offset space — silently. `suite.toml` carries the `awk` recipe.
- **`gh`** — the release tarball extracted to `~/bin/gh` (no sudo, and Ubuntu's `gh` package is not
  installed). `~/.profile` puts `~/bin` on the PATH, so it resolves in a login shell but *not* in
  `ssh a2 '<command>'`, which does not read `.profile`. Set `GH_BIN=$HOME/bin/gh` if you drive
  `run.py` non-interactively.
- **`cargo` has the same problem, and it is fatal rather than degraded.** `~/.cargo/env` is sourced
  from `~/.profile`, so `ssh a2 'python3 benchmarks/scripts/run.py …'` gets to `prepare_worktree`,
  tries to build, and dies with `FileNotFoundError: 'cargo'` — after taking the host lock and
  printing that it verified the datasets, which reads like a successful start. Prefix any
  non-interactive launch:

  ```sh
  ssh a2 'export PATH=$HOME/.cargo/bin:$PATH; cd ~/shmap-rs && setsid nohup python3 …'
  ```
- **bwa-mem2 and its index** — `benchmarks/scripts/setup_bwa_mem2.sh` installs it from bioconda into
  the `refmappers` env and builds the FM-index. The index needs **~87 GB of RAM** and about an hour
  (a2 57 min, galaxy 23 min); the script refuses to start if the memory is not there rather than
  leaving a truncated index behind. The aarch64 build is *not* the same artefact as the x86_64 one —
  see the header of that script before quoting any cross-architecture number from it.

## The authorization gate needs `gh` logged in

`run.py --pr N` shells out to `gh` **on this host** (see [`../SECURITY.md`](../SECURITY.md)). Until
`gh auth login` has been run as `mpiuser`, every `--pr` invocation fails at the authorization step,
and the only usable path is `--commit <sha>` — which skips the label check entirely and records
`authorized_by = n/a`. That is how every result set up to `1.3.1` was measured.

The token has to be able to read `repos/{repo}/collaborators/{login}/permission`, which is what
decides whether a PR author or labeller has write access. A token that cannot read it makes
`has_write` return false for everyone, so an authorized PR is refused rather than wrongly accepted —
the failure is safe, but it is silent about the cause.
