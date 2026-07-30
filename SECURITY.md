# Security model for the benchmark host

This is a **public** repository whose benchmarks run on a **private** machine (`a2`). That
combination is the entire security problem, and it has one irreducible property:

> **Benchmarking a pull request means building and executing its code.**
> `cargo build` runs `build.rs` and proc-macros; the benchmark runs the produced binary. There is
> no way to measure a change without running it.

So the question is never "can we sandbox this away" — it is "whose code do we agree to run".

---

## What GitHub cannot do

**"Only collaborators may open pull requests" is not achievable on a public repository.** Anyone
can fork and open a PR; GitHub provides no setting to prevent it, and disabling that would mean
making the repository private. Requests to restrict PR *creation* therefore have to be re-read as
restricting what a PR *causes to happen*, which is enforceable.

**`a2` is deliberately not a GitHub self-hosted runner.** GitHub's own guidance is that self-hosted
runners should not be used with public repositories, because a fork PR can execute arbitrary code
on them. Wiring `a2` up as a runner would hand every GitHub user a shell on the benchmark host.

## What we do instead

`a2` **pulls work; nothing pushes to it.** No inbound access, no runner registration, no webhook.
`benchmarks/run.py` executes on `a2` and decides for itself what it is willing to measure.

### Two tiers of checking

| tier | where | runs on | what |
|---|---|---|---|
| **Cheap checks** | GitHub-hosted runner, ephemeral | every PR, including forks | build, `cargo test`, `fmt`, `clippy` |
| **Benchmarks** | `a2` | only authorized commits | the full 105-invocation matrix |

The cheap tier is safe for untrusted code because the runner is disposable and holds no secrets of
ours. The expensive tier is gated.

### The authorization gate

`run.py --pr N` refuses to build or execute anything until it has confirmed, against the GitHub
API, that **either**:

- the PR author has `push` or `admin` permission on this repository, **or**
- a user with `push`/`admin` has applied the `bench-approved` label to the PR.

The label is the human review step: a maintainer reads the diff *before* the code runs on `a2`.
`run.py` records which user authorized the run in the result manifest, so the decision is auditable
after the fact.

**The label must be re-applied after any new push to the PR.** `run.py` records the head SHA that
was approved and refuses if the PR has moved on — otherwise "approve, then push something else" is
a trivial bypass.

### Containment

Authorization limits *whose* code runs; it does not make that code safe. In addition:

- Runs happen in a throwaway `git worktree` under `~/bench-work/`, never in a maintainer checkout.
- The GitHub token is **not** exposed to the build or the benchmark. `run.py` reads it before
  dropping into the work directory and does not export it into the child environment.
- `run.py` refuses to run as `root`.
- Datasets are opened read-only; the registry's identity triple is verified before measuring, so a
  run cannot silently benchmark a modified input.

Residual risk is real and is accepted knowingly: an approved PR can still do anything the `mpiuser`
account can. The mitigation is human review, which is why the label gate exists.

---

## Concurrency

**At most one benchmark runs on `a2` at any time, regardless of how many PRs are open.**

`run.py` takes an exclusive `flock` on `~/.shmap-bench.lock` and holds it for the whole run.
Concurrent invocations queue rather than fail (`--no-wait` makes them exit instead). The lock is a
kernel file lock, so it is released automatically if the process is killed or the machine reboots —
there is no stale-lock state to clean up by hand.

This matters for correctness, not just tidiness: two benchmarks sharing 64 cores would contaminate
each other's timings, and the results would be silently wrong rather than obviously broken. The
lockfile records the holding PID, commit and start time so `run.py --status` can say what is running.

---

## Repository settings to apply

These are not in-repo and must be set once in the GitHub UI or API:

| setting | value | why |
|---|---|---|
| Settings → Actions → Fork PR workflows | **Require approval for all outside collaborators** | stops fork PRs running even the cheap tier without a look |
| Settings → Actions → Workflow permissions | **Read repository contents** | a compromised workflow cannot write to the repo |
| Branch protection on `main` | require the cheap-tier check to pass | keeps a broken build off `main` |
| Secrets | none needed by CI | `run.py` uses a local token on `a2`, never a repo secret |

Branch protection deliberately does **not** require pull requests, so maintainers keep the
direct-to-`main` workflow this project uses. It requires the status check only.

## Reporting a vulnerability

Open a private security advisory via the repository's Security tab rather than a public issue.
