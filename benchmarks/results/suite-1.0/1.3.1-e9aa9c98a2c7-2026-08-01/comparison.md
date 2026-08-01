# Benchmark comparison — REVIEW

**REVIEW** — needs a human decision; see the findings below.

| | baseline | candidate |
|---|---|---|
| commit | `4c36739d9c85` | `e9aa9c98a2c7` |
| suite | 1.0 | 1.0 |
| datasets | v1 | v1 |
| host | a2 | a2 |
| measured | 2026-08-01T13:14:41Z | 2026-08-01T18:53:07Z |
| invocations | 150 | 150 |

## Findings

| level | kind | what | detail |
|---|---|---|---|
| **REVIEW** | perf | B01/bucket_SH peak RSS | median +6.2% across 7 thread counts (worst +23.4%) |
| **REVIEW** | perf | B02/Containment peak RSS | median +5.8% across 7 thread counts (worst +27.2%) |
| **REVIEW** | perf | B02/Jaccard peak RSS | median +5.3% across 7 thread counts (worst +27.2%) |
| **REVIEW** | perf | B05/Containment peak RSS | median +9.4% across 7 thread counts (worst +33.2%) |
| **REVIEW** | perf | B05/Jaccard peak RSS | median +13.3% across 7 thread counts (worst +32.8%) |
| **REVIEW** | perf | B05/bucket_SH peak RSS | median +6.2% across 7 thread counts (worst +31.6%) |

## Wall time by benchmark

Geometric mean of the per-thread-count ratios; a single thread count is too noisy to judge on its own, so the worst one is shown for context but does not decide the verdict.

**Host drift: -3.07% measured on the unchanged reference binary over 15 measurements.** The reference implementation's binary does not change between our commits, so its movement is the host's, not ours. shmap-rs rows are divided by it; the `raw` column is before that correction and the reference's own rows are never corrected.

| benchmark | metric | impl | threads | baseline | candidate | raw | corrected | worst |
|---|---|---|---|---|---|---|---|---|
| B01 | Containment | shmap-rs | 7 | 129.3s | 116.8s | -10.7% | -7.9% ✅ | -0.6% @-@8 |
| B01 | Jaccard | shmap-rs | 7 | 155.6s | 141.1s | -10.1% | -7.2% ✅ | -5.5% @-@16 |
| B01 | bucket_SH | shmap-rs | 7 | 114.2s | 98.1s | -15.9% | -13.2% ✅ | -8.0% @-@8 |
| B02 | Containment | shmap-rs | 7 | 112.8s | 97.8s | -13.5% | -10.7% ✅ | -10.4% @-@8 |
| B02 | Jaccard | shmap-rs | 7 | 141.5s | 126.9s | -11.1% | -8.3% ✅ | -6.0% @-@2 |
| B02 | bucket_SH | shmap-rs | 7 | 105.1s | 93.0s | -11.5% | -8.7% ✅ | +1.1% @-@32 |
| B03 | Containment | shmap-rs | 7 | 151.7s | 131.7s | -16.8% | -14.2% ✅ | -8.5% @-@1 |
| B03 | Jaccard | shmap-rs | 7 | 180.1s | 159.4s | -15.0% | -12.3% ✅ | -3.5% @-@2 |
| B03 | bucket_SH | shmap-rs | 7 | 136.2s | 112.9s | -20.7% | -18.2% ✅ | -12.7% @-@1 |
| B04 | Containment | shmap-rs | 7 | 1040.5s | 942.2s | -14.3% | -11.6% ✅ | -6.1% @-@1 |
| B04 | Jaccard | shmap-rs | 7 | 1361.2s | 1247.0s | -11.9% | -9.1% ✅ | -6.0% @-@4 |
| B04 | bucket_SH | shmap-rs | 7 | 867.9s | 783.4s | -15.1% | -12.4% ✅ | -5.7% @-@2 |
| B05 | Containment | shmap-rs | 7 | 79.5s | 67.4s | -14.5% | -11.8% ✅ | -9.5% @-@32 |
| B05 | Jaccard | shmap-rs | 7 | 85.9s | 73.9s | -14.8% | -12.1% ✅ | -5.7% @-@2 |
| B05 | bucket_SH | shmap-rs | 7 | 76.4s | 67.0s | -9.3% | -6.4% ✅ | +4.1% @-@64 |
| B01 | Containment | cpp-shmap | 1 | 106.4s | 106.3s | -0.1% | -0.1% | -0.1% @-@1 |
| B01 | Jaccard | cpp-shmap | 1 | 134.9s | 132.7s | -1.6% | -1.6% | -1.6% @-@1 |
| B01 | bucket_SH | cpp-shmap | 1 | 89.9s | 89.1s | -0.9% | -0.9% | -0.9% @-@1 |
| B02 | Containment | cpp-shmap | 1 | 93.3s | 89.3s | -4.2% | -4.2% | -4.2% @-@1 |
| B02 | Jaccard | cpp-shmap | 1 | 120.3s | 119.7s | -0.5% | -0.5% | -0.5% @-@1 |
| B02 | bucket_SH | cpp-shmap | 1 | 82.8s | 82.3s | -0.6% | -0.6% | -0.6% @-@1 |
| B03 | Containment | cpp-shmap | 1 | 117.0s | 116.5s | -0.5% | -0.5% | -0.5% @-@1 |
| B03 | Jaccard | cpp-shmap | 1 | 146.7s | 144.9s | -1.2% | -1.2% | -1.2% @-@1 |
| B03 | bucket_SH | cpp-shmap | 1 | 101.1s | 99.8s | -1.3% | -1.3% | -1.3% @-@1 |
| B04 | Containment | cpp-shmap | 1 | 838.9s | 841.1s | +0.3% | +0.3% | +0.3% @-@1 |
| B04 | Jaccard | cpp-shmap | 1 | 1144.9s | 1139.1s | -0.5% | -0.5% | -0.5% @-@1 |
| B04 | bucket_SH | cpp-shmap | 1 | 665.0s | 643.5s | -3.2% | -3.2% | -3.2% @-@1 |
| B05 | Containment | cpp-shmap | 1 | 60.0s | 55.6s | -7.4% | -7.4% | -7.4% @-@1 |
| B05 | Jaccard | cpp-shmap | 1 | 66.6s | 59.5s | -10.7% | -10.7% | -10.7% @-@1 |
| B05 | bucket_SH | cpp-shmap | 1 | 60.0s | 52.5s | -12.4% | -12.4% | -12.4% @-@1 |

## Agreement with the C++ reference

| benchmark | metric | baseline | candidate |
|---|---|---|---|
| B01 | Containment | 0.9832 | 0.9832 |
| B01 | Jaccard | 0.9961 | 0.9961 |
| B01 | bucket_SH | 0.9813 | 0.9813 |
| B02 | Containment | 0.9864 | 0.9864 |
| B02 | Jaccard | 0.9951 | 0.9951 |
| B02 | bucket_SH | 0.9828 | 0.9828 |
| B03 | Containment | 0.9855 | 0.9855 |
| B03 | Jaccard | 0.9927 | 0.9927 |
| B03 | bucket_SH | 0.9787 | 0.9787 |
| B04 | Containment | 0.9852 | 0.9852 |
| B04 | Jaccard | 0.9925 | 0.9925 |
| B04 | bucket_SH | 0.9784 | 0.9784 |
| B05 | Containment | 0.9792 | 0.9792 |
| B05 | Jaccard | 0.9978 | 0.9978 |
| B05 | bucket_SH | 0.9818 | 0.9818 |

---

Rules: [VERSIONING.md](../VERSIONING.md). Thresholds from `suite.toml`: wall review >3%, block >10%; any drop in mapped reads, mapq-60 reads or C++ agreement blocks.

Baseline: `/home/mpiuser/shmap-rs/benchmarks/results/suite-1.0/current`  
Candidate: `/home/mpiuser/bench-results/1.3.1-e9aa9c98a2c7-2026-08-01`
