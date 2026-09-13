# Native Optimization Validation

Candidate `6e33e5c6634dd7e8d5803f732094d7f3a414c9d6` versus release 1.6.0
(`2363d92eaceac4cfffcbdc46203ef26b9e363f41`), measured on a2 and galaxy.

All **540 invocations passed**: B01-B05, default/adaptive/parsing modes,
1/16/64 mapping workers, three repeats per revision on each host.
Normalized PAF hashes and adaptive work counters match across revisions,
worker counts, repeats, and hosts. B02 retains all 125,000 mapped reads
and 123,977 truth-overlap-correct placements in every mode.

## Performance

Ratios are baseline/candidate; greater than 1 is faster. Each cell uses
the median of three runs. Matrix aggregates are geometric means across
five datasets and three worker counts, not pooled read throughput.

| Host | Mode | Total Speedup | Mapping Speedup |
| --- | --- | ---: | ---: |
| a2 | default | 1.011x | 1.006x |
| a2 | adaptive | 1.096x | 1.009x |
| a2 | parsing | 1.102x | 1.013x |
| galaxy | default | 0.990x | 0.991x |
| galaxy | adaptive | 1.173x | 0.993x |
| galaxy | parsing | 1.193x | 1.010x |

### Deep HiFi (B04)

| Host | Mode | Workers | Total Seconds (Old / New) | Mapping Seconds (Old / New) |
| --- | --- | ---: | ---: | ---: |
| a2 | default | 1 | 380.71 / 368.86 | 373.30 / 361.60 |
| a2 | default | 16 | 33.57 / 32.76 | 29.79 / 29.02 |
| a2 | default | 64 | 32.25 / 33.01 | 28.57 / 29.14 |
| a2 | adaptive | 1 | 235.35 / 224.73 | 225.51 / 215.84 |
| a2 | adaptive | 16 | 25.71 / 24.49 | 18.85 / 18.74 |
| a2 | adaptive | 64 | 28.75 / 28.36 | 21.72 / 22.13 |
| a2 | parsing | 1 | 239.32 / 227.13 | 229.58 / 218.33 |
| a2 | parsing | 16 | 24.51 / 22.69 | 17.52 / 16.68 |
| a2 | parsing | 64 | 19.53 / 18.73 | 12.54 / 12.72 |
| galaxy | default | 1 | 416.12 / 424.75 | 409.24 / 417.59 |
| galaxy | default | 16 | 28.06 / 28.64 | 26.05 / 26.56 |
| galaxy | default | 64 | 21.69 / 20.91 | 19.52 / 18.81 |
| galaxy | adaptive | 1 | 260.84 / 256.01 | 248.31 / 247.88 |
| galaxy | adaptive | 16 | 22.72 / 20.07 | 15.88 / 15.93 |
| galaxy | adaptive | 64 | 15.48 / 14.47 | 11.60 / 11.66 |
| galaxy | parsing | 1 | 259.63 / 257.19 | 246.93 / 249.13 |
| galaxy | parsing | 16 | 23.95 / 19.26 | 16.06 / 16.06 |
| galaxy | parsing | 64 | 11.87 / 10.47 | 7.74 / 7.64 |

## Interpretation and Scope

Total-time gains in compact-index modes must not be presented as mapping
acceleration. The candidate bundles exact compact preallocation, ordered
sampled anchors, and reused postings; these across-build comparisons do not
isolate each change, and compiler layout and host variation remain factors.
No 10x gain is established. Small mapping differences are not decisive evidence.

Only native shmap was executed. Original-mapper fallback remains enabled;
MAPQ 255 means unavailable. Parser mode uses two additional reader workers.
Inputs are warmed before each run; index construction is included, with no
persistent cache reuse. Dense rescue, short reads, cold-cache behavior, and
the full maintained suite gate are not validated by this matrix.

## Evidence

[results.tsv](results.tsv) contains all 90 median comparisons, CPU time, and RSS.
Each host subdirectory preserves its original report, run log, and compressed
profiles/stderr/resource logs. Raw PAF files remain on each host under
`~/bench-results/native-compare-6e33e5c/raw/`.

- a2 report SHA-256: `4a2525ff40be420978c412f79ca1956192822848a9bacb948d23d1f32d5b978b`
- galaxy report SHA-256: `5e0610f028c1c735e7fa688ace5503ebab543138af106759e1a6898cd3233f90`

Regenerate with `python3 benchmarks/scripts/report_native_comparison.py`;
verify with `--check`. Verification checks complete matrix coverage,
cross-host/revision PAF and counter parity, and archived profile agreement.
