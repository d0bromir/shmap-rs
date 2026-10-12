# benchmarks/results/

Every measured result set in the repo. **The maintained numbers are in `suite-1.0/`**
and are presented in [`../../RESULTS.md`](../../RESULTS.md); everything else is a
self-contained experiment archive, kept as evidence.

Archives are immutable once committed. Each carries its own `README.md` (provenance,
commands, limitations) and, for the generated ones, the script that reproduces its
tables and a `--check` that CI runs.

## Maintained

| directory | what | status |
|---|---|---|
| [`suite-1.0/`](suite-1.0/) | the benchmark suite, one tree per architecture (`x86_64/`, `aarch64/`): `current/` is the baseline PRs are judged against, `<ver>-<sha>-<date>/` are archived runs. [`ARCH.md`](suite-1.0/x86_64/ARCH.md) describes each machine | **current** — source of all of RESULTS.md |
| [`ablation/`](ablation/) | cumulative optimization ladder: what each change in [PORT_CHANGES.md](../../PORT_CHANGES.md) is worth, one variable at a time | current (aarch64) |
| [`reference-mappers/`](reference-mappers/) | provenance of the cached external-mapper corpus (bwa-mem2, mapquik, Winnowmap2) | current |
| [`supported-e71ca0f/`](supported-e71ca0f/README.md) | default-mode-only Rust speedup over historical C++ at `e71ca0f`, both hosts | current spot check; not a suite gate |

## Experiments (not maintained numbers)

| directory | what | verdict |
|---|---|---|
| [`native-compare-6e33e5c/`](native-compare-6e33e5c/README.md) | 540 native invocations, `6e33e5c` vs release 1.6.0, plus the Rust-vs-historical-C++ chart | output unchanged; gain is setup time, not mapping throughput |
| [`native-778d519f001d/`](native-778d519f001d/README.md) | 240 invocations comparing default / adaptive / dense / parsing modes | adaptive modes since **retired**; dense regressed everywhere |
| [`adaptive-mapq-442928c/`](adaptive-mapq-442928c/README.md) | 13 200-read truth study of an offline MAPQ model | **not approved**; adaptive path retired |
| [`repeat-factored-shadow-e71ca0f/`](repeat-factored-shadow-e71ca0f/README.md) | whole-reference screen of a repeat-factored bucket search | **rejected**: little repeat sharing, 4.2 GB RSS, no speedup |
| [`flamegraphs-2.0.0-x86_64/`](flamegraphs-2.0.0-x86_64/README.md) | perf flame graphs for B01-B09 at 2.0.0 on a2: where time goes per function | diagnostic |
| [`flamegraphs-2.0.0-aarch64/`](flamegraphs-2.0.0-aarch64/README.md) | the same on galaxy (aarch64) | diagnostic; names are fully generic, so sort frames are attributable |

Why the retired adaptive path is still here, and what replaces it:
[`docs/ROADMAP.md`](../../docs/ROADMAP.md).

## Layout of a result set

`results.tsv` (one row per measurement, with the exact command), `manifest.json`
(commit, host, versions, dataset ids), `checks.tsv` (validation/determinism),
`profiles.tsv` plus `raw-profiles.tar.gz` (per-stage profiles). The conventions and how
`run.py` writes them are in [`../README.md`](../README.md).
